"""Small shared widgets for the face editor suite: icon tool buttons, a
wrapping flow layout, the OLED preview screen, the animation frame strip
and the suite's scoped stylesheet (built from theme tokens only)."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QPoint, QRect, QRectF, QSize, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame, QLabel, QLayout, QSizePolicy, QToolButton, QWidget,
)

from . import face_render as fr
from .servo_model import FACE_WIDTH, FACE_HEIGHT, FACE_MAX_FRAMES
from .theme import (
    ACCENT, ACCENT_SOFT, BG_ELEVATED, BG_SUNKEN, BORDER, BORDER_STRONG, FG,
    FG_DIM, FG_MUTED, FONT_FAMILY, FONT_MONO, PANEL, RADIUS, RADIUS_SM,
    SELECTION,
)

from . import icons as _icons
from .widgets import IconButton, Separator


def get_icon(name: str, color: str | None = None, size: int = 16):
    """QIcon for ``name`` or None when the icon set has no such glyph."""
    try:
        ic = _icons.icon(name, color, size)
    except Exception:
        return None
    return None if ic is None or ic.isNull() else ic


def painted_icon(draw, size: int = 16) -> QIcon:
    """Multi-state QIcon from a ``draw(painter, size, color)`` callback, for
    the few glyphs the shared icon set has no name for. Follows the icon
    set's conventions: FG normally, accent when the button is "on"."""
    result = QIcon()
    states = (
        (FG, QIcon.Normal, QIcon.Off), (FG, QIcon.Active, QIcon.Off),
        (ACCENT, QIcon.Normal, QIcon.On), (ACCENT, QIcon.Active, QIcon.On),
        (FG_MUTED, QIcon.Disabled, QIcon.Off), (FG_MUTED, QIcon.Disabled, QIcon.On),
    )
    for dpr in (1, 2, 3):
        for color, mode, state in states:
            pm = QPixmap(size * dpr, size * dpr)
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            draw(p, size, QColor(color))
            p.end()
            result.addPixmap(pm, mode, state)
    return result


def tool_button(icon_name: str, tooltip: str, shortcut: str | None = None, *,
                checkable: bool = False, text: str | None = None,
                icon: QIcon | None = None) -> QToolButton:
    """Flat square icon button from the shared design system. It never takes
    keyboard focus, so canvas shortcuts keep working after a click.
    ``shortcut`` is shown as a hint in the tooltip (bind it separately);
    ``icon`` supplies a custom glyph, ``text`` a one-character label."""
    btn = IconButton(icon_name, tooltip, shortcut, checkable=checkable, variant="flat")
    btn.setFocusPolicy(Qt.NoFocus)
    if icon is not None:
        btn.setIcon(icon)
    elif text:
        btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        btn.setText(text)
    return btn


def set_button_tooltip(btn: QToolButton, tooltip: str, shortcut: str | None = None) -> None:
    if hasattr(btn, "set_tooltip"):
        btn.set_tooltip(tooltip, shortcut)
    else:
        btn.setToolTip(tooltip)


def chip_button(label: str, tooltip: str = "", checkable: bool = True) -> QToolButton:
    """Pill-shaped filter / option chip."""
    chip = QToolButton()
    chip.setText(label)
    chip.setCheckable(checkable)
    chip.setProperty("chip", "true")
    chip.setFocusPolicy(Qt.NoFocus)
    chip.setCursor(Qt.PointingHandCursor)
    if tooltip:
        chip.setToolTip(tooltip)
    return chip


def separator(vertical: bool = True) -> QFrame:
    sep = Separator(Qt.Vertical if vertical else Qt.Horizontal)
    if vertical:
        sep.setFixedHeight(16)
        sep.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    return sep


