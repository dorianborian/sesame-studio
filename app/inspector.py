"""Per-servo angle sliders + live readouts, plus the selected frame's
delay/face fields. Edits push straight into the 3D viewport and, when a
timeline frame is selected, into that frame."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QSpinBox, QGroupBox,
    QFormLayout, QComboBox, QScrollArea,
)

from .servo_model import (
    SERVO_ORDER, SERVO_RANGES, SERVO_COLORS, DEFAULT_ANGLE, FACE_ANIM_MODES,
    clamp_angle,
)

# The slider track always spans the full 0-180 sweep, the same for every
# servo, so a given angle lands at the same screen position on every row
# (e.g. 90 is always dead center). All 8 servos share the same 0-180 range
# today (see SERVO_RANGES), but the track is kept independent of
# SERVO_RANGES -- with per-servo clamping in ServoRow._on_slider -- so the
# rows stay visually in sync even if a future hardware revision restricts
# an individual joint again.
SLIDER_MIN, SLIDER_MAX = 0, 180


class ServoRow(QWidget):
    changed = Signal(str, int)

    def __init__(self, servo: str, parent=None):
        super().__init__(parent)
        self.servo = servo
        lo, hi = SERVO_RANGES[servo]
        color = SERVO_COLORS[servo]

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)

        name_lbl = QLabel(servo)
        name_lbl.setFixedWidth(28)
        name_lbl.setStyleSheet(f"color:{color}; font-weight:700;")
        layout.addWidget(name_lbl)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(SLIDER_MIN, SLIDER_MAX)
        self.slider.setValue(DEFAULT_ANGLE if lo <= DEFAULT_ANGLE <= hi else lo)
        layout.addWidget(self.slider, 1)

        self.spin = QSpinBox()
        self.spin.setRange(lo, hi)
        self.spin.setValue(self.slider.value())
        self.spin.setFixedWidth(72)
        self.spin.setSuffix("°")
        layout.addWidget(self.spin)

        range_lbl = QLabel(f"{lo}-{hi}")
        range_lbl.setProperty("role", "subheading")
        range_lbl.setFixedWidth(56)
        layout.addWidget(range_lbl)

        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)

    def _on_slider(self, val: int) -> None:
        clamped = clamp_angle(self.servo, val)
        if clamped != val:
            # Dragged past this joint's real limit -- snap the handle back
            # rather than letting it show an angle the servo can't reach.
            self.slider.setValue(clamped)
            return
        self.spin.blockSignals(True)
        self.spin.setValue(val)
        self.spin.blockSignals(False)
        self.changed.emit(self.servo, val)

    def _on_spin(self, val: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(val)
        self.slider.blockSignals(False)
        self.changed.emit(self.servo, val)

    def set_value(self, val: int, emit: bool = False) -> None:
        val = clamp_angle(self.servo, val)
        block = not emit
        if block:
            self.slider.blockSignals(True)
            self.spin.blockSignals(True)
        self.slider.setValue(val)
        self.spin.setValue(val)
        if block:
            self.slider.blockSignals(False)
            self.spin.blockSignals(False)


class InspectorPanel(QWidget):
    """Emits anglesChanged whenever any servo row moves, and
    frameFieldsChanged when delay/face/face-mode fields change."""

    anglesChanged = Signal(dict)      # {servo: angle} full snapshot
    frameFieldsChanged = Signal(int, object, str)  # delay_ms, face|None, face_mode

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        inner = QWidget()
        scroll.setWidget(inner)
        layout = QVBoxLayout(inner)

        servo_box = QGroupBox("Servo Angles")
        servo_layout = QVBoxLayout(servo_box)
        self.rows: dict[str, ServoRow] = {}
        for servo in SERVO_ORDER:
            row = ServoRow(servo)
            row.changed.connect(self._on_row_changed)
            servo_layout.addWidget(row)
            self.rows[servo] = row
        layout.addWidget(servo_box)

        frame_box = QGroupBox("Selected Frame")
        form = QFormLayout(frame_box)

        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 60000)
        self.delay_spin.setSuffix(" ms")
        self.delay_spin.setValue(200)
        self.delay_spin.valueChanged.connect(self._emit_frame_fields)
        form.addRow("Delay:", self.delay_spin)

        self.face_combo = QComboBox()
        self.face_combo.addItem("(none)", None)
        self.face_combo.currentIndexChanged.connect(self._emit_frame_fields)
        form.addRow("Face:", self.face_combo)

        self.face_mode_combo = QComboBox()
        self.face_mode_combo.addItems(FACE_ANIM_MODES)
        self.face_mode_combo.currentIndexChanged.connect(self._emit_frame_fields)
        form.addRow("Face Mode:", self.face_mode_combo)

        layout.addWidget(frame_box)
        layout.addStretch(1)

        self._loading = False

    # -- external API -------------------------------------------------
    def set_face_options(self, face_names: list[str]) -> None:
        current = self.face_combo.currentData()
        self.face_combo.blockSignals(True)
        self.face_combo.clear()
        self.face_combo.addItem("(none)", None)
        for name in sorted(face_names):
            self.face_combo.addItem(name, name)
        idx = self.face_combo.findData(current)
        self.face_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.face_combo.blockSignals(False)

    def load_angles(self, angles: dict[str, int]) -> None:
        self._loading = True
        for servo, row in self.rows.items():
            row.set_value(angles.get(servo, DEFAULT_ANGLE))
        self._loading = False

    def load_frame_fields(self, delay_ms: int, face: str | None, face_mode: str) -> None:
        self._loading = True
        self.delay_spin.setValue(delay_ms)
        idx = self.face_combo.findData(face)
        self.face_combo.setCurrentIndex(idx if idx >= 0 else 0)
        mi = self.face_mode_combo.findText(face_mode)
        self.face_mode_combo.setCurrentIndex(mi if mi >= 0 else 0)
        self._loading = False

    def current_angles(self) -> dict[str, int]:
        return {s: r.slider.value() for s, r in self.rows.items()}

    # -- internal -------------------------------------------------------
    def _on_row_changed(self, servo: str, value: int) -> None:
        if self._loading:
            return
        self.anglesChanged.emit(self.current_angles())

    def _emit_frame_fields(self) -> None:
        if self._loading:
            return
        self.frameFieldsChanged.emit(
            self.delay_spin.value(), self.face_combo.currentData(),
            self.face_mode_combo.currentText(),
        )
