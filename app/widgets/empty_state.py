"""Empty-state placeholder: icon, title, hint and an optional action."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from .. import icons, theme

__all__ = ["EmptyState"]


class EmptyState(QWidget):
    """What a list/panel shows when it has nothing to show.

    ::

        empty = EmptyState("face", "No faces yet",
                           "Draw one, or open a face-bitmaps.h file.",
                           action_text="New Face", action_icon="plus")
        empty.actionRequested.connect(self._new_face)

    ``compact=True`` uses a smaller icon and tighter spacing for narrow docks.
    """

    actionRequested = Signal()

    def __init__(self, icon_name: str, title: str, hint: str = "",
                 action_text: str | None = None, parent: QWidget | None = None, *,
                 action_icon: str | None = None, action_variant: str | None = "primary",
                 compact: bool = False):
        super().__init__(parent)
        self._icon_px = 28 if compact else 40
        self._action_icon = action_icon
        self._action_variant = action_variant

        layout = QVBoxLayout(self)
        margin = 12 if compact else 24
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(0)
        layout.addStretch(1)

        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.icon_label, 0, Qt.AlignHCenter)
        layout.addSpacing(8 if compact else 12)

        self.title_label = QLabel(title)
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setWordWrap(True)
        self.title_label.setFont(theme.ui_font(theme.FONT_SIZE if compact else 13, 600))
        layout.addWidget(self.title_label)
        layout.addSpacing(4)

        self.hint_label = QLabel(hint)
        self.hint_label.setProperty("role", "subheading")
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setWordWrap(True)
        self.hint_label.setVisible(bool(hint))
        layout.addWidget(self.hint_label)

        self._action_gap = 12 if compact else 16
        self.action_button = QPushButton()
        self.action_button.setCursor(Qt.PointingHandCursor)
        if action_variant:
            self.action_button.setProperty("variant", action_variant)
        self.action_button.clicked.connect(self.actionRequested.emit)
        self.action_button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self._action_spacer_index = layout.count()
        layout.addSpacing(self._action_gap)
        layout.addWidget(self.action_button, 0, Qt.AlignHCenter)
        layout.addStretch(1)

        self.set_icon(icon_name)
        self.set_action(action_text)

    # -- API ---------------------------------------------------------------
    def set_icon(self, icon_name: str) -> None:
        self.icon_label.setPixmap(icons.pixmap(icon_name, theme.FG_MUTED, self._icon_px))
        self.icon_label.setFixedHeight(self._icon_px)

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)

    def set_hint(self, hint: str) -> None:
        self.hint_label.setText(hint)
        self.hint_label.setVisible(bool(hint))

    def set_action(self, text: str | None, icon_name: str | None = None) -> None:
        """Show the action button with ``text`` (``None`` hides it)."""
        if icon_name is not None:
            self._action_icon = icon_name
        self.action_button.setVisible(bool(text))
        spacer = self.layout().itemAt(self._action_spacer_index)
        if spacer is not None and spacer.spacerItem() is not None:
            spacer.spacerItem().changeSize(0, self._action_gap if text else 0)
            self.layout().invalidate()
        if not text:
            return
        self.action_button.setText(text)
        if self._action_icon:
            if self._action_variant == "primary":
                color = theme.ACCENT_FG
            elif self._action_variant == "success":
                color = "#06170d"
            else:
                color = None
            self.action_button.setIcon(icons.icon(self._action_icon, color))
            self.action_button.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
