"""
data.py - Modelli dati e logica pura per F1 Overlay.

Nessuna dipendenza da PySide6/Qt in questo modulo: contiene solo strutture
dati, calcoli geometrici (proiezione su tracciato, lunghezza giro) e il
tema (colori/dimensioni) personalizzabile da UI. Qt vive in widgets.py,
le chiamate di rete in worker.py.
"""

import math
from collections import deque
from datetime import datetime

API = "https://api.openf1.org/v1"

FALLBACK_COLORS = [
    "#e10600", "#00d2be", "#0600ef", "#ff8700", "#006f62",
    "#dc0000", "#900000", "#2b4562", "#b6babd", "#fdb827",
]

TRAIL_LEN = 18                # punti di scia per auto
FOLLOW_FACTOR = 0.28          # quanto velocemente la pos. mostrata insegue il target (0-1)
CAMERA_FOLLOW_FACTOR = 0.12
POLL_INTERVAL_S = 3.0         # intervallo di polling in modalita' live
CHUNK_MINUTES = 15            # dimensione blocco per il download di 'location' (dati pesanti)
LAPS_CHUNK_MINUTES = 25       # blocco per la ricerca dei giri (dati leggeri, blocchi piu' grandi ok)
LAPS_SEARCH_SAFETY_CAP_MIN = 45
DELTA_EPS = 0.0005            # soglia sotto la quale un delta e' "uguale" (niente colore)
GAP_TREND_EPS = 0.03          # soglia (secondi) sotto la quale il trend del gap e' "stabile"

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


def format_gap_seconds(seconds):
    if seconds is None:
        return "-"
    return f"+{seconds:.1f}s"


class Theme:
    """Tema visivo modificabile a runtime dall'UI (colori/dimensioni).

    Gli oggetti QColor/QFont vengono costruiti in widgets.py a partire da
    queste stringhe/valori semplici, cosi' questo modulo resta senza Qt.
    """

    def __init__(self):
        self.track_color = "#ffffff"
        self.sector_colors = ["#00e5ff", "#ffd400", "#ff2fd0"]  # S1, S2, S3
        self.car_label_size = 8
        self.standings_font_size = 10
        self.panel_opacity = 190       # 0-255, alpha sfondo pannelli
        self.panel_bg_color = "#0f0f12"  # colore base sfondo pannelli (RGB, senza alpha)


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

    def _bracket(self, current_t_ms):
        """Trova (i_lo, i_hi) attorno a current_t_ms nei campioni ordinati."""
        if not self.samples:
            return None
        if current_t_ms <= self.samples[0][0]:
            return 0, 0
        if current_t_ms >= self.samples[-1][0]:
            n = len(self.samples) - 1
            return n, n
        lo, hi = 0, len(self.samples) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.samples[mid][0] <= current_t_ms:
                lo = mid
            else:
                hi = mid
        return lo, hi

    def target_at(self, current_t_ms):
        """Interpola linearmente tra i due campioni che avvolgono current_t_ms."""
        bracket = self._bracket(current_t_ms)
        if bracket is None:
            return None
        lo, hi = bracket
        t0, x0, y0 = self.samples[lo]
        t1, x1, y1 = self.samples[hi]
        if t1 == t0:
            return x1, y1
        frac = (current_t_ms - t0) / (t1 - t0)
        return x0 + (x1 - x0) * frac, y0 + (y1 - y0) * frac

    def speed_at(self, current_t_ms):
        """Stima la velocita' (unita' di distanza al secondo) attorno a
        current_t_ms usando i due campioni piu' vicini. Le unita' x/y di
        OpenF1 sono espresse in metri, quindi il risultato e' m/s.
        Ritorna None se non calcolabile (troppo pochi campioni).
        """
        bracket = self._bracket(current_t_ms)
        if bracket is None:
            return None
        lo, hi = bracket
        if lo == hi:
            # ai bordi della serie: prova ad usare il segmento adiacente disponibile
            if hi + 1 < len(self.samples):
                lo, hi = hi, hi + 1
            elif lo - 1 >= 0:
                lo, hi = lo - 1, lo
            else:
                return None
        t0, x0, y0 = self.samples[lo]
        t1, x1, y1 = self.samples[hi]
        dt = (t1 - t0) / 1000.0
        if dt <= 0:
            return None
        dist = math.hypot(x1 - x0, y1 - y0)
        return dist / dt

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


class LapReference:
    """Traiettoria di un singolo giro completo, usata come riferimento per
    proiettare la posizione di qualunque auto e ottenere una distanza
    percorsa lungo il tracciato (arc-length), utile per calcolare i gap.
    """

    def __init__(self, points):
        # points: lista di (x, y) di un giro intero, in ordine
        self.points = points
        self.cum = [0.0]
        for i in range(1, len(points)):
            x0, y0 = points[i - 1]
            x1, y1 = points[i]
            self.cum.append(self.cum[-1] + math.hypot(x1 - x0, y1 - y0))
        self.length = self.cum[-1] if self.cum else 0.0

    def project(self, x, y):
        """Ritorna la distanza percorsa (arc-length, stessa unita' di x/y,
        cioe' metri) del punto (x,y) proiettato sul segmento piu' vicino
        della traiettoria di riferimento. O(n) sul numero di punti del giro
        (qualche centinaio): accettabile alla frequenza di refresh usata.
        """
        if len(self.points) < 2:
            return 0.0
        best_dist2 = None
        best_arc = 0.0
        for i in range(1, len(self.points)):
            x0, y0 = self.points[i - 1]
            x1, y1 = self.points[i]
            dx, dy = x1 - x0, y1 - y0
            seg_len2 = dx * dx + dy * dy
            if seg_len2 == 0:
                continue
            tproj = ((x - x0) * dx + (y - y0) * dy) / seg_len2
            tproj = max(0.0, min(1.0, tproj))
            px, py = x0 + tproj * dx, y0 + tproj * dy
            dist2 = (x - px) ** 2 + (y - py) ** 2
            if best_dist2 is None or dist2 < best_dist2:
                best_dist2 = dist2
                best_arc = self.cum[i - 1] + tproj * math.hypot(dx, dy)
        return best_arc


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
        self.lap_reference = None            # LapReference (o None se non ancora disponibile)
        self.prev_gap_seconds = {}           # number -> ultimo gap noto (per il trend colorato)

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
        intero nella finestra dati scaricata, del pilota con piu' campioni.
        Come sotto-effetto, costruisce anche il LapReference per i gap.
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
                best_track = self.drivers[best_num]
                lap_points = [(x, y) for (t, x, y) in best_track.samples if t0 <= t <= t_end]
                if len(lap_points) >= 2:
                    self.lap_reference = LapReference(lap_points)
                return

    def total_distance_for(self, number, current_t_ms):
        """Distanza totale percorsa (giri completi * lunghezza giro +
        avanzamento nel giro corrente), usata per ordinare le auto lungo il
        tracciato e calcolare i gap. Ritorna None se non calcolabile
        (serve il LapReference, cioe' un giro completo di riferimento).
        """
        if self.lap_reference is None or self.lap_reference.length <= 0:
            return None
        drv = self.drivers.get(number)
        if drv is None or drv.display_x is None:
            return None
        arc = self.lap_reference.project(drv.display_x, drv.display_y)
        last_completed_lap = 0
        for lap in self.laps.get(number, []):
            if lap["t_start_ms"] <= current_t_ms:
                last_completed_lap = lap["lap_number"] or 0
            else:
                break
        return last_completed_lap * self.lap_reference.length + arc