def caption(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("role", "caption")
    return lbl


_MONO_FAMILY: str | None = None


def mono_font(pixel_size: int = 11) -> QFont:
    """Monospace UI font. Deliberately a *single* installed family: asking
    Qt for a fallback list (or for a family that is not installed) makes it
    walk every font on the system, ~250 ms the first time on Windows."""
    global _MONO_FAMILY
    if _MONO_FAMILY is None:
        _MONO_FAMILY = next(
            (f for f in (FONT_MONO, "Consolas", "Courier New") if QFontDatabase.hasFamily(f)), "")
    font = QFont(_MONO_FAMILY) if _MONO_FAMILY else QFontDatabase.systemFont(QFontDatabase.FixedFont)
    font.setStyleHint(QFont.Monospace)
    font.setPixelSize(int(pixel_size))
    return font


def mono_label(text: str = "", pixel_size: int = 11) -> QLabel:
    """Dim monospace read-out (coordinates, zoom, counts)."""
    lbl = QLabel(text)
    lbl.setFont(mono_font(pixel_size))
    lbl.setProperty("role", "status")
    return lbl


def suite_stylesheet() -> str:
    """Scoped QSS for the editor dialog and library panel. Buttons, inputs,
    tabs, lists ... all come from the app theme; this only adds the suite's
    own regions and the filter chip."""
    return f"""
#FaceToolbar, #FaceOptionsBar, #FaceToolRail, #FaceSidePanel, #FaceFrameBar {{
    background-color: {PANEL};
}}
#FaceToolbar, #FaceOptionsBar {{ border-bottom: 1px solid {BORDER}; }}
#FaceToolRail {{ border-right: 1px solid {BORDER}; }}
#FaceSidePanel {{ border-left: 1px solid {BORDER}; }}
#FaceFrameBar {{ border-top: 1px solid {BORDER}; }}
#FaceStatusBar {{
    background-color: {BG_ELEVATED};
    border-top: 1px solid {BORDER};
}}
#FaceHeaderBar {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}

QToolButton[chip="true"] {{
    background-color: transparent;
    border: 1px solid {BORDER_STRONG};
    border-radius: 11px;
    color: {FG_DIM};
    padding: 2px 10px;
    min-height: 16px;
    font-size: 11px;
}}
QToolButton[chip="true"]:hover {{ background-color: {BG_ELEVATED}; color: {FG}; }}
QToolButton[chip="true"]:checked {{
    background-color: {ACCENT_SOFT};
    border-color: {ACCENT};
    color: {FG};
}}

QLabel[role="hint"] {{ color: {FG_MUTED}; font-size: 11px; }}
QLabel[role="status"] {{ color: {FG_DIM}; font-size: 11px; }}
"""


# ---------------------------------------------------------------------------
# Flow layout (wrapping row) for filter chips
# ---------------------------------------------------------------------------

class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, h_spacing: int = 6, v_spacing: int = 6):
        super().__init__(parent)
        self._items: list = []
        self._h = h_spacing
        self._v = v_spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._layout(QRect(0, 0, width, 0), dry=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._layout(rect, dry=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect: QRect, dry: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = area.x(), area.y(), 0
        for item in self._items:
            wid = item.widget()
            if wid is not None and wid.isHidden():
                continue
            hint = item.sizeHint()
            if x + hint.width() > area.right() + 1 and line_h > 0:
                x = area.x()
                y += line_h + self._v
                line_h = 0
            if not dry:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._h
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


# ---------------------------------------------------------------------------
# OLED preview screen
# ---------------------------------------------------------------------------

class OledPreview(QWidget):
    """A little SSD1306: shows one frame at a crisp integer scale inside a
    dark glass bezel."""

    def __init__(self, scale: int = 2, parent: QWidget | None = None):
        super().__init__(parent)
        self._scale = scale
        self._pad = 8
        self._arr: np.ndarray | None = None
        self.setFixedSize(FACE_WIDTH * scale + 2 * self._pad, FACE_HEIGHT * scale + 2 * self._pad)

    def set_array(self, arr: np.ndarray | None) -> None:
        self._arr = arr
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(BORDER_STRONG), 1))
        p.setBrush(QColor(fr.OLED_GLASS))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)
        p.setRenderHint(QPainter.Antialiasing, False)
        screen = QRect(self._pad, self._pad, FACE_WIDTH * self._scale, FACE_HEIGHT * self._scale)
        if self._arr is None:
            p.fillRect(screen, QColor(fr.OLED_OFF))
        else:
            p.drawImage(screen, fr.oled_image(self._arr))
        p.end()


