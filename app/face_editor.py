"""Bitmap face painter: a 128x64 1-bpp canvas ("MS Paint for faces") with
draw + type tools and a live, always-current Arduino byte-array preview in
the exact format firmware/face-bitmaps.h expects."""

from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, QRect, Signal
from PySide6.QtGui import (
    QImage, QPainter, QPen, QColor, QFont, QFontDatabase, QIcon, QPixmap,
)
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QToolButton, QButtonGroup,
    QLabel, QSpinBox, QLineEdit, QComboBox, QCheckBox, QPlainTextEdit,
    QPushButton, QListWidget, QListWidgetItem, QSplitter, QInputDialog,
    QMessageBox, QGroupBox, QFormLayout, QSizePolicy,
)

from .project import Face, blank_frame, pixels_to_frame
from .servo_model import (
    FACE_WIDTH, FACE_HEIGHT, FACE_MAX_FRAMES, FACE_CATEGORIES,
    FACE_CATEGORY_LABELS, FACE_CATEGORY_CUSTOM,
)
from .code_export import export_face, check_face_name_collision

ZOOM = 8
TOOLS = ["pencil", "eraser", "line", "rect", "rect_fill", "ellipse",
          "ellipse_fill", "fill", "text"]


def image_to_pixels(image: QImage) -> list[list[bool]]:
    pixels = [[False] * FACE_WIDTH for _ in range(FACE_HEIGHT)]
    for y in range(FACE_HEIGHT):
        for x in range(FACE_WIDTH):
            pixels[y][x] = QColor(image.pixel(x, y)).lightness() > 127
    return pixels


def blank_image() -> QImage:
    img = QImage(FACE_WIDTH, FACE_HEIGHT, QImage.Format_RGB32)
    img.fill(QColor("black"))
    return img


def frame_bytes_to_image(frame: bytes) -> QImage:
    img = blank_image()
    stride = FACE_WIDTH // 8
    for y in range(FACE_HEIGHT):
        for bx in range(stride):
            byte = frame[y * stride + bx]
            for bit in range(8):
                if byte & (0x80 >> bit):
                    img.setPixel(bx * 8 + bit, y, 0xFFFFFFFF)
    return img


