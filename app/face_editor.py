"""Face Editor: a pixel painter for the 128x64 1-bit OLED faces.

Layout: tool rail on the left, zoomable canvas in the middle, animated
preview + properties + (on demand) the Arduino byte-array on the right,
frame strip underneath. The dialog can be shown modeless -- it emits
``previewPixels`` so the 3D robot's screen can mirror the painting live.

Pixel work lives in face_core (numpy) and face_canvas; this module is the
window around them. ``frame_bytes_to_image`` / ``image_to_pixels`` /
``blank_image`` remain importable from here for older callers.
"""

from __future__ import annotations

import os

import numpy as np
from PySide6.QtCore import Qt, QPoint, QPointF, QRectF, QTimer, Signal
from PySide6.QtGui import (
    QColor, QGuiApplication, QIcon, QImage, QKeySequence, QPainter, QPen,
    QPolygonF, QShortcut,
)
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget,
    QTabWidget, QToolButton, QVBoxLayout, QWidget, QFrame,
)

from . import face_core as fc
from . import face_render as fr
from .code_export import check_face_name_collision, export_face
from .face_canvas import FaceCanvas, TOOLS, CLIP_MIME, clipboard_array
from .face_import import ImageImportDialog
from .face_render import blank_image, frame_bytes_to_image, image_to_array
from .face_widgets import (
    FrameStrip, OledPreview, caption, chip_button, get_icon, mono_font,
    mono_label, painted_icon, separator, set_button_tooltip, suite_stylesheet,
    tool_button,
)
from .project import Face, array_to_frame, frame_to_array
from .servo_model import (
    FACE_CATEGORIES, FACE_CATEGORY_CUSTOM, FACE_CATEGORY_LABELS, FACE_HEIGHT,
    FACE_MAX_FRAMES, FACE_WIDTH, RESERVED_FACE_NAMES,
)
from .theme import ACCENT, DANGER, WARNING

__all__ = [
    "FaceEditorDialog", "FaceCanvas", "TOOLS", "frame_bytes_to_image",
    "image_to_pixels", "image_to_array", "blank_image",
]

IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;All Files (*)"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")

# (tool id, icon name, label, shortcut)
TOOL_DEFS = [
    ("pencil", "pencil", "Pencil", "B"),
    ("eraser", "eraser", "Eraser", "E"),
    ("line", "line", "Line", "L"),
    ("rect", "rect", "Rectangle", "R"),
    ("rect_fill", "rect-fill", "Filled Rectangle", "Shift+R"),
    ("ellipse", "ellipse", "Ellipse", "O"),
    ("ellipse_fill", "ellipse-fill", "Filled Ellipse", "Shift+O"),
    ("fill", "fill", "Flood Fill", "G"),
    ("text", "text", "Text", "T"),
    ("select", "select", "Select / Move", "M"),
]

TOOL_HINTS = {
    "pencil": "Drag to draw  ·  right-drag erases  ·  Shift for a straight line",
    "eraser": "Drag to erase  ·  Shift for a straight line",
    "line": "Drag a line  ·  Shift snaps to 45°  ·  right-drag erases",
    "rect": "Drag a box  ·  Shift for a square  ·  right-drag erases",
    "rect_fill": "Drag a box  ·  Shift for a square  ·  right-drag erases",
    "ellipse": "Drag an ellipse  ·  Shift for a circle  ·  right-drag erases",
    "ellipse_fill": "Drag an ellipse  ·  Shift for a circle  ·  right-drag erases",
    "fill": "Click to fill an enclosed area  ·  right-click fills with black",
    "text": "Type above, then click the canvas to stamp  ·  right-click cuts the text out",
    "select": "Drag to select  ·  drag inside to move (Ctrl copies)  ·  arrows nudge  ·  Del clears  ·  Esc deselects",
}


def image_to_pixels(image: QImage) -> list[list[bool]]:
    """QImage -> 128x64 list-of-lists bool grid (legacy helper; new code
    should use ``image_to_array``)."""
    return image_to_array(image).tolist()


def _symmetry_icon(vertical_axis: bool) -> QIcon:
    """Dashed axis with an arrow-head on each side (mirror drawing)."""
    def draw(p: QPainter, s: int, col: QColor) -> None:
        if not vertical_axis:
            p.translate(s, 0)
            p.rotate(90)
        pen = QPen(col, 1.3, Qt.DashLine)
        pen.setDashPattern([1.6, 1.6])
        p.setPen(pen)
        p.drawLine(QPointF(8, 1.5), QPointF(8, 14.5))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawPolygon(QPolygonF([QPointF(5.2, 8), QPointF(1.5, 4.6), QPointF(1.5, 11.4)]))
        p.drawPolygon(QPolygonF([QPointF(10.8, 8), QPointF(14.5, 4.6), QPointF(14.5, 11.4)]))
    return painted_icon(draw)


