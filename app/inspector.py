"""Pose inspector: per-servo sliders grouped by leg, pose tools (presets,
mirror, copy/paste) and the selected frame's delay/face fields.

Edits push straight into the 3D viewport and, when a timeline frame is
selected, into that frame. The inspector and the 3D view share one notion of
"selected joint" and "hovered joint" so a limb lights up in 3D when its row
is hovered and vice-versa.

Signals
-------
anglesChanged(dict)                 full {servo: int} snapshot after any edit
frameFieldsChanged(int, object, str) delay_ms, face name | None, face_mode
jointHovered(object)                servo name under the mouse, or None
jointSelected(object)               servo name clicked/focused, or None
editStarted() / editFinished()      bracket a continuous slider drag so the
                                    owner can fold it into one undo step
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QSpinBox,
    QFormLayout, QComboBox, QScrollArea, QToolButton, QFrame, QMenu, QApplication,
    QSizePolicy,
)

from .servo_model import (
    SERVO_ORDER, SERVO_RANGES, SERVO_COLORS, DEFAULT_ANGLE, FACE_ANIM_MODES,
    clamp_angle,
)
from .builtin_sequences import STAND_STATE, REST_STATE
from .icons import icon
from . import theme
from .theme import ACCENT_SOFT, ACCENT_SOFT_BORDER, BORDER, PANEL, RADIUS
from .widgets import Separator

# The slider track always spans the full 0-180 sweep so a given angle lands at
# the same screen position on every row (90 is always dead center), even if a
# future hardware revision restricts an individual joint via SERVO_RANGES.
SLIDER_MIN, SLIDER_MAX = 0, 180

# Legs in the order they read top-down on the robot (front first), each with
# its hip servo then its knee servo.
LEGS: list[tuple[str, str, str]] = [
    ("Front left", "L1", "L3"),
    ("Front right", "R1", "R3"),
    ("Rear left", "L2", "L4"),
    ("Rear right", "R2", "R4"),
]
JOINT_ROLE = {"R1": "hip", "R2": "hip", "L1": "hip", "L2": "hip",
              "R3": "knee", "R4": "knee", "L3": "knee", "L4": "knee"}
# Left/right twins. Mirroring a pose swaps each pair and flips the angle
# around 90 (R1=135 looks like L1=45), matching how the servos are mounted.
MIRROR_OF = {"R1": "L1", "L1": "R1", "R2": "L2", "L2": "R2",
             "R3": "L3", "L3": "R3", "R4": "L4", "L4": "R4"}


def mirrored_angle(angle: int) -> int:
    return SLIDER_MAX - int(angle)


class _ColorDot(QWidget):
    def __init__(self, color: str, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(10, 10)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(1, 1, 8, 8)


class ServoRow(QFrame):
    changed = Signal(str, int)
    hovered = Signal(object)        # servo name or None
    clicked = Signal(str)
    dragStarted = Signal()
    dragFinished = Signal()
    contextRequested = Signal(str, object)   # servo, global QPoint

    def __init__(self, servo: str, parent=None):
        super().__init__(parent)
        self.servo = servo
        self.setObjectName("ServoRow")
        self.setAttribute(Qt.WA_Hover, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._selected = False
        self._highlighted = False
        lo, hi = SERVO_RANGES[servo]

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 6, 3)
        layout.setSpacing(8)

        layout.addWidget(_ColorDot(SERVO_COLORS[servo]))

        name_lbl = QLabel(servo)
        name_lbl.setFixedWidth(22)
        name_lbl.setFont(theme.ui_font(theme.FONT_SIZE, 600))
        layout.addWidget(name_lbl)

        role_lbl = QLabel(JOINT_ROLE[servo])
        role_lbl.setFixedWidth(30)
        role_lbl.setProperty("role", "subheading")
        role_lbl.setFont(theme.ui_font(theme.FONT_SIZE_SM))
        layout.addWidget(role_lbl)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(SLIDER_MIN, SLIDER_MAX)
        self.slider.setPageStep(15)
        self.slider.setValue(DEFAULT_ANGLE if lo <= DEFAULT_ANGLE <= hi else lo)
        self.slider.setToolTip(f"{servo} {JOINT_ROLE[servo]} angle ({lo}–{hi}°). Right-click for options.")
        layout.addWidget(self.slider, 1)

        self.spin = QSpinBox()
        self.spin.setRange(lo, hi)
        self.spin.setValue(self.slider.value())
        self.spin.setFixedWidth(64)
        self.spin.setSuffix("°")
        self.spin.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.spin)

        self.slider.valueChanged.connect(self._on_slider)
        self.slider.sliderPressed.connect(self._on_pressed)
        self.slider.sliderReleased.connect(self.dragFinished)
        self.spin.valueChanged.connect(self._on_spin)
        self._restyle()

    # -- state ----------------------------------------------------------
    def set_selected(self, selected: bool) -> None:
        if selected != self._selected:
            self._selected = selected
            self._restyle()

    def set_highlighted(self, on: bool) -> None:
        if on != self._highlighted:
            self._highlighted = on
            self._restyle()

    def _restyle(self) -> None:
        if self._selected:
            bg, border = ACCENT_SOFT, ACCENT_SOFT_BORDER
        elif self._highlighted:
            bg, border = PANEL, BORDER
        else:
            bg, border = "transparent", "transparent"
        self.setStyleSheet(
            f"#ServoRow {{ background:{bg}; border:1px solid {border}; border-radius:{RADIUS}px; }}"
        )

    # -- events ----------------------------------------------------------
    def enterEvent(self, event) -> None:
        self.hovered.emit(self.servo)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.hovered.emit(None)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.servo)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.contextRequested.emit(self.servo, event.globalPos())

    def _on_pressed(self) -> None:
        self.clicked.emit(self.servo)
        self.dragStarted.emit()

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

    def value(self) -> int:
        return self.slider.value()

    def set_value(self, val: int, emit: bool = False) -> None:
        val = clamp_angle(self.servo, int(round(val)))
        if val == self.slider.value() and val == self.spin.value():
            return
        block = not emit
        if block:
            self.slider.blockSignals(True)
            self.spin.blockSignals(True)
        self.slider.setValue(val)
        self.spin.setValue(val)
        if block:
            self.slider.blockSignals(False)
            self.spin.blockSignals(False)


def _tool_button(icon_name: str, text: str, tooltip: str) -> QToolButton:
    btn = QToolButton()
    ico = icon(icon_name)
    if ico.isNull():
        btn.setText(text)
    else:
        btn.setIcon(ico)
        btn.setText(text)
        btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    btn.setToolTip(tooltip)
    btn.setProperty("variant", "flat")
    btn.setCursor(Qt.PointingHandCursor)
    return btn


class InspectorPanel(QWidget):
    """Emits anglesChanged whenever any servo row moves, and
    frameFieldsChanged when delay/face/face-mode fields change."""

    anglesChanged = Signal(dict)                   # {servo: angle} full snapshot
    frameFieldsChanged = Signal(int, object, str)  # delay_ms, face|None, face_mode
    jointHovered = Signal(object)
    jointSelected = Signal(object)
    editStarted = Signal()
    editFinished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        self._selected_joint: str | None = None
        self._pose_clipboard: dict[str, int] | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -- pose tools strip ------------------------------------------------
        tools = QWidget()
        tools.setProperty("role", "toolbar")
        tools.setAttribute(Qt.WA_StyledBackground, True)
        tools_layout = QHBoxLayout(tools)
        tools_layout.setContentsMargins(8, 6, 8, 6)
        tools_layout.setSpacing(2)

        self.preset_btn = _tool_button("robot", "Presets", "Jump to a standard pose")
        self.preset_btn.setPopupMode(QToolButton.InstantPopup)
        preset_menu = QMenu(self.preset_btn)
        preset_menu.addAction("Stand", lambda: self.apply_pose(STAND_STATE, "Stand"))
        preset_menu.addAction("Rest (all 90°)", lambda: self.apply_pose(REST_STATE, "Rest"))
        self.preset_btn.setMenu(preset_menu)
        tools_layout.addWidget(self.preset_btn)

        self.mirror_btn = _tool_button("flip-h", "Mirror", "Mirror the pose between left and right legs")
        self.mirror_btn.setPopupMode(QToolButton.InstantPopup)
        mirror_menu = QMenu(self.mirror_btn)
        mirror_menu.addAction("Copy left legs → right", lambda: self.mirror_pose("L"))
        mirror_menu.addAction("Copy right legs → left", lambda: self.mirror_pose("R"))
        mirror_menu.addAction("Swap left ↔ right", lambda: self.mirror_pose("swap"))
        self.mirror_btn.setMenu(mirror_menu)
        tools_layout.addWidget(self.mirror_btn)

        tools_layout.addStretch(1)

        self.copy_btn = _tool_button("copy", "", "Copy pose (all 8 angles)")
        self.copy_btn.setToolButtonStyle(Qt.ToolButtonIconOnly if not icon("copy").isNull() else Qt.ToolButtonTextOnly)
        if icon("copy").isNull():
            self.copy_btn.setText("Copy")
        self.copy_btn.clicked.connect(self.copy_pose)
        tools_layout.addWidget(self.copy_btn)

        self.paste_btn = _tool_button("paste", "", "Paste pose")
        self.paste_btn.setToolButtonStyle(Qt.ToolButtonIconOnly if not icon("paste").isNull() else Qt.ToolButtonTextOnly)
        if icon("paste").isNull():
            self.paste_btn.setText("Paste")
        self.paste_btn.clicked.connect(self.paste_pose)
        self.paste_btn.setEnabled(False)
        tools_layout.addWidget(self.paste_btn)
        outer.addWidget(tools)

        # -- scrolling body --------------------------------------------------
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll, 1)
        inner = QWidget()
        scroll.setWidget(inner)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)

        self.readonly_label = QLabel(
            "Built-in sequence — poses are read-only. Duplicate it in the timeline to edit."
        )
        self.readonly_label.setWordWrap(True)
        self.readonly_label.setProperty("role", "subheading")
        self.readonly_label.setVisible(False)
        layout.addWidget(self.readonly_label)

        self.rows: dict[str, ServoRow] = {}
        for title, hip, knee in LEGS:
            caption = QLabel(title.upper())
            caption.setProperty("role", "caption")
            caption.setContentsMargins(8, 8, 0, 2)
            layout.addWidget(caption)
            for servo in (hip, knee):
                row = ServoRow(servo)
                row.changed.connect(self._on_row_changed)
                row.hovered.connect(self._on_row_hovered)
                row.clicked.connect(self._on_row_clicked)
                row.dragStarted.connect(self.editStarted)
                row.dragFinished.connect(self.editFinished)
                row.contextRequested.connect(self._show_row_menu)
                layout.addWidget(row)
                self.rows[servo] = row

        layout.addSpacing(10)
        layout.addWidget(Separator())

        frame_caption = QLabel("SELECTED FRAME")
        frame_caption.setProperty("role", "caption")
        frame_caption.setContentsMargins(8, 10, 0, 4)
        layout.addWidget(frame_caption)
        self.frame_caption = frame_caption

        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setContentsMargins(8, 0, 6, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 60000)
        self.delay_spin.setSingleStep(10)
        self.delay_spin.setSuffix(" ms")
        self.delay_spin.setValue(200)
        self.delay_spin.setToolTip("How long the robot takes/holds to reach this pose before the next frame")
        self.delay_spin.valueChanged.connect(self._emit_frame_fields)
        form.addRow("Duration", self.delay_spin)

        self.face_combo = QComboBox()
        self.face_combo.addItem("(none)", None)
        self.face_combo.setToolTip("OLED face shown when this frame starts")
        self.face_combo.currentIndexChanged.connect(self._emit_frame_fields)
        self.face_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        form.addRow("Face", self.face_combo)

        self.face_mode_combo = QComboBox()
        self.face_mode_combo.addItems(FACE_ANIM_MODES)
        self.face_mode_combo.setToolTip("How an animated face plays: loop, once, or boomerang")
        self.face_mode_combo.currentIndexChanged.connect(self._emit_frame_fields)
        form.addRow("Face mode", self.face_mode_combo)
        layout.addWidget(form_host)
        self.frame_form = form_host

        layout.addStretch(1)

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

    def load_angles(self, angles: dict) -> None:
        """Show a pose without emitting anglesChanged (external update)."""
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
        return {s: self.rows[s].value() for s in SERVO_ORDER}

    def set_selected_joint(self, servo: str | None) -> None:
        """Highlight a row (e.g. because the limb was clicked in 3D)."""
        self._selected_joint = servo if servo in self.rows else None
        for name, row in self.rows.items():
            row.set_selected(name == self._selected_joint)

    def set_hovered_joint(self, servo: str | None) -> None:
        for name, row in self.rows.items():
            row.set_highlighted(name == servo)

    def set_read_only(self, read_only: bool) -> None:
        """Built-in sequences can be posed for preview but not stored; the
        frame fields are disabled and a hint explains why."""
        self.readonly_label.setVisible(read_only)
        self.frame_form.setEnabled(not read_only)

    def set_frame_caption(self, text: str) -> None:
        self.frame_caption.setText(text.upper())

    # -- pose tools -----------------------------------------------------
    def apply_pose(self, angles: dict, label: str = "Pose") -> None:
        """Set all rows and emit one anglesChanged (bracketed as one edit)."""
        self.editStarted.emit()
        self.load_angles(angles)
        self.anglesChanged.emit(self.current_angles())
        self.editFinished.emit()

    def mirror_pose(self, source: str) -> None:
        cur = self.current_angles()
        new = dict(cur)
        for servo, twin in MIRROR_OF.items():
            if source == "swap":
                new[servo] = mirrored_angle(cur[twin])
            elif servo.startswith(source):
                new[twin] = mirrored_angle(cur[servo])
        self.apply_pose(new, "Mirror")

    def copy_pose(self) -> None:
        self._pose_clipboard = self.current_angles()
        self.paste_btn.setEnabled(True)
        text = ", ".join(f"{s}={a}" for s, a in self._pose_clipboard.items())
        QApplication.clipboard().setText(text)

    def paste_pose(self) -> None:
        if self._pose_clipboard:
            self.apply_pose(self._pose_clipboard, "Paste pose")

    # -- internal -------------------------------------------------------
    def _show_row_menu(self, servo: str, global_pos) -> None:
        row = self.rows[servo]
        twin = MIRROR_OF[servo]
        menu = QMenu(self)
        menu.addAction("Reset to 90°", lambda: self._set_single(servo, DEFAULT_ANGLE))
        menu.addAction(f"Set to stand ({STAND_STATE[servo]}°)", lambda: self._set_single(servo, STAND_STATE[servo]))
        menu.addSeparator()
        menu.addAction(f"Mirror onto {twin}", lambda: self._set_single(twin, mirrored_angle(row.value())))
        menu.exec(global_pos)

    def _set_single(self, servo: str, value: int) -> None:
        self.editStarted.emit()
        self.rows[servo].set_value(value)
        self.anglesChanged.emit(self.current_angles())
        self.editFinished.emit()

    def _on_row_changed(self, servo: str, value: int) -> None:
        if self._loading:
            return
        self.anglesChanged.emit(self.current_angles())

    def _on_row_hovered(self, servo) -> None:
        self.jointHovered.emit(servo)

    def _on_row_clicked(self, servo: str) -> None:
        self.set_selected_joint(servo)
        self.jointSelected.emit(servo)

    def _emit_frame_fields(self) -> None:
        if self._loading:
            return
        self.frameFieldsChanged.emit(
            self.delay_spin.value(), self.face_combo.currentData(),
            self.face_mode_combo.currentText(),
        )
