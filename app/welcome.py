"""Start screen: product header, the four primary actions and recent files.

Public contract (used by ``main_window``): ``WelcomePage(logo_path)``, the
signals ``newProjectRequested``, ``openProjectRequested``,
``openFaceBitmapsRequested``, ``recentProjectRequested(str)``,
``tutorialRequested`` and ``set_recent(paths)``.

Everything is keyboard reachable (Tab / arrow keys between the action cards,
Enter or Space to activate; Up/Down + Enter in the recent list) and a
``.sesproj`` / ``.h`` file can be dropped anywhere on the page to open it.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QAbstractItemView, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QSizePolicy, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from . import __version__, icons, theme
from .widgets.buttons import KeyHint
from .widgets.empty_state import EmptyState
from .widgets.layout import Badge, Card
from .widgets.logo import logo_pixmap

_OPENABLE = (".sesproj", ".h", ".hpp")
_ROW_HEIGHT = 48
_MAX_ROWS_HINT = 8
_PATH_ROLE = Qt.UserRole
_MISSING_ROLE = Qt.UserRole + 1


def _caption(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setProperty("role", "caption")
    font = label.font()
    font.setLetterSpacing(QFont.AbsoluteSpacing, 0.8)
    label.setFont(font)
    return label


class _ActionCard(Card):
    """Large clickable card: icon chip, title, one-line description, shortcut."""

    def __init__(self, icon_name: str, title: str, description: str,
                 shortcut: str | None = None, primary: bool = False, parent=None):
        super().__init__(parent, clickable=True, padding=16, spacing=0)
        self.setMinimumSize(168, 136)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        chip = QLabel()
        chip.setProperty("role", "iconChip")
        if primary:
            chip.setProperty("tone", "solid")
        chip.setFixedSize(36, 36)
        chip.setAlignment(Qt.AlignCenter)
        chip.setPixmap(icons.pixmap(
            icon_name, theme.ACCENT_FG if primary else theme.ACCENT_HOVER, 20))
        top.addWidget(chip, 0, Qt.AlignTop)
        top.addStretch(1)
        if shortcut:
            top.addWidget(KeyHint(shortcut), 0, Qt.AlignTop)
        self.body.addLayout(top)
        self.body.addSpacing(14)

        title_label = QLabel(title)
        title_label.setFont(theme.ui_font(theme.FONT_SIZE_LG, 600))
        self.body.addWidget(title_label)
        self.body.addSpacing(4)

        desc = QLabel(description)
        desc.setProperty("role", "subheading")
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.body.addWidget(desc, 1)

    def sizeHint(self) -> QSize:
        return QSize(214, 140)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Right, Qt.Key_Down):
            self.focusNextChild()
            event.accept()
        elif event.key() in (Qt.Key_Left, Qt.Key_Up):
            self.focusPreviousChild()
            event.accept()
        else:
            super().keyPressEvent(event)


class _RecentDelegate(QStyledItemDelegate):
    """Two-line row: type icon, file name over its folder, type badge."""

    def sizeHint(self, option, index) -> QSize:
        return QSize(0, _ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        path = index.data(_PATH_ROLE) or ""
        missing = bool(index.data(_MISSING_ROLE))
        is_header = path.lower().endswith((".h", ".hpp"))
        rect = option.rect.adjusted(2, 1, -2, -1)
        selected = bool(option.state & QStyle.State_Selected)
        focused = bool(option.state & QStyle.State_HasFocus) or bool(
            option.state & QStyle.State_Active and option.widget and option.widget.hasFocus())
        hovered = bool(option.state & QStyle.State_MouseOver)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if selected and focused:
            fill = QColor(theme.ACCENT_SOFT)
        elif hovered or selected:
            fill = QColor(theme.HOVER)
        else:
            fill = None
        if fill is not None:
            bg = QPainterPath()
            bg.addRoundedRect(QRectF(rect), theme.RADIUS, theme.RADIUS)
            painter.fillPath(bg, fill)

        # icon
        icon_px = 18
        icon_color = theme.FG_DISABLED if missing else (
            theme.ACCENT_HOVER if selected and focused else theme.FG_DIM)
        pm = icons.pixmap("file-code" if is_header else "robot", icon_color, icon_px)
        painter.drawPixmap(rect.left() + 12, rect.center().y() - icon_px // 2 + 1, pm)

        # badge (right)
        if missing:
            badge_text, badge_bg, badge_fg = "MISSING", theme.DANGER_SOFT, theme.DANGER
        elif is_header:
            badge_text, badge_bg, badge_fg = "FACE BITMAPS", theme.ACCENT_SOFT, theme.ACCENT_HOVER
        else:
            badge_text, badge_bg, badge_fg = "PROJECT", theme.BUTTON_BG, theme.FG_DIM
        badge_font = theme.ui_font(10, 600)
        badge_font.setLetterSpacing(QFont.AbsoluteSpacing, 0.5)
        fm_badge = QFontMetrics(badge_font)
        badge_w = fm_badge.horizontalAdvance(badge_text) + 16
        badge_rect = QRect(rect.right() - 12 - badge_w, rect.center().y() - 8, badge_w, 18)
        pill = QPainterPath()
        pill.addRoundedRect(QRectF(badge_rect), 9, 9)
        painter.fillPath(pill, QColor(badge_bg))
        painter.setFont(badge_font)
        painter.setPen(QColor(badge_fg))
        painter.drawText(badge_rect, Qt.AlignCenter, badge_text)

        # text block
        text_left = rect.left() + 12 + icon_px + 12
        text_w = max(10, badge_rect.left() - 16 - text_left)
        name_font = theme.ui_font(13, 600)
        path_font = theme.ui_font(theme.FONT_SIZE_SM)
        fm_name, fm_path = QFontMetrics(name_font), QFontMetrics(path_font)
        block_h = fm_name.height() + 1 + fm_path.height()
        top = rect.top() + (rect.height() - block_h) // 2

        painter.setFont(name_font)
        painter.setPen(QColor(theme.FG_MUTED if missing else theme.FG))
        name = fm_name.elidedText(os.path.basename(path) or path, Qt.ElideMiddle, text_w)
        painter.drawText(QRect(text_left, top, text_w, fm_name.height()),
                         Qt.AlignLeft | Qt.AlignVCenter, name)

        painter.setFont(path_font)
        painter.setPen(QColor(theme.FG_MUTED))
        folder = os.path.dirname(path)
        home = os.path.expanduser("~")
        if home and folder.lower().startswith(home.lower()):
            folder = "~" + folder[len(home):]
        folder = fm_path.elidedText(folder, Qt.ElideMiddle, text_w)
        painter.drawText(QRect(text_left, top + fm_name.height() + 1, text_w, fm_path.height()),
                         Qt.AlignLeft | Qt.AlignVCenter, folder)
        painter.restore()


class _RecentList(QListWidget):
    """List that sizes itself to its rows (up to a cap) instead of 256x192."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "card")
        self.setItemDelegate(_RecentDelegate(self))
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setAccessibleName("Recent files")

    def _chrome(self) -> int:
        return 2 * self.frameWidth() + 8

    def sizeHint(self) -> QSize:
        rows = max(1, min(self.count(), _MAX_ROWS_HINT))
        return QSize(400, rows * _ROW_HEIGHT + self._chrome())

    def minimumSizeHint(self) -> QSize:
        rows = max(1, min(self.count(), 2))
        return QSize(200, rows * _ROW_HEIGHT + self._chrome())