def _center_icon() -> QIcon:
    """A block centred inside a frame."""
    def draw(p: QPainter, s: int, col: QColor) -> None:
        p.setPen(QPen(col, 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(2, 3.5, 12, 9), 1.5, 1.5)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRect(QRectF(6, 6.25, 4, 3.5))
    return painted_icon(draw)


def _brush_icon(n: int) -> QIcon:
    """A square the relative size of an n-pixel brush."""
    def draw(p: QPainter, s: int, col: QColor) -> None:
        p.setRenderHint(QPainter.Antialiasing, False)
        d = n * 2 + 2
        o = (s - d) // 2
        p.fillRect(o, o, d, d, col)
    return painted_icon(draw)


class _FrameList(list):
    """Frames as (64,128) bool arrays. Assigning a QImage or packed bytes
    converts it, which keeps older scripts that poke ``dlg.frames[i]``
    working."""

    @staticmethod
    def _coerce(value) -> np.ndarray:
        if isinstance(value, QImage):
            return image_to_array(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return frame_to_array(value)
        return np.asarray(value, dtype=bool)

    def __setitem__(self, index, value) -> None:
        if isinstance(index, slice):
            super().__setitem__(index, [self._coerce(v) for v in value])
        else:
            super().__setitem__(index, self._coerce(value))

    def append(self, value) -> None:
        super().append(self._coerce(value))

    def insert(self, index, value) -> None:
        super().insert(index, self._coerce(value))


class FaceEditorDialog(QDialog):
    """Edit (or create) a single Face, including its animation frames.

    On Save ``result_face`` is set, ``faceSaved`` is emitted and the dialog
    is accepted. Works both with ``exec()`` and modeless via ``show()``.
    """

    previewPixels = Signal(object)   # (64,128) bool ndarray of the visible frame, <= 30 Hz
    faceSaved = Signal(object)       # the resulting Face

    def __init__(self, face: Face | None, existing_names: set[str], parent=None,
                 is_header_mode: bool = False):
        super().__init__(parent)
        self.setObjectName("FaceEditor")
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint
                            | Qt.WindowMinimizeButtonHint)
        self.setStyleSheet(suite_stylesheet())
        self.setAcceptDrops(True)
        self.is_header_mode = is_header_mode
        self.setWindowTitle(f"Face Editor - {face.name}" if (face and is_header_mode) else "Face Editor")
        self.existing_names = set(existing_names) - ({face.name} if face else set())
        self.result_face: Face | None = None
        self._source_face = face

        self.frames = _FrameList()
        if face is not None and face.frames:
            for f in face.frames[:FACE_MAX_FRAMES]:
                self.frames.append(frame_to_array(f))
        else:
            self.frames.append(fc.blank())
        self._histories = [fc.History() for _ in self.frames]
        self.frame_index = 0

        self._dirty = False
        self._loading = True
        self._playing = False
        self._play_pos = 0
        self._live_pending = False

        self._build_ui(face)
        self._build_shortcuts()

        # Coalesces thumbnail / preview / robot-mirror updates to <= 30 Hz.
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(33)
        self._live_timer.timeout.connect(self._on_live_timer)
        # The byte-array text is only rebuilt while its tab is showing.
        self._code_timer = QTimer(self)
        self._code_timer.setSingleShot(True)
        self._code_timer.setInterval(200)
        self._code_timer.timeout.connect(self._refresh_output)
        self._code_stale = True
        self._play_timer = QTimer(self)
        self._play_timer.timeout.connect(self._advance_playback)

        self._refresh_frame_list()
        self._select_frame(0)
        self.set_tool("pencil")
        self._refresh_warning()
        self._update_history_buttons()
        self._loading = False
        self._dirty = False
        self._update_dirty_indicator()

        self.setMinimumSize(980, 600)
        self._initial_resize()
        self.canvas.setFocus()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _initial_resize(self) -> None:
        w, h = 1180, 760
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            w = min(w, max(980, avail.width() - 80))
            h = min(h, max(600, avail.height() - 80))
        self.resize(w, h)

    def _build_ui(self, face: Face | None) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.canvas = FaceCanvas()
        self.canvas.changed.connect(self._on_canvas_changed)
        self.canvas.committed.connect(self._on_canvas_committed)
        self.canvas.hoverChanged.connect(self._on_hover)
        self.canvas.zoomChanged.connect(self._on_zoom_changed)
        self.canvas.historyChanged.connect(self._update_history_buttons)
        self.canvas.selectionChanged.connect(self._update_selection_buttons)
        self.canvas.toolRequested.connect(self.set_tool)

        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_options_bar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(0)
        work = QHBoxLayout()
        work.setContentsMargins(0, 0, 0, 0)
        work.setSpacing(0)
        work.addWidget(self._build_tool_rail())
        work.addWidget(self.canvas, 1)
        left.addLayout(work, 1)
        left.addWidget(self._build_frame_bar())
        body.addLayout(left, 1)
        body.addWidget(self._build_side_panel(face))
        root.addLayout(body, 1)
        root.addWidget(self._build_status_bar())

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("FaceToolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(2)

        def add(btn: QToolButton, slot=None) -> QToolButton:
            if slot is not None:
                btn.clicked.connect(slot)
            row.addWidget(btn)
            return btn

        def gap() -> None:
            row.addSpacing(6)
            row.addWidget(separator())
            row.addSpacing(6)

        self.undo_btn = add(tool_button("undo", "Undo", "Ctrl+Z"), self.canvas.undo)
        self.redo_btn = add(tool_button("redo", "Redo", "Ctrl+Y"), self.canvas.redo)
        gap()
        add(tool_button("clear", "Clear the frame, or the selection", "Ctrl+Del"), self.canvas.clear_all)
        add(tool_button("invert", "Invert pixels", "Ctrl+I"), self.canvas.invert)
        add(tool_button("flip-h", "Flip horizontally", "H"), self.canvas.flip_horizontal)
        add(tool_button("flip-v", "Flip vertically", "V"), self.canvas.flip_vertical)
        add(tool_button("", "Centre the drawing on the screen", icon=_center_icon()),
            self.canvas.center_content)
        gap()
        for icon_name, tip, key, dx, dy in (
            ("chevron-left", "Shift left 1 px (wraps around)", "Alt+Left", -1, 0),
            ("chevron-up", "Shift up 1 px (wraps around)", "Alt+Up", 0, -1),
            ("chevron-down", "Shift down 1 px (wraps around)", "Alt+Down", 0, 1),
            ("chevron-right", "Shift right 1 px (wraps around)", "Alt+Right", 1, 0),
        ):
            b = add(tool_button(icon_name, tip, key),
                    lambda _=False, dx=dx, dy=dy: self.canvas.shift(dx, dy))
            b.setAutoRepeat(True)
            b.setAutoRepeatDelay(350)
            b.setAutoRepeatInterval(60)
        gap()
        self.mirror_x_btn = tool_button("", "Mirror drawing left / right", "S", checkable=True,
                                        icon=_symmetry_icon(True))
        self.mirror_x_btn.toggled.connect(lambda on: self.canvas.set_mirror(mirror_x=on))
        row.addWidget(self.mirror_x_btn)
        self.mirror_y_btn = tool_button("", "Mirror drawing top / bottom", "Shift+S", checkable=True,
                                        icon=_symmetry_icon(False))
        self.mirror_y_btn.toggled.connect(lambda on: self.canvas.set_mirror(mirror_y=on))
        row.addWidget(self.mirror_y_btn)
        gap()
        self.grid_btn = tool_button("grid", "Show pixel grid", "#", checkable=True)
        self.grid_btn.setChecked(True)
        self.grid_btn.toggled.connect(self.canvas.set_grid_visible)
        row.addWidget(self.grid_btn)

        row.addStretch(1)

        add(tool_button("zoom-out", "Zoom out", "Ctrl+-"), self.canvas.zoom_out)
        self.zoom_label = mono_label("4x")
        self.zoom_label.setAlignment(Qt.AlignCenter)
        self.zoom_label.setFixedWidth(34)
        self.zoom_label.setToolTip("Zoom level  ·  Ctrl+wheel zooms, Space-drag or middle-drag pans")
        row.addWidget(self.zoom_label)
        add(tool_button("zoom-in", "Zoom in", "Ctrl++"), self.canvas.zoom_in)
        add(tool_button("fit", "Fit to window", "Ctrl+0"), self.canvas.zoom_fit)
        gap()
        add(tool_button("import", "Import an image...", "Ctrl+O"), self.import_image)
        add(tool_button("paste", "Paste from clipboard", "Ctrl+V"), self.paste_from_clipboard)
        add(tool_button("export", "Export this frame as PNG...", "Ctrl+E"), self.export_png)
        return bar

    def _build_options_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("FaceOptionsBar")
        bar.setFixedHeight(40)
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 0, 10, 0)
        row.setSpacing(8)

        self.tool_title = QLabel("Pencil")
        self.tool_title.setProperty("role", "caption")
        self.tool_title.setMinimumWidth(118)
        row.addWidget(self.tool_title)
        row.addWidget(separator())

        self.options_stack = QStackedWidget()
        row.addWidget(self.options_stack, 1)

        # -- brush page ---------------------------------------------------
        brush = QWidget()
        b_row = QHBoxLayout(brush)
        b_row.setContentsMargins(0, 0, 0, 0)
        b_row.setSpacing(2)
        b_row.addWidget(caption("SIZE"))
        b_row.addSpacing(6)
        self.brush_group = QButtonGroup(self)
        self.brush_group.setExclusive(True)
        self.brush_buttons: list[QToolButton] = []
        for n in range(1, fc.MAX_BRUSH + 1):
            b = tool_button("", f"Brush size {n} px", str(n), checkable=True, icon=_brush_icon(n))
            b.setChecked(n == 1)
            b.clicked.connect(lambda _=False, n=n: self.set_brush_size(n))
            self.brush_group.addButton(b, n)
            self.brush_buttons.append(b)
            b_row.addWidget(b)
        b_row.addStretch(1)
        self._page_brush = self.options_stack.addWidget(brush)

        # -- no-option page (fill, filled shapes) ---------------------------
        self._page_none = self.options_stack.addWidget(QWidget())

        # -- text page ----------------------------------------------------
        text = QWidget()
        t_row = QHBoxLayout(text)
        t_row.setContentsMargins(0, 0, 0, 0)
        t_row.setSpacing(6)
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("Type text, then click the canvas to stamp it")
        self.text_input.textChanged.connect(self._sync_text_options)
        self.font_combo = QComboBox()
        self.font_combo.setToolTip("Font family")
        self.font_combo.setMaxVisibleItems(18)
        # Fixed width + no per-item measuring: sizing a combo to its widest
        # entry would lay out every installed font name up front.
        self.font_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.font_combo.setMinimumContentsLength(14)
        self.font_combo.setFixedWidth(190)
        self._fonts_loaded = False   # the family list is filled on first use of the Text tool
        self.font_combo.currentTextChanged.connect(self._sync_text_options)
        self.font_size = QSpinBox()
        self.font_size.setRange(6, 64)
        self.font_size.setValue(16)
        self.font_size.setSuffix(" px")
        self.font_size.setToolTip("Text height in pixels")
        self.font_size.valueChanged.connect(self._sync_text_options)
        self.bold_btn = tool_button("", "Bold", checkable=True, text="B")
        bold_font = self.bold_btn.font()
        bold_font.setBold(True)
        self.bold_btn.setFont(bold_font)
        self.bold_btn.toggled.connect(self._sync_text_options)
        t_row.addWidget(self.text_input, 1)
        t_row.addWidget(self.font_combo)
        t_row.addWidget(self.font_size)
        t_row.addWidget(self.bold_btn)
        self._page_text = self.options_stack.addWidget(text)

        # -- select page --------------------------------------------------
        sel = QWidget()
        s_row = QHBoxLayout(sel)
        s_row.setContentsMargins(0, 0, 0, 0)
        s_row.setSpacing(2)
        self.copy_btn = tool_button("copy", "Copy selection", "Ctrl+C")
        self.copy_btn.clicked.connect(self.copy_selection)
        self.cut_btn = tool_button("cut", "Cut selection", "Ctrl+X")
        self.cut_btn.clicked.connect(self.cut_selection)
        self.paste_btn = tool_button("paste", "Paste", "Ctrl+V")
        self.paste_btn.clicked.connect(self.paste_from_clipboard)
        self.delete_sel_btn = tool_button("trash", "Delete selection", "Del")
        self.delete_sel_btn.clicked.connect(self.canvas.delete_selection)
        for b in (self.copy_btn, self.cut_btn, self.paste_btn, self.delete_sel_btn):
            s_row.addWidget(b)
        s_row.addSpacing(8)
        self.select_all_btn = chip_button("Select all", "Select the whole frame (Ctrl+A)", checkable=False)
        self.select_all_btn.clicked.connect(self.canvas.select_all)
        s_row.addWidget(self.select_all_btn)
        s_row.addSpacing(8)
        self.selection_label = mono_label("")
        s_row.addWidget(self.selection_label)
        s_row.addStretch(1)
        self._page_select = self.options_stack.addWidget(sel)
        return bar

    def _build_tool_rail(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("FaceToolRail")
        col = QVBoxLayout(rail)
        col.setContentsMargins(6, 8, 6, 8)
        col.setSpacing(2)
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_buttons: dict[str, QToolButton] = {}
        for tool, icon_name, label, key in TOOL_DEFS:
            btn = tool_button(icon_name, label, key, checkable=True)
            btn.clicked.connect(lambda _=False, t=tool: self.set_tool(t))
            self.tool_group.addButton(btn)
            self.tool_buttons[tool] = btn
            col.addWidget(btn)
            if tool in ("eraser", "ellipse_fill", "text"):
                col.addSpacing(3)
                col.addWidget(separator(vertical=False))
                col.addSpacing(3)
        col.addStretch(1)
        return rail

    def _build_frame_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("FaceFrameBar")
        col = QVBoxLayout(bar)
        col.setContentsMargins(10, 6, 10, 8)
        col.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(2)
        head.addWidget(caption("FRAMES"))
        head.addSpacing(6)
        self.frame_count_label = mono_label("1 / 6")
        head.addWidget(self.frame_count_label)
        head.addStretch(1)
        self.onion_check = tool_button(
            "onion", "Onion skin: ghost the previous frame under this one", "K", checkable=True)
        self.onion_check.setChecked(True)
        self.onion_check.toggled.connect(lambda _: self._refresh_onion())
        head.addWidget(self.onion_check)
        head.addSpacing(6)
        self.add_frame_btn = tool_button("plus", "New blank frame", "Ctrl+N")
        self.add_frame_btn.clicked.connect(self._add_frame)
        self.dup_frame_btn = tool_button("duplicate", "Duplicate this frame", "Ctrl+D")
        self.dup_frame_btn.clicked.connect(self._duplicate_frame)
        self.del_frame_btn = tool_button("trash", "Delete this frame")
        self.del_frame_btn.clicked.connect(self._delete_frame)
        for b in (self.add_frame_btn, self.dup_frame_btn, self.del_frame_btn):
            head.addWidget(b)
        col.addLayout(head)

        self.frame_list = FrameStrip()
        self.frame_list.currentChanged.connect(self._select_frame)
        self.frame_list.moveRequested.connect(self.move_frame)
        self.frame_list.addRequested.connect(self._add_frame)
        self.frame_list.contextRequested.connect(self._frame_context_menu)
        scroll = QScrollArea()
        scroll.setObjectName("FaceFrameScroll")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self.frame_list)
        scroll.setWidgetResizable(False)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setFixedHeight(FrameStrip.CELL_H + 2)
        self._frame_scroll = scroll
        col.addWidget(scroll)
        return bar

    def _build_side_panel(self, face: Face | None) -> QWidget:
        panel = QWidget()
        panel.setObjectName("FaceSidePanel")
        panel.setFixedWidth(296)
        col = QVBoxLayout(panel)
        col.setContentsMargins(12, 10, 12, 12)
        col.setSpacing(8)

        # -- animated preview ---------------------------------------------
        col.addWidget(caption("PREVIEW"))
        self.preview = OledPreview(scale=2)
        col.addWidget(self.preview, 0, Qt.AlignHCenter)

        play_row = QHBoxLayout()
        play_row.setSpacing(4)
        self.play_btn = tool_button("play", "Play / pause the animation", "P", checkable=True)
        self.play_btn.toggled.connect(self._set_playing)
        play_row.addWidget(self.play_btn)
        self.boomerang_btn = chip_button(
            "Boomerang",
            "Preview as a boomerang (1-2-3-2-1...) instead of a loop. Preview only: "
            "the play mode is chosen per timeline frame.")
        self.boomerang_btn.toggled.connect(lambda _: self._restart_playback())
        play_row.addWidget(self.boomerang_btn)
        play_row.addStretch(1)
        fps_lbl = QLabel("FPS")
        fps_lbl.setProperty("role", "caption")
        play_row.addWidget(fps_lbl)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 30)
        self.fps_spin.setValue(max(1, min(30, face.fps)) if face else 4)
        self.fps_spin.setToolTip("Animation speed in frames per second (saved with the face)")
        self.fps_spin.setFixedWidth(64)
        self.fps_spin.valueChanged.connect(self._on_fps_changed)
        play_row.addWidget(self.fps_spin)
        col.addLayout(play_row)

        # -- tabs: properties / byte array --------------------------------
        self.side_tabs = QTabWidget()
        self.side_tabs.setDocumentMode(True)

        props = QWidget()
        grid = QGridLayout(props)
        grid.setContentsMargins(2, 12, 2, 4)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)

        name_lbl = QLabel("Name")
        name_lbl.setProperty("role", "subheading")
        self.name_edit = QLineEdit(face.name if face else self._default_new_name())
        self.name_edit.setPlaceholderText("e.g. sleepy_wink")
        self.name_edit.setMaxLength(48)
        self.name_edit.textChanged.connect(self._on_meta_changed)
        grid.addWidget(name_lbl, 0, 0)
        grid.addWidget(self.name_edit, 0, 1)

        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setProperty("role", "warning")
        self.warning_label.setVisible(False)
        grid.addWidget(self.warning_label, 1, 1)

        cat_lbl = QLabel("Category")
        cat_lbl.setProperty("role", "subheading")
        self.category_combo = QComboBox()
        for cat in FACE_CATEGORIES:
            self.category_combo.addItem(FACE_CATEGORY_LABELS[cat], cat)
        ci = self.category_combo.findData(face.category if face else FACE_CATEGORY_CUSTOM)
        self.category_combo.setCurrentIndex(ci if ci >= 0 else self.category_combo.findData(FACE_CATEGORY_CUSTOM))
        self.category_combo.currentIndexChanged.connect(self._on_meta_changed)
        grid.addWidget(cat_lbl, 2, 0)
        grid.addWidget(self.category_combo, 2, 1)

        self.override_check = QCheckBox("Override existing firmware face")
        self.override_check.setToolTip(
            "Tick this when the face deliberately replaces a built-in firmware face of the same name.")
        self.override_check.setChecked(face.is_override if face else False)
        self.override_check.toggled.connect(self._on_meta_changed)
        if self.is_header_mode:
            self.override_check.setVisible(False)
        grid.addWidget(self.override_check, 3, 0, 1, 2)
        grid.setRowStretch(4, 1)
        self.side_tabs.addTab(props, "Properties")

        code = QWidget()
        c_col = QVBoxLayout(code)
        c_col.setContentsMargins(2, 10, 2, 4)
        c_col.setSpacing(6)
        c_head = QHBoxLayout()
        self.code_info = QLabel("")
        self.code_info.setProperty("role", "hint")
        c_head.addWidget(self.code_info, 1)
        self.copy_code_btn = QPushButton("Copy")
        self.copy_code_btn.setProperty("variant", "flat")
        self.copy_code_btn.setAutoDefault(False)
        self.copy_code_btn.setFocusPolicy(Qt.NoFocus)
        self.copy_code_btn.setToolTip("Copy the C byte array(s) to the clipboard")
        ic = get_icon("copy")
        if ic is not None:
            self.copy_code_btn.setIcon(ic)
        self.copy_code_btn.clicked.connect(self._copy_code)
        c_head.addWidget(self.copy_code_btn)
        c_col.addLayout(c_head)
        self.output_view = QPlainTextEdit()
        self.output_view.setReadOnly(True)
        self.output_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.output_view.setFont(mono_font(11))
        c_col.addWidget(self.output_view, 1)
        self._code_tab = self.side_tabs.addTab(code, "C Array")
        self.side_tabs.setTabToolTip(self._code_tab, "Arduino byte array for face-bitmaps.h (generated on demand)")
        self.side_tabs.currentChanged.connect(self._on_tab_changed)

        col.addWidget(self.side_tabs, 1)
        return panel

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("FaceStatusBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(10)
        self.pos_label = mono_label("")
        self.pos_label.setFixedWidth(92)
        self.pos_label.setToolTip("Pixel under the cursor")
        row.addWidget(self.pos_label)
        row.addWidget(separator())
        self.hint_label = QLabel("")
        self.hint_label.setProperty("role", "hint")
        self.hint_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.hint_label, 1)
        self.dirty_label = QLabel("")
        self.dirty_label.setProperty("role", "hint")
        row.addWidget(self.dirty_label)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setProperty("variant", "flat")
        self.cancel_btn.setAutoDefault(False)
        self.cancel_btn.setFocusPolicy(Qt.NoFocus)
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = QPushButton("Save Face")
        self.save_btn.setProperty("variant", "primary")
        self.save_btn.setAutoDefault(False)
        self.save_btn.setFocusPolicy(Qt.NoFocus)
        self.save_btn.setToolTip("Save and close (Ctrl+S)")
        self.save_btn.clicked.connect(self._on_save)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.save_btn)
        return bar

    def _build_shortcuts(self) -> None:
        def sc(keys, slot) -> None:
            for key in ([keys] if isinstance(keys, str) else keys):
                s = QShortcut(QKeySequence(key), self)
                s.setContext(Qt.WindowShortcut)
                s.activated.connect(slot)

        for tool, _icon, _label, key in TOOL_DEFS:
            sc(key, lambda t=tool: self.set_tool(t))
        for n in range(1, fc.MAX_BRUSH + 1):
            sc(str(n), lambda n=n: self.set_brush_size(n))
        sc("[", lambda: self.set_brush_size(self.canvas.brush_size - 1))
        sc("]", lambda: self.set_brush_size(self.canvas.brush_size + 1))
        sc("Ctrl+Z", self.canvas.undo)
        sc(["Ctrl+Y", "Ctrl+Shift+Z"], self.canvas.redo)
        sc("Ctrl+C", self.copy_selection)
        sc("Ctrl+X", self.cut_selection)
        sc("Ctrl+V", self.paste_from_clipboard)
        sc("Ctrl+A", lambda: (self.set_tool("select"), self.canvas.select_all()))
        sc("Ctrl+I", self.canvas.invert)
        sc("Ctrl+Del", self.canvas.clear_all)
        sc("H", self.canvas.flip_horizontal)
        sc("V", self.canvas.flip_vertical)
        sc("S", self.mirror_x_btn.toggle)
        sc("Shift+S", self.mirror_y_btn.toggle)
        sc("#", self.grid_btn.toggle)
        sc("K", self.onion_check.toggle)
        sc("P", self.play_btn.toggle)
        sc(["Ctrl+=", "Ctrl++"], self.canvas.zoom_in)
        sc("Ctrl+-", self.canvas.zoom_out)
        sc("Ctrl+0", self.canvas.zoom_fit)
        sc("Ctrl+O", self.import_image)
        sc("Ctrl+E", self.export_png)
        sc("Ctrl+S", self._on_save)
        sc("Ctrl+N", self._add_frame)
        sc("Ctrl+D", self._duplicate_frame)
        sc(",", lambda: self._select_frame(self.frame_index - 1))
        sc(".", lambda: self._select_frame(self.frame_index + 1))

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    def set_tool(self, tool: str) -> None:
        if tool not in self.tool_buttons:
            return
        btn = self.tool_buttons[tool]
        if not btn.isChecked():
            btn.setChecked(True)
        self.canvas.set_tool(tool)
        label = next(d[2] for d in TOOL_DEFS if d[0] == tool)
        self.tool_title.setText(label.upper())
        if tool == "text":
            self._ensure_fonts()
            self.options_stack.setCurrentIndex(self._page_text)
            self._sync_text_options()
            if not self._loading and not self.text_input.text():
                self.text_input.setFocus()
        elif tool == "select":
            self.options_stack.setCurrentIndex(self._page_select)
        elif tool in ("pencil", "eraser", "line", "rect", "ellipse"):
            self.options_stack.setCurrentIndex(self._page_brush)
        else:
            self.options_stack.setCurrentIndex(self._page_none)
        self.hint_label.setText(TOOL_HINTS.get(tool, ""))
        self.hint_label.setToolTip(TOOL_HINTS.get(tool, ""))
        self._update_selection_buttons()

    _set_tool = set_tool  # legacy name

    def current_tool(self) -> str:
        return self.canvas.tool

    def set_brush_size(self, size: int) -> None:
        size = max(1, min(fc.MAX_BRUSH, int(size)))
        self.canvas.set_brush_size(size)
        self.brush_buttons[size - 1].setChecked(True)

    def _ensure_fonts(self) -> None:
        """Fill the font list the first time the Text tool is used (listing
        every installed family is the slowest part of opening the editor).
        Comic Sans is filtered out by face_render.text_font_families()."""
        if self._fonts_loaded:
            return
        self._fonts_loaded = True
        families = fr.text_font_families()
        self.font_combo.blockSignals(True)
        self.font_combo.addItems(families)
        default_family = fr.default_text_font(families)
        if default_family:
            self.font_combo.setCurrentText(default_family)
        self.font_combo.blockSignals(False)

    def _sync_text_options(self, *_args) -> None:
        if not self._fonts_loaded:
            return
        self.canvas.set_text_options(
            self.text_input.text(), self.font_combo.currentText(),
            self.font_size.value(), self.bold_btn.isChecked(),
        )

    # ------------------------------------------------------------------
    # Canvas plumbing
    # ------------------------------------------------------------------
    def _on_canvas_changed(self) -> None:
        self.frames[self.frame_index] = self.canvas.pixels
        self._mark_dirty()
        self._schedule_live()

    def _on_canvas_committed(self) -> None:
        self.frames[self.frame_index] = self.canvas.pixels
        self._mark_dirty()
        self._schedule_live()

    def _on_hover(self, x: int, y: int) -> None:
        self.pos_label.setText(f"x {x:>3}  y {y:>2}" if x >= 0 else "")

    def _on_zoom_changed(self, zoom: int) -> None:
        self.zoom_label.setText(f"{zoom}x")

    def _update_history_buttons(self) -> None:
        self.undo_btn.setEnabled(self.canvas.can_undo())
        self.redo_btn.setEnabled(self.canvas.can_redo())

    def _update_selection_buttons(self) -> None:
        has = self.canvas.has_selection()
        for b in (self.cut_btn, self.delete_sel_btn):
            b.setEnabled(has)
        sel = self.canvas.selection
        self.selection_label.setText(
            f"{sel.width()} x {sel.height()} at {sel.left()},{sel.top()}" if sel is not None else "")

    def _schedule_live(self) -> None:
        if self._live_timer.isActive():
            self._live_pending = True
            return
        self._flush_live()
        self._live_timer.start()

    def _on_live_timer(self) -> None:
        if self._live_pending:
            self._live_pending = False
            self._flush_live()
            self._live_timer.start()

    def _flush_live(self) -> None:
        """Push the current pixels to everything that mirrors them."""
        arr = self.frames[self.frame_index]
        self.frame_list.update_frame(self.frame_index, array_to_frame(arr))
        if not self._playing:
            self.preview.set_array(arr)
            self.previewPixels.emit(arr.copy())
        self._code_stale = True
        if self._code_visible():
            self._code_timer.start()

    def visible_pixels(self) -> np.ndarray:
        """The frame currently shown by the preview (playing or not)."""
        if self._playing and self.frames:
            seq = self._play_sequence()
            return self.frames[seq[self._play_pos % len(seq)]].copy()
        return self.frames[self.frame_index].copy()

    # ------------------------------------------------------------------
    # Frames
    # ------------------------------------------------------------------
    def _packed_frames(self) -> list[bytearray]:
        return [array_to_frame(f) for f in self.frames]

    def _refresh_frame_list(self) -> None:
        self.frame_list.set_frames(self._packed_frames(), self.frame_index)
        n = len(self.frames)
        full = n >= FACE_MAX_FRAMES
        self.frame_count_label.setText(f"{n} / {FACE_MAX_FRAMES}")
        self.frame_count_label.setStyleSheet(f"color: {WARNING};" if full else "")
        self.frame_count_label.setToolTip(
            f"A face can have at most {FACE_MAX_FRAMES} frames." if full
            else f"{n} of {FACE_MAX_FRAMES} frames used")
        limit_tip = f"Maximum of {FACE_MAX_FRAMES} frames reached"
        self.add_frame_btn.setEnabled(not full)
        set_button_tooltip(self.add_frame_btn, *((limit_tip,) if full else ("New blank frame", "Ctrl+N")))
        self.dup_frame_btn.setEnabled(not full)
        set_button_tooltip(self.dup_frame_btn, *((limit_tip,) if full else ("Duplicate this frame", "Ctrl+D")))
        self.del_frame_btn.setEnabled(n > 1)
        self.play_btn.setEnabled(n > 1)
        set_button_tooltip(self.play_btn, *(("Play / pause the animation", "P") if n > 1
                                            else ("Add a second frame to preview an animation",)))
        if n <= 1 and self._playing:
            self.play_btn.setChecked(False)

    def _select_frame(self, row: int) -> None:
        if row < 0 or row >= len(self.frames):
            return
        self.canvas.finish_interaction()
        self.frame_index = row
        self.frame_list.set_current(row)
        self.canvas.set_frame(self.frames[row], self._histories[row], self._onion_for(row))
        self._frame_scroll.ensureVisible(self.frame_list.cell_center(row).x(), 0, 60, 0)
        if not self._playing:
            self.preview.set_array(self.frames[row])
            if not self._loading:
                self.previewPixels.emit(self.frames[row].copy())

    def _onion_for(self, row: int) -> np.ndarray | None:
        if self.onion_check.isChecked() and row > 0:
            return self.frames[row - 1]
        return None

    def _refresh_onion(self) -> None:
        self.canvas.set_onion(self._onion_for(self.frame_index))

    def _insert_frame(self, index: int, arr: np.ndarray) -> bool:
        if len(self.frames) >= FACE_MAX_FRAMES:
            self._flash_hint(f"A face can have at most {FACE_MAX_FRAMES} frames.")
            return False
        self.canvas.finish_interaction()
        self.frames.insert(index, arr)
        self._histories.insert(index, fc.History())
        self._after_frames_changed(index)
        return True

    def _add_frame(self) -> None:
        """New blank frame after the current one."""
        self._insert_frame(self.frame_index + 1, fc.blank())

    def _duplicate_frame(self) -> None:
        self._insert_frame(self.frame_index + 1, self.frames[self.frame_index].copy())

    def _delete_frame(self) -> None:
        if len(self.frames) <= 1:
            return
        self.canvas.finish_interaction()
        i = self.frame_index
        del self.frames[i]
        del self._histories[i]
        self._after_frames_changed(max(0, min(i, len(self.frames) - 1)))

    def move_frame(self, src: int, dst: int) -> None:
        """Reorder: the frame at ``src`` ends up at index ``dst``."""
        n = len(self.frames)
        if not (0 <= src < n and 0 <= dst < n) or src == dst:
            return
        self.canvas.finish_interaction()
        current = self.frames[self.frame_index]
        frame = self.frames.pop(src)
        hist = self._histories.pop(src)
        self.frames.insert(dst, frame)
        self._histories.insert(dst, hist)
        new_index = next(i for i, f in enumerate(self.frames) if f is current)
        self._after_frames_changed(new_index)

    def _after_frames_changed(self, select: int) -> None:
        self.frame_index = select
        self._refresh_frame_list()
        self._select_frame(select)
        self._mark_dirty()
        self._code_stale = True
        if self._code_visible():
            self._code_timer.start()
        if self._playing:
            self._restart_playback()

    def _frame_context_menu(self, index: int, global_pos: QPoint) -> None:
        menu = QMenu(self)
        full = len(self.frames) >= FACE_MAX_FRAMES
        a_dup = menu.addAction("Duplicate")
        a_dup.setEnabled(not full)
        a_new = menu.addAction("Insert blank frame after")
        a_new.setEnabled(not full)
        menu.addSeparator()
        a_left = menu.addAction("Move left")
        a_left.setEnabled(index > 0)
        a_right = menu.addAction("Move right")
        a_right.setEnabled(index < len(self.frames) - 1)
        menu.addSeparator()
        a_del = menu.addAction("Delete")
        a_del.setEnabled(len(self.frames) > 1)
        chosen = menu.exec(global_pos)
        if chosen is a_dup:
            self._duplicate_frame()
        elif chosen is a_new:
            self._add_frame()
        elif chosen is a_left:
            self.move_frame(index, index - 1)
        elif chosen is a_right:
            self.move_frame(index, index + 1)
        elif chosen is a_del:
            self._delete_frame()

    # ------------------------------------------------------------------
    # Animated preview
    # ------------------------------------------------------------------
    def _play_sequence(self) -> list[int]:
        n = len(self.frames)
        seq = list(range(n))
        if self.boomerang_btn.isChecked() and n > 2:
            seq += list(range(n - 2, 0, -1))
        return seq

    def _set_playing(self, playing: bool) -> None:
        playing = bool(playing) and len(self.frames) > 1
        self._playing = playing
        ic = get_icon("pause" if playing else "play")
        if ic is not None:
            self.play_btn.setIcon(ic)
        if playing:
            self._play_pos = 0
            self._play_timer.start(max(1, round(1000 / max(1, self.fps_spin.value()))))
            self._show_play_frame()
        else:
            self._play_timer.stop()
            if self.play_btn.isChecked():
                self.play_btn.setChecked(False)
            arr = self.frames[self.frame_index]
            self.preview.set_array(arr)
            self.previewPixels.emit(arr.copy())

    def _restart_playback(self) -> None:
        if self._playing:
            self._play_pos = 0
            self._play_timer.start(max(1, round(1000 / max(1, self.fps_spin.value()))))

    def _advance_playback(self) -> None:
        self._play_pos += 1
        self._show_play_frame()

    def _show_play_frame(self) -> None:
        seq = self._play_sequence()
        if not seq:
            return
        self._play_pos %= len(seq)
        arr = self.frames[seq[self._play_pos]]
        self.preview.set_array(arr)
        self.previewPixels.emit(arr.copy())

    def _on_fps_changed(self, _value: int) -> None:
        self._on_meta_changed()
        if self._playing:
            self._play_timer.setInterval(max(1, round(1000 / max(1, self.fps_spin.value()))))

    # ------------------------------------------------------------------
    # Clipboard / import / export
    # ------------------------------------------------------------------
    def copy_selection(self) -> None:
        self.canvas.copy_selection()
        self._flash_hint("Copied selection" if self.canvas.has_selection() else "Copied frame")

    def cut_selection(self) -> None:
        if self.canvas.cut_selection():
            self._flash_hint("Cut selection")

    def paste_from_clipboard(self) -> None:
        """Paste a region copied in the editor, or any image on the system
        clipboard (large images go through the import dialog)."""
        mime = QGuiApplication.clipboard().mimeData()
        arr = None
        if mime is not None and mime.hasFormat(CLIP_MIME):
            raw = bytes(mime.data(CLIP_MIME))
            if len(raw) >= 2:
                w, h = raw[0], raw[1]
                bits = np.unpackbits(np.frombuffer(raw[2:], dtype=np.uint8))
                if w and h and bits.size >= w * h:
                    arr = bits[:w * h].reshape(h, w).astype(bool)
        elif mime is not None and mime.hasImage():
            img = QGuiApplication.clipboard().image()
            if not img.isNull():
                if img.width() <= FACE_WIDTH and img.height() <= FACE_HEIGHT:
                    arr = image_to_array(img)[:img.height(), :img.width()]
                else:
                    self.import_qimage(img, title="Paste Image")
                    return
        if arr is None:
            arr = clipboard_array()
        if arr is None:
            self._flash_hint("Nothing to paste: copy a selection or an image first")
            return
        self.canvas.paste_array(arr)
        self._flash_hint("Pasted: drag to position, Enter or Esc to drop")

    def import_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Image", "", IMAGE_FILTER)
        if path:
            self._import_path(path)

    def _import_path(self, path: str) -> None:
        img = QImage(path)
        if img.isNull():
            self._flash_hint(f"Could not read {os.path.basename(path)}", error=True)
            return
        self.import_qimage(img, title=f"Import {os.path.basename(path)}")

    def import_qimage(self, image: QImage, title: str = "Import Image") -> bool:
        dlg = ImageImportDialog(image, self, title)
        if dlg.exec() and dlg.result_array is not None:
            self.canvas.set_pixels(dlg.result_array)
            self._flash_hint("Image imported (Ctrl+Z to undo)")
            return True
        return False

    def export_png(self) -> None:
        name = self.name_edit.text().strip() or "face"
        suffix = f"_{self.frame_index}" if len(self.frames) > 1 else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Frame as PNG", f"{name}{suffix}.png", "PNG Image (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        if fr.array_to_image(self.frames[self.frame_index]).save(path, "PNG"):
            self._flash_hint(f"Exported {os.path.basename(path)} (128 x 64)")
        else:
            self._flash_hint("Could not write the PNG file", error=True)

    def dragEnterEvent(self, event) -> None:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if any(u.toLocalFile().lower().endswith(IMAGE_SUFFIXES) for u in urls):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for u in event.mimeData().urls():
            path = u.toLocalFile()
            if path.lower().endswith(IMAGE_SUFFIXES):
                event.acceptProposedAction()
                # Let the drop finish before opening a modal dialog.
                QTimer.singleShot(0, lambda p=path: self._import_path(p))
                return

    # ------------------------------------------------------------------
    # Byte-array view
    # ------------------------------------------------------------------
    def _code_visible(self) -> bool:
        return self.side_tabs.currentIndex() == self._code_tab and self.output_view.isVisible()

    def _on_tab_changed(self, index: int) -> None:
        if index == self._code_tab and self._code_stale:
            self._refresh_output()

    def _export_text(self) -> str:
        name = self.name_edit.text().strip() or "unnamed"
        stub = Face(name=name, frames=self._packed_frames(), fps=self.fps_spin.value())
        try:
            return export_face(stub)
        except Exception as exc:  # pragma: no cover - defensive
            return f"// error generating preview: {exc}"

    def _refresh_output(self) -> None:
        """Regenerate the C byte-array text now (normally debounced and only
        while the tab is visible)."""
        self._code_timer.stop()
        bar = self.output_view.verticalScrollBar()
        pos = bar.value()
        self.output_view.setPlainText(self._export_text())
        bar.setValue(min(pos, bar.maximum()))
        n = len(self.frames)
        self.code_info.setText(f"{n} frame{'s' if n != 1 else ''}  ·  {n * 1024} bytes")
        self._code_stale = False

    def _copy_code(self) -> None:
        self._refresh_output()
        QGuiApplication.clipboard().setText(self.output_view.toPlainText())
        self._flash_hint("Byte array copied to the clipboard")

    # ------------------------------------------------------------------
    # Name / validation / dirty state
    # ------------------------------------------------------------------
    def _default_new_name(self) -> str:
        i = 1
        while True:
            name = f"face_{i}"
            if name not in self.existing_names and name not in RESERVED_FACE_NAMES:
                return name
            i += 1

    def set_context(self, existing_names, is_header_mode: bool | None = None) -> None:
        """Update what the name is validated against while the editor is
        open (the library changes underneath a modeless editor)."""
        self.existing_names = set(existing_names)
        if is_header_mode is not None and bool(is_header_mode) != self.is_header_mode:
            self.is_header_mode = bool(is_header_mode)
            self.override_check.setVisible(not self.is_header_mode)
        self._refresh_warning()

    def _validate(self) -> tuple[str | None, bool]:
        """-> (message, blocking). Blocking problems prevent saving;
        non-blocking ones are advisory (same rules as before)."""
        name = self.name_edit.text().strip()
        if self.is_header_mode:
            if not name:
                return "Enter a face name.", True
            if not name.isidentifier():
                return ("Name must be a valid C identifier (letters, digits, underscores, "
                        "starting with letter/underscore)."), True
            if name in self.existing_names:
                return f"A face named '{name}' already exists in this file.", True
            return None, False
        if not name:
            return "Enter a face name.", True
        if name in self.existing_names:
            return f"A face named '{name}' already exists in this project.", True
        stub = Face(name=name, is_override=self.override_check.isChecked())
        warning = check_face_name_collision(stub)
        if warning:
            return warning, False
        if not name.isidentifier():
            return ("Heads-up: this name is not a valid C identifier, so the exported "
                    "bitmap will not compile until it is renamed."), False
        return None, False

    def _refresh_warning(self) -> None:
        message, blocking = self._validate()
        self.warning_label.setText(message or "")
        self.warning_label.setVisible(bool(message))
        self.warning_label.setStyleSheet(f"color: {DANGER if blocking else WARNING};" if message else "")
        self.name_edit.setStyleSheet(f"border: 1px solid {DANGER};" if blocking else "")
        self.save_btn.setEnabled(not blocking)
        self.save_btn.setToolTip(message if blocking and message else "Save and close (Ctrl+S)")

    def _on_meta_changed(self, *_args) -> None:
        self._refresh_warning()
        self._mark_dirty()
        self._code_stale = True
        if self._code_visible():
            self._code_timer.start()

    def _mark_dirty(self) -> None:
        if self._loading or self._dirty:
            return
        self._dirty = True
        self._update_dirty_indicator()

    def is_dirty(self) -> bool:
        return self._dirty

    def _update_dirty_indicator(self) -> None:
        self.dirty_label.setText("● Unsaved changes" if self._dirty else "")
        self.dirty_label.setStyleSheet(f"color: {ACCENT};" if self._dirty else "")

    def _flash_hint(self, text: str, error: bool = False, ms: int = 2600) -> None:
        """Short status message in place of the tool hint."""
        self.hint_label.setText(text)
        self.hint_label.setStyleSheet(f"color: {DANGER};" if error else f"color: {ACCENT};")
        token = object()
        self._hint_token = token

        def restore() -> None:
            if getattr(self, "_hint_token", None) is token:
                self.hint_label.setStyleSheet("")
                self.hint_label.setText(TOOL_HINTS.get(self.canvas.tool, ""))
        QTimer.singleShot(ms, restore)

    # ------------------------------------------------------------------
    # Save / close
    # ------------------------------------------------------------------
    def build_face(self) -> Face:
        """The face as currently edited (does not validate or close)."""
        return Face(
            name=self.name_edit.text().strip(),
            category=self.category_combo.currentData(),
            fps=self.fps_spin.value(),
            frames=self._packed_frames(),
            is_override=True if self.is_header_mode else self.override_check.isChecked(),
        )

    def _on_save(self) -> None:
        self.canvas.finish_interaction()
        message, blocking = self._validate()
        if blocking:
            self._refresh_warning()
            self.side_tabs.setCurrentIndex(0)
            self.name_edit.setFocus()
            self.name_edit.selectAll()
            self._flash_hint(message or "Fix the face name before saving", error=True)
            return
        self.result_face = self.build_face()
        self._dirty = False
        self.faceSaved.emit(self.result_face)
        self.accept()

    def _confirm_discard(self) -> bool:
        """True when it is OK to close without saving."""
        if not self._dirty or not self.isVisible():
            return True
        name = self.name_edit.text().strip() or "this face"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"Save your changes to '{name}'?")
        box.setInformativeText("Closing the editor now would lose what you painted.")
        save = box.addButton("Save", QMessageBox.AcceptRole)
        discard = box.addButton("Discard", QMessageBox.DestructiveRole)
        box.addButton("Keep Editing", QMessageBox.RejectRole)
        box.setDefaultButton(save)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save:
            self._on_save()   # closes via accept() when the name is valid
            return False
        return clicked is discard

    def request_close(self) -> bool:
        """Ask to close (with the unsaved-changes prompt). Returns True if
        the editor is closed afterwards."""
        if not self.isVisible():
            return True
        self.reject()
        return not self.isVisible()

    def force_close(self) -> None:
        """Close immediately, discarding changes (the document went away)."""
        self._dirty = False
        super().reject()

    def reject(self) -> None:
        if self._confirm_discard():
            self._dirty = False
            super().reject()

    def done(self, result: int) -> None:
        self._play_timer.stop()
        self._live_timer.stop()
        self._code_timer.stop()
        super().done(result)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.previewPixels.emit(self.frames[self.frame_index].copy())

    def keyPressEvent(self, event) -> None:
        if self.canvas.handle_key(event):
            event.accept()
            return
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            return  # never let Enter press a button behind the user's back
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self.canvas.set_space_pan(False)
        super().keyReleaseEvent(event)
