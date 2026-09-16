#!/usr/bin/env python3
"""
F1 Overlay (Pro) - versione Qt/PySide6
========================================
Overlay desktop trasparente, sempre in primo piano, con:

  - STORICO: replay di un GP passato (play/pausa/velocità). La quantità di
    gara da scaricare si sceglie in NUMERO DI GIRI (5/10/20/gara intera):
    il programma calcola da solo, usando i tempi giro reali, quanto tempo
    scaricare, e lo fa "a blocchi" per rispettare i limiti dell'API
    OpenF1 (che rifiuta richieste troppo grandi in un colpo solo).
  - LIVE: polling incrementale (con backoff sui rate-limit) e
    "inseguimento" fluido della posizione reale delle auto (stesso
    ritardo del live timing ufficiale, circa 3-4s).
  - CLASSIFICA in tempo reale (nascosta di default, tasto 🏁): posizione,
    ultimo giro, tempi per settore. Il tempo/settori di ogni pilota sono
    colorati rispetto al pilota davanti in classifica: verde se più
    basso (migliore), giallo se più alto (peggiore).
  - SETTORI sul tracciato: colorazione approssimata S1/S2/S3 basata su un
    giro completo reale trovato nella finestra caricata.
  - TELECAMERA DINAMICA: possibilità di "seguire" un pilota con zoom
    (2x/4x/8x) invece della vista dell'intero circuito.
  - Finestra ridimensionabile (maniglia in basso a destra) e pannello
    classifica compatto, per dare priorità visiva al tracciato.

Nota importante sull'API: OpenF1 restituisce HTTP 404 quando una query
non produce risultati (non un array vuoto!) e HTTP 422 se si richiedono
troppi dati in una sola chiamata (es. oltre ~60 minuti di "location").
Il codice gestisce entrambi i casi.

Dati: https://openf1.org (API pubblica, nessuna API key richiesta).

Dipendenze esterne (da installare sul tuo PC, NON incluse qui):
    pip install PySide6 requests

Avvio:
    python3 f1_overlay_qt.py
"""

import sys
import time
import threading
from collections import deque
from datetime import datetime, timedelta

import requests
from PySide6.QtCore import Qt, QTimer, Signal, QObject, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QPainterPath, QRadialGradient
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLineEdit, QPushButton, QComboBox, QLabel, QSizePolicy, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
)

API = "https://api.openf1.org/v1"

FALLBACK_COLORS = [
    "#e10600", "#00d2be", "#0600ef", "#ff8700", "#006f62",
    "#dc0000", "#900000", "#2b4562", "#b6babd", "#fdb827",
]

SECTOR_COLORS = {
    0: QColor(0, 229, 255),    # settore 1 - ciano
    1: QColor(255, 212, 0),    # settore 2 - giallo
    2: QColor(255, 47, 208),   # settore 3 - magenta
}
DEFAULT_TRACK_COLOR = QColor(255, 255, 255, 200)
GREEN_DELTA = QColor(60, 220, 120)
YELLOW_DELTA = QColor(255, 209, 60)
NEUTRAL_TEXT = QColor(255, 255, 255)

TRAIL_LEN = 18                # punti di scia per auto
FOLLOW_FACTOR = 0.28           # quanto velocemente la pos. mostrata insegue il target (0-1)
CAMERA_FOLLOW_FACTOR = 0.12
POLL_INTERVAL_S = 3.0          # intervallo di polling in modalita' live
CHUNK_MINUTES = 15             # dimensione blocco per il download di 'location' (dati pesanti)
LAPS_CHUNK_MINUTES = 25        # blocco per la ricerca dei giri (dati leggeri, blocchi piu' grandi ok)
LAPS_SEARCH_SAFETY_CAP_MIN = 45  # tetto di sicurezza mentre si cercano gli N giri richiesti
DELTA_EPS = 0.0005              # soglia sotto la quale un delta e' considerato "uguale" (niente colore)

LAPS_OPTIONS = {"5 giri": 5, "10 giri": 10, "20 giri": 20, "Gara intera": None}


def parse_iso(dt_str):
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def to_openf1_iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def format_time(seconds):
    """Formatta un tempo (float secondi) in stile F1: 'm:ss.mmm' oppure 'ss.mmm'."""
    if seconds is None:
        return "-"
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m}:{s:06.3f}" if m > 0 else f"{s:.3f}"


