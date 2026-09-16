"""
widgets.py - Componenti UI Qt per F1 Overlay: tracciato, classifica+gap,
pannello impostazioni tema, barra titolo, maniglia di ridimensionamento.
"""

import time

from PySide6.QtCore import Qt, QTimer, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QPainterPath, QRadialGradient
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLineEdit, QPushButton, QComboBox,
    QLabel, QSizePolicy, QCheckBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QSlider, QColorDialog,
)

from data import LAPS_OPTIONS, CAMERA_FOLLOW_FACTOR

GREEN_DELTA = QColor(60, 220, 120)
YELLOW_DELTA = QColor(255, 209, 60)
NEUTRAL_TEXT = QColor(255, 255, 255)


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
    """Canvas che disegna tracciato (con settori), scia e auto con antialiasing.
    Colori/dimensioni prelevati dal Theme condiviso (self.theme), cosi'
    l'utente puo' modificarli a runtime dal pannello impostazioni.
    """

    def __init__(self, data, theme):
        super().__init__()
        self.data = data
        self.theme = theme
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
        track_color = QColor(self.theme.track_color)
        track_color.setAlpha(200)

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
            _draw_glow_path(painter, path, track_color)
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

        _draw_glow_path(painter, paths["default"], track_color)
        for k in (0, 1, 2):
            _draw_glow_path(painter, paths[k], QColor(self.theme.sector_colors[k]))

    def _draw_cars(self, painter):
        label_size = self.theme.car_label_size
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

            painter.setFont(QFont("Segoe UI", label_size, QFont.DemiBold))
            label_pos = center + QPointF(radius + 4, -radius)
            painter.setPen(QColor(0, 0, 0, 200))
            painter.drawText(label_pos + QPointF(1, 1), d.name)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(label_pos, d.name)


class StandingsPanel(QWidget):
    """Pannello classifica compatto: posizione, ultimo giro, tempi settore
    e gap live lungo il tracciato rispetto al pilota davanti.

    Colorazione:
      - Ultimo giro (e settori, se la checkbox e' attiva): verde se piu'
        basso del pilota davanti, giallo se piu' alto.
      - Gap: verde se si sta riducendo rispetto alla lettura precedente
        (l'auto sta recuperando), giallo se sta aumentando (sta perdendo
        terreno).
    """

    COL_WIDTHS = [20, 44, 52, 38, 38, 38, 52]  # pos, pilota, giro, s1, s2, s3, gap

    def __init__(self, theme):
        super().__init__()
        self.theme = theme
        self._update_style()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(3)

        header_row = QHBoxLayout()
        self.title_label = QLabel("Classifica")
        header_row.addWidget(self.title_label)
        header_row.addStretch(1)
        self.sectors_checkbox = QCheckBox("settori")
        header_row.addWidget(self.sectors_checkbox)
        self.gap_checkbox = QCheckBox("gap")
        self.gap_checkbox.setChecked(True)
        header_row.addWidget(self.gap_checkbox)
        layout.addLayout(header_row)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["#", "Pilota", "Giro", "S1", "S2", "S3", "Gap"])
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
        layout.addWidget(self.table)

        total_w = sum(self.COL_WIDTHS) + 14 + 12
        self.setFixedWidth(total_w)
        self.apply_theme()

    def _update_style(self):
        c = QColor(self.theme.panel_bg_color)
        self.setStyleSheet(
            f"background-color: rgba({c.red()},{c.green()},{c.blue()},{self.theme.panel_opacity}); "
            f"border-radius: 8px;")

    def apply_theme(self):
        self._update_style()
        fs = self.theme.standings_font_size
        self.title_label.setStyleSheet(f"color: white; font-weight: 600; font-size: {fs}px;")
        self.sectors_checkbox.setStyleSheet(f"color: #bbbbbb; font-size: {max(fs - 1, 7)}px;")
        self.gap_checkbox.setStyleSheet(f"color: #bbbbbb; font-size: {max(fs - 1, 7)}px;")
        self.table.setStyleSheet(f"""
            QTableWidget {{ background: transparent; color: white; border: none; font-size: {fs}px; }}
            QHeaderView::section {{ background: rgba(255,255,255,30); color: #ddd;
                                    border: none; padding: 1px; font-size: {max(fs - 1, 7)}px; }}
        """)

    def update_rows(self, rows, color_sectors, show_gap):
        self.table.setColumnHidden(6, not show_gap)
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
            cols.append((6, "gap", "gap_color", True))

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


