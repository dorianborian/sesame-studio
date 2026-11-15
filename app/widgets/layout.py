"""Small layout/surface helpers: Card, ToolStrip, Separator, SectionLabel, Badge."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from .. import theme

__all__ = ["Card", "ToolStrip", "Separator", "SectionLabel", "Badge"]


class Card(QFrame):
    """Panel-surface container (``role="card"``).

    ``card.body`` is a ready ``QVBoxLayout``. With ``clickable=True`` the card
    behaves like a big button: hover + focus feedback, pointer cursor and a
    ``clicked`` signal fired by the mouse, Enter/Return or Space.
    """

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None, *, clickable: bool = False,
                 padding: int = 12, spacing: int = theme.SPACING):
        super().__init__(parent)
        self.setProperty("role", "card")
        self._clickable = clickable
        self._armed = False
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding, padding, padding)
        self.body.setSpacing(spacing)
        if clickable:
            self.setProperty("clickable", "true")
            self.setAttribute(Qt.WA_Hover, True)
            self.setCursor(Qt.PointingHandCursor)
            self.setFocusPolicy(Qt.StrongFocus)

    def is_clickable(self) -> bool:
        return self._clickable

    # -- activation --------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if self._clickable and event.button() == Qt.LeftButton:
            self._armed = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._clickable and event.button() == Qt.LeftButton:
            armed, self._armed = self._armed, False
            if armed and self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if self._clickable and event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if not event.isAutoRepeat():
                self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class Separator(QFrame):
    """One-pixel hairline (``role="separator"``), horizontal by default."""

    def __init__(self, orientation: Qt.Orientation = Qt.Horizontal,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("role", "separator")
        if orientation == Qt.Horizontal:
            self.setFixedHeight(1)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        else:
            self.setFixedWidth(1)
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)


class SectionLabel(QLabel):
    """Small muted section caption (``role="caption"``)."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setProperty("role", "caption")


class Badge(QLabel):
    """Pill label (``role="badge"``); ``tone`` is ``None`` (neutral),
    ``"accent"``, ``"success"``, ``"danger"``, ``"warning"`` or ``"info"``."""

    def __init__(self, text: str = "", tone: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setProperty("role", "badge")
        if tone:
            self.setProperty("tone", tone)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)

    def set_tone(self, tone: str | None) -> None:
        self.setProperty("tone", tone)
        theme.repolish(self)


class ToolStrip(QFrame):
    """Horizontal strip for the top of a dock/panel (``role="toolbar"``).

    ::

        strip = ToolStrip()
        strip.add(IconButton("plus", "Add frame", "Ins"))
        strip.add_separator()
        strip.add_stretch()
        strip.add(search_edit, 1)
    """

    def __init__(self, parent: QWidget | None = None, *, spacing: int = theme.SPACING_XS,
                 margins: tuple[int, int, int, int] = (8, 4, 8, 4)):
        super().__init__(parent)
        self.setProperty("role", "toolbar")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(*margins)
        self._layout.setSpacing(spacing)

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self._layout.addWidget(widget, stretch, Qt.AlignVCenter)
        return widget

    def add_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("role", "subheading")
        self._layout.addWidget(label, 0, Qt.AlignVCenter)
        return label

    def add_stretch(self, stretch: int = 1) -> None:
        self._layout.addStretch(stretch)

    def add_spacing(self, px: int = theme.SPACING) -> None:
        self._layout.addSpacing(px)

    def add_separator(self) -> Separator:
        sep = Separator(Qt.Vertical)
        sep.setFixedHeight(16)
        sep.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._layout.addSpacing(4)
        self._layout.addWidget(sep, 0, Qt.AlignVCenter)
        self._layout.addSpacing(4)
        return sep

    def strip_layout(self) -> QHBoxLayout:
        return self._layout
