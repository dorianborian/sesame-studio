"""Transient status notifications ("toasts") that fade in/out over the
main window instead of blocking dialogs for routine confirmations."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import QLabel, QGraphicsOpacityEffect, QWidget

from ..theme import ACCENT, SUCCESS, WARNING, DANGER

_LEVEL_COLORS = {
    "info": ACCENT,
    "success": SUCCESS,
    "warning": WARNING,
    "error": DANGER,
}


class Toast(QLabel):
    def __init__(self, parent: QWidget, text: str, level: str = "info", duration_ms: int = 2600):
        super().__init__(parent)
        color = _LEVEL_COLORS.get(level, _LEVEL_COLORS["info"])
        self.setText(text)
        self.setStyleSheet(
            f"background-color:{color}; color:white; padding:10px 18px;"
            "border-radius:6px; font-weight:600;"
        )
        self.setAlignment(Qt.AlignCenter)
        self.adjustSize()
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._effect.setOpacity(0.0)
        self._reposition()
        self.show()
        self.raise_()

        self._fade_in = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade_in.setDuration(180)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_in.start()

        QTimer.singleShot(duration_ms, self._fade_out)

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent:
            x = (parent.width() - self.width()) // 2
            y = parent.height() - self.height() - 36
            self.move(max(0, x), max(0, y))

    def _fade_out(self) -> None:
        anim = QPropertyAnimation(self._effect, b"opacity", self)
        anim.setDuration(260)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.finished.connect(self.deleteLater)
        anim.start()
        self._fade_out_anim = anim  # keep alive


def show_toast(parent: QWidget, text: str, level: str = "info") -> None:
    Toast(parent, text, level)