# ---------------------------------------------------------------------------
# Animation frame strip
# ---------------------------------------------------------------------------

class FrameStrip(QWidget):
    """Horizontal strip of live frame thumbnails. Click to select, drag to
    reorder, the dashed "+" tile adds a frame, right-click for more."""

    currentChanged = Signal(int)
    moveRequested = Signal(int, int)        # from index, to index
    addRequested = Signal()
    contextRequested = Signal(int, QPoint)  # frame index, global position

    THUMB_W, THUMB_H = 88, 44   # six cells fit beside the side panel at 1000px
    CELL_W, CELL_H = 96, 66
    GAP = 6

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._frames: list[bytes] = []
        self._current = 0
        self._hover = -1
        self._press_index = -1
        self._press_pos = QPoint()
        self._dragging = False
        self._drop_slot = -1
        self.max_frames = FACE_MAX_FRAMES
        self.setMouseTracking(True)
        self.setFixedHeight(self.CELL_H)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._resize_to_content()

    # -- data ----------------------------------------------------------
    def set_frames(self, frames: list[bytes], current: int | None = None) -> None:
        self._frames = [bytes(f) for f in frames]
        if current is not None:
            self._current = current
        self._current = max(0, min(len(self._frames) - 1, self._current))
        self._resize_to_content()
        self.update()

    def update_frame(self, index: int, frame: bytes) -> None:
        if 0 <= index < len(self._frames):
            data = bytes(frame)
            if data != self._frames[index]:
                self._frames[index] = data
                self.update(self._cell_rect(index))

    def set_current(self, index: int) -> None:
        index = max(0, min(len(self._frames) - 1, index))
        if index != self._current:
            self._current = index
            self.update()

    def count(self) -> int:
        return len(self._frames)

    def can_add(self) -> bool:
        return len(self._frames) < self.max_frames

    # -- geometry ------------------------------------------------------
    def _resize_to_content(self) -> None:
        n = len(self._frames) + (1 if self.can_add() else 0)
        self.setFixedWidth(max(self.CELL_W, n * self.CELL_W + max(0, n - 1) * self.GAP))

    def _cell_rect(self, index: int) -> QRect:
        return QRect(index * (self.CELL_W + self.GAP), 0, self.CELL_W, self.CELL_H)

    def _index_at(self, pos: QPoint) -> int:
        """Frame index under ``pos``; len(frames) for the add tile; -1 none."""
        step = self.CELL_W + self.GAP
        i = pos.x() // step
        if pos.x() - i * step >= self.CELL_W or not (0 <= pos.y() < self.CELL_H):
            return -1
        if 0 <= i < len(self._frames):
            return i
        if i == len(self._frames) and self.can_add():
            return i
        return -1

    def _slot_at(self, pos: QPoint) -> int:
        """Insertion slot (0..n) nearest to ``pos`` while dragging."""
        step = self.CELL_W + self.GAP
        return max(0, min(len(self._frames), (pos.x() + step // 2) // step))

    def cell_center(self, index: int) -> QPoint:
        return self._cell_rect(index).center()

    # -- events --------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        i = self._index_at(event.position().toPoint())
        if event.button() == Qt.LeftButton:
            if i == len(self._frames):
                self.addRequested.emit()
            elif i >= 0:
                self._press_index = i
                self._press_pos = event.position().toPoint()
                if i != self._current:
                    self._current = i
                    self.update()
                    self.currentChanged.emit(i)
        elif event.button() == Qt.RightButton and 0 <= i < len(self._frames):
            if i != self._current:
                self._current = i
                self.update()
                self.currentChanged.emit(i)
            self.contextRequested.emit(i, event.globalPosition().toPoint())

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        if self._press_index >= 0 and (event.buttons() & Qt.LeftButton):
            if not self._dragging and (pos - self._press_pos).manhattanLength() > 8 and len(self._frames) > 1:
                self._dragging = True
                self.setCursor(Qt.ClosedHandCursor)
            if self._dragging:
                slot = self._slot_at(pos)
                if slot != self._drop_slot:
                    self._drop_slot = slot
                    self.update()
            return
        hover = self._index_at(pos)
        if hover != self._hover:
            self._hover = hover
            if hover == len(self._frames):
                self.setToolTip(f"Add a frame (copies the current one). Up to {self.max_frames} frames.")
            elif hover >= 0:
                self.setToolTip(f"Frame {hover + 1}. Drag to reorder, right-click for options.")
            else:
                self.setToolTip("")
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        if self._dragging:
            src, slot = self._press_index, self._drop_slot
            self._dragging = False
            self._drop_slot = -1
            self.unsetCursor()
            if src >= 0 and slot >= 0:
                dst = slot - 1 if slot > src else slot
                if dst != src:
                    self.moveRequested.emit(src, dst)
            self.update()
        self._press_index = -1

    def leaveEvent(self, event) -> None:
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(event)

    # -- painting ------------------------------------------------------
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        dpr = self.devicePixelRatioF()
        label_font = QFont(FONT_FAMILY)
        label_font.setPixelSize(10)
        label_font.setBold(True)
        p.setFont(label_font)
        for i, frame in enumerate(self._frames):
            cell = self._cell_rect(i)
            current = i == self._current
            dragged = self._dragging and i == self._press_index
            p.setOpacity(0.45 if dragged else 1.0)
            border = QColor(ACCENT if current else BORDER_STRONG if i == self._hover else BORDER)
            p.setPen(QPen(border, 2 if current else 1))
            p.setBrush(QColor(ACCENT_SOFT if current else BG_SUNKEN))
            inset = 1.0 if current else 0.5
            p.drawRoundedRect(QRectF(cell).adjusted(inset, inset, -inset, -inset), RADIUS_SM, RADIUS_SM)
            tx = cell.x() + (self.CELL_W - self.THUMB_W) // 2
            p.setRenderHint(QPainter.Antialiasing, False)
            p.drawPixmap(tx, cell.y() + 4, fr.oled_pixmap(frame, self.THUMB_W, self.THUMB_H, dpr))
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QColor(FG if current else FG_DIM))
            p.drawText(QRect(tx + 1, cell.y() + 4 + self.THUMB_H, self.THUMB_W, cell.height() - self.THUMB_H - 5),
                       Qt.AlignVCenter | Qt.AlignLeft, str(i + 1))
        p.setOpacity(1.0)
        if self.can_add():
            cell = self._cell_rect(len(self._frames))
            hot = self._hover == len(self._frames)
            pen = QPen(QColor(ACCENT if hot else BORDER_STRONG), 1, Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(cell).adjusted(0.5, 0.5, -0.5, -0.5), RADIUS_SM, RADIUS_SM)
            plus_font = QFont(FONT_FAMILY)
            plus_font.setPixelSize(22)
            p.setFont(plus_font)
            p.setPen(QColor(ACCENT if hot else FG_MUTED))
            p.drawText(cell.adjusted(0, -2, 0, -2), Qt.AlignCenter, "+")
        if self._dragging and self._drop_slot >= 0:
            x = self._drop_slot * (self.CELL_W + self.GAP) - self.GAP // 2
            x = max(1, min(self.width() - 2, x))
            p.setPen(QPen(QColor(SELECTION), 2))
            p.drawLine(x, 2, x, self.CELL_H - 2)
        p.end()
