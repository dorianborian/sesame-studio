"""Icon buttons and keyboard-shortcut hints."""

from __future__ import annotations

import html

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QToolButton, QWidget

from .. import icons, theme

__all__ = ["IconButton", "KeyHint", "tooltip_with_shortcut"]


def tooltip_with_shortcut(text: str, shortcut: str | None = None) -> str:
    """Tooltip text with the shortcut shown as a dimmed hint after it.

    Returns plain ``text`` when there is no shortcut, otherwise rich text, so
    it can be passed to any ``setToolTip``.
    """
    if not shortcut:
        return text
    return (
        f"<span style='white-space:nowrap;'>{html.escape(text)}"
        f"&nbsp;&nbsp;<span style='color:{theme.FG_MUTED};'>{html.escape(shortcut)}</span></span>"
    )


class IconButton(QToolButton):
    """Tool button with a themed vector icon.

    ``IconButton("undo", "Undo", "Ctrl+Z")`` gives a flat 28px square button
    whose tooltip reads "Undo  Ctrl+Z". Pass ``text=`` for icon + label,
    ``checkable=True`` for a toggle (the icon turns accent when on),
    ``variant=None | "primary" | "success" | "danger" | "flat"`` for the fill.
    The shortcut is only a hint unless ``bind_shortcut=True``.
    """

    def __init__(self, icon_name: str, tooltip: str = "", shortcut: str | None = None,
                 parent: QWidget | None = None, *, text: str | None = None,
                 checkable: bool = False, variant: str | None = "flat",
                 icon_size: int = theme.ICON_SIZE, bind_shortcut: bool = False):
        super().__init__(parent)
        self._icon_name = icon_name
        self._icon_px = int(icon_size)
        self._variant = variant
        self._tooltip = tooltip
        self._shortcut = shortcut
        self.setCheckable(checkable)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(self._icon_px, self._icon_px))
        if variant:
            self.setProperty("variant", variant)
        if text:
            self.setText(text)
            self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        else:
            self.setProperty("iconOnly", "true")
            self.setToolButtonStyle(Qt.ToolButtonIconOnly)
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        if tooltip or shortcut:
            self.setToolTip(tooltip_with_shortcut(tooltip, shortcut))
            self.setAccessibleName(tooltip)
        if shortcut and bind_shortcut:
            self.setShortcut(QKeySequence(shortcut))
        self._apply_icon()

    # -- API ---------------------------------------------------------------
    def icon_name(self) -> str:
        return self._icon_name

    def set_icon(self, icon_name: str) -> None:
        """Swap the glyph (e.g. play <-> pause)."""
        if icon_name != self._icon_name:
            self._icon_name = icon_name
            self._apply_icon()

    def set_tooltip(self, tooltip: str, shortcut: str | None = None) -> None:
        self._tooltip, self._shortcut = tooltip, shortcut
        self.setToolTip(tooltip_with_shortcut(tooltip, shortcut))
        self.setAccessibleName(tooltip)

    def set_variant(self, variant: str | None) -> None:
        self._variant = variant
        theme.set_variant(self, variant)
        self._apply_icon()

    # -- internals -----------------------------------------------------------
    def _apply_icon(self) -> None:
        # Filled variants need a glyph that contrasts with the fill; everything
        # else follows the theme (FG, accent when checked, dim when disabled).
        if self._variant == "primary":
            color = theme.ACCENT_FG
        elif self._variant == "success":
            color = "#06170d"
        else:
            color = None
        self.setIcon(icons.icon(self._icon_name, color, self._icon_px))


class KeyHint(QWidget):
    """Key-cap chips for a shortcut: ``KeyHint("Ctrl+Shift+S")``."""

    def __init__(self, sequence: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(3)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._sequence = ""
        self.set_sequence(sequence)

    def sequence(self) -> str:
        return self._sequence

    def set_sequence(self, sequence: str) -> None:
        self._sequence = sequence or ""
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for key in self._split(self._sequence):
            chip = QLabel(key)
            chip.setProperty("role", "key")
            chip.setAlignment(Qt.AlignCenter)
            self._layout.addWidget(chip)
        self.setVisible(bool(self._sequence))
        self.updateGeometry()

    @staticmethod
    def _split(sequence: str) -> list[str]:
        if not sequence:
            return []
        if sequence == "+":
            return ["+"]
        parts = [part.strip() for part in sequence.replace("++", "+Plus").split("+")]
        return [("+" if part == "Plus" else part) for part in parts if part]
