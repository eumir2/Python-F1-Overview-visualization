"""
worker.py - Chiamate di rete verso OpenF1 (storico + live) in thread separato.

Contiene un rate limiter adattivo condiviso: normalmente distanzia le
richieste di un intervallo minimo, e se riceve 429 (rate limit) aumenta
temporaneamente quella distanza (fino a un tetto), per evitare di
martellare l'API durante gare con tanto traffico. La penalita' decade
lentamente quando le richieste tornano ad avere successo.
"""

import threading
import time
from datetime import datetime, timedelta

import requests
from PySide6.QtCore import QObject, Signal

from data import (
    API, LAPS_CHUNK_MINUTES, CHUNK_MINUTES, LAPS_SEARCH_SAFETY_CAP_MIN,
    SessionData, parse_iso, to_openf1_iso,
)


class RateLimiter:
    """Throttling adattivo condiviso tra tutte le richieste HTTP verso OpenF1."""

    def __init__(self, min_interval=0.25, max_penalty=10.0):
        self.min_interval = min_interval
        self.max_penalty = max_penalty
        self._penalty = 0.0
        self._last_call = 0.0
        self._lock = threading.Lock()

    @property
    def penalty(self):
        with self._lock:
            return self._penalty

    def wait(self):
        with self._lock:
            now = time.monotonic()
            target = self._last_call + self.min_interval + self._penalty
            wait_time = max(0.0, target - now)
        if wait_time > 0:
            time.sleep(wait_time)
        with self._lock:
            self._last_call = time.monotonic()

    def register_429(self):
        with self._lock:
            self._penalty = min(self._penalty + 1.5, self.max_penalty)

    def register_success(self):
        with self._lock:
            if self._penalty > 0:
                self._penalty = max(0.0, self._penalty - 0.15)


def http_get_json(session, rate_limiter, url, max_retries=5):
    """GET con throttling adattivo e gestione delle peculiarita' OpenF1:
    404 = 'nessun risultato' (non e' un errore, torna lista vuota),
    422 = richiesta troppo ampia (errore esplicito e chiaro),
    429 = rate limit (backoff + penalita' condivisa che rallenta tutti).
    """
    delay = 1.0
    for attempt in range(max_retries):
        rate_limiter.wait()
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 404:
                rate_limiter.register_success()
                return []
            if resp.status_code == 422:
                raise RuntimeError(
                    "L'API ha rifiutato la richiesta perche' troppo ampia "
                    "(troppi dati in una volta). Riduci il numero di giri richiesti."
                )
            if resp.status_code == 429:
                rate_limiter.register_429()
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            resp.raise_for_status()
            rate_limiter.register_success()
            return resp.json()
        except requests.RequestException:
            if attempt == max_retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 20)
    raise RuntimeError("Troppi tentativi falliti (rate limit persistente)")


