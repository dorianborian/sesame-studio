"""Segmented control: a row of mutually exclusive pill buttons."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QSizePolicy, QToolButton, QWidget,
)

from .. import icons, theme
from .buttons import tooltip_with_shortcut

__all__ = ["SegmentedControl"]


class SegmentedControl(QFrame):
    """Exclusive choice shown as joined pills.

    ::

        mode = SegmentedControl([("move", "Move", "move"), ("rotate", "Rotate", "rotate")])
        mode.currentChanged.connect(on_mode)      # emits the option key
        mode.set_current("rotate")

    Each option is ``key``, ``(key, label)``, ``(key, label, icon_name)`` or
    ``(key, label, icon_name, tooltip)``. With ``icon_only=True`` the labels
    become tooltips and only the icons are shown. Arrow keys move the
    selection when the control has keyboard focus.
    """

    currentChanged = Signal(str)

    def __init__(self, options=(), parent: QWidget | None = None, *,
                 current: str | None = None, icon_only: bool = False):
        super().__init__(parent)
        self.setProperty("role", "segmented")
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.setFixedHeight(theme.CONTROL_HEIGHT)
        self._icon_only = icon_only
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(3, 3, 3, 3)  # 1px frame border + 2px inset
        self._layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QToolButton] = {}
        self._current: str | None = None
        self._group.buttonToggled.connect(self._on_toggled)
        for option in options:
            if isinstance(option, str):
                self.add_option(option)
            else:
                self.add_option(*option)
        if current is not None:
            self.set_current(current)
        elif self._buttons:
            self.set_current(next(iter(self._buttons)))

    # -- API ---------------------------------------------------------------
    def add_option(self, key: str, label: str | None = None, icon_name: str | None = None,
                   tooltip: str | None = None, shortcut: str | None = None) -> QToolButton:
        """Append an option and return its button."""
        label = key if label is None else label
        btn = QToolButton(self)
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.TabFocus)
        btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        if icon_name:
            btn.setIcon(icons.icon(icon_name, None, theme.ICON_SIZE))
            btn.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
        if self._icon_only and icon_name:
            btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
            btn.setProperty("iconOnly", "true")
            btn.setToolTip(tooltip_with_shortcut(tooltip or label, shortcut))
            btn.setAccessibleName(label)
        else:
            btn.setText(label)
            btn.setToolButtonStyle(
                Qt.ToolButtonTextBesideIcon if icon_name else Qt.ToolButtonTextOnly)
            if tooltip or shortcut:
                btn.setToolTip(tooltip_with_shortcut(tooltip or label, shortcut))
        self._group.addButton(btn)
        self._layout.addWidget(btn)
        self._buttons[key] = btn
        return btn

    def keys(self) -> list[str]:
        return list(self._buttons)

    def button(self, key: str) -> QToolButton | None:
        return self._buttons.get(key)

    def current(self) -> str | None:
        return self._current

    def set_current(self, key: str, emit: bool = False) -> None:
        """Select ``key``. Emits ``currentChanged`` only if ``emit`` is true."""
        btn = self._buttons.get(key)
        if btn is None or key == self._current:
            return
        blocked = self.blockSignals(not emit)
        try:
            btn.setChecked(True)
        finally:
            self.blockSignals(blocked)

    def set_option_enabled(self, key: str, enabled: bool) -> None:
        btn = self._buttons.get(key)
        if btn is not None:
            btn.setEnabled(enabled)

    # -- internals -----------------------------------------------------------
    def _on_toggled(self, button, checked: bool) -> None:
        if not checked:
            return
        for key, btn in self._buttons.items():
            if btn is button:
                if key != self._current:
                    self._current = key
                    self.currentChanged.emit(key)
                return