class SettingsPanel(QWidget):
    """Pannello per personalizzare il tema (colori tracciato/settori,
    dimensione testo, opacita' pannelli) direttamente da UI.
    """

    def __init__(self, theme, on_change):
        super().__init__()
        self.theme = theme
        self.on_change = on_change  # callback chiamata dopo ogni modifica
        self.setFixedWidth(210)
        self._update_style()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        title = QLabel("Impostazioni tema")
        title.setStyleSheet("color: white; font-weight: 600; font-size: 11px;")
        outer.addWidget(title)

        outer.addWidget(self._color_row("Colore tracciato", "track_color"))
        outer.addWidget(self._color_row("Settore 1", "sector_colors", 0))
        outer.addWidget(self._color_row("Settore 2", "sector_colors", 1))
        outer.addWidget(self._color_row("Settore 3", "sector_colors", 2))
        outer.addWidget(self._color_row("Sfondo pannelli", "panel_bg_color"))

        outer.addWidget(self._slider_row("Dim. testo auto", "car_label_size", 6, 16))
        outer.addWidget(self._slider_row("Dim. testo classifica", "standings_font_size", 7, 16))
        outer.addWidget(self._slider_row("Opacita' pannelli", "panel_opacity", 60, 255))

        outer.addStretch(1)

    def _update_style(self):
        c = QColor(self.theme.panel_bg_color)
        self.setStyleSheet(
            f"background-color: rgba({c.red()},{c.green()},{c.blue()},{self.theme.panel_opacity}); "
            f"border-radius: 8px; color: white;")

    def _color_row(self, label_text, attr, index=None):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(label_text)
        lbl.setStyleSheet("color: #dddddd; font-size: 10px;")
        h.addWidget(lbl, 1)

        current = getattr(self.theme, attr)
        current_color = current[index] if index is not None else current
        btn = QPushButton()
        btn.setFixedSize(28, 18)
        btn.setStyleSheet(f"background-color: {current_color}; border: 1px solid #555; border-radius: 3px;")

        def pick_color():
            color = QColorDialog.getColor(QColor(current_color), self, "Scegli colore")
            if color.isValid():
                if index is not None:
                    getattr(self.theme, attr)[index] = color.name()
                else:
                    setattr(self.theme, attr, color.name())
                btn.setStyleSheet(f"background-color: {color.name()}; border: 1px solid #555; border-radius: 3px;")
                self._update_style()
                self.on_change()

        btn.clicked.connect(pick_color)
        h.addWidget(btn)
        return row

    def _slider_row(self, label_text, attr, lo, hi):
        row = QWidget()
        v = QVBoxLayout(row)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)
        lbl = QLabel(f"{label_text}: {getattr(self.theme, attr)}")
        lbl.setStyleSheet("color: #dddddd; font-size: 10px;")
        v.addWidget(lbl)

        slider = QSlider(Qt.Horizontal)
        slider.setMinimum(lo)
        slider.setMaximum(hi)
        slider.setValue(int(getattr(self.theme, attr)))

        def on_value_changed(val):
            setattr(self.theme, attr, val)
            lbl.setText(f"{label_text}: {val}")
            self._update_style()
            self.on_change()

        slider.valueChanged.connect(on_value_changed)
        v.addWidget(slider)
        return row


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

        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedWidth(26)
        self.settings_btn.setCheckable(True)
        self.settings_btn.setChecked(False)
        layout.addWidget(self.settings_btn)

        self.status_label = QLabel("pronto")
        self.status_label.setStyleSheet("color: #cfcfcf; font-size: 10px;")
        self.status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.status_label, 1)

        close_btn = QPushButton("\u2715")
        close_btn.setFixedWidth(24)
        close_btn.setStyleSheet("QPushButton:hover { background-color: #c0392b; }")
        close_btn.clicked.connect(self.main_window.close)
        layout.addWidget(close_btn)

        for btn in (self.load_btn, self.play_btn, self.standings_btn, self.settings_btn, close_btn):
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