class Worker(QObject):
    """Esegue le chiamate di rete in un thread separato ed emette segnali verso la UI."""

    status = Signal(str)
    session_loaded = Signal()
    error = Signal(str)

    def __init__(self, data: SessionData):
        super().__init__()
        self.data = data
        self.http = requests.Session()
        self.rate_limiter = RateLimiter()
        self._live_thread = None
        self._live_stop = threading.Event()

    def _get(self, url):
        return http_get_json(self.http, self.rate_limiter, url)

    # --------------------------------------------------------- storico ---
    def load_historical(self, session_key_raw, n_laps):
        threading.Thread(target=self._load_historical_impl, args=(session_key_raw, n_laps), daemon=True).start()

    def _load_historical_impl(self, session_key_raw, n_laps):
        try:
            self.data.reset()
            self.status.emit("carico sessione storica...")
            if session_key_raw:
                sessions = self._get(f"{API}/sessions?session_key={session_key_raw}")
                info = sessions[0] if sessions else None
            else:
                self.status.emit("cerco un GP storico di default (2023)...")
                sessions = self._get(f"{API}/sessions?year=2023&session_type=Race")
                info = sessions[-1] if sessions else None
            if not info:
                raise RuntimeError("Sessione non trovata (session_key inesistente o sessione non ancora disponibile)")

            sk = info["session_key"]
            self.data.session_key = sk
            self.status.emit(f"sessione: {info.get('location')} {info.get('session_name')} "
                              f"({info.get('date_start')})")

            self._load_drivers(sk)
            if not self.data.drivers:
                raise RuntimeError("Nessun pilota trovato per questa sessione")

            session_start = parse_iso(info["date_start"])
            session_end = parse_iso(info["date_end"]) if info.get("date_end") else session_start + timedelta(hours=2)

            if n_laps is None:
                window_end = session_end
                self.status.emit("modalita' 'gara intera': scarico fino alla fine sessione...")
            else:
                window_end = self._find_time_for_n_laps(sk, session_start, session_end, n_laps)

            if window_end <= session_start:
                window_end = session_start + timedelta(minutes=5)  # rete di sicurezza

            self._download_chunked(
                sk, "location", "date", session_start, window_end,
                self._ingest_point, CHUNK_MINUTES, "posizioni",
            )
            for d in self.data.drivers.values():
                d.samples.sort(key=lambda s: s[0])
            if not self.data.drivers or self.data.t_min is None:
                raise RuntimeError("Nessun dato di posizione nella finestra calcolata")

            self._download_chunked(
                sk, "position", "date", session_start, window_end,
                self._ingest_position, LAPS_CHUNK_MINUTES, "classifica",
            )
            for arr in self.data.positions.values():
                arr.sort(key=lambda p: p[0])

            self.data.compute_sector_split()

            window_s = int((self.data.t_max - self.data.t_min) / 1000) if self.data.t_max else 0
            sector_msg = "settori OK" if self.data.sector_split else "settori non disponibili"
            self.status.emit(f"pronto: {len(self.data.drivers)} auto, finestra {window_s}s, {sector_msg}")
            self.session_loaded.emit()
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))

    def _find_time_for_n_laps(self, sk, session_start, session_end, n_laps):
        """Scarica i giri a blocchi crescenti finche' la maggior parte dei
        piloti ha completato almeno n_laps giri, poi restituisce l'istante
        in cui l'ultimo di questi completa il giro n_laps.
        """
        cur = session_start
        safety_limit = session_start + timedelta(minutes=LAPS_SEARCH_SAFETY_CAP_MIN)
        hard_end = min(session_end, safety_limit)
        max_lap_seen = {}

        while cur < hard_end:
            nxt = min(cur + timedelta(minutes=LAPS_CHUNK_MINUTES), hard_end)
            chunk = self._get(
                f"{API}/laps?session_key={sk}&date_start>{to_openf1_iso(cur)}&date_start<{to_openf1_iso(nxt)}")
            for lap in chunk:
                self._ingest_lap(lap)
                num = lap.get("driver_number")
                ln = lap.get("lap_number")
                if num is not None and ln is not None:
                    max_lap_seen[num] = max(max_lap_seen.get(num, 0), ln)
            cur = nxt

            reached = sum(1 for v in max_lap_seen.values() if v >= n_laps)
            total_known = max(len(self.data.drivers), 1)
            self.status.emit(f"cerco {n_laps} giri... {reached}/{total_known} piloti li hanno completati")
            if total_known and reached >= max(1, int(total_known * 0.8)):
                break

        for arr in self.data.laps.values():
            arr.sort(key=lambda l: l["t_start_ms"])

        end_candidates = [
            lap["t_start_ms"] + lap["duration"] * 1000
            for arr in self.data.laps.values()
            for lap in arr if lap["lap_number"] == n_laps
        ]
        if end_candidates:
            return datetime.fromtimestamp(max(end_candidates) / 1000.0, tz=cur.tzinfo)
        return cur

    def _download_chunked(self, sk, endpoint, date_field, start_dt, end_dt, ingest_fn, chunk_minutes, label):
        cur = start_dt
        total = 0
        total_seconds = max((end_dt - start_dt).total_seconds(), 1)
        while cur < end_dt:
            nxt = min(cur + timedelta(minutes=chunk_minutes), end_dt)
            url = (f"{API}/{endpoint}?session_key={sk}&{date_field}>{to_openf1_iso(cur)}"
                   f"&{date_field}<{to_openf1_iso(nxt)}")
            chunk = self._get(url)
            for rec in chunk:
                ingest_fn(rec)
            total += len(chunk)
            pct = int(((nxt - start_dt).total_seconds() / total_seconds) * 100)
            self.status.emit(f"scarico {label}... {pct}% ({total} record)")
            cur = nxt
        return total

    # ------------------------------------------------------------ live ---
    def start_live(self, session_key_raw):
        self.stop_live()
        self._live_stop.clear()
        self._live_thread = threading.Thread(target=self._live_loop, args=(session_key_raw,), daemon=True)
        self._live_thread.start()

    def stop_live(self):
        self._live_stop.set()
        if self._live_thread and self._live_thread.is_alive():
            self._live_thread.join(timeout=2)

    def _live_loop(self, session_key_raw):
        try:
            self.data.reset()
            self.status.emit("cerco sessione live...")
            key_param = session_key_raw or "latest"
            sessions = self._get(f"{API}/sessions?session_key={key_param}")
            info = sessions[0] if sessions else None
            if not info:
                raise RuntimeError("Nessuna sessione live trovata")

            sk = info["session_key"]
            self.data.session_key = sk
            self.status.emit(f"LIVE: {info.get('location')} {info.get('session_name')}")
            self._load_drivers(sk)

            since_iso = (datetime.utcnow() - timedelta(seconds=20)).isoformat() + "Z"
            self.data.last_seen_iso = since_iso
            self.data.last_seen_laps_iso = since_iso
            self.data.last_seen_positions_iso = since_iso

            self.session_loaded.emit()

            cycle = 0
            base_interval = 3.0
            while not self._live_stop.is_set():
                cycle += 1
                effective_interval = base_interval + self.rate_limiter.penalty
                try:
                    new_points = self._get(f"{API}/location?session_key={sk}&date>{self.data.last_seen_iso}")
                    if new_points:
                        for p in new_points:
                            self._ingest_point(p)
                        self.data.last_seen_iso = new_points[-1]["date"]

                    new_laps = self._get(f"{API}/laps?session_key={sk}&date_start>{self.data.last_seen_laps_iso}")
                    if new_laps:
                        for lap in new_laps:
                            self._ingest_lap(lap)
                        self.data.last_seen_laps_iso = new_laps[-1]["date_start"]
                        for arr in self.data.laps.values():
                            arr.sort(key=lambda l: l["t_start_ms"])
                        self.data.compute_sector_split()

                    new_positions = self._get(
                        f"{API}/position?session_key={sk}&date>{self.data.last_seen_positions_iso}")
                    if new_positions:
                        for pos in new_positions:
                            self._ingest_position(pos)
                        self.data.last_seen_positions_iso = new_positions[-1]["date"]
                        for arr in self.data.positions.values():
                            arr.sort(key=lambda p: p[0])

                    n_new = len(new_points) if new_points else 0
                    effective_interval = base_interval + self.rate_limiter.penalty
                    self.status.emit(f"LIVE ciclo {cycle}: +{n_new} punti posizione "
                                      f"({len(self.data.drivers)} auto)")
                except Exception as e:  # noqa: BLE001
                    effective_interval = base_interval + self.rate_limiter.penalty
                    self.status.emit(f"live: errore rete ({e}), riprovo tra {effective_interval:.1f}s...")

                self._live_stop.wait(effective_interval)
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))

    # --------------------------------------------------------- comuni ---
    def _load_drivers(self, session_key):
        drivers = self._get(f"{API}/drivers?session_key={session_key}")
        for d in drivers:
            num = d["driver_number"]
            color = d.get("team_colour")
            self.data.get_driver(
                num,
                name=d.get("name_acronym") or d.get("broadcast_name") or str(num),
                color=f"#{color}" if color else None,
            )

    def _ingest_point(self, p):
        x, y = p.get("x"), p.get("y")
        if x is None or y is None:
            return
        num = p["driver_number"]
        t_ms = parse_iso(p["date"]).timestamp() * 1000.0
        d = self.data.get_driver(num)
        d.add_sample(t_ms, x, y)
        self.data.expand_bounds(x, y)
        self.data.t_min = t_ms if self.data.t_min is None else min(self.data.t_min, t_ms)
        self.data.t_max = t_ms if self.data.t_max is None else max(self.data.t_max, t_ms)

    def _ingest_lap(self, rec):
        num = rec.get("driver_number")
        date_start = rec.get("date_start")
        if num is None or not date_start or rec.get("lap_duration") is None:
            return
        t_ms = parse_iso(date_start).timestamp() * 1000.0
        self.data.laps.setdefault(num, []).append({
            "t_start_ms": t_ms,
            "lap_number": rec.get("lap_number"),
            "duration": rec.get("lap_duration"),
            "s1": rec.get("duration_sector_1"),
            "s2": rec.get("duration_sector_2"),
            "s3": rec.get("duration_sector_3"),
        })

    def _ingest_position(self, rec):
        num = rec.get("driver_number")
        pos = rec.get("position")
        date = rec.get("date")
        if num is None or pos is None or not date:
            return
        t_ms = parse_iso(date).timestamp() * 1000.0
        self.data.positions.setdefault(num, []).append((t_ms, pos))
