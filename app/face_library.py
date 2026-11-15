"""Face Library: a searchable thumbnail grid of every face (project faces
plus the read-only firmware built-ins, or the contents of an open
face-bitmaps.h), with category chips, hover playback for animated faces
and the actions to create / edit / duplicate / delete / assign them.

The Face Editor is opened modeless from here; while it is open its live
pixels are re-emitted through ``facePixelsPreview`` so the 3D robot can
mirror the painting.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent, QPoint, QPointF, QRect, QRectF, QSize, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListView, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QSizePolicy, QStyle, QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget,
)

from . import face_render as fr
from .builtin_faces import BUILTIN_FACES
from .face_bitmaps import FaceBitmapsFile
from .face_editor import FaceEditorDialog, frame_bytes_to_image  # noqa: F401  (re-export)
from .face_widgets import (
    FlowLayout, chip_button, get_icon, set_button_tooltip, suite_stylesheet,
    tool_button,
)
from .project import Face
from .servo_model import FACE_CATEGORIES, FACE_CATEGORY_LABELS
from .widgets import EmptyState
from .theme import (
    ACCENT, ACCENT_SOFT, BG_ELEVATED, BORDER, BORDER_STRONG, FG, FG_DIM,
    FG_MUTED, FONT_FAMILY, PANEL, RADIUS, WARNING,
)

_MIN_THUMB_W = 96
_MAX_THUMB_W = 128
_CARD_PAD = 6          # card inset around the thumbnail
_CELL_GAP = 6          # gap between cards
_NAME_H = 22


class _ElidedLabel(QLabel):
    """QLabel that elides instead of clipping (``text()`` stays intact)."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        fm = QFontMetrics(self.font())
        p.setPen(self.palette().windowText().color())
        p.drawText(self.rect(), Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(self.text(), Qt.ElideMiddle, self.width()))
        p.end()


