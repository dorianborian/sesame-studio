"""Timeline editor.

One custom-painted canvas (see timeline_canvas.py) shows a ruler, a lane of
frame blocks whose width is their delay, a face lane and eight collapsible
per-servo tracks; this module is the widget around it: sequence bar,
transport, selection, clipboard, undo/redo and real-time playback.

Timing model (matches the firmware exporter): a frame's ``delay_ms`` is the
transition INTO its pose, so the pose is reached at the END of its block.
Selecting a frame therefore parks the playhead on the block's right edge
(the diamond key marker). Playback interpolates linearly between poses for
the preview; ``frameTargetChanged`` additionally reports each frame's target
angles the moment the playhead enters it, which is what the real robot is
told (it writes the target at the start of the delay and the servos slew).

Every edit goes through ``undo_stack``. Editing the selected frame's angles
or fields updates only that frame's visuals (O(1)) and never re-announces
the selection, so inspector slider drags cannot loop back.

The firmware's built-in poses (builtin_sequences.py) are listed alongside
the project's sequences as read-only; "Duplicate" makes an editable copy.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer, QElapsedTimer, QSize
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QUndoStack
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QComboBox, QLabel, QMenu,
    QInputDialog, QMessageBox, QToolButton, QScrollBar, QFrame, QSizePolicy,
)

from .project import Sequence, Frame
from .builtin_sequences import BUILTIN_SEQUENCES, BUILTIN_ORDER
from .icons import icon as _theme_icon
from .theme import FG_MUTED, FG_DIM, SPACING
from .timeline_model import (  # noqa: F401  (lerp_angles is re-exported: create_trailer.py imports it from here)
    lerp_angles, pose_at, format_clock, frames_to_text, frames_from_text,
)
from .timeline_canvas import TimelineCanvas
from .timeline_commands import (
    SetAnglesCommand, SetFieldsCommand, SetDelaysCommand, InsertFramesCommand,
    RemoveFramesCommand, MoveFramesCommand, AddSequenceCommand,
    DeleteSequenceCommand, RenameSequenceCommand,
)

TICK_MS = 16                      # ~60 Hz playback timer
MAX_TICK_DT_MS = 250.0            # a stalled event loop doesn't teleport the playhead
BLOCKED_THROTTLE_MS = 1500
SPEEDS = (0.25, 0.5, 1.0, 2.0)
READ_ONLY_MESSAGE = ("Built-in sequences are read-only. "
                     "Use 'Duplicate to edit' to make an editable copy.")
_UNSET = object()

_MOD_MASK = Qt.ControlModifier | Qt.ShiftModifier | Qt.AltModifier | Qt.MetaModifier


def icon(name: str) -> QIcon:
    """Icons are decoration: never let a missing glyph/theme token break the
    timeline (buttons fall back to text when the icon is null)."""
    try:
        return _theme_icon(name)
    except Exception:
        return QIcon()


def _speed_label(v: float) -> str:
    return f"{v:g}×"


class TimelineWidget(QWidget):
    frameSelected = Signal(int)                 # primary selected frame index
    poseAtIndex = Signal(dict, object, str)     # interpolated angles, face|None, face_mode
    sequencesChanged = Signal()                 # any data edit (mark project dirty)
    editBlocked = Signal(str)                   # tried to edit a read-only sequence (throttled)
    playbackStateChanged = Signal(bool)
    playbackStarted = Signal(float)             # start time in ms (0 = from the top)
    frameTargetChanged = Signal(dict)           # target angles of the frame the playhead entered
    currentSequenceChanged = Signal(str)

    _session_tracks_visible = True              # remembered for the session

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sequences: dict[str, Sequence] = {}
        self.builtin_sequences: dict[str, Sequence] = dict(BUILTIN_SEQUENCES)
        self.current_seq_name: str | None = None
        self.selected_index: int | None = None
        self._selection: set[int] = set()
        self._anchor: int | None = None
        self._attached = False              # playhead parked on the primary frame's key
        self._clipboard: list[Frame] = []
        self._view_states: dict[str, tuple] = {}

        self._play_ms = 0.0
        self._playing = False
        self._loop = True
        self._speed = 1.0
        self._cur_idx = None                # frame the playhead was last reported in
        self._last_face = _UNSET
        self._tick_count = 0

        self._applying = 0                  # >0 while a command's refresh is running
        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(300)
        self._edit_group = None
        self._edit_label = None
        self._edit_serial = 0
        self._edit_clock = QElapsedTimer()
        self._edit_clock.start()
        self._blocked_clock = QElapsedTimer()

        self._play_timer = QTimer(self)
        self._play_timer.setTimerType(Qt.PreciseTimer)
        self._play_timer.setInterval(TICK_MS)
        self._play_timer.timeout.connect(self._tick)
        self._play_clock = QElapsedTimer()

        self._build_ui()
        self.undo_stack.canUndoChanged.connect(self.undo_btn.setEnabled)
        self.undo_stack.canRedoChanged.connect(self.redo_btn.setEnabled)
        self.undo_stack.undoTextChanged.connect(self._update_undo_tooltips)
        self.undo_stack.redoTextChanged.connect(self._update_undo_tooltips)
        self._update_undo_tooltips()
        self._update_transport_ui()
        self._update_edit_enabled()
        self._update_readout()

    # ======================================================================
    # UI construction
    # ======================================================================
    def _tool_button(self, icon_name: str, tooltip: str, fallback: str, slot=None,
                     checkable: bool = False) -> QToolButton:
        btn = QToolButton()
        ico = icon(icon_name)
        if ico.isNull():
            btn.setText(fallback)
        else:
            btn.setIcon(ico)
            btn.setIconSize(QSize(16, 16))
            btn.setProperty("iconOnly", "true")
        btn.setToolTip(tooltip)
        btn.setAutoRaise(True)
        btn.setCheckable(checkable)
        btn.setFocusPolicy(Qt.NoFocus)      # keep keyboard focus on the canvas
        if slot is not None:
            btn.clicked.connect(slot)
        return btn

    @staticmethod
    def _separator() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFixedWidth(1)
        line.setProperty("role", "separator")
        return line

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- row 1: sequence bar -------------------------------------------
        seq_bar = QFrame()
        seq_bar.setProperty("role", "toolbar")
        row = QHBoxLayout(seq_bar)
        row.setContentsMargins(SPACING, 4, SPACING, 4)
        row.setSpacing(4)

        self.seq_combo = QComboBox()
        self.seq_combo.setMinimumWidth(190)
        self.seq_combo.setMaxVisibleItems(24)
        self.seq_combo.setToolTip("Sequence shown in the timeline")
        self.seq_combo.setFocusPolicy(Qt.ClickFocus)
        self.seq_combo.currentIndexChanged.connect(self._on_sequence_selected)
        self.seq_combo.activated.connect(lambda _i: self.canvas.setFocus())
        row.addWidget(self.seq_combo)

        self.new_seq_btn = self._tool_button("plus", "New sequence", "New", lambda: self.new_sequence())
        self.dup_seq_btn = self._tool_button(
            "duplicate", "Duplicate this sequence (built-ins become an editable copy)", "Duplicate",
            lambda: self.duplicate_sequence())
        self.rename_seq_btn = self._tool_button("rename", "Rename sequence", "Rename",
                                                lambda: self.rename_sequence())
        self.delete_seq_btn = self._tool_button("trash", "Delete sequence (can be undone)", "Delete",
                                                lambda: self.delete_sequence())
        for b in (self.new_seq_btn, self.dup_seq_btn, self.rename_seq_btn, self.delete_seq_btn):
            row.addWidget(b)

        # inline read-only banner for built-in sequences
        self.readonly_banner = QFrame()
        self.readonly_banner.setObjectName("TimelineReadOnlyBanner")
        brow = QHBoxLayout(self.readonly_banner)
        brow.setContentsMargins(SPACING, 0, 0, 0)
        brow.setSpacing(6)
        lock = icon("lock")
        if not lock.isNull():
            lock_lbl = QLabel()
            lock_lbl.setPixmap(lock.pixmap(14, 14))
            brow.addWidget(lock_lbl)
        banner_lbl = QLabel("Built-in firmware sequence · read-only")
        banner_lbl.setProperty("role", "caption")
        banner_lbl.setToolTip("Built-in sequences mirror the firmware and can't be changed here.")
        brow.addWidget(banner_lbl)
        self.duplicate_btn = QPushButton("Duplicate to edit")
        dup_icon = icon("duplicate")
        if not dup_icon.isNull():
            self.duplicate_btn.setIcon(dup_icon)
        self.duplicate_btn.setToolTip("Copy this built-in sequence into your project so you can edit it")
        self.duplicate_btn.setFocusPolicy(Qt.NoFocus)
        self.duplicate_btn.clicked.connect(lambda: self.duplicate_sequence())
        brow.addWidget(self.duplicate_btn)
        self.readonly_banner.setVisible(False)
        row.addWidget(self.readonly_banner)

        row.addStretch(1)
        self.undo_btn = self._tool_button("undo", "Undo", "Undo", self.undo)
        self.redo_btn = self._tool_button("redo", "Redo", "Redo", self.redo)
        self.undo_btn.setEnabled(False)
        self.redo_btn.setEnabled(False)
        row.addWidget(self.undo_btn)
        row.addWidget(self.redo_btn)
        root.addWidget(seq_bar)

        # -- row 2: transport + frame tools + zoom ---------------------------
        bar = QFrame()
        bar.setProperty("role", "toolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(SPACING, 4, SPACING, 4)
        row.setSpacing(4)

        self.start_btn = self._tool_button("skip-start", "Go to start (Home)", "|<", self.go_to_start)
        self.step_back_btn = self._tool_button("step-back", "Previous frame (Left)", "<",
                                               lambda: self.step(-1))
        self.play_btn = QPushButton("Play")
        self.play_btn.setProperty("variant", "primary")
        self.play_btn.setFocusPolicy(Qt.NoFocus)
        self.play_btn.setMinimumWidth(84)
        self.play_btn.clicked.connect(self.toggle_playback)
        self.step_fwd_btn = self._tool_button("step-forward", "Next frame (Right)", ">",
                                              lambda: self.step(1))
        self.stop_btn = self._tool_button("stop", "Stop and return to start", "Stop", self.stop)
        self.loop_btn = self._tool_button("loop", "Loop playback (L)", "Loop", checkable=True)
        self.loop_btn.setChecked(self._loop)
        self.loop_btn.toggled.connect(self.set_loop)
        for b in (self.start_btn, self.step_back_btn, self.play_btn, self.step_fwd_btn,
                  self.stop_btn, self.loop_btn):
            row.addWidget(b)

        self.speed_combo = QComboBox()
        for v in SPEEDS:
            self.speed_combo.addItem(_speed_label(v), v)
        self.speed_combo.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_combo.setToolTip("Playback speed")
        self.speed_combo.setFocusPolicy(Qt.ClickFocus)
        self.speed_combo.setFixedWidth(72)
        self.speed_combo.currentIndexChanged.connect(self._on_speed_changed)
        self.speed_combo.activated.connect(lambda _i: self.canvas.setFocus())
        row.addWidget(self.speed_combo)

        row.addSpacing(SPACING)
        self.time_label = QLabel()
        self.time_label.setProperty("role", "mono")
        self.time_label.setToolTip(
            "Playhead time / sequence length, and the frame under the playhead.\n"
            "A frame's pose is reached at the END of its block (the diamond marker).")
        self.time_label.setMinimumWidth(230)
        row.addWidget(self.time_label)

        row.addStretch(1)
        self.add_frame_btn = QPushButton("Add Frame")
        ico = icon("plus")
        if not ico.isNull():
            self.add_frame_btn.setIcon(ico)
        else:
            self.add_frame_btn.setText("+ Add Frame")
        self.add_frame_btn.setFocusPolicy(Qt.NoFocus)
        self.add_frame_btn.setToolTip(
            "Insert a copy of the selected frame right after it, then pose it (Insert)")
        self.add_frame_btn.clicked.connect(self.add_frame)
        row.addWidget(self.add_frame_btn)
        row.addWidget(self._separator())

        self.zoom_out_btn = self._tool_button("zoom-out", "Zoom out (-)", "-", self.zoom_out)
        self.zoom_in_btn = self._tool_button("zoom-in", "Zoom in (+)   Ctrl+wheel zooms at the cursor",
                                             "+", self.zoom_in)
        self.fit_btn = self._tool_button("fit", "Zoom to fit (F)", "Fit", self.zoom_fit)
        self.tracks_btn = self._tool_button("sliders", "Show servo tracks (T)", "Tracks",
                                            checkable=True)
        self.tracks_btn.setChecked(TimelineWidget._session_tracks_visible)
        self.tracks_btn.toggled.connect(self.set_tracks_visible)
        for b in (self.zoom_out_btn, self.zoom_in_btn, self.fit_btn, self.tracks_btn):
            row.addWidget(b)
        root.addWidget(bar)

        # -- canvas + scrollbar ----------------------------------------------
        self.hbar = QScrollBar(Qt.Horizontal)
        self.hbar.setFocusPolicy(Qt.NoFocus)
        self.canvas = TimelineCanvas(self, self.hbar)
        self.canvas.show_tracks = TimelineWidget._session_tracks_visible
        self.canvas._apply_height()
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.view = self.canvas             # old attribute name
        root.addWidget(self.canvas, 100)    # servo tracks grow with the dock, up to a cap
        root.addWidget(self.hbar)
        root.addStretch(1)
        self.setFocusProxy(self.canvas)

    # ======================================================================
    # sequences
    # ======================================================================
    def load_project_sequences(self, sequences: dict[str, Sequence]) -> None:
        self.pause()
        self.undo_stack.clear()
        self._view_states.clear()
        self.sequences = sequences
        self.current_seq_name = None
        self._rebuild_combo()

    def current_sequence(self) -> Sequence | None:
        name = self.current_seq_name
        if name is None:
            return None
        if name in self.sequences:
            return self.sequences[name]
        return self.builtin_sequences.get(name)

    def current_is_builtin(self) -> bool:
        return (self.current_seq_name in self.builtin_sequences
                and self.current_seq_name not in self.sequences)

    def sequence_names(self) -> list[str]:
        return sorted(self.sequences)

    def set_current_sequence(self, name: str) -> bool:
        idx = self.seq_combo.findData(name)
        if idx < 0:
            return False
        if self.seq_combo.currentIndex() == idx:
            if self.current_seq_name != name:
                self._show_sequence(name)
        else:
            self.seq_combo.setCurrentIndex(idx)
        return True

    def _rebuild_combo(self, select: str | None = None) -> None:
        select = select or self.current_seq_name
        combo = self.seq_combo
        combo.blockSignals(True)
        combo.clear()
        model = combo.model()
        header_font = QFont(combo.font())
        header_font.setBold(True)
        header_font.setPointSizeF(max(6.5, header_font.pointSizeF() * 0.82))

        def header(text: str) -> None:
            combo.addItem(text)
            item = model.item(combo.count() - 1)
            item.setFlags(Qt.NoItemFlags)
            item.setFont(header_font)
            item.setData(QColor(FG_MUTED), Qt.ForegroundRole)

        header("MY SEQUENCES")
        for name in sorted(self.sequences):
            combo.addItem(name, name)
        builtin_names = [n for n in BUILTIN_ORDER
                         if n in self.builtin_sequences and n not in self.sequences]
        if builtin_names:
            combo.insertSeparator(combo.count())
            header("BUILT-IN (FIRMWARE · READ-ONLY)")
            lock = icon("lock")
            for name in builtin_names:
                if lock.isNull():
                    combo.addItem(name, name)
                else:
                    combo.addItem(lock, name, name)
                model.item(combo.count() - 1).setData(QColor(FG_DIM), Qt.ForegroundRole)

        idx = combo.findData(select) if select else -1
        if idx < 0:
            idx = next((i for i in range(combo.count()) if combo.itemData(i)), -1)
        combo.setCurrentIndex(idx)
        combo.blockSignals(False)
        if idx >= 0:
            self._show_sequence(combo.itemData(idx))
        else:
            self._show_sequence(None)

    def _on_sequence_selected(self, index: int) -> None:
        name = self.seq_combo.itemData(index)
        if name:
            self._show_sequence(name)

    def _show_sequence(self, name, select_first: bool = True) -> None:
        if self._playing:
            self.pause()
        self.canvas.cancel_drag()
        prev = self.current_seq_name
        if prev is not None and prev != name and (prev in self.sequences or prev in self.builtin_sequences):
            self._view_states[prev] = self.canvas.view_state()
        self.current_seq_name = name
        seq = self.current_sequence()
        self._selection = set()
        self.selected_index = None
        self._anchor = None
        self._attached = False
        self._cur_idx = None
        self._last_face = _UNSET
        self._play_ms = 0.0
        self._update_edit_enabled()
        self.canvas.set_sequence(seq.frames if seq else None, self.current_is_builtin())
        state = self._view_states.get(name)
        if state:
            self.canvas.set_view(*state)
        else:
            self.canvas.request_fit()
        if name != prev and name is not None:
            self.currentSequenceChanged.emit(name)
        if seq and seq.frames and select_first:
            # frame 1 selected, but the playhead starts at 0 (frame 1 holds its
            # pose for its whole block) so Play starts from the very top
            self._selection = {0}
            self.selected_index = 0
            self._anchor = 0
            self.canvas.selection_changed()
            self.frameSelected.emit(0)
            self._set_playhead(0.0, force=True)
        else:
            self._set_playhead(0.0, emit=False)

    def refresh(self) -> None:
        """Re-read the current sequence after its frames were changed from
        outside the timeline API (keeps zoom, re-validates the selection)."""
        seq = self.current_sequence()
        self.canvas.cancel_drag()
        self.canvas.set_sequence(seq.frames if seq else None, self.current_is_builtin())
        self._cur_idx = None
        n = len(seq.frames) if seq else 0
        self._selection = {i for i in self._selection if i < n}
        if self.selected_index not in self._selection:
            self.selected_index = max(self._selection) if self._selection else None
        self._after_timing_change()

    def _sequence_renamed(self, src: str, dst: str) -> None:
        self._view_states.pop(src, None)
        if self.current_seq_name == src:
            self._view_states[dst] = self.canvas.view_state()

    def _update_edit_enabled(self) -> None:
        has = self.current_sequence() is not None
        ro = self.current_is_builtin()
        self.readonly_banner.setVisible(ro)
        self.add_frame_btn.setEnabled(has and not ro)
        self.rename_seq_btn.setEnabled(has and not ro)
        self.delete_seq_btn.setEnabled(has and not ro)
        self.dup_seq_btn.setEnabled(has)
        self.rename_seq_btn.setToolTip("Built-in sequences can't be renamed" if ro else "Rename sequence")
        self.delete_seq_btn.setToolTip("Built-in sequences can't be deleted" if ro
                                       else "Delete sequence (can be undone)")

    def _notify_blocked(self, message: str = READ_ONLY_MESSAGE) -> None:
        """Emit editBlocked at most once per BLOCKED_THROTTLE_MS so a slider or
        mouse drag on a read-only sequence produces one toast, not hundreds."""
        if self._blocked_clock.isValid() and self._blocked_clock.elapsed() < BLOCKED_THROTTLE_MS:
            return
        self._blocked_clock.start()
        self.editBlocked.emit(message)

    def _name_taken(self, name: str) -> bool:
        return name in self.sequences or name in self.builtin_sequences

    def _unique_name(self, base: str) -> str:
        if not self._name_taken(base):
            return base
        i = 2
        while self._name_taken(f"{base}_{i}"):
            i += 1
        return f"{base}_{i}"

    def _ask_name(self, title: str, default: str):
        name, ok = QInputDialog.getText(self, title, "Name:", text=default)
        if not ok:
            return None
        name = name.strip()
        if not name:
            return None
        return name

    def new_sequence(self, name: str | None = None) -> str | None:
        """Create a sequence with one neutral frame. Prompts when name is None."""
        interactive = name is None
        if interactive:
            name = self._ask_name("New Sequence", self._unique_name("sequence"))
            if name is None:
                return None
        if self._name_taken(name):
            self._name_conflict(name, interactive)
            return None
        self._push(AddSequenceCommand(
            self, Sequence(name, [Frame()]), f"New Sequence '{name}'", self.current_seq_name))
        return name

    def duplicate_sequence(self, name: str | None = None) -> str | None:
        """Editable copy of the current sequence (custom or built-in)."""
        seq = self.current_sequence()
        if not seq:
            return None
        if name is None:
            name = self._unique_name(f"{seq.name}_custom" if self.current_is_builtin()
                                     else f"{seq.name}_copy")
        elif self._name_taken(name):
            self._name_conflict(name, False)
            return None
        self._push(AddSequenceCommand(
            self, Sequence(name, [f.clone() for f in seq.frames]),
            f"Duplicate Sequence '{seq.name}'", self.current_seq_name))
        return name

    def rename_sequence(self, name: str | None = None) -> bool:
        seq = self.current_sequence()
        if not seq:
            return False
        if self.current_is_builtin():
            self._notify_blocked()
            return False
        interactive = name is None
        if interactive:
            name = self._ask_name("Rename Sequence", seq.name)
            if name is None:
                return False
        if name == seq.name:
            return False
        if self._name_taken(name):
            self._name_conflict(name, interactive)
            return False
        self._push(RenameSequenceCommand(self, seq, name))
        return True

    def delete_sequence(self) -> bool:
        seq = self.current_sequence()
        if not seq:
            return False
        if self.current_is_builtin():
            self._notify_blocked()
            return False
        if len(self.sequences) <= 1:
            self.editBlocked.emit("At least one custom sequence must remain.")
            return False
        names = sorted(self.sequences)
        i = names.index(seq.name)
        after = names[i + 1] if i + 1 < len(names) else names[i - 1]
        self._push(DeleteSequenceCommand(self, seq, after))
        return True

    def _name_conflict(self, name: str, interactive: bool) -> None:
        msg = f"A sequence named '{name}' already exists."
        if interactive:
            QMessageBox.warning(self, "Name in use", msg)
        else:
            self.editBlocked.emit(msg)

    # old private names (menus / scripts written against the previous timeline)
    def _new_sequence(self) -> None:
        self.new_sequence()

    def _rename_sequence(self) -> None:
        self.rename_sequence()

    def _delete_sequence(self) -> None:
        self.delete_sequence()

    def _duplicate_builtin_to_custom(self) -> None:
        self.duplicate_sequence()

    # ======================================================================
    # selection
    # ======================================================================
    def selected_indices(self) -> list[int]:
        return sorted(self._selection)

    def select_frame(self, index: int) -> None:
        """Select one frame, park the playhead on its key (block end) and
        announce it (frameSelected, then poseAtIndex)."""
        seq = self.current_sequence()
        if not seq or not (0 <= index < len(seq.frames)):
            return
        if self._playing:
            self.pause()
        self._apply_selection([index], index)

    def select_all(self) -> None:
        seq = self.current_sequence()
        if not seq or not seq.frames:
            return
        n = len(seq.frames)
        primary = self.selected_index if self.selected_index is not None else n - 1
        self._selection = set(range(n))
        self.selected_index = primary
        self.canvas.selection_changed()

    def _sel_snapshot(self):
        return (sorted(self._selection), self.selected_index)

    def _apply_selection(self, indices, primary, emit: bool = True) -> None:
        seq = self.current_sequence()
        n = len(seq.frames) if seq else 0
        idx = sorted({i for i in indices if 0 <= i < n})
        self._selection = set(idx)
        if primary not in self._selection:
            primary = idx[-1] if idx else None
        self.selected_index = primary
        self._anchor = primary
        self.canvas.selection_changed()
        if primary is None:
            self._attached = False
            return
        self.canvas.ensure_frame_visible(primary)
        self._attached = True
        if emit:
            self.frameSelected.emit(primary)
        self._set_playhead(self.canvas.tm.ends[primary], force=True)

    def _click_select(self, index: int, ctrl: bool, shift: bool, defer: bool = True) -> bool:
        """Mouse selection. Returns True when a plain click landed on a frame
        of a multi-selection: the group is kept (it may be about to be
        dragged) and the caller collapses it on release if no drag happened."""
        if self._playing:
            self.pause()
        sel = self._selection
        if ctrl:
            new = set(sel)
            if index in new:
                new.discard(index)
                primary = self.selected_index if self.selected_index in new else (max(new) if new else None)
            else:
                new.add(index)
                primary = index
            self._apply_selection(new, primary)      # anchor follows the primary
        elif shift and self._anchor is not None:
            anchor = self._anchor
            lo, hi = (anchor, index) if anchor <= index else (index, anchor)
            self._apply_selection(range(lo, hi + 1), index)
            self._anchor = anchor
        else:
            if defer and index in sel and len(sel) > 1:
                self._apply_selection(sel, index)
                return True
            self._apply_selection([index], index)
        return False

    # ======================================================================
    # frame editing (all undoable)
    # ======================================================================
    def _editable_seq(self) -> Sequence | None:
        seq = self.current_sequence()
        if seq is None:
            return None
        if self.current_is_builtin():
            self._notify_blocked()
            return None
        return seq

    def _editable_selected(self):
        seq = self.current_sequence()
        i = self.selected_index
        if seq is None or i is None or not (0 <= i < len(seq.frames)):
            return None, None
        if self.current_is_builtin():
            self._notify_blocked()
            return None, None
        return seq, i

    def begin_edit(self, label: str = "Edit Pose") -> None:
        """Open a coalescing bracket: every angle/field edit of the same frame
        until end_edit() becomes ONE undo step named ``label`` (use around an
        external drag, e.g. posing a limb in the 3D view)."""
        self._edit_serial += 1
        self._edit_group = self._edit_serial
        self._edit_label = label

    def end_edit(self) -> None:
        self._edit_group = None
        self._edit_label = None

    def update_selected_frame_angles(self, angles: dict) -> None:
        """Live edit of the selected frame's pose. O(1); emits sequencesChanged
        only (never frameSelected / poseAtIndex)."""
        seq, i = self._editable_selected()
        if seq is None:
            return
        frame = seq.frames[i]
        new = dict(angles)
        if new == frame.angles:
            return
        self._push(SetAnglesCommand(
            self, seq, i, dict(frame.angles), new, self._edit_label or "Edit Pose",
            self._edit_group, self._edit_clock.elapsed()))

    def update_selected_frame_fields(self, delay_ms: int, face, face_mode: str) -> None:
        seq, i = self._editable_selected()
        if seq is None:
            return
        frame = seq.frames[i]
        old = (frame.delay_ms, frame.face, frame.face_mode)
        new = (int(delay_ms), face or None, face_mode)
        if new == old:
            return
        if old[0] != new[0] and old[1:] == new[1:]:
            text = "Change Delay"
        elif old[0] == new[0]:
            text = "Change Face"
        else:
            text = "Edit Frame"
        self._push(SetFieldsCommand(
            self, seq, i, old, new, self._edit_label or text,
            self._edit_group, self._edit_clock.elapsed()))

    def add_frame(self) -> None:
        """Insert a copy of the selected frame right after it (append a copy
        of the last frame when nothing is selected) and select the new one."""
        seq = self._editable_seq()
        if seq is None:
            return
        frames = seq.frames
        i = self.selected_index
        if i is not None and 0 <= i < len(frames):
            src, at = frames[i], i + 1
        elif frames:
            src, at = frames[-1], len(frames)
        else:
            src, at = Frame(), 0
        self._push_insert(seq, at, [src.clone()], "Add Frame")

    _add_frame_from_current = add_frame      # old name, still used by menus

    def add_frame_capture(self, frame: Frame) -> None:
        """Append the given frame to the end and select it."""
        seq = self._editable_seq()
        if seq is None:
            return
        self._push_insert(seq, len(seq.frames), [frame], "Add Frame")

    def insert_blank(self, index: int) -> None:
        seq = self._editable_seq()
        if seq is None:
            return
        index = max(0, min(len(seq.frames), index))
        self._push_insert(seq, index, [Frame()], "Insert Blank Frame")

    def _push_insert(self, seq, at: int, frames: list, text: str) -> None:
        self._push(InsertFramesCommand(self, seq, at, frames, text, self._sel_snapshot()))

    def duplicate_selected(self) -> None:
        seq = self._editable_seq()
        if seq is None:
            return
        sel = self.selected_indices()
        if not sel:
            return
        clones = [seq.frames[i].clone() for i in sel]
        self._push_insert(seq, sel[-1] + 1, clones,
                          "Duplicate Frame" if len(sel) == 1 else f"Duplicate {len(sel)} Frames")

    def duplicate_frame(self, index: int) -> None:
        seq = self._editable_seq()
        if seq is None or not (0 <= index < len(seq.frames)):
            return
        self._push_insert(seq, index + 1, [seq.frames[index].clone()], "Duplicate Frame")

    def delete_selected(self) -> None:
        seq = self._editable_seq()
        if seq is None:
            return
        self._delete(seq, self.selected_indices())

    def delete_frame(self, index: int) -> None:
        seq = self._editable_seq()
        if seq is None or not (0 <= index < len(seq.frames)):
            return
        self._delete(seq, [index])

    def _delete(self, seq, indices) -> None:
        if not indices:
            return
        if len(indices) >= len(seq.frames):
            self.editBlocked.emit("A sequence needs at least one frame.")
            return
        text = "Delete Frame" if len(indices) == 1 else f"Delete {len(indices)} Frames"
        self._push(RemoveFramesCommand(self, seq, indices, text, self._sel_snapshot()))

    def move_frames(self, indices, gap: int) -> None:
        """Move frames (kept in order) so they sit at insertion position
        ``gap`` (0..n, counted in the current order)."""
        seq = self._editable_seq()
        if seq is None:
            return
        n = len(seq.frames)
        moving = sorted({i for i in indices if 0 <= i < n})
        if not moving:
            return
        gap = max(0, min(n, gap))
        mset = set(moving)
        rest = [i for i in range(n) if i not in mset]
        pos = gap - sum(1 for i in moving if i < gap)
        perm = rest[:pos] + moving + rest[pos:]
        if perm == list(range(n)):
            return
        new_sel = list(range(pos, pos + len(moving)))
        prim = self.selected_index
        new_prim = pos + moving.index(prim) if prim in mset else new_sel[-1]
        text = "Move Frame" if len(moving) == 1 else f"Move {len(moving)} Frames"
        self._push(MoveFramesCommand(self, seq, perm, text,
                                               (new_sel, new_prim), self._sel_snapshot()))

    def reorder_frame(self, index: int, target: int) -> None:
        """Old API: move one frame so it ends up at index ``target``."""
        seq = self.current_sequence()
        if not seq or not (0 <= target < len(seq.frames)) or target == index:
            return
        self.move_frames([index], target if target < index else target + 1)

    def retime_frame(self, index: int, delay_ms: int) -> None:
        seq = self._editable_seq()
        if seq is None or not (0 <= index < len(seq.frames)):
            return
        old = seq.frames[index].delay_ms
        if old == delay_ms:
            return
        self._push(SetDelaysCommand(self, seq, {index: old}, {index: int(delay_ms)}, index))

    # -- retime drag protocol (canvas) -----------------------------------------
    def _retime_begin(self, index: int):
        """-> {frame index: original delay} for the frames this drag will
        retime (the whole selection when the grabbed frame is part of it)."""
        seq = self._editable_seq()
        if seq is None:
            return None
        if self._playing:
            self.pause()
        if index not in self._selection:
            self._apply_selection([index], index)
        elif self.selected_index != index:
            self._apply_selection(self._selection, index)
        return {i: seq.frames[i].delay_ms for i in sorted(self._selection)}

    def _retime_preview(self, delays: dict) -> None:
        seq = self.current_sequence()
        for i, d in delays.items():
            seq.frames[i].delay_ms = d
        self.canvas.relayout(delays_only=True)
        self._after_timing_change()

    def _retime_finish(self, orig: dict, commit: bool) -> None:
        seq = self.current_sequence()
        if seq is None:
            return
        new = {i: seq.frames[i].delay_ms for i in orig}
        if not commit:
            if new != orig:
                self._retime_preview(orig)
            return
        if new == orig:
            return
        text = "Retime Frame" if len(orig) == 1 else f"Retime {len(orig)} Frames"
        self._push(SetDelaysCommand(self, seq, orig, new, self.selected_index, text))
        if self.selected_index is not None:
            self.frameSelected.emit(self.selected_index)    # inspector reloads the delay

    # -- clipboard ---------------------------------------------------------------
    def copy(self) -> None:
        seq = self.current_sequence()
        sel = self.selected_indices()
        if not seq or not sel:
            return
        self._clipboard = [seq.frames[i].clone() for i in sel]
        try:
            QGuiApplication.clipboard().setText(frames_to_text(self._clipboard))
        except RuntimeError:
            pass

    def cut(self) -> None:
        seq = self._editable_seq()
        if seq is None:
            return
        sel = self.selected_indices()
        if not sel:
            return
        if len(sel) >= len(seq.frames):
            self.editBlocked.emit("A sequence needs at least one frame.")
            return
        self.copy()
        text = "Cut Frame" if len(sel) == 1 else f"Cut {len(sel)} Frames"
        self._push(RemoveFramesCommand(self, seq, sel, text, self._sel_snapshot()))

    def paste(self) -> None:
        """Paste the copied frames after the selection (or at the end)."""
        seq = self._editable_seq()
        if seq is None:
            return
        frames = None
        try:
            frames = frames_from_text(QGuiApplication.clipboard().text())
        except RuntimeError:
            frames = None
        if frames is None:
            frames = [f.clone() for f in self._clipboard]
        if not frames:
            return
        sel = self.selected_indices()
        at = sel[-1] + 1 if sel else len(seq.frames)
        self._push_insert(seq, at, frames,
                          "Paste Frame" if len(frames) == 1 else f"Paste {len(frames)} Frames")

    def can_paste(self) -> bool:
        if self._clipboard:
            return True
        try:
            return frames_from_text(QGuiApplication.clipboard().text()) is not None
        except RuntimeError:
            return False

    # -- undo -----------------------------------------------------------------
    def undo(self) -> None:
        self.canvas.cancel_drag()
        self.undo_stack.undo()

    def redo(self) -> None:
        self.canvas.cancel_drag()
        self.undo_stack.redo()

    def _update_undo_tooltips(self, *_args) -> None:
        u, r = self.undo_stack.undoText(), self.undo_stack.redoText()
        self.undo_btn.setToolTip(f"Undo {u} (Ctrl+Z)" if u else "Undo (Ctrl+Z)")
        self.redo_btn.setToolTip(f"Redo {r} (Ctrl+Y)" if r else "Redo (Ctrl+Y)")

    def _push(self, cmd) -> None:
        if self._applying:
            # an edit requested re-entrantly from one of our own change
            # notifications (e.g. a slot reacting to frameSelected): pushing
            # inside QUndoStack.push()/undo()/redo() would corrupt the history
            return
        self.undo_stack.push(cmd)

    def _command_applied(self, cmd, undo: bool, first: bool) -> None:
        """Called by every command after it changed the data: refresh exactly
        what that kind of edit can have affected."""
        self._applying += 1
        try:
            self._refresh_after_command(cmd, undo, first)
        finally:
            self._applying -= 1

    def _refresh_after_command(self, cmd, undo: bool, first: bool) -> None:
        kind = cmd.kind
        if kind == "sequences":
            self._rebuild_combo(select=cmd.show_undo if undo else cmd.show_redo)
            self.sequencesChanged.emit()
            return
        seq = cmd.seq
        if self.current_sequence() is not seq:
            # history step for a sequence that isn't on screen: show it first
            idx = self.seq_combo.findData(seq.name)
            if idx >= 0:
                self.seq_combo.blockSignals(True)
                self.seq_combo.setCurrentIndex(idx)
                self.seq_combo.blockSignals(False)
                self._show_sequence(seq.name, select_first=False)
        canvas = self.canvas
        if kind == "angles":
            canvas.frame_angles_changed(cmd.index)
        elif kind == "fields":
            canvas.frame_fields_changed(cmd.index, cmd.delay_changed, cmd.face_changed)
            if cmd.delay_changed:
                self._after_timing_change()
        elif kind == "delays":
            canvas.relayout(delays_only=True)
            self._after_timing_change()
        else:
            canvas.relayout()
            self._cur_idx = None
        live = first and kind != "structure"
        if not live:
            sel = cmd.sel_undo if undo else cmd.sel_redo
            if sel is not None:
                self._apply_selection(sel[0], sel[1])
            else:
                self._apply_selection(self._selection, self.selected_index)
        self.sequencesChanged.emit()

    def _after_timing_change(self) -> None:
        """Durations changed: keep the playhead on the selected frame's key if
        it was parked there, otherwise just keep it inside the sequence."""
        tm = self.canvas.tm
        i = self.selected_index
        if self._attached and i is not None and 0 <= i < tm.n:
            self._play_ms = tm.ends[i]
        else:
            self._play_ms = max(0.0, min(tm.total_ms, self._play_ms))
        self.canvas.set_playhead(self._play_ms)
        self._update_readout()

    # ======================================================================
    # playhead / playback
    # ======================================================================
    def current_time_ms(self) -> float:
        return self._play_ms

    def total_time_ms(self) -> float:
        return self.canvas.tm.total_ms

    def is_playing(self) -> bool:
        return self._playing

    def playhead_frame_index(self) -> int:
        """Frame the playhead is in (-1 when there are no frames)."""
        return self.canvas.tm.index_at_time(self._play_ms)

    def loop_enabled(self) -> bool:
        return self._loop

    def set_loop(self, on: bool) -> None:
        self._loop = bool(on)
        if self.loop_btn.isChecked() != self._loop:
            self.loop_btn.setChecked(self._loop)

    def speed(self) -> float:
        return self._speed

    def set_speed(self, speed: float) -> None:
        self._speed = max(0.05, float(speed))
        idx = self.speed_combo.findData(self._speed)
        if idx >= 0 and idx != self.speed_combo.currentIndex():
            self.speed_combo.setCurrentIndex(idx)

    def _on_speed_changed(self, index: int) -> None:
        v = self.speed_combo.itemData(index)
        if v:
            self._speed = float(v)

    def _set_playhead(self, ms: float, emit: bool = True, force: bool = False,
                      follow: bool = False, readout: bool = True) -> None:
        tm = self.canvas.tm
        ms = max(0.0, min(tm.total_ms, ms))
        self._play_ms = ms
        self.canvas.set_playhead(ms, follow)
        if readout:
            self._update_readout()
        seq = self.current_sequence()
        if not emit or seq is None or not seq.frames or tm.n != len(seq.frames):
            return
        angles, i = pose_at(seq.frames, tm, ms)
        face, mode = self.canvas._eff[i]
        # the face is only announced when it changes (or on an explicit
        # seek/select): resolving bitmaps 60x a second would be wasteful
        if force or face != self._last_face:
            self._last_face = face
            self.poseAtIndex.emit(angles, face, mode)
        else:
            self.poseAtIndex.emit(angles, None, mode)
        if force or i != self._cur_idx:
            self._cur_idx = i
            self.frameTargetChanged.emit(dict(seq.frames[i].angles))

    def seek_ms(self, ms: float) -> None:
        """Move the playhead (does not change the selection). Emits
        poseAtIndex, and frameTargetChanged if a different frame is entered."""
        self._attached = False
        self._set_playhead(ms, follow=True)
        if self._playing:
            self._play_clock.restart()

    def _user_scrub(self, ms: float) -> None:
        if self._playing:
            self.pause()
        self._attached = False
        self._set_playhead(ms)

    def go_to_start(self) -> None:
        self.seek_ms(0.0)
        self.canvas.set_scroll(0.0)

    def go_to_end(self) -> None:
        self.seek_ms(self.total_time_ms())

    def step(self, direction: int, extend: bool = False) -> None:
        """Select the previous/next frame relative to the playhead (extend =
        grow the selection, Shift+arrow)."""
        seq = self.current_sequence()
        tm = self.canvas.tm
        if not seq or tm.n == 0:
            return
        if self._playing:
            self.pause()
        if extend and self.selected_index is not None:
            target = max(0, min(tm.n - 1, self.selected_index + direction))
            anchor = self._anchor if self._anchor is not None else self.selected_index
            lo, hi = (anchor, target) if anchor <= target else (target, anchor)
            self._apply_selection(range(lo, hi + 1), target)
            self._anchor = anchor
            return
        cur = tm.index_at_time(self._play_ms)
        on_key = abs(self._play_ms - tm.ends[cur]) < 0.5
        if direction > 0:
            target = cur + 1 if on_key else cur
        else:
            target = cur - 1
        target = max(0, min(tm.n - 1, target))
        self._apply_selection([target], target)

    def toggle_playback(self) -> None:
        if self._playing:
            self.pause()
        else:
            self.play()

    def play(self) -> None:
        """Start (or resume) from the playhead; from the top if it is at the end."""
        seq = self.current_sequence()
        if self._playing or not seq or not seq.frames:
            return
        self.canvas.cancel_drag()
        total = self.total_time_ms()
        if self._play_ms >= total - 0.5:
            self._play_ms = 0.0
        self._attached = False
        self._playing = True
        self._tick_count = 0
        self._play_clock.start()
        self._play_timer.start()
        self._update_transport_ui()
        self.playbackStateChanged.emit(True)
        self.playbackStarted.emit(self._play_ms)
        self._set_playhead(self._play_ms, force=True, follow=True)

    def pause(self) -> None:
        if not self._playing:
            return
        self._playing = False
        self._play_timer.stop()
        self._update_transport_ui()
        self._update_readout()
        self.playbackStateChanged.emit(False)

    def stop(self) -> None:
        """Pause and return the playhead to the start."""
        self.pause()
        self._attached = False
        self._set_playhead(0.0, force=True)
        self.canvas.set_scroll(0.0)

    def _tick(self) -> None:
        seq = self.current_sequence()
        if not seq or not seq.frames:
            self.pause()
            return
        dt = self._play_clock.nsecsElapsed() / 1e6
        self._play_clock.restart()
        if dt > MAX_TICK_DT_MS:
            dt = MAX_TICK_DT_MS
        t = self._play_ms + dt * self._speed
        total = self.total_time_ms()
        if t >= total:
            if self._loop and total > 0:
                t %= total
                self._cur_idx = None        # re-announce the frame target after wrapping
            else:
                self._set_playhead(total, follow=True)
                self.pause()
                return
        self._tick_count += 1
        self._set_playhead(t, follow=True, readout=(self._tick_count % 3 == 0))

    def _update_transport_ui(self) -> None:
        playing = self._playing
        ico = icon("pause" if playing else "play")
        self.play_btn.setText("Pause" if playing else "Play")
        if not ico.isNull():
            self.play_btn.setIcon(ico)
        self.play_btn.setToolTip("Pause (Space)" if playing else "Play from the playhead (Space)")

    def _update_readout(self) -> None:
        tm = self.canvas.tm
        n = tm.n
        cur = tm.index_at_time(self._play_ms) + 1 if n else 0
        self.time_label.setText(
            f"{format_clock(self._play_ms)} / {format_clock(tm.total_ms)} s"
            f"    frame {cur} / {n}")

    # old private names
    def _start_playback(self) -> None:
        self.play()

    def _stop_playback(self) -> None:
        self.pause()

    @property
    def _total_time_ms(self) -> float:
        return self.total_time_ms()

    def _time_to_x(self, ms: float) -> float:
        """Canvas x of a time (old scene-coordinate helper)."""
        return self.canvas.time_to_widget_x(ms)

    def _x_to_time(self, x: float) -> float:
        return self.canvas.widget_x_to_time(x)

    def _scrub_to_x(self, x: float) -> None:
        self._attached = False
        self._set_playhead(self.canvas.widget_x_to_time(x))

    # ======================================================================
    # zoom / view
    # ======================================================================
    def zoom_fit(self) -> None:
        self.canvas.zoom_fit()

    def zoom_in(self) -> None:
        self.canvas.zoom_by(1.25)

    def zoom_out(self) -> None:
        self.canvas.zoom_by(1 / 1.25)

    def tracks_visible(self) -> bool:
        return self.canvas.show_tracks

    def set_tracks_visible(self, on: bool) -> None:
        on = bool(on)
        TimelineWidget._session_tracks_visible = on
        self.canvas.set_tracks_visible(on)
        if self.tracks_btn.isChecked() != on:
            self.tracks_btn.setChecked(on)
        self.tracks_btn.setToolTip("Hide servo tracks (T)" if on else "Show servo tracks (T)")

    # ======================================================================
    # keyboard + context menu
    # ======================================================================
    def _key_action(self, event):
        key = event.key()
        mods = event.modifiers() & _MOD_MASK
        if mods == Qt.NoModifier:
            if key == Qt.Key_Space:
                return self.toggle_playback
            if key == Qt.Key_Left:
                return lambda: self.step(-1)
            if key == Qt.Key_Right:
                return lambda: self.step(1)
            if key == Qt.Key_Home:
                return self._select_first
            if key == Qt.Key_End:
                return self._select_last
            if key in (Qt.Key_Delete, Qt.Key_Backspace):
                return self.delete_selected
            if key == Qt.Key_Insert:
                return self.add_frame
            if key in (Qt.Key_Plus, Qt.Key_Equal):
                return self.zoom_in
            if key == Qt.Key_Minus:
                return self.zoom_out
            if key == Qt.Key_F:
                return self.zoom_fit
            if key == Qt.Key_L:
                return lambda: self.set_loop(not self._loop)
            if key == Qt.Key_T:
                return lambda: self.set_tracks_visible(not self.canvas.show_tracks)
            if key == Qt.Key_Escape and (self.canvas.is_dragging() or len(self._selection) > 1):
                return self._escape
        elif mods == Qt.ShiftModifier:
            if key == Qt.Key_Left:
                return lambda: self.step(-1, extend=True)
            if key == Qt.Key_Right:
                return lambda: self.step(1, extend=True)
            if key == Qt.Key_Plus:
                return self.zoom_in
        elif mods == Qt.ControlModifier:
            if key == Qt.Key_C:
                return self.copy
            if key == Qt.Key_X:
                return self.cut
            if key == Qt.Key_V:
                return self.paste
            if key == Qt.Key_D:
                return self.duplicate_selected
            if key == Qt.Key_A:
                return self.select_all
            if key == Qt.Key_Z:
                return self.undo
            if key == Qt.Key_Y:
                return self.redo
            if key == Qt.Key_0:
                return self.zoom_fit
        elif mods == (Qt.ControlModifier | Qt.ShiftModifier):
            if key == Qt.Key_Z:
                return self.redo
        return None

    def handles_key(self, event) -> bool:
        return self._key_action(event) is not None

    def handle_key(self, event) -> bool:
        """Run the timeline shortcut for a key event. True if it was one."""
        action = self._key_action(event)
        if action is None:
            return False
        action()
        event.accept()
        return True

    def keyPressEvent(self, event) -> None:
        if not self.handle_key(event):
            super().keyPressEvent(event)

    def _escape(self) -> None:
        if self.canvas.cancel_drag():
            return
        if len(self._selection) > 1 and self.selected_index is not None:
            self._apply_selection([self.selected_index], self.selected_index)

    def _select_first(self) -> None:
        seq = self.current_sequence()
        if seq and seq.frames:
            if self._playing:
                self.pause()
            self._apply_selection([0], 0)
            self.canvas.set_scroll(0.0)

    def _select_last(self) -> None:
        seq = self.current_sequence()
        if seq and seq.frames:
            self.select_frame(len(seq.frames) - 1)

    def build_context_menu(self, index: int | None) -> QMenu:
        """Context menu for frame ``index`` (None = empty lane)."""
        menu = QMenu(self)
        ro = self.current_is_builtin()
        seq = self.current_sequence()
        n = len(seq.frames) if seq else 0
        count = len(self._selection)

        def add(icon_name, text, slot, enabled=True):
            act = menu.addAction(icon(icon_name), text)
            act.triggered.connect(lambda _checked=False: slot())
            act.setEnabled(enabled)
            return act

        if index is not None:
            many = count > 1
            add("plus", "Add Frame After (copy)", self.add_frame, not ro)
            add("duplicate", f"Duplicate {count} Frames\tCtrl+D" if many else "Duplicate\tCtrl+D",
                self.duplicate_selected, not ro)
            menu.addSeparator()
            add("cut", "Cut\tCtrl+X", self.cut, not ro and count < n)
            add("copy", "Copy\tCtrl+C", self.copy)
            add("paste", "Paste After\tCtrl+V", self.paste, not ro and self.can_paste())
            menu.addSeparator()
            add("plus", "Insert Blank Before", lambda: self.insert_blank(index), not ro)
            add("plus", "Insert Blank After", lambda: self.insert_blank(index + 1), not ro)
            menu.addSeparator()
            add("trash", f"Delete {count} Frames\tDel" if many else "Delete\tDel",
                self.delete_selected, not ro and count < n)
        else:
            add("plus", "Add Frame", self.add_frame, not ro)
            add("plus", "Insert Blank at End", lambda: self.insert_blank(n), not ro)
            add("paste", "Paste\tCtrl+V", self.paste, not ro and self.can_paste())
            menu.addSeparator()
            add("more", "Select All\tCtrl+A", self.select_all, n > 0)
            add("fit", "Zoom to Fit\tF", self.zoom_fit)
        if ro:
            menu.addSeparator()
            add("duplicate", "Duplicate Sequence to Edit", self.duplicate_sequence)
        return menu

    def _context_menu(self, index, global_pos) -> None:
        if index is not None and index not in self._selection:
            if self._playing:
                self.pause()
            self._apply_selection([index], index)
        menu = self.build_context_menu(index)
        menu.exec(global_pos)
        menu.deleteLater()
