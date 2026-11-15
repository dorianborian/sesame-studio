"""Launch splash: logo, product name, version and a thin progress line.

Contract: ``SplashScreen(logo_path)``, ``start()`` and the ``finished``
signal. The splash fades in, stays for ``min_duration_ms`` (well under a
second by default), emits ``finished`` and then fades out *over* whatever the
caller shows in response, so there is never an empty desktop in between.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve, QPropertyAnimation, QRectF, Qt, QTimer, QVariantAnimation, Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from . import __version__, theme
from .widgets.logo import logo_pixmap

_RADIUS = 14


class SplashScreen(QWidget):
    finished = Signal()

    def __init__(self, logo_path: str, min_duration_ms: int = 650):
        super().__init__(None, Qt.SplashScreen | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setStyleSheet("background: transparent;")
        self.setFixedSize(400, 292)
        self._min_duration_ms = max(0, int(min_duration_ms))
        self._progress = 0.0
        self._done = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 30, 32, 30)
        layout.setSpacing(0)
        layout.addStretch(1)

        self.logo_label = QLabel()
        self.logo_label.setAlignment(Qt.AlignCenter)
        pix = logo_pixmap(logo_path, 128)
        if not pix.isNull():
            self.logo_label.setPixmap(pix)
            self.logo_label.setFixedHeight(128)
        layout.addWidget(self.logo_label)
        layout.addSpacing(14)

        title = QLabel("Sesame Studio")
        title.setAlignment(Qt.AlignCenter)
        title_font = theme.ui_font(22, 600)
        title_font.setLetterSpacing(QFont.AbsoluteSpacing, 0.2)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {theme.FG};")
        layout.addWidget(title)
        layout.addSpacing(4)

        subtitle = QLabel(f"Version {__version__}")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setFont(theme.ui_font(theme.FONT_SIZE_SM))
        subtitle.setStyleSheet(f"color: {theme.FG_MUTED};")
        layout.addWidget(subtitle)
        layout.addStretch(1)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._bar = QVariantAnimation(self)
        self._bar.setStartValue(0.0)
        self._bar.setEndValue(1.0)
        self._bar.setEasingCurve(QEasingCurve.InOutCubic)
        self._bar.valueChanged.connect(self._set_progress)
        self._center_on_screen()

    # -- painting -------------------------------------------------------------
    def _set_progress(self, value) -> None:
        self._progress = float(value)
        self.update(0, self.height() - 12, self.width(), 12)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        card = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        shape = QPainterPath()
        shape.addRoundedRect(card, _RADIUS, _RADIUS)
        painter.fillPath(shape, QColor(theme.BG_ELEVATED))
        # progress line hugging the bottom edge, clipped to the rounded card
        painter.save()
        painter.setClipPath(shape)
        track = QRectF(0, self.height() - 3, self.width(), 3)
        painter.fillRect(track, QColor(theme.BORDER))
        painter.fillRect(QRectF(0, track.top(), track.width() * self._progress, 3),
                         QColor(theme.ACCENT))
        painter.restore()
        painter.setPen(QPen(QColor(theme.BORDER_CONTROL), 1))
        painter.drawPath(shape)

    # -- lifecycle --------------------------------------------------------------
    def _center_on_screen(self) -> None:
        screen = self.screen()
        if screen:
            geo = screen.availableGeometry()
            self.move(
                geo.x() + (geo.width() - self.width()) // 2,
                geo.y() + (geo.height() - self.height()) // 2,
            )

    def start(self) -> None:
        self.setWindowOpacity(0.0)
        self.show()
        self._fade.stop()
        self._fade.setDuration(140)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)
        self._fade.start()
        self._bar.setDuration(max(1, self._min_duration_ms))
        self._bar.start()
        QTimer.singleShot(self._min_duration_ms, self._finish)

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        self._set_progress(1.0)
        # Let the caller bring up the main window first; the splash stays on
        # top and fades away over it once the event loop is free again.
        self.finished.emit()
        QTimer.singleShot(0, self._fade_out)

    def _fade_out(self) -> None:
        self._fade.stop()
        self._fade.setDuration(170)
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.setEasingCurve(QEasingCurve.InCubic)
        self._fade.finished.connect(self.close)
        self._fade.start()
