"""Transient, non-blocking notifications ("toasts").

``show_toast(parent, text, level="info")`` pops a small pill at the bottom of
``parent``'s window. Toasts stack (newest at the bottom), slide/fade in and
out, follow the window when it is resized, never take keyboard focus, pause
while hovered and can be dismissed with a click. Repeating a message that is
still on screen just restarts its timer instead of stacking a duplicate.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, Qt, QTimer,
)
from PySide6.QtWidgets import (
    QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QMainWindow, QStatusBar, QWidget,
)

from .. import icons, theme

__all__ = ["Toast", "show_toast"]

_LEVELS = {
    # level: (icon name, icon colour, base duration ms)
    "info": ("info", theme.INFO, 2600),
    "success": ("check-circle", theme.SUCCESS, 2600),
    "warning": ("warning", theme.WARNING, 3800),
    "error": ("x-circle", theme.DANGER, 5000),
}
_MAX_VISIBLE = 4
_GAP = 8
_MAX_TEXT_WIDTH = 440


class _ToastHost(QObject):
    """Stacks and positions the toasts of one window."""

    NAME = "_sesame_toast_host"

    def __init__(self, window: QWidget):
        super().__init__(window)
        self.setObjectName(self.NAME)
        self.window = window
        self.toasts: list[Toast] = []
        window.installEventFilter(self)

    @classmethod
    def for_window(cls, window: QWidget) -> "_ToastHost":
        for child in window.children():
            if isinstance(child, _ToastHost):
                return child
        return cls(window)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.window and event.type() in (QEvent.Resize, QEvent.Show):
            self.relayout(animate=False)
        return False

    def find(self, text: str, level: str) -> "Toast | None":
        for toast in self.toasts:
            if toast.text() == text and toast.level() == level and not toast.is_closing():
                return toast
        return None

    def add(self, toast: "Toast") -> None:
        self.toasts.append(toast)
        alive = [t for t in self.toasts if not t.is_closing()]
        for old in alive[:-_MAX_VISIBLE]:
            old.dismiss()
        self.relayout(animate=True, entering=toast)

    def remove(self, toast: "Toast") -> None:
        if toast in self.toasts:
            self.toasts.remove(toast)
            self.relayout(animate=True)

    def _bottom_margin(self) -> int:
        margin = 20
        if isinstance(self.window, QMainWindow):
            # findChild, not statusBar(): the latter would create one.
            bar = self.window.findChild(QStatusBar, options=Qt.FindDirectChildrenOnly)
            if bar is not None and bar.isVisible():
                margin += bar.height()
        return margin

    def relayout(self, animate: bool, entering: "Toast | None" = None) -> None:
        width = self.window.width()
        bottom = self.window.height() - self._bottom_margin()
        for toast in reversed(self.toasts):
            toast.fit(width)
            x = max(8, (width - toast.width()) // 2)
            y = max(8, bottom - toast.height())
            target = QPoint(x, y)
            if toast is entering:
                toast.enter_at(target)
            else:
                toast.slide_to(target, animate)
            bottom = y - _GAP


class Toast(QFrame):
    """One notification. Normally created through :func:`show_toast`."""

    def __init__(self, parent: QWidget, text: str, level: str = "info",
                 duration_ms: int | None = None):
        window = parent.window() if parent is not None else None
        super().__init__(window)
        self._level = level if level in _LEVELS else "info"
        self._text = text
        self._closing = False
        icon_name, color, base_ms = _LEVELS[self._level]
        if duration_ms is None:
            duration_ms = base_ms + min(3000, 28 * len(text))
        self._duration_ms = int(duration_ms)

        self.setProperty("role", "toast")
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setAccessibleName(text)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 14, 9)
        layout.setSpacing(9)
        icon_label = QLabel()
        icon_label.setPixmap(icons.pixmap(icon_name, color, 16))
        icon_label.setFixedSize(16, 16)
        layout.addWidget(icon_label, 0, Qt.AlignTop)
        self._label = QLabel(text)
        self._label.setTextFormat(Qt.PlainText)
        self._label.setFont(theme.ui_font(theme.FONT_SIZE, 500))
        layout.addWidget(self._label, 1)

        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._move = QPropertyAnimation(self, b"pos", self)
        self._move.setEasingCurve(QEasingCurve.OutCubic)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

        self._host = _ToastHost.for_window(window) if window is not None else None
        if self._host is None:
            self.hide()
            return
        self.show()
        self.raise_()
        self._host.add(self)
        self._timer.start(self._duration_ms)

    # -- queries -----------------------------------------------------------
    def text(self) -> str:
        return self._text

    def level(self) -> str:
        return self._level

    def is_closing(self) -> bool:
        return self._closing

    # -- layout (driven by the host) ------------------------------------------
    def fit(self, window_width: int) -> None:
        limit = max(120, min(_MAX_TEXT_WIDTH, window_width - 96))
        natural = self._label.fontMetrics().horizontalAdvance(self._text) + 2
        self._label.setWordWrap(natural > limit)
        self._label.setFixedWidth(min(natural, limit))
        self.adjustSize()

    def enter_at(self, target: QPoint) -> None:
        self.move(target + QPoint(0, 12))
        self._animate_opacity(1.0, 160, QEasingCurve.OutCubic)
        self._animate_pos(target, 200)

    def slide_to(self, target: QPoint, animate: bool) -> None:
        if not animate or not self.isVisible():
            self._move.stop()
            self.move(target)
        elif self._move.state() == QPropertyAnimation.Running and self._move.endValue() == target:
            return
        elif self.pos() != target:
            self._animate_pos(target, 160)

    def _animate_pos(self, target: QPoint, ms: int) -> None:
        self._move.stop()
        self._move.setDuration(ms)
        self._move.setStartValue(self.pos())
        self._move.setEndValue(target)
        self._move.start()

    def _animate_opacity(self, end: float, ms: int, curve) -> None:
        self._fade.stop()
        self._fade.setDuration(ms)
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(end)
        self._fade.setEasingCurve(curve)
        self._fade.start()

    # -- lifetime -----------------------------------------------------------
    def restart(self) -> None:
        """Keep the toast up for another full duration."""
        if not self._closing:
            self._timer.start(self._duration_ms)
            self.raise_()

    def dismiss(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._timer.stop()
        self._animate_opacity(0.0, 200, QEasingCurve.InCubic)
        self._fade.finished.connect(self._finish)

    def _finish(self) -> None:
        if self._host is not None:
            self._host.remove(self)
        self.hide()
        self.deleteLater()

    # -- interaction ----------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        self.dismiss()
        event.accept()

    def enterEvent(self, event) -> None:
        if not self._closing:
            self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._closing:
            self._timer.start(max(1200, self._duration_ms // 2))
        super().leaveEvent(event)


def show_toast(parent: QWidget, text: str, level: str = "info",
               duration_ms: int | None = None) -> Toast | None:
    """Show ``text`` as a toast over ``parent``'s window.

    ``level`` is ``"info"``, ``"success"``, ``"warning"`` or ``"error"``
    (unknown values fall back to info). Returns the toast, or ``None`` when
    there is no window to attach to.
    """
    if parent is None:
        return None
    window = parent.window()
    if window is None:
        return None
    level = level if level in _LEVELS else "info"
    existing = _ToastHost.for_window(window).find(text, level)
    if existing is not None:
        existing.restart()
        return existing
    return Toast(parent, text, level, duration_ms)