class _FaceDelegate(QStyledItemDelegate):
    """Paints one face card: OLED thumbnail, name and state badges."""

    def __init__(self, panel: "FaceLibraryPanel"):
        super().__init__(panel)
        self.panel = panel

    def sizeHint(self, option, index) -> QSize:
        return self.panel._cell_size

    def paint(self, p: QPainter, option, index) -> None:
        panel = self.panel
        name = index.data(Qt.UserRole)
        face = panel.get_face(name) if name else None
        if face is None:
            return
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        tw = panel._thumb_w
        th = tw // 2
        cell = option.rect
        card_w = tw + 2 * _CARD_PAD
        card_h = th + _CARD_PAD + _NAME_H
        card = QRect(cell.x() + (cell.width() - card_w) // 2,
                     cell.y() + (cell.height() - card_h) // 2, card_w, card_h)

        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        if selected:
            p.setPen(QPen(QColor(ACCENT), 1.5))
            p.setBrush(QColor(ACCENT_SOFT))
        elif hovered:
            p.setPen(QPen(QColor(BORDER_STRONG), 1))
            p.setBrush(QColor(BG_ELEVATED))
        else:
            p.setPen(QPen(QColor(BORDER), 1))
            p.setBrush(QColor(PANEL))
        p.drawRoundedRect(QRectF(card).adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)

        # thumbnail (animated faces advance while hovered)
        frames = face.frames or [b""]
        fi = panel._hover_frame % len(frames) if name == panel._hover_name else 0
        thumb = QRect(card.x() + _CARD_PAD, card.y() + _CARD_PAD, tw, th)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.drawPixmap(thumb.topLeft(), fr.oled_pixmap(frames[fi], tw, th, panel.devicePixelRatioF()))
        p.setRenderHint(QPainter.Antialiasing, True)

        # name
        font = QFont(FONT_FAMILY)
        font.setPixelSize(11)
        font.setWeight(QFont.DemiBold if selected else QFont.Normal)
        p.setFont(font)
        builtin = panel.is_builtin(name)
        name_rect = QRect(card.x() + _CARD_PAD, thumb.bottom() + 1, tw, _NAME_H)
        name_fm = QFontMetrics(font)
        lock_w = 13 if builtin else 0   # read-only marker sits before the name
        text = name_fm.elidedText(name, Qt.ElideRight, name_rect.width() - lock_w)
        text_w = name_fm.horizontalAdvance(text)
        x0 = name_rect.x() + (name_rect.width() - text_w - lock_w) // 2
        if builtin:
            self._lock(p, x0, name_rect.y() + (_NAME_H - 11) // 2)
        p.setPen(QColor(FG if (selected or not builtin) else FG_DIM))
        p.drawText(QRect(x0 + lock_w, name_rect.y(), text_w + 2, _NAME_H),
                   Qt.AlignVCenter | Qt.AlignLeft, text)

        # badges
        badge_font = QFont(FONT_FAMILY)
        badge_font.setPixelSize(9)
        badge_font.setBold(True)
        p.setFont(badge_font)
        fm = QFontMetrics(badge_font)

        def pill(text: str, right: int, top: int, color: str, glyph: str | None = None) -> None:
            w = fm.horizontalAdvance(text) + 8 + (8 if glyph else 0)
            r = QRectF(right - w, top, w, 13)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 190))
            p.drawRoundedRect(r, 6.5, 6.5)
            x = r.x() + 4
            if glyph == "play":
                p.setBrush(QColor(color))
                cy = r.center().y()
                p.drawPolygon(QPolygonF([QPointF(x, cy - 3), QPointF(x + 5, cy), QPointF(x, cy + 3)]))
                x += 8
            p.setPen(QColor(color))
            p.drawText(QRectF(x, r.y(), r.right() - x - 3, r.height()), Qt.AlignVCenter | Qt.AlignLeft, text)

        if len(face.frames) > 1:
            pill(str(len(face.frames)), thumb.right() - 2, thumb.bottom() - 15, fr.OLED_ON, "play")
        if panel._is_undefined(name):
            pill("EMPTY", thumb.x() + 3 + fm.horizontalAdvance("EMPTY") + 8, thumb.y() + 3, FG_DIM)
        if panel._is_override(name):
            pill("OVERRIDE", thumb.right() - 2, thumb.y() + 3, WARNING)
        if name in panel._used_faces:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ACCENT))
            p.drawEllipse(QRectF(thumb.x() + 4, thumb.bottom() - 10, 7, 7))
        p.restore()

    @staticmethod
    def _lock(p: QPainter, x: int, y: int) -> None:
        """Tiny padlock, 9 x 11 px (read-only built-in)."""
        col = QColor(FG_MUTED)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(col, 1.2))
        p.drawArc(QRectF(x + 2, y + 0.8, 5, 6.4), 0, 180 * 16)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(x + 0.5, y + 4.5, 8, 6), 1.2, 1.2)


