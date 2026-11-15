"""Collapsible section used to keep dense dock panels tidy."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from .. import icons, theme

__all__ = ["CollapsibleSection"]

_MAX = 16777215  # QWIDGETSIZE_MAX


class CollapsibleSection(QWidget):
    """A titled section whose body folds away.

    ``CollapsibleSection("Servo Angles", content_widget)`` shows a chevron
    header; clicking it (or Space/Enter when focused) smoothly expands or
    collapses ``content``. ``toggle_btn`` (checked = expanded) and ``content``
    stay available as attributes; ``add_header_widget`` puts small controls at
    the right end of the header row.
    """

    toggled = Signal(bool)

    def __init__(self, title: str, content: QWidget, parent=None, expanded: bool = True):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QHBoxLayout()
        self._header.setContentsMargins(0, 0, 0, 0)
        self._header.setSpacing(theme.SPACING_XS)
        self.toggle_btn = QToolButton()
        self.toggle_btn.setProperty("role", "sectionHeader")
        self.toggle_btn.setText(title)
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setChecked(expanded)
        self.toggle_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle_btn.setIconSize(QSize(12, 12))
        self.toggle_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.toggle_btn.toggled.connect(self._on_toggled)
        self._header.addWidget(self.toggle_btn, 1)
        layout.addLayout(self._header)

        self.content = content
        self.content.setVisible(expanded)
        layout.addWidget(self.content)

        self._anim = QPropertyAnimation(self.content, b"maximumHeight", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(self._on_anim_finished)
        self._animate_next = True
        self._update_chevron()

    # -- API ---------------------------------------------------------------
    def is_expanded(self) -> bool:
        return self.toggle_btn.isChecked()

    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        if expanded == self.toggle_btn.isChecked():
            return
        self._animate_next = animate
        try:
            self.toggle_btn.setChecked(expanded)
        finally:
            self._animate_next = True

    def set_title(self, title: str) -> None:
        self.toggle_btn.setText(title)

    def title(self) -> str:
        return self.toggle_btn.text()

    def add_header_widget(self, widget: QWidget) -> QWidget:
        """Add a small control (e.g. an IconButton) at the right of the header."""
        self._header.addWidget(widget, 0, Qt.AlignVCenter)
        return widget

    # -- internals -----------------------------------------------------------
    def _update_chevron(self) -> None:
        name = "chevron-down" if self.toggle_btn.isChecked() else "chevron-right"
        self.toggle_btn.setIcon(icons.icon(name, theme.FG_MUTED, 12))

    def _on_toggled(self, expanded: bool) -> None:
        self._update_chevron()
        self._anim.stop()
        if not (self._animate_next and self.isVisible()):
            self.content.setMaximumHeight(_MAX)
            self.content.setVisible(expanded)
        elif expanded:
            self.content.setMaximumHeight(0)
            self.content.setVisible(True)
            target = max(self.content.sizeHint().height(),
                         self.content.minimumSizeHint().height())
            if self.content.hasHeightForWidth() and self.content.width() > 0:
                target = max(target, self.content.heightForWidth(self.content.width()))
            self._anim.setStartValue(0)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._anim.setStartValue(self.content.height())
            self._anim.setEndValue(0)
            self._anim.start()
        self.toggled.emit(expanded)

    def _on_anim_finished(self) -> None:
        if not self.toggle_btn.isChecked():
            self.content.setVisible(False)
        self.content.setMaximumHeight(_MAX)

    # Backwards-compatible name used by the first version of this widget.
    def _toggle(self) -> None:
        self._on_toggled(self.toggle_btn.isChecked())