def http_get_json(session: requests.Session, url: str, max_retries: int = 5):
    """GET con backoff esponenziale sui 429 (rate limit). Gestisce le
    peculiarità di OpenF1: 404 = 'nessun risultato' (non un errore, torna
    lista vuota), 422 = richiesta troppo ampia (errore esplicito e chiaro).
    """
    delay = 1.0
    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 404:
                return []  # OpenF1: nessun dato per questa query, non e' un errore
            if resp.status_code == 422:
                raise RuntimeError(
                    "L'API ha rifiutato la richiesta perche' troppo ampia "
                    "(troppi dati in una volta). Riduci il numero di giri richiesti."
                )
            if resp.status_code == 429:
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            if attempt == max_retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 20)
    raise RuntimeError("Troppi tentativi falliti (rate limit persistente)")


class DriverTrack:
    """Storico posizioni di un pilota + stato di interpolazione per il disegno."""

    def __init__(self, number, name, color):
        self.number = number
        self.name = name
        self.color = color
        self.samples = []          # [(t_ms, x, y), ...] ordinati per tempo
        self.display_x = None
        self.display_y = None
        self.trail = deque(maxlen=TRAIL_LEN)

    def add_sample(self, t_ms, x, y):
        self.samples.append((t_ms, x, y))

    def target_at(self, current_t_ms):
        """Interpola linearmente tra i due campioni che avvolgono current_t_ms."""
        if not self.samples:
            return None
        if current_t_ms <= self.samples[0][0]:
            return self.samples[0][1], self.samples[0][2]
        if current_t_ms >= self.samples[-1][0]:
            return self.samples[-1][1], self.samples[-1][2]
        lo, hi = 0, len(self.samples) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.samples[mid][0] <= current_t_ms:
                lo = mid
            else:
                hi = mid
        t0, x0, y0 = self.samples[lo]
        t1, x1, y1 = self.samples[hi]
        if t1 == t0:
            return x1, y1
        frac = (current_t_ms - t0) / (t1 - t0)
        return x0 + (x1 - x0) * frac, y0 + (y1 - y0) * frac

    def latest(self):
        if not self.samples:
            return None
        return self.samples[-1][1], self.samples[-1][2]

    def step_towards(self, tx, ty):
        if self.display_x is None:
            self.display_x, self.display_y = tx, ty
        else:
            self.display_x += (tx - self.display_x) * FOLLOW_FACTOR
            self.display_y += (ty - self.display_y) * FOLLOW_FACTOR
        self.trail.append((self.display_x, self.display_y))


class SessionData:
    def __init__(self):
        self.session_key = None
        self.drivers = {}          # number -> DriverTrack
        self.laps = {}             # number -> [ {t_start_ms, lap_number, duration, s1, s2, s3}, ... ]
        self.positions = {}        # number -> [(t_ms, position), ...]
        self.min_x = self.max_x = None
        self.min_y = self.max_y = None
        self.t_min = self.t_max = None
        self.last_seen_iso = None            # location: per il polling incrementale live
        self.last_seen_laps_iso = None
        self.last_seen_positions_iso = None
        self.sector_split = None             # dict con confini settore stimati (o None)

    def reset(self):
        self.__init__()

    def expand_bounds(self, x, y):
        self.min_x = x if self.min_x is None else min(self.min_x, x)
        self.max_x = x if self.max_x is None else max(self.max_x, x)
        self.min_y = y if self.min_y is None else min(self.min_y, y)
        self.max_y = y if self.max_y is None else max(self.max_y, y)

    def get_driver(self, number, name=None, color=None):
        if number not in self.drivers:
            self.drivers[number] = DriverTrack(
                number, name or str(number),
                color or FALLBACK_COLORS[number % len(FALLBACK_COLORS)],
            )
        return self.drivers[number]

    def compute_sector_split(self):
        """Stima i confini S1/S2/S3 usando il primo giro completo, contenuto per
        intero nella finestra dati scaricata, del pilota con piu' campioni
        (lo stesso usato per disegnare il contorno pista). Approssimazione:
        OpenF1 non fornisce le coordinate esatte dei confini settore, solo i
        tempi; usiamo quindi gli istanti di inizio/fine settore di un giro
        reale per marcare i punti (x,y) corrispondenti sulla traiettoria.
        """
        if self.sector_split is not None or not self.drivers:
            return
        best_num = max(self.drivers.items(), key=lambda kv: len(kv[1].samples))[0]
        for lap in self.laps.get(best_num, []):
            dur, s1, s2 = lap.get("duration"), lap.get("s1"), lap.get("s2")
            if dur is None or s1 is None or s2 is None:
                continue
            t0 = lap["t_start_ms"]
            t_end = t0 + dur * 1000
            if self.t_min is not None and self.t_max is not None and t0 >= self.t_min and t_end <= self.t_max:
                self.sector_split = {
                    "driver": best_num,
                    "t0": t0,
                    "b1": t0 + s1 * 1000,
                    "b2": t0 + (s1 + s2) * 1000,
                    "t_end": t_end,
                }
                return