class FaceLibraryPanel(QWidget):
    faceAssigned = Signal(str)          # face name -> assign to selected timeline frame
    facesChanged = Signal()
    facePreviewRequested = Signal(str)  # face name -> show on the model's face plane
    facePixelsPreview = Signal(object)  # (64,128) bool ndarray while painting, None when the editor closes
    editBlocked = Signal(str)
    headerFileSaveRequested = Signal()
    headerFileSaveAsRequested = Signal()
    openHeaderRequested = Signal()
    closeHeaderRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FaceLibrary")
        self.setStyleSheet(suite_stylesheet())
        self.faces: dict[str, Face] = {}
        self.builtin_faces: dict[str, Face] = dict(BUILTIN_FACES)
        self.header_file: FaceBitmapsFile | None = None

        self._category: str | None = None        # None = all
        self._used_faces: set[str] = set()
        self._thumb_w = _MIN_THUMB_W
        self._cell_size = QSize(_MIN_THUMB_W + 2 * _CARD_PAD + _CELL_GAP,
                                _MIN_THUMB_W // 2 + _CARD_PAD + _NAME_H + _CELL_GAP)
        self._hover_name: str | None = None
        self._hover_frame = 0
        self._hover_timer = QTimer(self)
        self._hover_timer.timeout.connect(self._advance_hover)

        self._editor: FaceEditorDialog | None = None
        self._editor_kind = "new"                # "new" | "edit" | "builtin_copy"
        self._editor_original: str | None = None
        self._watched_window: QWidget | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # -- header-file bar (only while a face-bitmaps.h is open) -----------
        self.header_banner = QFrame()
        self.header_banner.setObjectName("FaceHeaderBar")
        banner = QHBoxLayout(self.header_banner)
        banner.setContentsMargins(10, 5, 5, 5)
        banner.setSpacing(6)
        self.header_dirty_dot = QLabel("●")
        self.header_dirty_dot.setToolTip("No unsaved changes")
        self.header_dirty_dot.setStyleSheet(f"color: {FG_MUTED}; font-size: 10px;")
        banner.addWidget(self.header_dirty_dot)
        self.header_label = _ElidedLabel("Editing: face-bitmaps.h")
        self.header_label.setStyleSheet("font-weight: 600; font-size: 12px;")
        banner.addWidget(self.header_label, 1)

        self.save_header_btn = QPushButton("Save .h")
        self.save_header_btn.setProperty("variant", "primary")
        self.save_header_btn.setToolTip("Write the faces back into the header file (Ctrl+S)")
        self.save_header_btn.clicked.connect(self.headerFileSaveRequested.emit)
        banner.addWidget(self.save_header_btn)

        self.save_as_header_btn = tool_button("save", "Save the header under a new name...")
        self.save_as_header_btn.clicked.connect(self.headerFileSaveAsRequested.emit)
        banner.addWidget(self.save_as_header_btn)

        self.close_header_btn = tool_button("close", "Close the header file and return to project mode")
        self.close_header_btn.clicked.connect(self.closeHeaderRequested.emit)
        banner.addWidget(self.close_header_btn)

        self.header_banner.setVisible(False)
        layout.addWidget(self.header_banner)

        # -- search + create ---------------------------------------------------
        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search faces")
        self.search_edit.textChanged.connect(self._refresh)
        search_row.addWidget(self.search_edit, 1)

        self.new_btn = QPushButton("New")
        self.new_btn.setToolTip("Paint a new face")
        ic = get_icon("plus")
        if ic is not None:
            self.new_btn.setIcon(ic)
        self.new_btn.clicked.connect(self._new_face)
        search_row.addWidget(self.new_btn)

        self.open_header_btn = tool_button(
            "folder", "Open a face-bitmaps.h header to edit and save directly")
        self.open_header_btn.clicked.connect(self.openHeaderRequested.emit)
        search_row.addWidget(self.open_header_btn)
        layout.addLayout(search_row)

        # -- category chips ----------------------------------------------------
        chip_host = QWidget()
        chips = FlowLayout(chip_host, h_spacing=4, v_spacing=4)
        self._chip_group = QButtonGroup(self)
        self._chip_group.setExclusive(True)
        self.category_buttons: dict[str | None, QToolButton] = {}
        for cat, label in [(None, "All")] + [(c, FACE_CATEGORY_LABELS[c]) for c in FACE_CATEGORIES]:
            chip = chip_button(
                label, "Show every face" if cat is None else f"Show only {label.lower()} faces")
            chip.setChecked(cat is None)
            chip.clicked.connect(lambda _=False, c=cat: self.set_category_filter(c))
            self._chip_group.addButton(chip)
            self.category_buttons[cat] = chip
            chips.addWidget(chip)
        layout.addWidget(chip_host)

        self.count_label = QLabel("")
        self.count_label.setProperty("role", "hint")
        layout.addWidget(self.count_label)

        # -- grid ----------------------------------------------------------------
        self.list_widget = QListWidget()
        self.list_widget.setObjectName("FaceGrid")
        self.list_widget.setViewMode(QListView.IconMode)
        self.list_widget.setMovement(QListView.Static)
        self.list_widget.setResizeMode(QListView.Adjust)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setSpacing(0)
        self.list_widget.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list_widget.setDragEnabled(False)
        self.list_widget.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget.setMouseTracking(True)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.setItemDelegate(_FaceDelegate(self))
        self.list_widget.itemDoubleClicked.connect(self._edit_selected)
        self.list_widget.currentItemChanged.connect(self._on_selection_changed)
        self.list_widget.itemEntered.connect(self._on_item_entered)
        self.list_widget.viewportEntered.connect(self._stop_hover)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)
        self.list_widget.viewport().installEventFilter(self)
        self.list_widget.installEventFilter(self)
        layout.addWidget(self.list_widget, 1)

        self.empty_state = EmptyState(
            "face", "No faces yet", "Paint the first one.", action_text="New Face",
            parent=self.list_widget.viewport(), action_icon="plus", action_variant=None,
            compact=True)
        self.empty_state.actionRequested.connect(self._on_empty_action)
        self._empty_action = "new"
        self.empty_state.hide()

        # -- actions -------------------------------------------------------------
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self.assign_btn = QPushButton("Assign to Frame")
        self.assign_btn.setProperty("variant", "primary")
        self.assign_btn.clicked.connect(self._assign_selected)
        btn_row.addWidget(self.assign_btn, 1)
        self.edit_btn = tool_button("pencil", "Edit", "double-click or Enter")
        self.edit_btn.clicked.connect(self._edit_selected)
        self.duplicate_btn = tool_button("duplicate", "Duplicate", "Ctrl+D")
        self.duplicate_btn.clicked.connect(self._duplicate_selected)
        self.delete_btn = tool_button("trash", "Delete", "Del")
        self.delete_btn.clicked.connect(self._delete_selected)
        for b in (self.edit_btn, self.duplicate_btn, self.delete_btn):
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        self._refresh()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def load_faces(self, faces: dict[str, Face]) -> None:
        self.header_file = None
        self.builtin_faces = dict(BUILTIN_FACES)
        self.faces = faces
        self.header_banner.setVisible(False)
        self._document_replaced()
        self._refresh()

    def load_header_file(self, header_file: FaceBitmapsFile) -> None:
        self.header_file = header_file
        self.builtin_faces = {}
        self.faces = header_file.faces
        self.header_label.setText(f"Editing: {header_file.filename}")
        self.header_label.setToolTip(header_file.path)
        self.header_banner.setVisible(True)
        self.update_header_dirty(header_file.dirty)
        self._document_replaced()
        self._refresh()

    def update_header_dirty(self, dirty: bool) -> None:
        if self.header_file:
            self.save_header_btn.setText("Save .h*" if dirty else "Save .h")
            self.header_dirty_dot.setStyleSheet(
                f"color: {ACCENT if dirty else FG_MUTED}; font-size: 10px;")
            self.header_dirty_dot.setToolTip("Unsaved changes" if dirty else "No unsaved changes")

    def get_face(self, name: str) -> Face | None:
        """Custom faces shadow built-ins of the same name, same priority
        rule as Timeline sequences."""
        return self.faces.get(name) or self.builtin_faces.get(name)

    def is_builtin(self, name: str) -> bool:
        return name in self.builtin_faces and name not in self.faces

    def all_face_names(self) -> list[str]:
        return sorted(set(self.faces.keys()) | set(self.builtin_faces.keys()))

    def set_used_faces(self, names) -> None:
        """Optional: names of faces assigned in the current sequence; they
        get a small accent dot in the grid."""
        used = set(names or ())
        if used != self._used_faces:
            self._used_faces = used
            self._refresh()

    def _is_undefined(self, name: str) -> bool:
        return bool(self.header_file) and name in self.header_file.undefined_names

    def _is_override(self, name: str) -> bool:
        """A project face that replaces a firmware face (not meaningful in
        header mode, where every face *is* the firmware face)."""
        if self.header_file or name not in self.faces:
            return False
        return name in self.builtin_faces or bool(self.faces[name].is_override)

    # ------------------------------------------------------------------
    # Filtering / grid
    # ------------------------------------------------------------------
    def set_category_filter(self, category: str | None) -> None:
        self._category = category if category in FACE_CATEGORIES else None
        chip = self.category_buttons.get(self._category)
        if chip is not None and not chip.isChecked():
            chip.setChecked(True)
        self._refresh()

    def _visible_names(self) -> list[str]:
        query = self.search_edit.text().strip().lower()
        out = []
        for name in self.all_face_names():
            face = self.get_face(name)
            if self._category is not None and face.category != self._category:
                continue
            if query and query not in name.lower():
                continue
            out.append(name)
        return out

    def _tooltip_for(self, name: str, face: Face) -> str:
        label = FACE_CATEGORY_LABELS.get(face.category, face.category)
        tip = f"{name} ({label})"
        n = len(face.frames)
        if n > 1:
            tip += f"\n{n} frames at {face.fps} fps"
        if self.is_builtin(name):
            tip += "\nBuilt-in, read-only. Edit or duplicate to save a custom copy."
        elif self._is_undefined(name):
            tip += "\nIn FACE_LIST (no bitmap defined yet). Double-click to paint."
        elif self._is_override(name):
            tip += "\nOverrides the firmware face of the same name."
        if name in self._used_faces:
            tip += "\nUsed in the current sequence."
        return tip

    def _refresh(self, *_args) -> None:
        keep = self._selected_name()
        names = self._visible_names()
        lw = self.list_widget
        lw.blockSignals(True)
        lw.clear()
        restore = None
        for name in names:
            face = self.get_face(name)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, name)
            item.setData(Qt.AccessibleTextRole, name)
            item.setSizeHint(self._cell_size)
            item.setToolTip(self._tooltip_for(name, face))
            lw.addItem(item)
            if name == keep:
                restore = item
        if restore is not None:
            lw.setCurrentItem(restore)
        lw.blockSignals(False)

        total = len(self.all_face_names())
        if len(names) == total:
            self.count_label.setText(f"{total} face{'s' if total != 1 else ''}")
        else:
            self.count_label.setText(f"{len(names)} of {total} faces")
        if names:
            self.empty_state.hide()
        else:
            query = self.search_edit.text().strip()
            if total == 0:
                self._empty_action = "new"
                self.empty_state.set_title("No faces yet")
                self.empty_state.set_hint("Paint the first one, or open a face-bitmaps.h file.")
                self.empty_state.set_action("New Face", "plus")
            else:
                self._empty_action = "clear"
                if query:
                    self.empty_state.set_title(f"No faces match “{query}”")
                else:
                    label = FACE_CATEGORY_LABELS.get(self._category, "matching")
                    self.empty_state.set_title(f"No {label.lower()} faces")
                self.empty_state.set_hint("Try another search or category.")
                self.empty_state.set_action("Clear filters", "close")
            self._place_empty_state()
            self.empty_state.show()
        self._stop_hover()
        self._update_actions()
        self._sync_editor_context()

    def _relayout_grid(self) -> None:
        """Spread whole columns across the viewport and grow thumbnails to
        fill them (up to 1:1), so the grid has no ragged right gutter."""
        # Always budget for the scroll bar, so the grid does not change
        # (and re-flow) when it appears or disappears. QListView also wraps
        # a row unless it is strictly narrower than the viewport, hence the
        # 2px of slack.
        bar = self.list_widget.verticalScrollBar()
        vw = self.list_widget.viewport().width() - 2
        if not bar.isVisible():
            vw -= bar.sizeHint().width()
        min_cell = _MIN_THUMB_W + 2 * _CARD_PAD + _CELL_GAP
        cols = max(1, vw // min_cell)
        cell_w = max(min_cell, vw // cols)
        tw = min(_MAX_THUMB_W, (cell_w - 2 * _CARD_PAD - _CELL_GAP) // 2 * 2)
        tw = max(_MIN_THUMB_W, tw)
        cell = QSize(cell_w, tw // 2 + _CARD_PAD + _NAME_H + _CELL_GAP)
        if cell == self._cell_size and tw == self._thumb_w:
            return
        self._thumb_w = tw
        self._cell_size = cell
        self.list_widget.setGridSize(cell)
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setSizeHint(cell)

    def _place_empty_state(self) -> None:
        self.empty_state.setGeometry(self.list_widget.viewport().rect())

    def _on_empty_action(self) -> None:
        if self._empty_action == "new":
            self._new_face()
        else:
            self.clear_filters()

    def clear_filters(self) -> None:
        """Show every face again (empties the search, selects "All")."""
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self._category = None
        self.category_buttons[None].setChecked(True)
        self._refresh()

    def _selected_name(self) -> str | None:
        item = self.list_widget.currentItem()
        return item.data(Qt.UserRole) if item else None

    def select_face(self, name: str) -> bool:
        """Select ``name`` in the grid (clearing filters that hide it)."""
        if name not in self.faces and name not in self.builtin_faces:
            return False
        if name not in self._visible_names():
            self.clear_filters()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.UserRole) == name:
                self.list_widget.setCurrentItem(item)
                self.list_widget.scrollToItem(item)
                return True
        return False

    def _on_selection_changed(self, current, previous) -> None:
        self._update_actions()
        name = current.data(Qt.UserRole) if current else None
        if name:
            self.facePreviewRequested.emit(name)

    def _update_actions(self) -> None:
        name = self._selected_name()
        has = name is not None
        builtin = has and self.is_builtin(name)
        self.assign_btn.setEnabled(has)
        self.assign_btn.setToolTip(
            f"Show '{name}' on the selected timeline frame" if has
            else "Select a face to assign it to the selected timeline frame")
        self.edit_btn.setEnabled(has)
        set_button_tooltip(
            self.edit_btn,
            "Edit a custom copy of this built-in face" if builtin else "Edit",
            "double-click or Enter")
        self.duplicate_btn.setEnabled(has)
        self.delete_btn.setEnabled(has and not builtin)
        if builtin:
            set_button_tooltip(self.delete_btn, "Built-in faces are read-only and cannot be deleted")
        else:
            set_button_tooltip(self.delete_btn, "Delete", "Del")

    # -- hover playback -------------------------------------------------
    def _on_item_entered(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.UserRole)
        if name == self._hover_name:
            return
        self._stop_hover()
        face = self.get_face(name) if name else None
        if face is not None and len(face.frames) > 1:
            self._hover_name = name
            self._hover_frame = 0
            self._hover_timer.start(max(33, round(1000 / max(1, face.fps))))

    def _advance_hover(self) -> None:
        if self._hover_name is None:
            self._hover_timer.stop()
            return
        self._hover_frame += 1
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.UserRole) == self._hover_name:
                self.list_widget.viewport().update(self.list_widget.visualItemRect(item))
                return
        self._stop_hover()

    def _stop_hover(self) -> None:
        if self._hover_name is None:
            return
        self._hover_timer.stop()
        self._hover_name = None
        self._hover_frame = 0
        self.list_widget.viewport().update()

    # -- Qt plumbing ----------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        et = event.type()
        if obj is self.list_widget.viewport():
            if et == QEvent.Resize:
                self._relayout_grid()
                self._place_empty_state()
            elif et == QEvent.Leave:
                self._stop_hover()
        elif obj is self.list_widget and et == QEvent.KeyPress:
            key, mods = event.key(), event.modifiers()
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self._edit_selected()
                return True
            if key == Qt.Key_Delete:
                self._delete_selected()
                return True
            if key == Qt.Key_D and mods & Qt.ControlModifier:
                self._duplicate_selected()
                return True
        elif obj is self._watched_window and et == QEvent.Close:
            # The main window is closing: give an unsaved painting the
            # chance to be saved (or the close to be cancelled).
            if self._editor is not None and not self._editor.request_close():
                event.ignore()
                return True
        return super().eventFilter(obj, event)

    def _show_context_menu(self, pos: QPoint) -> None:
        item = self.list_widget.itemAt(pos)
        menu = QMenu(self)
        if item is None:
            menu.addAction("New Face...", self._new_face)
        else:
            self.list_widget.setCurrentItem(item)
            name = item.data(Qt.UserRole)
            builtin = self.is_builtin(name)
            menu.addAction("Assign to Selected Frame", self._assign_selected)
            menu.addSeparator()
            menu.addAction("Edit a Copy..." if builtin else "Edit...", self._edit_selected)
            menu.addAction("Duplicate", self._duplicate_selected)
            menu.addSeparator()
            delete = menu.addAction("Delete", self._delete_selected)
            delete.setEnabled(not builtin)
            menu.addSeparator()
            menu.addAction("New Face...", self._new_face)
        menu.exec(self.list_widget.viewport().mapToGlobal(pos))

    def _notify_blocked(self) -> None:
        self.editBlocked.emit(
            "Built-in faces are read-only. Edit one to save a custom copy under a new name."
        )

    # ------------------------------------------------------------------
    # Editor management (modeless, one at a time)
    # ------------------------------------------------------------------
    def has_open_editor(self) -> bool:
        return self._editor is not None

    def editor(self) -> FaceEditorDialog | None:
        return self._editor

    def close_editor(self, force: bool = False) -> bool:
        """Close the open Face Editor. Without ``force`` the user is asked
        about unsaved changes and may cancel; returns True once no editor
        is open."""
        ed = self._editor
        if ed is None:
            return True
        if force:
            ed.force_close()
            return True
        return ed.request_close()

    def _editor_existing_names(self) -> set[str]:
        """Names the edited face must not collide with (the editor itself
        removes the face's own starting name)."""
        names = set(self.faces.keys())
        if self._editor_kind == "builtin_copy" and not self.header_file:
            names |= set(self.builtin_faces.keys())
        if self._editor_kind == "edit" and self._editor_original:
            names.discard(self._editor_original)
        return names

    def _open_editor(self, face: Face | None, kind: str, original: str | None) -> FaceEditorDialog | None:
        if self._editor is not None:
            if not self._editor.request_close():
                self._editor.raise_()
                self._editor.activateWindow()
                return None
        self._editor_kind = kind
        self._editor_original = original
        window = self.window()
        dlg = FaceEditorDialog(face, self._editor_existing_names(), window,
                               is_header_mode=bool(self.header_file))
        dlg.previewPixels.connect(self.facePixelsPreview)
        dlg.faceSaved.connect(self._apply_editor_result)
        dlg.finished.connect(self._on_editor_finished)
        self._editor = dlg
        if window is not self._watched_window:
            if self._watched_window is not None:
                self._watched_window.removeEventFilter(self)
            self._watched_window = window
            if window is not self:
                window.installEventFilter(self)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return dlg

    def _sync_editor_context(self) -> None:
        """Keep an open editor's name validation in step with the library."""
        ed = self._editor
        if ed is None:
            return
        ed.set_context(self._editor_existing_names(), bool(self.header_file))

    def _document_replaced(self) -> None:
        """The library now shows another project / header. A clean editor
        is closed; one with unsaved painting stays open as a draft that
        will be added to the new library on Save."""
        ed = self._editor
        if ed is None:
            return
        if ed.is_dirty():
            self._editor_kind = "new"
            self._editor_original = None
        else:
            ed.force_close()

    def _apply_editor_result(self, new_face: Face) -> None:
        kind, original = self._editor_kind, self._editor_original
        replacing = kind == "edit" and original is not None and original in self.faces
        if self.header_file:
            new_face.is_override = True
            if replacing:
                if new_face.name != original:
                    del self.faces[original]
                    self.header_file.rename_face(original, new_face.name)
                self.faces[new_face.name] = new_face
                self.header_file.undefined_names.discard(new_face.name)
                self.header_file.mark_dirty()
            else:
                self.faces[new_face.name] = new_face
                self.header_file.add_face(new_face)
        else:
            if replacing and new_face.name != original:
                del self.faces[original]
            self.faces[new_face.name] = new_face
        # From here on the editor (if it stays open) is editing this face.
        self._editor_kind = "edit"
        self._editor_original = new_face.name
        self._refresh()
        self.facesChanged.emit()
        was_selected = self._selected_name() == new_face.name
        self.select_face(new_face.name)
        if was_selected or self._selected_name() != new_face.name:
            # Selecting emits the preview request itself; when the selection
            # did not change, push the new pixels to the robot explicitly.
            self.facePreviewRequested.emit(new_face.name)

    def _on_editor_finished(self, _result: int = 0) -> None:
        dlg = self._editor
        self._editor = None
        if dlg is not None:
            dlg.deleteLater()
        self.facePixelsPreview.emit(None)
        name = self._selected_name()
        if name:
            self.facePreviewRequested.emit(name)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _new_face(self) -> None:
        self._open_editor(None, "new", None)

    def _unique_name(self, base: str, suffix: str) -> str:
        taken = set(self.faces.keys()) | set(self.builtin_faces.keys())
        name, i = f"{base}_{suffix}", 2
        while name in taken:
            name = f"{base}_{suffix}{i}"
            i += 1
        return name

    def _edit_selected(self, *_args) -> None:
        name = self._selected_name()
        if not name:
            return
        face = self.get_face(name)
        if face is None:
            return
        if not self.header_file and self.is_builtin(name):
            # Duplicate-to-custom: pre-fill the built-in's bitmap but start
            # the name already renamed, so Save just works without hitting
            # the "name already exists" guard for the original.
            seed = Face(
                name=self._unique_name(name, "custom"), category=face.category, fps=face.fps,
                frames=[bytearray(f) for f in face.frames],
            )
            self._open_editor(seed, "builtin_copy", None)
            return
        self._open_editor(face, "edit", name)

    def _duplicate_selected(self) -> None:
        """Make an editable copy right away (works for built-ins too)."""
        name = self._selected_name()
        if not name:
            return
        face = self.get_face(name)
        if face is None:
            return
        copy = Face(
            name=self._unique_name(name, "copy"), category=face.category, fps=face.fps,
            frames=[bytearray(f) for f in face.frames],
            is_override=True if self.header_file else False,
        )
        self.faces[copy.name] = copy
        if self.header_file:
            self.header_file.add_face(copy)
        self._refresh()
        self.facesChanged.emit()
        self.select_face(copy.name)

    def _delete_selected(self) -> None:
        name = self._selected_name()
        if not name:
            return

        if self.header_file:
            if QMessageBox.question(
                self, "Delete Face", f"Delete face '{name}' from {self.header_file.filename}?"
            ) != QMessageBox.Yes:
                return
            del self.faces[name]
            self.header_file.remove_face(name)
            self._refresh()
            self.facesChanged.emit()
            return

        if self.is_builtin(name):
            self._notify_blocked()
            return
        if QMessageBox.question(self, "Delete face", f"Delete '{name}'?") != QMessageBox.Yes:
            return
        del self.faces[name]
        self._refresh()
        self.facesChanged.emit()

    def _assign_selected(self) -> None:
        name = self._selected_name()
        if name:
            self.faceAssigned.emit(name)