class WelcomePage(QWidget):
    newProjectRequested = Signal()
    openProjectRequested = Signal()
    openFaceBitmapsRequested = Signal()
    recentProjectRequested = Signal(str)
    tutorialRequested = Signal()

    _MAX_CONTENT_WIDTH = 960
    _SIDE_MARGIN = 40

    def __init__(self, logo_path: str, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._last_open: tuple[str, float] = ("", 0.0)

        # A single content column, capped at _MAX_CONTENT_WIDTH and centred by
        # resizeEvent. It is positioned by hand rather than by a layout so the
        # page's minimum size stays the column's true minimum at any width.
        self._column = QWidget(self)
        col = QVBoxLayout(self._column)
        col.setContentsMargins(0, 24, 0, 24)
        col.setSpacing(0)
        col.addStretch(2)

        # -- header ---------------------------------------------------------
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(16)
        logo = logo_pixmap(logo_path, 72)
        if not logo.isNull():
            logo_label = QLabel()
            logo_label.setPixmap(logo)
            logo_label.setFixedSize(72, 72)
            header.addWidget(logo_label, 0, Qt.AlignVCenter)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title = QLabel("Sesame Studio")
        title.setProperty("role", "title")
        title_row.addWidget(title, 0, Qt.AlignVCenter)
        version = Badge(f"v{__version__}")
        version.setToolTip(f"Sesame Studio {__version__}")
        title_row.addWidget(version, 0, Qt.AlignVCenter)
        title_row.addStretch(1)
        titles.addLayout(title_row)
        tagline = QLabel("Pose, animate and design faces for your Sesame robot.")
        tagline.setProperty("role", "subheading")
        tagline.setFont(theme.ui_font(13))
        titles.addWidget(tagline)
        header.addLayout(titles, 1)
        col.addLayout(header)
        col.addSpacing(28)

        # -- primary actions --------------------------------------------------
        col.addWidget(_caption("Start"))
        col.addSpacing(8)
        self._cards = [
            self._card("file-plus", "New Project",
                       "Start a fresh animation with the default poses.",
                       "Ctrl+N", self.newProjectRequested, primary=True),
            self._card("folder-open", "Open Project",
                       "Continue working on a .sesproj file.",
                       "Ctrl+O", self.openProjectRequested),
            self._card("face", "Open face-bitmaps.h",
                       "Edit the firmware's OLED faces directly.",
                       "Ctrl+Shift+O", self.openFaceBitmapsRequested),
            self._card("compass", "Take the Tour",
                       "A short guided walkthrough of posing, the timeline and faces.",
                       None, self.tutorialRequested),
        ]
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        for i, card in enumerate(self._cards):
            grid.addWidget(card, 0, i)
            grid.setColumnStretch(i, 1)
        col.addLayout(grid)
        col.addSpacing(28)

        # -- recent files -----------------------------------------------------
        col.addWidget(_caption("Recent"))
        col.addSpacing(8)
        self.recent_list = _RecentList()
        self.recent_list.itemClicked.connect(self._open_item)
        self.recent_list.itemActivated.connect(self._open_item)
        col.addWidget(self.recent_list)

        self._empty = Card(padding=0)
        self._empty.body.addWidget(EmptyState(
            "clock", "No recent files",
            "Projects and face-bitmaps.h files you open will show up here.",
            compact=True))
        self._empty.setFixedHeight(128)
        col.addWidget(self._empty)

        col.addSpacing(12)
        tip = QLabel("Tip: drop a .sesproj or face-bitmaps.h file anywhere on this page to open it.")
        tip.setProperty("role", "caption")
        tip.setFont(theme.ui_font(theme.FONT_SIZE_SM))
        col.addWidget(tip)
        col.addStretch(3)

        for first, second in zip(self._cards, self._cards[1:]):
            QWidget.setTabOrder(first, second)
        QWidget.setTabOrder(self._cards[-1], self.recent_list)
        self.set_recent([])

    # -- construction helpers -------------------------------------------------
    def _card(self, icon_name, title, description, shortcut, signal, primary=False) -> _ActionCard:
        card = _ActionCard(icon_name, title, description, shortcut, primary)
        card.clicked.connect(signal.emit)
        return card

    # -- geometry ---------------------------------------------------------------
    def minimumSizeHint(self) -> QSize:
        hint = self._column.minimumSizeHint()
        return QSize(hint.width() + 2 * self._SIDE_MARGIN, hint.height())

    def sizeHint(self) -> QSize:
        hint = self._column.sizeHint()
        return QSize(max(hint.width(), self._MAX_CONTENT_WIDTH) + 2 * self._SIDE_MARGIN,
                     hint.height())

    def _place_column(self) -> None:
        width = max(1, min(self._MAX_CONTENT_WIDTH, self.width() - 2 * self._SIDE_MARGIN))
        self._column.setGeometry((self.width() - width) // 2, 0, width, self.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_column()

    # -- recent files -----------------------------------------------------------
    def set_recent(self, paths: list[str]) -> None:
        """Show ``paths`` (most recent first) in the Recent list."""
        self.recent_list.clear()
        for path in paths or []:
            if not path:
                continue
            item = QListWidgetItem(os.path.basename(path))
            item.setData(_PATH_ROLE, path)
            try:
                missing = not os.path.exists(path)
            except OSError:
                missing = True
            item.setData(_MISSING_ROLE, missing)
            item.setToolTip(f"{path}\n(file not found)" if missing else path)
            self.recent_list.addItem(item)
        has_items = self.recent_list.count() > 0
        self.recent_list.setVisible(has_items)
        self._empty.setVisible(not has_items)
        self.recent_list.updateGeometry()
        self.updateGeometry()

    def _open_item(self, item: QListWidgetItem) -> None:
        path = item.data(_PATH_ROLE)
        if not path:
            return
        # itemClicked and itemActivated can both fire for one gesture.
        now = time.monotonic()
        if self._last_open[0] == path and now - self._last_open[1] < 0.6:
            return
        self._last_open = (path, now)
        self.recentProjectRequested.emit(path)

    # -- drag & drop ------------------------------------------------------------
    @staticmethod
    def _dropped_path(mime) -> str | None:
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            local = url.toLocalFile()
            if local and local.lower().endswith(_OPENABLE):
                return local
        return None

    def dragEnterEvent(self, event) -> None:
        if self._dropped_path(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        path = self._dropped_path(event.mimeData())
        if path:
            event.acceptProposedAction()
            self.recentProjectRequested.emit(path)
        else:
            event.ignore()