class Worker(QObject):
    """Esegue le chiamate di rete in un thread separato ed emette segnali verso la UI."""

    status = Signal(str)
    session_loaded = Signal()
    error = Signal(str)

    def __init__(self, data: SessionData):
        super().__init__()
        self.data = data
        self.http = requests.Session()
        self._live_thread = None
        self._live_stop = threading.Event()

    # --------------------------------------------------------- storico ---
    def load_historical(self, session_key_raw, n_laps):
        threading.Thread(target=self._load_historical_impl, args=(session_key_raw, n_laps), daemon=True).start()

    def _load_historical_impl(self, session_key_raw, n_laps):
        try:
            self.data.reset()
            self.status.emit("carico sessione storica...")
            if session_key_raw:
                sessions = http_get_json(self.http, f"{API}/sessions?session_key={session_key_raw}")
                info = sessions[0] if sessions else None
            else:
                self.status.emit("cerco un GP storico di default (2023)...")
                sessions = http_get_json(self.http, f"{API}/sessions?year=2023&session_type=Race")
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

            # --- 1) determina fino a quando scaricare, in base ai giri richiesti ---
            if n_laps is None:
                window_end = session_end
                self.status.emit("modalita' 'gara intera': scarico fino alla fine sessione...")
            else:
                window_end = self._find_time_for_n_laps(sk, session_start, session_end, n_laps)

            if window_end <= session_start:
                window_end = session_start + timedelta(minutes=5)  # rete di sicurezza

            # --- 2) scarica posizioni (dati pesanti: a blocchi) ---
            self._download_chunked(
                sk, "location", "date", session_start, window_end,
                self._ingest_point, CHUNK_MINUTES, "posizioni",
            )
            for d in self.data.drivers.values():
                d.samples.sort(key=lambda s: s[0])
            if not self.data.drivers or self.data.t_min is None:
                raise RuntimeError("Nessun dato di posizione nella finestra calcolata")

            # --- 3) classifica (posizioni in gara) ---
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
        piloti ha completato almeno n_laps giri, poi restituisce l'istante in
        cui l'ultimo di questi completa il giro n_laps. Ha un tetto di
        sicurezza per non scaricare per sempre in caso di dati incompleti.
        """
        cur = session_start
        safety_limit = session_start + timedelta(minutes=LAPS_SEARCH_SAFETY_CAP_MIN)
        hard_end = min(session_end, safety_limit)
        max_lap_seen = {}

        while cur < hard_end:
            nxt = min(cur + timedelta(minutes=LAPS_CHUNK_MINUTES), hard_end)
            chunk = http_get_json(
                self.http,
                f"{API}/laps?session_key={sk}&date_start>{to_openf1_iso(cur)}&date_start<{to_openf1_iso(nxt)}",
            )
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
        # fallback: nessun pilota ha completato esattamente n_laps nella finestra cercata
        # -> usa quanto scaricato finora (cur) come fine finestra
        return cur

    def _download_chunked(self, sk, endpoint, date_field, start_dt, end_dt, ingest_fn, chunk_minutes, label):
        cur = start_dt
        total = 0
        total_seconds = max((end_dt - start_dt).total_seconds(), 1)
        while cur < end_dt:
            nxt = min(cur + timedelta(minutes=chunk_minutes), end_dt)
            url = (f"{API}/{endpoint}?session_key={sk}&{date_field}>{to_openf1_iso(cur)}"
                   f"&{date_field}<{to_openf1_iso(nxt)}")
            chunk = http_get_json(self.http, url)
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
            sessions = http_get_json(self.http, f"{API}/sessions?session_key={key_param}")
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
            while not self._live_stop.is_set():
                cycle += 1
                try:
                    new_points = http_get_json(
                        self.http, f"{API}/location?session_key={sk}&date>{self.data.last_seen_iso}")
                    if new_points:
                        for p in new_points:
                            self._ingest_point(p)
                        self.data.last_seen_iso = new_points[-1]["date"]

                    new_laps = http_get_json(
                        self.http, f"{API}/laps?session_key={sk}&date_start>{self.data.last_seen_laps_iso}")
                    if new_laps:
                        for lap in new_laps:
                            self._ingest_lap(lap)
                        self.data.last_seen_laps_iso = new_laps[-1]["date_start"]
                        for arr in self.data.laps.values():
                            arr.sort(key=lambda l: l["t_start_ms"])
                        self.data.compute_sector_split()

                    new_positions = http_get_json(
                        self.http, f"{API}/position?session_key={sk}&date>{self.data.last_seen_positions_iso}")
                    if new_positions:
                        for pos in new_positions:
                            self._ingest_position(pos)
                        self.data.last_seen_positions_iso = new_positions[-1]["date"]
                        for arr in self.data.positions.values():
                            arr.sort(key=lambda p: p[0])

                    n_new = len(new_points) if new_points else 0
                    self.status.emit(f"LIVE ciclo {cycle}: +{n_new} punti posizione "
                                      f"({len(self.data.drivers)} auto)")
                except Exception as e:  # noqa: BLE001
                    self.status.emit(f"live: errore rete ({e}), riprovo...")

                self._live_stop.wait(POLL_INTERVAL_S)
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))

    # --------------------------------------------------------- comuni ---
    def _load_drivers(self, session_key):
        drivers = http_get_json(self.http, f"{API}/drivers?session_key={session_key}")
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
            return  # giro incompleto/out-lap: niente durata utile
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


def _draw_glow_path(painter, path, color, glow_alpha=40, core_alpha=200, glow_width=8, core_width=2):
    glow = QColor(color)
    glow.setAlpha(glow_alpha)
    glow_pen = QPen(glow, glow_width)
    glow_pen.setCapStyle(Qt.RoundCap)
    glow_pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(glow_pen)
    painter.drawPath(path)

    core = QColor(color)
    core.setAlpha(core_alpha)
    core_pen = QPen(core, core_width)
    core_pen.setCapStyle(Qt.RoundCap)
    core_pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(core_pen)
    painter.drawPath(path)


class TrackWidget(QWidget):
    """Canvas che disegna tracciato (con settori), scia e auto con antialiasing."""

    def __init__(self, data: SessionData):
        super().__init__()
        self.data = data
        self.mode = "historical"     # "historical" | "live"
        self.playing = False
        self.speed = 5.0
        self.playhead_ms = 0.0
        self._last_frame_time = None

        self.follow_driver_number = None
        self.zoom_factor = 4.0
        self.camera_center = None    # QPointF in coordinate-dati (non pixel)

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAutoFillBackground(False)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_frame)
        self.timer.start(16)  # ~60 fps

    def reset_playback(self):
        self.playhead_ms = 0.0
        self._last_frame_time = None
        self.camera_center = None

    def current_time_ms(self):
        if self.mode == "historical":
            return (self.data.t_min or 0) + self.playhead_ms
        return time.time() * 1000.0

    def _on_frame(self):
        now = time.monotonic() * 1000.0
        if self.mode == "historical":
            if self.playing and self._last_frame_time is not None:
                dt = (now - self._last_frame_time) * self.speed
                self.playhead_ms += dt
                total = (self.data.t_max - self.data.t_min) if (self.data.t_max and self.data.t_min) else 0
                if self.playhead_ms >= total:
                    self.playhead_ms = total
                    self.playing = False
            current_t = (self.data.t_min or 0) + self.playhead_ms
            for d in self.data.drivers.values():
                target = d.target_at(current_t)
                if target:
                    d.step_towards(*target)
        else:  # live: insegue sempre l'ultimo dato ricevuto
            for d in self.data.drivers.values():
                target = d.latest()
                if target:
                    d.step_towards(*target)

        if self.follow_driver_number is not None:
            drv = self.data.drivers.get(self.follow_driver_number)
            if drv and drv.display_x is not None:
                if self.camera_center is None:
                    self.camera_center = QPointF(drv.display_x, drv.display_y)
                else:
                    cx = self.camera_center.x() + (drv.display_x - self.camera_center.x()) * CAMERA_FOLLOW_FACTOR
                    cy = self.camera_center.y() + (drv.display_y - self.camera_center.y()) * CAMERA_FOLLOW_FACTOR
                    self.camera_center = QPointF(cx, cy)

        self._last_frame_time = now
        self.update()

    def _project(self, x, y):
        d = self.data
        w_px = self.width() - 70
        h_px = self.height() - 70
        if w_px <= 0 or h_px <= 0 or d.max_x is None or d.max_x == d.min_x:
            return QPointF(35, 35)

        if self.follow_driver_number is not None and self.camera_center is not None:
            data_w = (d.max_x - d.min_x) or 1
            data_h = (d.max_y - d.min_y) or 1
            base_scale = min(w_px / data_w, h_px / data_h)
            scale = base_scale * self.zoom_factor
            cx, cy = self.camera_center.x(), self.camera_center.y()
            px = self.width() / 2 + (x - cx) * scale
            py = self.height() / 2 - (y - cy) * scale
            return QPointF(px, py)

        sx = (x - d.min_x) / (d.max_x - d.min_x or 1)
        sy = (y - d.min_y) / (d.max_y - d.min_y or 1)
        return QPointF(35 + sx * w_px, 35 + (1 - sy) * h_px)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        if not self.data.drivers:
            return

        self._draw_track_outline(painter)
        self._draw_cars(painter)

    def _draw_track_outline(self, painter):
        best = max(self.data.drivers.values(), key=lambda d: len(d.samples), default=None)
        if not best or len(best.samples) < 2:
            return

        split = self.data.sector_split
        use_sectors = split is not None and split.get("driver") == best.number

        if not use_sectors:
            path = QPainterPath()
            first = True
            for (_t, x, y) in best.samples:
                pt = self._project(x, y)
                if first:
                    path.moveTo(pt)
                    first = False
                else:
                    path.lineTo(pt)
            _draw_glow_path(painter, path, DEFAULT_TRACK_COLOR)
            return

        paths = {"default": QPainterPath(), 0: QPainterPath(), 1: QPainterPath(), 2: QPainterPath()}
        current_key = None
        for (t, x, y) in best.samples:
            pt = self._project(x, y)
            if split["t0"] <= t <= split["t_end"]:
                key = 0 if t < split["b1"] else (1 if t < split["b2"] else 2)
            else:
                key = "default"
            if current_key is None:
                paths[key].moveTo(pt)
            elif key != current_key:
                paths[current_key].lineTo(pt)
                paths[key].moveTo(pt)
            else:
                paths[key].lineTo(pt)
            current_key = key

        _draw_glow_path(painter, paths["default"], DEFAULT_TRACK_COLOR)
        for k in (0, 1, 2):
            _draw_glow_path(painter, paths[k], SECTOR_COLORS[k])

    def _draw_cars(self, painter):
        for d in self.data.drivers.values():
            if d.display_x is None:
                continue

            trail_pts = list(d.trail)
            for i in range(1, len(trail_pts)):
                alpha = int(160 * (i / len(trail_pts)))
                col = QColor(d.color)
                col.setAlpha(alpha)
                pen = QPen(col, 3)
                pen.setCapStyle(Qt.RoundCap)
                painter.setPen(pen)
                p0 = self._project(*trail_pts[i - 1])
                p1 = self._project(*trail_pts[i])
                painter.drawLine(p0, p1)

            center = self._project(d.display_x, d.display_y)
            radius = 7.0

            shadow_grad = QRadialGradient(center, radius * 2.2)
            shadow_grad.setColorAt(0.0, QColor(0, 0, 0, 90))
            shadow_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.setBrush(QBrush(shadow_grad))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(center, radius * 2.2, radius * 2.2)

            body_grad = QRadialGradient(center, radius)
            base = QColor(d.color)
            light = QColor(base).lighter(140)
            body_grad.setColorAt(0.0, light)
            body_grad.setColorAt(1.0, base)
            painter.setBrush(QBrush(body_grad))
            painter.setPen(QPen(QColor(0, 0, 0, 200), 1.4))
            painter.drawEllipse(center, radius, radius)

            painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
            label_pos = center + QPointF(radius + 4, -radius)
            painter.setPen(QColor(0, 0, 0, 200))
            painter.drawText(label_pos + QPointF(1, 1), d.name)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(label_pos, d.name)


class StandingsPanel(QWidget):
    """Pannello classifica compatto: posizione, ultimo giro e tempi settore.

    Il tempo/i settori sono colorati rispetto al pilota davanti in classifica:
    verde se piu' basso (migliore), giallo se piu' alto (peggiore).
    """

    COL_WIDTHS = [22, 46, 56, 40, 40, 40]  # pos, pilota, ultimo giro, s1, s2, s3

    def __init__(self):
        super().__init__()
        self.setStyleSheet("background-color: rgba(8,8,10,230); border-radius: 8px;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(3)

        header_row = QHBoxLayout()
        title = QLabel("Classifica")
        title.setStyleSheet("color: white; font-weight: 600; font-size: 11px;")
        header_row.addWidget(title)
        header_row.addStretch(1)
        self.sectors_checkbox = QCheckBox("settori")
        self.sectors_checkbox.setStyleSheet("color: #bbbbbb; font-size: 10px;")
        header_row.addWidget(self.sectors_checkbox)
        layout.addLayout(header_row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "Pilota", "Giro", "S1", "S2", "S3"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setWordWrap(False)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        for i, w in enumerate(self.COL_WIDTHS):
            header.setSectionResizeMode(i, QHeaderView.Fixed)
            self.table.setColumnWidth(i, w)
        self.table.setStyleSheet("""
            QTableWidget { background: transparent; color: white; border: none; font-size: 10px; }
            QHeaderView::section { background: rgba(255,255,255,30); color: #ddd;
                                    border: none; padding: 1px; font-size: 9px; }
        """)
        layout.addWidget(self.table)

        total_w = sum(self.COL_WIDTHS) + 14 + 12  # colonne + scrollbar verticale + margini
        self.setFixedWidth(total_w)

    def update_rows(self, rows, color_sectors):
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            pos_item = QTableWidgetItem(str(r["position"]) if r["position"] is not None else "-")
            pos_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 0, pos_item)

            name_item = QTableWidgetItem(r["name"])
            name_item.setForeground(QColor(r["color"]))
            self.table.setItem(i, 1, name_item)

            cols = [(2, "last_lap", "last_lap_color", True)]
            for col, key, color_key in (
                (3, "s1", "s1_color"), (4, "s2", "s2_color"), (5, "s3", "s3_color"),
            ):
                cols.append((col, key, color_key, color_sectors))

            for col, key, color_key, apply_color in cols:
                item = QTableWidgetItem(r[key])
                item.setTextAlignment(Qt.AlignCenter)
                color_name = r.get(color_key) if apply_color else None
                if color_name == "green":
                    item.setForeground(GREEN_DELTA)
                elif color_name == "yellow":
                    item.setForeground(YELLOW_DELTA)
                else:
                    item.setForeground(NEUTRAL_TEXT)
                self.table.setItem(i, col, item)


class ResizeGrip(QWidget):
    """Piccola maniglia in basso a destra per ridimensionare la finestra
    (necessaria perche' una finestra 'frameless' perde i bordi nativi)."""

    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.setFixedSize(16, 16)
        self.setCursor(Qt.SizeFDiagCursor)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(255, 255, 255, 160), 2)
        painter.setPen(pen)
        w, h = self.width(), self.height()
        for i in range(3):
            off = i * 5
            painter.drawLine(w - 2 - off, h - 2, w - 2, h - 2 - off)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            handle = self.main_window.windowHandle()
            if handle is not None:
                handle.startSystemResize(Qt.Edge.BottomEdge | Qt.Edge.RightEdge)


