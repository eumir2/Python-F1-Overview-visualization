

#!/usr/bin/env python3
"""
main.py - Entry point di F1 Overlay (Pro).

Overlay desktop trasparente, sempre in primo piano, con:
  - STORICO: replay di un GP passato, quantita' di gara scelta in NUMERO
    DI GIRI (5/10/20/gara intera), download a blocchi.
  - LIVE: polling incrementale con rate-limiting adattivo (rallenta da
    solo se l'API risponde 429 per traffico elevato), posizione reale
    delle auto con lo stesso ritardo del live timing ufficiale (~3-4s).
  - CLASSIFICA (nascosta di default, tasto 🏁): posizione, ultimo giro,
    tempi per settore (colorati vs pilota davanti) e GAP LIVE lungo il
    tracciato (non solo a fine giro), colorato in base al trend
    (si riduce = verde, aumenta = giallo).
  - SETTORI sul tracciato: colorazione approssimata S1/S2/S3.
  - TELECAMERA DINAMICA: segui pilota con zoom.
  - TEMA PERSONALIZZABILE da UI (tasto ⚙): colori tracciato/settori,
    dimensione testo, opacita' pannelli.
  - Finestra ridimensionabile (maniglia in basso a destra).

Dati: https://openf1.org (API pubblica, nessuna API key richiesta).

Dipendenze esterne (da installare sul tuo PC, NON incluse qui):
    pip install PySide6 requests

Avvio:
    python3 main.py
"""

import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout

from data import SessionData, Theme, LAPS_OPTIONS, format_time, format_gap_seconds, DELTA_EPS, GAP_TREND_EPS
from worker import Worker
from widgets import TrackWidget, StandingsPanel, SettingsPanel, TitleBar, ResizeGrip


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(500, 380)
        self.resize(940, 660)

        self.theme = Theme()
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

        self.track_widget = TrackWidget(self.data, self.theme)
        self.track_widget.setStyleSheet("background-color: rgba(10,10,12,140); "
                                          "border-bottom-left-radius: 10px; "
                                          "border-bottom-right-radius: 10px;")
        body.addWidget(self.track_widget, 1)

        self.settings_panel = SettingsPanel(self.theme, self._on_theme_changed)
        self.settings_panel.setVisible(False)
        body.addWidget(self.settings_panel)

        self.standings_panel = StandingsPanel(self.theme)
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
        self.title_bar.settings_btn.toggled.connect(self.settings_panel.setVisible)

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

    def _on_theme_changed(self):
        self.standings_panel.apply_theme()
        self.settings_panel._update_style()

    def _on_status(self, msg):
        self.title_bar.status_label.setText(msg)
        print("[status]", msg)

    def _on_session_loaded(self):
        self.track_widget.reset_playback()
        self.track_widget.playing = False
        self.title_bar.play_btn.setText("\u25B6")
        self.data.prev_gap_seconds.clear()
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

        # distanza totale percorsa per ogni pilota (per gap live lungo il tracciato)
        distances = {}
        for num in d.drivers:
            dist = d.total_distance_for(num, current_t)
            if dist is not None:
                distances[num] = dist

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
                "number": num,
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
                "distance": distances.get(num),
            })
        rows.sort(key=lambda r: r["_sort_pos"])

        # velocita' media approssimata (per stimare i secondi di gap dalla distanza)
        avg_speed = self._estimate_avg_speed(current_t)

        # colora tempo giro/settori rispetto al pilota davanti in classifica
        for i, r in enumerate(rows):
            ref = rows[i - 1] if i > 0 else None
            for _disp_key, val_key, color_key in (
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

            # gap live in metri->secondi rispetto al pilota davanti, lungo il tracciato
            gap_seconds = None
            if ref is not None and r["distance"] is not None and ref["distance"] is not None and avg_speed:
                meters_behind = ref["distance"] - r["distance"]
                if meters_behind >= 0:
                    gap_seconds = meters_behind / avg_speed
            r["gap"] = format_gap_seconds(gap_seconds)

            gap_color = None
            if gap_seconds is not None:
                prev = d.prev_gap_seconds.get(r["number"])
                if prev is not None:
                    trend = gap_seconds - prev
                    if trend < -GAP_TREND_EPS:
                        gap_color = "green"   # si sta avvicinando
                    elif trend > GAP_TREND_EPS:
                        gap_color = "yellow"  # si sta allontanando
                d.prev_gap_seconds[r["number"]] = gap_seconds
            r["gap_color"] = gap_color

        self.standings_panel.update_rows(
            rows,
            self.standings_panel.sectors_checkbox.isChecked(),
            self.standings_panel.gap_checkbox.isChecked(),
        )

    def _estimate_avg_speed(self, current_t):
        """Velocita' media (m/s) tra tutte le auto attorno all'istante
        corrente, usata per convertire una distanza (metri) in un gap
        stimato in secondi. Ritorna None se non calcolabile.
        """
        speeds = []
        for drv in self.data.drivers.values():
            s = drv.speed_at(current_t)
            if s and s > 1:  # scarta valori ~0 (box, inizio/fine dati)
                speeds.append(s)
        if not speeds:
            return None
        return sum(speeds) / len(speeds)

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
