"""Timeline editor: a Premiere-style ruler+grid, a draggable scrub cursor,
one lane of frame blocks (width proportional to delay) per sequence plus a
face-marker sub-lane, drag to reorder/retime, and playback that smoothly
interpolates servo angles between frames (as if the servos were actually
rotating) rather than snapping. Also exposes the firmware's built-in poses
(see builtin_sequences.py) as read-only, playable, "Duplicate to Custom"-able
sequences alongside the project's own.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer, QPointF
from PySide6.QtGui import QColor, QBrush, QPen, QFont, QPainter
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGraphicsView, QGraphicsScene,
    QGraphicsRectItem, QGraphicsSimpleTextItem, QGraphicsLineItem, QPushButton,
    QComboBox, QLabel, QSlider, QMenu, QInputDialog, QMessageBox, QToolButton,
)

from .project import Sequence, Frame
from .servo_model import SERVO_COLORS
from .builtin_sequences import BUILTIN_SEQUENCES, BUILTIN_ORDER
from .theme import ACCENT

RULER_H = 22
LANE_Y = RULER_H + 4
LANE_H = 56
FACE_LANE_Y = LANE_Y + LANE_H + 8
FACE_LANE_H = 20
CONTENT_LEFT = 8.0
PX_PER_MS = 0.12
MIN_BLOCK_W = 3.0
HANDLE_W = 8
TICK_MS = 16
GRID_STEP_MS = 500
GRID_MAJOR_EVERY = 2  # every 2nd grid line (1000ms) is a labeled major line


def lerp_angles(a: dict, b: dict, t: float) -> dict:
    t = max(0.0, min(1.0, t))
    return {k: a[k] + (b[k] - a[k]) * t for k in a}


class FrameBlockItem(QGraphicsRectItem):
    def __init__(self, index: int, frame: Frame, x: float, timeline: "TimelineWidget"):
        w = max(MIN_BLOCK_W, frame.delay_ms * PX_PER_MS)
        super().__init__(0, 0, w, LANE_H)
        self.index = index
        self.frame = frame
        self.timeline = timeline
        self.setPos(x, LANE_Y)
        self.setFlag(QGraphicsRectItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setBrush(QBrush(QColor("#2c2c2c")))
        self.setPen(QPen(QColor("#444"), 1))
        self.setCursor(Qt.PointingHandCursor)

        if w >= 16:
            label = QGraphicsSimpleTextItem(f"{index + 1}", self)
            label.setBrush(QBrush(QColor("#e6e6e6")))
            label.setFont(QFont("Segoe UI", 8, QFont.Bold))
            label.setPos(4, 2)
        if w >= 30:
            delay_label = QGraphicsSimpleTextItem(f"{frame.delay_ms}ms", self)
            delay_label.setBrush(QBrush(QColor("#9a9a9a")))
            delay_label.setFont(QFont("Segoe UI", 7))
            delay_label.setPos(4, LANE_H - 16)

        self._dragging = False
        self._resizing = False
        self._drag_start = QPointF()

    def set_selected_style(self, selected: bool) -> None:
        self.setPen(QPen(QColor(ACCENT if selected else "#444"), 2 if selected else 1))

    def hoverMoveEvent(self, event) -> None:
        if event.pos().x() > self.rect().width() - HANDLE_W:
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.setCursor(Qt.PointingHandCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        self.timeline.select_frame(self.index)
        if self.timeline.current_is_builtin():
            self.timeline._notify_blocked()
            return
        if event.pos().x() > self.rect().width() - HANDLE_W:
            self._resizing = True
        else:
            self._dragging = True
            self._drag_start = event.scenePos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._resizing:
            new_w = max(MIN_BLOCK_W, event.pos().x())
            new_delay = max(1, int(new_w / PX_PER_MS))
            self.timeline.retime_frame(self.index, new_delay)
            return
        if self._dragging:
            dx = event.scenePos().x() - self._drag_start.x()
            if abs(dx) > max(self.rect().width(), MIN_BLOCK_W * 4) * 0.5:
                target = self.index + (1 if dx > 0 else -1)
                self.timeline.reorder_frame(self.index, target)
                self._drag_start = event.scenePos()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._dragging = False
        self._resizing = False
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        if self.timeline.current_is_builtin():
            self.timeline._notify_blocked()
            return
        menu = QMenu()
        dup = menu.addAction("Duplicate")
        ins_before = menu.addAction("Insert Blank Before")
        ins_after = menu.addAction("Insert Blank After")
        menu.addSeparator()
        delete = menu.addAction("Delete")
        action = menu.exec(event.screenPos())
        if action == dup:
            self.timeline.duplicate_frame(self.index)
        elif action == ins_before:
            self.timeline.insert_blank(self.index)
        elif action == ins_after:
            self.timeline.insert_blank(self.index + 1)
        elif action == delete:
            self.timeline.delete_frame(self.index)


class RulerItem(QGraphicsRectItem):
    """Click/drag anywhere in the ruler strip to scrub the playhead,
    Premiere-style."""

    def __init__(self, timeline: "TimelineWidget", width: float):
        super().__init__(0, 0, width, RULER_H)
        self.timeline = timeline
        self.setBrush(QBrush(QColor("#181818")))
        self.setPen(QPen(QColor("#333"), 1))
        self.setCursor(Qt.SizeHorCursor)
        self.setZValue(5)

    def mousePressEvent(self, event) -> None:
        self.timeline._scrub_to_x(event.pos().x())

    def mouseMoveEvent(self, event) -> None:
        self.timeline._scrub_to_x(event.pos().x())


class PlayheadItem(QGraphicsLineItem):
    def __init__(self, height: float):
        super().__init__(0, 0, 0, height)
        self.setPen(QPen(QColor("#ff5555"), 2))
        self.setZValue(10)


class TimelineWidget(QWidget):
    frameSelected = Signal(int)                    # index into current sequence
    poseAtIndex = Signal(dict, object, str)         # angles, face, face_mode (playback/scrub)
    sequencesChanged = Signal()
    editBlocked = Signal(str)                       # message when editing a read-only (built-in) sequence

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sequences: dict[str, Sequence] = {}
        self.builtin_sequences: dict[str, Sequence] = dict(BUILTIN_SEQUENCES)
        self.current_seq_name: str | None = None
        self.selected_index: int | None = None
        self._blocks: list[FrameBlockItem] = []
        self._grid_items: list = []
        self._total_time_ms: float = 0.0
        self._play_timer = QTimer(self)
        self._play_timer.timeout.connect(self._advance_playback)
        self._play_time_ms = 0.0
        self._playing = False
        self._loop = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        toolbar = QHBoxLayout()
        self.seq_combo = QComboBox()
        self.seq_combo.currentIndexChanged.connect(self._on_sequence_selected)
        toolbar.addWidget(QLabel("Sequence:"))
        toolbar.addWidget(self.seq_combo, 1)

        new_btn = QToolButton(); new_btn.setText("+"); new_btn.setToolTip("New sequence")
        new_btn.clicked.connect(self._new_sequence)
        rename_btn = QToolButton(); rename_btn.setText("Rename")
        rename_btn.clicked.connect(self._rename_sequence)
        del_btn = QToolButton(); del_btn.setText("Delete")
        del_btn.clicked.connect(self._delete_sequence)
        self.duplicate_btn = QToolButton(); self.duplicate_btn.setText("Duplicate to Custom")
        self.duplicate_btn.setToolTip("Built-in sequences are read-only -- copy this one to edit it")
        self.duplicate_btn.clicked.connect(self._duplicate_builtin_to_custom)
        self.duplicate_btn.setVisible(False)
        toolbar.addWidget(new_btn)
        toolbar.addWidget(rename_btn)
        toolbar.addWidget(del_btn)
        toolbar.addWidget(self.duplicate_btn)

        toolbar.addSpacing(16)
        self.add_frame_btn = QPushButton("+ Add Frame")
        self.add_frame_btn.clicked.connect(self._add_frame_from_current)
        toolbar.addWidget(self.add_frame_btn)

        self.play_btn = QPushButton("Play")
        self.play_btn.clicked.connect(self.toggle_playback)
        toolbar.addWidget(self.play_btn)

        toolbar.addWidget(QLabel("Speed:"))
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(10, 300)
        self.speed_slider.setValue(100)
        self.speed_slider.setFixedWidth(100)
        toolbar.addWidget(self.speed_slider)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.scene = QGraphicsScene()
        self.view = QGraphicsView(self.scene)
        self.view.setObjectName("TimelineView")
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setMinimumHeight(140)
        self.view.setDragMode(QGraphicsView.NoDrag)
        layout.addWidget(self.view)

        self._ruler: RulerItem | None = None
        self._playhead = PlayheadItem(FACE_LANE_Y + FACE_LANE_H + 12)
        self.scene.addItem(self._playhead)

    # -- sequence management -------------------------------------------
    def load_project_sequences(self, sequences: dict[str, Sequence]) -> None:
        self.sequences = sequences
        self._rebuild_combo()

    def _rebuild_combo(self, select: str | None = None) -> None:
        select = select or self.current_seq_name
        self.seq_combo.blockSignals(True)
        self.seq_combo.clear()
        for name in sorted(self.sequences.keys()):
            self.seq_combo.addItem(name, name)
        if self.sequences and self.builtin_sequences:
            self.seq_combo.insertSeparator(self.seq_combo.count())
        for name in BUILTIN_ORDER:
            self.seq_combo.addItem(f"★ {name}", name)
        self.seq_combo.blockSignals(False)

        idx = self.seq_combo.findData(select) if select else -1
        if idx < 0:
            idx = 0 if self.seq_combo.count() else -1
        if idx >= 0:
            self.seq_combo.setCurrentIndex(idx)
            self._on_sequence_selected(idx)

    def _on_sequence_selected(self, index: int) -> None:
        name = self.seq_combo.itemData(index)
        if not name:
            return
        self.current_seq_name = name
        self.selected_index = None
        is_builtin = self.current_is_builtin()
        self.duplicate_btn.setVisible(is_builtin)
        self.add_frame_btn.setEnabled(not is_builtin)
        self._relayout()

    def current_sequence(self) -> Sequence | None:
        if self.current_seq_name is None:
            return None
        if self.current_seq_name in self.sequences:
            return self.sequences[self.current_seq_name]
        return self.builtin_sequences.get(self.current_seq_name)

    def current_is_builtin(self) -> bool:
        return (self.current_seq_name in self.builtin_sequences
                and self.current_seq_name not in self.sequences)

    def _notify_blocked(self) -> None:
        self.editBlocked.emit(
            "Built-in sequences are read-only. Use 'Duplicate to Custom' to edit a copy."
        )

    def _new_sequence(self) -> None:
        name, ok = QInputDialog.getText(self, "New Sequence", "Name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self.sequences or name in self.builtin_sequences:
            QMessageBox.warning(self, "Name in use", f"'{name}' already exists.")
            return
        self.sequences[name] = Sequence(name, [Frame()])
        self._rebuild_combo(select=name)
        self.sequencesChanged.emit()

    def _rename_sequence(self) -> None:
        if not self.current_seq_name or self.current_is_builtin():
            if self.current_is_builtin():
                self._notify_blocked()
            return
        old = self.current_seq_name
        name, ok = QInputDialog.getText(self, "Rename Sequence", "Name:", text=old)
        if not ok or not name.strip() or name == old:
            return
        name = name.strip()
        if name in self.sequences or name in self.builtin_sequences:
            QMessageBox.warning(self, "Name in use", f"'{name}' already exists.")
            return
        seq = self.sequences.pop(old)
        seq.name = name
        self.sequences[name] = seq
        self._rebuild_combo(select=name)
        self.sequencesChanged.emit()

    def _delete_sequence(self) -> None:
        if self.current_is_builtin():
            self._notify_blocked()
            return
        if not self.current_seq_name or len(self.sequences) <= 1:
            QMessageBox.information(self, "Can't delete", "At least one custom sequence must remain.")
            return
        name = self.current_seq_name
        if QMessageBox.question(self, "Delete sequence", f"Delete '{name}'?") != QMessageBox.Yes:
            return
        del self.sequences[name]
        self._rebuild_combo()
        self.sequencesChanged.emit()

    def _duplicate_builtin_to_custom(self) -> None:
        seq = self.current_sequence()
        if not seq or not self.current_is_builtin():
            return
        base = seq.name
        new_name = f"{base}_custom"
        i = 2
        while new_name in self.sequences or new_name in self.builtin_sequences:
            new_name = f"{base}_custom{i}"
            i += 1
        self.sequences[new_name] = Sequence(new_name, [f.clone() for f in seq.frames])
        self._rebuild_combo(select=new_name)
        self.sequencesChanged.emit()

    # -- frame editing ---------------------------------------------------
    def select_frame(self, index: int) -> None:
        self.selected_index = index
        for i, block in enumerate(self._blocks):
            block.set_selected_style(i == index)
        seq = self.current_sequence()
        if seq and 0 <= index < len(seq.frames):
            self.frameSelected.emit(index)
            # Snap to the block's *end*, not its start: a frame's delay is
            # the transition into its pose, so the end of the block is the
            # exact moment that pose is reached -- landing the cursor there
            # makes clear you're setting this frame's target/end position.
            block = self._blocks[index]
            self._scrub_to_x(block.pos().x() + block.rect().width())

    def add_frame_capture(self, frame: Frame) -> None:
        if self.current_is_builtin():
            self._notify_blocked()
            return
        seq = self.current_sequence()
        if not seq:
            return
        seq.frames.append(frame)
        self._relayout()
        self.select_frame(len(seq.frames) - 1)
        self.sequencesChanged.emit()

    def _add_frame_from_current(self) -> None:
        if self.current_is_builtin():
            self._notify_blocked()
            return
        seq = self.current_sequence()
        if not seq:
            return
        base = seq.frames[-1].clone() if seq.frames else Frame()
        self.add_frame_capture(base)

    def update_selected_frame_angles(self, angles: dict[str, int]) -> None:
        if self.current_is_builtin():
            return
        seq = self.current_sequence()
        if not seq or self.selected_index is None:
            return
        seq.frames[self.selected_index].angles = dict(angles)
        self._relayout(keep_selection=True)
        self.sequencesChanged.emit()

    def update_selected_frame_fields(self, delay_ms: int, face, face_mode: str) -> None:
        if self.current_is_builtin():
            return
        seq = self.current_sequence()
        if not seq or self.selected_index is None:
            return
        frame = seq.frames[self.selected_index]
        frame.delay_ms = delay_ms
        frame.face = face
        frame.face_mode = face_mode
        self._relayout(keep_selection=True)
        self.sequencesChanged.emit()

    def retime_frame(self, index: int, delay_ms: int) -> None:
        if self.current_is_builtin():
            return
        seq = self.current_sequence()
        if not seq:
            return
        seq.frames[index].delay_ms = delay_ms
        self._relayout(keep_selection=True)
        self.sequencesChanged.emit()

    def reorder_frame(self, index: int, target: int) -> None:
        if self.current_is_builtin():
            return
        seq = self.current_sequence()
        if not seq or not (0 <= target < len(seq.frames)) or target == index:
            return
        frame = seq.frames.pop(index)
        seq.frames.insert(target, frame)
        self._relayout()
        self.select_frame(target)
        self.sequencesChanged.emit()

    def duplicate_frame(self, index: int) -> None:
        if self.current_is_builtin():
            return
        seq = self.current_sequence()
        if not seq:
            return
        seq.frames.insert(index + 1, seq.frames[index].clone())
        self._relayout()
        self.select_frame(index + 1)
        self.sequencesChanged.emit()

    def insert_blank(self, index: int) -> None:
        if self.current_is_builtin():
            return
        seq = self.current_sequence()
        if not seq:
            return
        seq.frames.insert(index, Frame())
        self._relayout()
        self.select_frame(index)
        self.sequencesChanged.emit()

    def delete_frame(self, index: int) -> None:
        if self.current_is_builtin():
            return
        seq = self.current_sequence()
        if not seq or len(seq.frames) <= 1:
            return
        del seq.frames[index]
        self._relayout()
        self.select_frame(min(index, len(seq.frames) - 1))
        self.sequencesChanged.emit()

    # -- layout -------------------------------------------------------
    def _time_to_x(self, ms: float) -> float:
        return CONTENT_LEFT + ms * PX_PER_MS

    def _x_to_time(self, x: float) -> float:
        return max(0.0, (x - CONTENT_LEFT) / PX_PER_MS)

    def _relayout(self, keep_selection: bool = False) -> None:
        keep = self.selected_index if keep_selection else None
        for block in self._blocks:
            self.scene.removeItem(block)
        self._blocks = []
        for item in self._grid_items:
            self.scene.removeItem(item)
        self._grid_items = []
        if self._ruler is not None:
            self.scene.removeItem(self._ruler)
            self._ruler = None

        seq = self.current_sequence()
        if not seq:
            self.scene.setSceneRect(0, 0, 400, 120)
            self._total_time_ms = 0.0
            return

        x = CONTENT_LEFT
        for i, frame in enumerate(seq.frames):
            block = FrameBlockItem(i, frame, x, self)
            self.scene.addItem(block)
            self._blocks.append(block)
            if frame.face:
                marker = QGraphicsRectItem(0, 0, block.rect().width(), FACE_LANE_H)
                marker.setPos(x, FACE_LANE_Y)
                marker.setBrush(QBrush(QColor(SERVO_COLORS.get("R1", "#666"))))
                marker.setOpacity(0.35)
                marker.setToolTip(frame.face)
                self.scene.addItem(marker)
                self._grid_items.append(marker)
            x += block.rect().width() + 3

        self._total_time_ms = sum(max(1, f.delay_ms) for f in seq.frames)
        total_w = max(400.0, x + 40)
        bottom = FACE_LANE_Y + FACE_LANE_H + 20
        self.scene.setSceneRect(0, 0, total_w, bottom)

        self._ruler = RulerItem(self, total_w)
        self.scene.addItem(self._ruler)

        self._rebuild_grid(total_w, bottom)

        self._playhead.setLine(0, 0, 0, bottom - 8)
        self.scene.removeItem(self._playhead)
        self.scene.addItem(self._playhead)  # keep on top after rebuilding

        if keep is not None and 0 <= keep < len(seq.frames):
            self.select_frame(keep)
        elif seq.frames:
            self.select_frame(0)

    def _rebuild_grid(self, total_w: float, bottom: float) -> None:
        t = 0
        i = 0
        while self._time_to_x(t) <= total_w:
            x = self._time_to_x(t)
            major = (i % GRID_MAJOR_EVERY == 0)
            line = QGraphicsLineItem(x, RULER_H, x, bottom)
            line.setPen(QPen(QColor("#3a3a3a" if major else "#232323"), 1))
            line.setZValue(-5)
            self.scene.addItem(line)
            self._grid_items.append(line)
            if major:
                label = QGraphicsSimpleTextItem(f"{t / 1000:.1f}s")
                label.setBrush(QBrush(QColor("#888")))
                label.setFont(QFont("Segoe UI", 7))
                label.setPos(x + 2, 2)
                label.setZValue(6)
                self.scene.addItem(label)
                self._grid_items.append(label)
            t += GRID_STEP_MS
            i += 1

    # -- scrubbing / interpolated playback ---------------------------------
    def _pose_at_time(self, seq: Sequence, ms: float):
        cum = 0.0
        frames = seq.frames
        for i, frame in enumerate(frames):
            dur = max(1, frame.delay_ms)
            end = cum + dur
            if ms <= end or i == len(frames) - 1:
                if i == 0:
                    return dict(frame.angles), frame.face, frame.face_mode
                prev = frames[i - 1]
                t = 0.0 if dur <= 0 else (ms - cum) / dur
                angles = lerp_angles(prev.angles, frame.angles, t)
                face = frame.face if t >= 0.999 else None
                return angles, face, frame.face_mode
            cum = end
        return dict(frames[-1].angles), frames[-1].face, frames[-1].face_mode

    def _scrub_to_x(self, x: float) -> None:
        total_w = self.scene.sceneRect().width()
        x = max(0.0, min(total_w, x))
        self._playhead.setX(x)
        seq = self.current_sequence()
        if not seq or not seq.frames:
            return
        ms = self._x_to_time(x)
        angles, face, mode = self._pose_at_time(seq, ms)
        self.poseAtIndex.emit(angles, face, mode)

    # -- playback -------------------------------------------------------
    def toggle_playback(self) -> None:
        if self._playing:
            self._stop_playback()
        else:
            self._start_playback()

    def _start_playback(self) -> None:
        seq = self.current_sequence()
        if not seq or not seq.frames:
            return
        self._playing = True
        self.play_btn.setText("Stop")
        self._play_time_ms = 0.0
        self._play_timer.start(TICK_MS)

    def _stop_playback(self) -> None:
        self._playing = False
        self.play_btn.setText("Play")
        self._play_timer.stop()

    def _advance_playback(self) -> None:
        seq = self.current_sequence()
        if not seq or not seq.frames:
            self._stop_playback()
            return
        speed = self.speed_slider.value() / 100.0
        self._play_time_ms += TICK_MS * speed
        total = self._total_time_ms
        if self._play_time_ms >= total:
            if self._loop:
                self._play_time_ms = 0.0
            else:
                self._scrub_to_x(self._time_to_x(total))
                self._stop_playback()
                return
        self._scrub_to_x(self._time_to_x(self._play_time_ms))