class TitleBar(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.setFixedHeight(38)
        self.setStyleSheet("""
            background-color: rgba(18,18,20,210);
            border-top-left-radius: 10px;
            border-top-right-radius: 10px;
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 6, 4)
        layout.setSpacing(5)

        title = QLabel("🏎️")
        title.setStyleSheet("color: white; font-weight: 600;")
        layout.addWidget(title)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Storico", "Live"])
        self.mode_combo.setFixedWidth(70)
        layout.addWidget(self.mode_combo)

        self.session_input = QLineEdit()
        self.session_input.setPlaceholderText("session_key")
        self.session_input.setFixedWidth(90)
        layout.addWidget(self.session_input)

        self.laps_combo = QComboBox()
        self.laps_combo.addItems(list(LAPS_OPTIONS.keys()))
        self.laps_combo.setFixedWidth(80)
        layout.addWidget(self.laps_combo)

        self.load_btn = QPushButton("Carica")
        layout.addWidget(self.load_btn)

        self.play_btn = QPushButton("\u25B6")
        self.play_btn.setFixedWidth(26)
        layout.addWidget(self.play_btn)

        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["1x", "5x", "20x", "50x"])
        self.speed_combo.setCurrentIndex(1)
        self.speed_combo.setFixedWidth(46)
        layout.addWidget(self.speed_combo)

        self.follow_combo = QComboBox()
        self.follow_combo.addItem("Nessuno", userData=None)
        self.follow_combo.setFixedWidth(88)
        layout.addWidget(self.follow_combo)

        self.zoom_combo = QComboBox()
        self.zoom_combo.addItems(["2x", "4x", "8x"])
        self.zoom_combo.setCurrentIndex(1)
        self.zoom_combo.setFixedWidth(42)
        self.zoom_combo.setEnabled(False)
        layout.addWidget(self.zoom_combo)

        self.standings_btn = QPushButton("🏁")
        self.standings_btn.setFixedWidth(26)
        self.standings_btn.setCheckable(True)
        self.standings_btn.setChecked(False)
        layout.addWidget(self.standings_btn)

        self.status_label = QLabel("pronto")
        self.status_label.setStyleSheet("color: #cfcfcf; font-size: 10px;")
        self.status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.status_label, 1)

        close_btn = QPushButton("\u2715")
        close_btn.setFixedWidth(24)
        close_btn.setStyleSheet("QPushButton:hover { background-color: #c0392b; }")
        close_btn.clicked.connect(self.main_window.close)
        layout.addWidget(close_btn)

        for btn in (self.load_btn, self.play_btn, self.standings_btn, close_btn):
            btn.setStyleSheet(btn.styleSheet() + "color: white; background-color: #2a2a2e; "
                                                   "border: none; border-radius: 4px; padding: 2px 6px;")

    def set_drivers(self, driver_list):
        """driver_list: lista di tuple (numero, nome) ordinate per numero."""
        current_data = self.follow_combo.currentData()
        self.follow_combo.blockSignals(True)
        self.follow_combo.clear()
        self.follow_combo.addItem("Nessuno", userData=None)
        for num, name in driver_list:
            self.follow_combo.addItem(f"{name} #{num}", userData=num)
        idx = self.follow_combo.findData(current_data)
        self.follow_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.follow_combo.blockSignals(False)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            handle = self.main_window.windowHandle()
            if handle is not None:
                handle.startSystemMove()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(500, 380)
        self.resize(940, 660)

        self.data = SessionData()
        self.worker = Worker(self.data)
        self.worker.status.connect(self._on_status)
        self.worker.session_loaded.connect(self._on_session_loaded)
        self.worker.error.connect(self._on_error)

        central = QWidget()
        central.setStyleSheet("background: transparent;")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        self.title_bar = TitleBar(self)
        outer.addWidget(self.title_bar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)

        self.track_widget = TrackWidget(self.data)
        self.track_widget.setStyleSheet("background-color: rgba(10,10,12,140); "
                                          "border-bottom-left-radius: 10px; "
                                          "border-bottom-right-radius: 10px;")
        body.addWidget(self.track_widget, 1)

        self.standings_panel = StandingsPanel()
        self.standings_panel.setVisible(False)   # nascosto di default: priorita' al circuito
        body.addWidget(self.standings_panel)

        outer.addLayout(body)
        self.setCentralWidget(central)

        self.resize_grip = ResizeGrip(self)
        self.resize_grip.raise_()

        self.title_bar.load_btn.clicked.connect(self._on_load_click)
        self.title_bar.play_btn.clicked.connect(self._on_play_click)
        self.title_bar.speed_combo.currentTextChanged.connect(self._on_speed_change)
        self.title_bar.mode_combo.currentTextChanged.connect(self._on_mode_change)
        self.title_bar.follow_combo.currentIndexChanged.connect(self._on_follow_change)
        self.title_bar.zoom_combo.currentTextChanged.connect(self._on_zoom_change)
        self.title_bar.standings_btn.toggled.connect(self.standings_panel.setVisible)

        self.standings_timer = QTimer(self)
        self.standings_timer.timeout.connect(self._refresh_standings)
        self.standings_timer.start(400)

        self._on_load_click()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "resize_grip"):
            self.resize_grip.move(self.width() - 20, self.height() - 20)
            self.resize_grip.raise_()

    # -------------------------------------------------------- handlers ---
    def _on_mode_change(self, text):
        is_live = text == "Live"
        self.track_widget.mode = "live" if is_live else "historical"
        self.title_bar.play_btn.setEnabled(not is_live)
        self.title_bar.speed_combo.setEnabled(not is_live)
        self.title_bar.laps_combo.setEnabled(not is_live)
        if not is_live:
            self.worker.stop_live()

    def _on_load_click(self):
        key = self.title_bar.session_input.text().strip()
        if self.title_bar.mode_combo.currentText() == "Live":
            self.worker.start_live(key or None)
        else:
            n_laps = LAPS_OPTIONS[self.title_bar.laps_combo.currentText()]
            self.worker.stop_live()
            self.worker.load_historical(key or None, n_laps)

    def _on_play_click(self):
        tw = self.track_widget
        tw.playing = not tw.playing
        self.title_bar.play_btn.setText("\u23F8" if tw.playing else "\u25B6")

    def _on_speed_change(self, text):
        self.track_widget.speed = float(text.replace("x", ""))

    def _on_follow_change(self, idx):
        num = self.title_bar.follow_combo.itemData(idx)
        self.track_widget.follow_driver_number = num
        self.title_bar.zoom_combo.setEnabled(num is not None)
        if num is None:
            self.track_widget.camera_center = None

    def _on_zoom_change(self, text):
        self.track_widget.zoom_factor = float(text.replace("x", ""))

    def _on_status(self, msg):
        self.title_bar.status_label.setText(msg)
        print("[status]", msg)

    def _on_session_loaded(self):
        self.track_widget.reset_playback()
        self.track_widget.playing = False
        self.title_bar.play_btn.setText("\u25B6")
        driver_list = sorted(
            ((num, d.name) for num, d in self.data.drivers.items()), key=lambda t: t[0])
        self.title_bar.set_drivers(driver_list)

    def _on_error(self, msg):
        self.title_bar.status_label.setText(f"ERRORE: {msg}")
        print("[error]", msg)

    # ------------------------------------------------------- classifica ---
    def _refresh_standings(self):
        d = self.data
        if not d.drivers or not self.standings_panel.isVisible():
            return
        current_t = self.track_widget.current_time_ms()
        rows = []
        for num, drv in d.drivers.items():
            position = None
            for (t, p) in d.positions.get(num, []):
                if t <= current_t:
                    position = p
                else:
                    break
            lap_rec = None
            for lap in d.laps.get(num, []):
                if lap["t_start_ms"] <= current_t:
                    lap_rec = lap
                else:
                    break
            rows.append({
                "position": position,
                "_sort_pos": position if position is not None else 9999,
                "name": drv.name,
                "color": drv.color,
                "last_lap": format_time(lap_rec["duration"]) if lap_rec else "-",
                "s1": format_time(lap_rec["s1"]) if lap_rec else "-",
                "s2": format_time(lap_rec["s2"]) if lap_rec else "-",
                "s3": format_time(lap_rec["s3"]) if lap_rec else "-",
                "last_lap_val": lap_rec["duration"] if lap_rec else None,
                "s1_val": lap_rec["s1"] if lap_rec else None,
                "s2_val": lap_rec["s2"] if lap_rec else None,
                "s3_val": lap_rec["s3"] if lap_rec else None,
            })
        rows.sort(key=lambda r: r["_sort_pos"])

        # colora ogni cella rispetto al pilota subito davanti in classifica
        for i, r in enumerate(rows):
            ref = rows[i - 1] if i > 0 else None
            for disp_key, val_key, color_key in (
                ("last_lap", "last_lap_val", "last_lap_color"),
                ("s1", "s1_val", "s1_color"),
                ("s2", "s2_val", "s2_color"),
                ("s3", "s3_val", "s3_color"),
            ):
                color = None
                if ref is not None and r[val_key] is not None and ref[val_key] is not None:
                    delta = r[val_key] - ref[val_key]
                    if delta < -DELTA_EPS:
                        color = "green"
                    elif delta > DELTA_EPS:
                        color = "yellow"
                r[color_key] = color

        self.standings_panel.update_rows(rows, self.standings_panel.sectors_checkbox.isChecked())

    def closeEvent(self, event):
        self.worker.stop_live()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