class FaceCanvas(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = blank_image()
        self.onion_image: QImage | None = None
        self.tool = "pencil"
        self.setFixedSize(FACE_WIDTH * ZOOM, FACE_HEIGHT * ZOOM)
        self._drawing = False
        self._last_pt: QPoint | None = None
        self._drag_start: QPoint | None = None
        self._preview_image: QImage | None = None
        self._undo_stack: list[QImage] = []
        self._redo_stack: list[QImage] = []

    # -- helpers -----------------------------------------------------
    def _to_pixel(self, pos) -> QPoint:
        return QPoint(max(0, min(FACE_WIDTH - 1, pos.x() // ZOOM)),
                       max(0, min(FACE_HEIGHT - 1, pos.y() // ZOOM)))

    def _push_undo(self) -> None:
        self._undo_stack.append(self.image.copy())
        if len(self._undo_stack) > 60:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def undo(self) -> None:
        if not self._undo_stack:
            return
        self._redo_stack.append(self.image.copy())
        self.image = self._undo_stack.pop()
        self.update(); self.changed.emit()

    def redo(self) -> None:
        if not self._redo_stack:
            return
        self._undo_stack.append(self.image.copy())
        self.image = self._redo_stack.pop()
        self.update(); self.changed.emit()

    def set_image(self, image: QImage) -> None:
        self._push_undo()
        self.image = image.copy()
        self.update(); self.changed.emit()

    def clear(self) -> None:
        self._push_undo()
        self.image.fill(QColor("black"))
        self.update(); self.changed.emit()

    def invert(self) -> None:
        self._push_undo()
        self.image.invertPixels()
        self.update(); self.changed.emit()

    def mirror(self, horizontal: bool) -> None:
        self._push_undo()
        self.image = self.image.mirrored(horizontal, not horizontal)
        self.update(); self.changed.emit()

    def shift(self, dx: int, dy: int) -> None:
        self._push_undo()
        shifted = blank_image()
        p = QPainter(shifted)
        p.drawImage(dx, dy, self.image)
        p.end()
        self.image = shifted
        self.update(); self.changed.emit()

    def stamp_text(self, text: str, font_family: str, size: int, pos: QPoint) -> None:
        self._push_undo()
        p = QPainter(self.image)
        p.setPen(QColor("white"))
        p.setFont(QFont(font_family, size))
        p.drawText(pos, text)
        p.end()
        self.update(); self.changed.emit()

    def _flood_fill(self, x: int, y: int, on: bool) -> None:
        pixels = image_to_pixels(self.image)
        target = pixels[y][x]
        if target == on:
            return
        stack = [(x, y)]
        seen = set()
        while stack:
            cx, cy = stack.pop()
            if (cx, cy) in seen or not (0 <= cx < FACE_WIDTH and 0 <= cy < FACE_HEIGHT):
                continue
            if pixels[cy][cx] != target:
                continue
            seen.add((cx, cy))
            pixels[cy][cx] = on
            stack.extend([(cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)])
        for (px, py) in seen:
            self.image.setPixel(px, py, 0xFFFFFFFF if on else 0xFF000000)

    # -- mouse -----------------------------------------------------
    def mousePressEvent(self, event) -> None:
        pt = self._to_pixel(event.position().toPoint())
        on = event.button() == Qt.LeftButton
        if not on and event.button() != Qt.RightButton:
            return
        if self.tool == "text":
            return
        self._push_undo()
        self._drawing = True
        self._last_pt = pt
        self._drag_start = pt
        self._preview_image = self.image.copy()
        self._paint_value = 0xFFFFFFFF if on else 0xFF000000
        if self.tool in ("pencil", "eraser"):
            val = 0xFFFFFFFF if self.tool == "pencil" and on else 0xFF000000
            self.image.setPixel(pt.x(), pt.y(), val)
            self.update(); self.changed.emit()
        elif self.tool == "fill":
            self._flood_fill(pt.x(), pt.y(), self.tool == "fill" and on)
            self.update(); self.changed.emit()

    def mouseMoveEvent(self, event) -> None:
        if not self._drawing:
            return
        pt = self._to_pixel(event.position().toPoint())
        if self.tool in ("pencil", "eraser"):
            painter = QPainter(self.image)
            painter.setPen(QPen(QColor("white" if self._paint_value == 0xFFFFFFFF else "black"), 1))
            painter.drawLine(self._last_pt, pt)
            painter.end()
            self._last_pt = pt
            self.update(); self.changed.emit()
        elif self.tool in ("line", "rect", "rect_fill", "ellipse", "ellipse_fill"):
            self.image = self._preview_image.copy()
            self._draw_shape(self._drag_start, pt)
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self.tool in ("line", "rect", "rect_fill", "ellipse", "ellipse_fill") and self._drawing:
            self.changed.emit()
        self._drawing = False
        self._last_pt = None

    def _draw_shape(self, start: QPoint, end: QPoint) -> None:
        painter = QPainter(self.image)
        pen = QPen(QColor("white" if self._paint_value == 0xFFFFFFFF else "black"), 1)
        painter.setPen(pen)
        rect = QRect(start, end).normalized()
        if self.tool == "line":
            painter.drawLine(start, end)
        elif self.tool == "rect":
            painter.drawRect(rect)
        elif self.tool == "rect_fill":
            painter.fillRect(rect, pen.color())
        elif self.tool == "ellipse":
            painter.drawEllipse(rect)
        elif self.tool == "ellipse_fill":
            painter.setBrush(pen.color())
            painter.drawEllipse(rect)
        painter.end()

    def mouseDoubleClickEvent(self, event) -> None:
        pass

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        has_onion = self.onion_image is not None
        if has_onion:
            painter.setOpacity(0.25)
            painter.drawImage(QRect(0, 0, self.width(), self.height()), self.onion_image)
            painter.setOpacity(1.0)
            # self.image is a fully opaque black/white bitmap (no alpha), so
            # drawing it with the default SourceOver mode would paint solid
            # black over the whole canvas and completely hide the dimmed
            # onion layer just drawn underneath it. Lighten takes the max of
            # each channel instead: black pixels let the onion show through,
            # while actual white strokes still render at full brightness.
            painter.setCompositionMode(QPainter.CompositionMode_Lighten)
        painter.drawImage(QRect(0, 0, self.width(), self.height()), self.image)
        if has_onion:
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        if ZOOM >= 4:
            painter.setPen(QColor(40, 40, 40))
            for x in range(0, FACE_WIDTH + 1):
                painter.drawLine(x * ZOOM, 0, x * ZOOM, FACE_HEIGHT * ZOOM)
            for y in range(0, FACE_HEIGHT + 1):
                painter.drawLine(0, y * ZOOM, FACE_WIDTH * ZOOM, y * ZOOM)


class FaceEditorDialog(QDialog):
    """Edit (or create) a single Face, including its animation frames."""

    def __init__(self, face: Face | None, existing_names: set[str], parent=None, is_header_mode: bool = False):
        super().__init__(parent)
        self.is_header_mode = is_header_mode
        self.setWindowTitle(f"Face Editor - {face.name}" if (face and is_header_mode) else "Face Editor")
        self.existing_names = existing_names - ({face.name} if face else set())
        self.result_face: Face | None = None

        if face is not None:
            self.frames = [frame_bytes_to_image(bytes(f)) for f in face.frames]
        else:
            self.frames = [blank_image()]
        self.frame_index = 0

        root = QHBoxLayout(self)

        # -- left: tools + canvas --------------------------------------
        left = QVBoxLayout()
        tool_row = QHBoxLayout()
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        tool_labels = {
            "pencil": "Pencil", "eraser": "Eraser", "line": "Line",
            "rect": "Rect", "rect_fill": "Rect Fill", "ellipse": "Ellipse",
            "ellipse_fill": "Ellipse Fill", "fill": "Bucket", "text": "Text",
        }
        for tool in TOOLS:
            btn = QToolButton()
            btn.setText(tool_labels[tool])
            btn.setCheckable(True)
            btn.setChecked(tool == "pencil")
            btn.clicked.connect(lambda _, t=tool: self._set_tool(t))
            self.tool_group.addButton(btn)
            tool_row.addWidget(btn)
        left.addLayout(tool_row)

        self.canvas = FaceCanvas()
        self.canvas.changed.connect(self._on_canvas_changed)
        left.addWidget(self.canvas, alignment=Qt.AlignCenter)

        edit_row = QHBoxLayout()
        undo_btn = QPushButton("Undo"); undo_btn.clicked.connect(self.canvas.undo)
        redo_btn = QPushButton("Redo"); redo_btn.clicked.connect(self.canvas.redo)
        clear_btn = QPushButton("Clear"); clear_btn.setProperty("variant", "danger")
        clear_btn.clicked.connect(self.canvas.clear)
        invert_btn = QPushButton("Invert"); invert_btn.clicked.connect(self.canvas.invert)
        mirror_h_btn = QPushButton("Mirror H"); mirror_h_btn.clicked.connect(lambda: self.canvas.mirror(True))
        mirror_v_btn = QPushButton("Mirror V"); mirror_v_btn.clicked.connect(lambda: self.canvas.mirror(False))
        for b in (undo_btn, redo_btn, clear_btn, invert_btn, mirror_h_btn, mirror_v_btn):
            b.setProperty("variant", "flat")
            edit_row.addWidget(b)
        left.addLayout(edit_row)

        shift_row = QHBoxLayout()
        for label, dx, dy in [("<", -1, 0), (">", 1, 0), ("^", 0, -1), ("v", 0, 1)]:
            b = QPushButton(label)
            b.setProperty("variant", "flat")
            b.setFixedWidth(32)
            b.clicked.connect(lambda _, dx=dx, dy=dy: self.canvas.shift(dx, dy))
            shift_row.addWidget(b)
        shift_row.addWidget(QLabel("Shift bitmap"))
        shift_row.addStretch(1)
        left.addLayout(shift_row)

        text_row = QHBoxLayout()
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("Type text, then click canvas to stamp it")
        self.font_combo = QComboBox()
        self.font_combo.addItems(sorted(QFontDatabase.families())[:200])
        idx = self.font_combo.findText("Consolas")
        if idx >= 0:
            self.font_combo.setCurrentIndex(idx)
        self.font_size = QSpinBox(); self.font_size.setRange(6, 64); self.font_size.setValue(24)
        text_row.addWidget(self.text_input, 1)
        text_row.addWidget(self.font_combo)
        text_row.addWidget(self.font_size)
        left.addLayout(text_row)
        self.canvas.mousePressEvent = self._wrap_canvas_press(self.canvas.mousePressEvent)

        root.addLayout(left, 2)

        # -- right: frames, metadata, live output ----------------------
        right = QVBoxLayout()

        frames_box = QGroupBox("Animation Frames (root + up to 5)")
        frames_layout = QVBoxLayout(frames_box)
        self.frame_list = QListWidget()
        self.frame_list.setFlow(QListWidget.LeftToRight)
        self.frame_list.setFixedHeight(48)
        self.frame_list.currentRowChanged.connect(self._select_frame)
        frames_layout.addWidget(self.frame_list)
        frame_btn_row = QHBoxLayout()
        add_frame_btn = QPushButton("+ Frame"); add_frame_btn.clicked.connect(self._add_frame)
        dup_frame_btn = QPushButton("Duplicate"); dup_frame_btn.clicked.connect(self._duplicate_frame)
        del_frame_btn = QPushButton("Delete"); del_frame_btn.setProperty("variant", "danger")
        del_frame_btn.clicked.connect(self._delete_frame)
        for b in (add_frame_btn, dup_frame_btn, del_frame_btn):
            frame_btn_row.addWidget(b)
        frames_layout.addLayout(frame_btn_row)
        self.onion_check = QCheckBox("Onion-skin previous frame")
        self.onion_check.setChecked(True)
        self.onion_check.toggled.connect(lambda _: self._refresh_onion())
        frames_layout.addWidget(self.onion_check)
        right.addWidget(frames_box)

        meta_box = QGroupBox("Face Properties")
        form = QFormLayout(meta_box)
        self.name_edit = QLineEdit(face.name if face else "")
        self.name_edit.textChanged.connect(self._refresh_output)
        form.addRow("Name:", self.name_edit)
        self.category_combo = QComboBox()
        for cat in FACE_CATEGORIES:
            self.category_combo.addItem(FACE_CATEGORY_LABELS[cat], cat)
        if face:
            ci = self.category_combo.findData(face.category)
            if ci >= 0:
                self.category_combo.setCurrentIndex(ci)
        else:
            ci = self.category_combo.findData(FACE_CATEGORY_CUSTOM)
            self.category_combo.setCurrentIndex(ci)
        form.addRow("Category:", self.category_combo)
        self.fps_spin = QSpinBox(); self.fps_spin.setRange(1, 30)
        self.fps_spin.setValue(face.fps if face else 4)
        form.addRow("FPS:", self.fps_spin)
        self.override_check = QCheckBox("Override existing firmware face")
        self.override_check.setChecked(face.is_override if face else False)
        if self.is_header_mode:
            self.override_check.setVisible(False)
        form.addRow(self.override_check)
        right.addWidget(meta_box)

        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color:#e0a020;")
        right.addWidget(self.warning_label)
        self.name_edit.textChanged.connect(self._refresh_warning)
        self.override_check.toggled.connect(self._refresh_warning)

        output_box = QGroupBox("Live Byte-Array Output")
        out_layout = QVBoxLayout(output_box)
        self.output_view = QPlainTextEdit()
        self.output_view.setReadOnly(True)
        self.output_view.setFont(QFont("Consolas", 8))
        out_layout.addWidget(self.output_view)
        right.addWidget(output_box, 1)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save Face"); save_btn.setProperty("variant", "success")
        save_btn.clicked.connect(self._on_save)
        cancel_btn = QPushButton("Cancel"); cancel_btn.setProperty("variant", "flat")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        right.addLayout(btn_row)

        root.addLayout(right, 1)
        self.resize(1180, 760)

        self._refresh_frame_list()
        self._select_frame(0)
        self._refresh_output()
        self._refresh_warning()

    # -- tool/frame plumbing -------------------------------------------
    def _set_tool(self, tool: str) -> None:
        self.canvas.tool = tool

    def _wrap_canvas_press(self, original):
        def handler(event):
            if self.canvas.tool == "text" and event.button() == Qt.LeftButton and self.text_input.text():
                pt = self.canvas._to_pixel(event.position().toPoint())
                self.canvas.stamp_text(
                    self.text_input.text(), self.font_combo.currentText(),
                    self.font_size.value(), pt,
                )
                return
            original(event)
        return handler

    def _on_canvas_changed(self) -> None:
        self.frames[self.frame_index] = self.canvas.image.copy()
        self._refresh_output()

    def _refresh_frame_list(self) -> None:
        self.frame_list.blockSignals(True)
        self.frame_list.clear()
        for i in range(len(self.frames)):
            item = QListWidgetItem(f"F{i}")
            pix = QPixmap.fromImage(self.frames[i].scaled(32, 16))
            item.setIcon(QIcon(pix))
            self.frame_list.addItem(item)
        self.frame_list.blockSignals(False)

    def _select_frame(self, row: int) -> None:
        if row < 0 or row >= len(self.frames):
            return
        self.frame_index = row
        self.frame_list.setCurrentRow(row)
        self.canvas.image = self.frames[row].copy()
        self._refresh_onion()
        self.canvas.update()

    def _refresh_onion(self) -> None:
        if self.onion_check.isChecked() and self.frame_index > 0:
            self.canvas.onion_image = self.frames[self.frame_index - 1]
        else:
            self.canvas.onion_image = None
        self.canvas.update()

    def _add_frame(self) -> None:
        if len(self.frames) >= FACE_MAX_FRAMES:
            QMessageBox.information(self, "Limit reached", f"Max {FACE_MAX_FRAMES} frames per face.")
            return
        self.frames.append(self.frames[self.frame_index].copy())
        self._refresh_frame_list()
        self._select_frame(len(self.frames) - 1)
        self._refresh_output()

    def _duplicate_frame(self) -> None:
        self._add_frame()

    def _delete_frame(self) -> None:
        if len(self.frames) <= 1:
            return
        del self.frames[self.frame_index]
        self._refresh_frame_list()
        self._select_frame(max(0, self.frame_index - 1))
        self._refresh_output()

    def _refresh_warning(self) -> None:
        name = self.name_edit.text().strip()
        if self.is_header_mode:
            warning = None
            if not name:
                warning = "Enter a face name."
            elif not name.isidentifier():
                warning = "Name must be a valid C identifier (letters, digits, underscores, starting with letter/underscore)."
            elif name in self.existing_names:
                warning = f"A face named '{name}' already exists in this file."
            self.warning_label.setText(warning or "")
            return

        stub = Face(name=name, is_override=self.override_check.isChecked())
        warning = check_face_name_collision(stub)
        if name in self.existing_names:
            warning = f"A face named '{name}' already exists in this project."
        self.warning_label.setText(warning or "")

    def _refresh_output(self) -> None:
        name = self.name_edit.text().strip() or "unnamed"
        packed = [pixels_to_frame(image_to_pixels(img)) for img in self.frames]
        stub = Face(name=name, frames=packed, fps=self.fps_spin.value())
        try:
            self.output_view.setPlainText(export_face(stub))
        except Exception as exc:  # pragma: no cover - defensive
            self.output_view.setPlainText(f"// error generating preview: {exc}")

    def _on_save(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Name required", "Enter a face name.")
            return
        if self.is_header_mode and not name.isidentifier():
            QMessageBox.warning(
                self, "Invalid name",
                "Face name must be a valid C identifier (letters, digits, underscores, starting with letter or underscore)."
            )
            return
        if name in self.existing_names:
            QMessageBox.warning(self, "Name in use", f"A face named '{name}' already exists.")
            return
        packed = [pixels_to_frame(image_to_pixels(img)) for img in self.frames]
        self.result_face = Face(
            name=name,
            category=self.category_combo.currentData(),
            fps=self.fps_spin.value(),
            frames=packed,
            is_override=True if self.is_header_mode else self.override_check.isChecked(),
        )
        self.accept()
