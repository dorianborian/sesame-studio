"""Animated logo splash shown on launch, before the main window appears."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer, Signal, QPropertyAnimation, QEasingCurve, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QGraphicsOpacityEffect

from .theme import BG, FG_DIM, ACCENT


class SplashScreen(QWidget):
    finished = Signal()

    def __init__(self, logo_path: str, min_duration_ms: int = 1400):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFixedSize(420, 320)
        self.setStyleSheet(f"background-color:{BG};")

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(14)

        self.logo_label = QLabel()
        self.logo_label.setAlignment(Qt.AlignCenter)
        if os.path.exists(logo_path):
            pix = QPixmap(logo_path).scaled(
                QSize(140, 140), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self.logo_label.setPixmap(pix)
        layout.addWidget(self.logo_label)

        title = QLabel("SESAME STUDIO")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f"color:{ACCENT}; font-size:22px; font-weight:800; letter-spacing:2px;")
        layout.addWidget(title)

        subtitle = QLabel("Loading workspace...")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet(f"color:{FG_DIM}; font-size:11px;")
        layout.addWidget(subtitle)

        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._effect.setOpacity(0.0)

        self._center_on_screen()
        self._min_duration_ms = min_duration_ms

    def _center_on_screen(self) -> None:
        screen = self.screen()
        if screen:
            geo = screen.availableGeometry()
            self.move(
                geo.x() + (geo.width() - self.width()) // 2,
                geo.y() + (geo.height() - self.height()) // 2,
            )

    def start(self) -> None:
        self.show()
        fade_in = QPropertyAnimation(self._effect, b"opacity", self)
        fade_in.setDuration(320)
        fade_in.setStartValue(0.0)
        fade_in.setEndValue(1.0)
        fade_in.setEasingCurve(QEasingCurve.OutCubic)
        fade_in.start()
        self._fade_in = fade_in
        QTimer.singleShot(self._min_duration_ms, self._finish)

    def _finish(self) -> None:
        fade_out = QPropertyAnimation(self._effect, b"opacity", self)
        fade_out.setDuration(260)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.0)
        fade_out.setEasingCurve(QEasingCurve.InCubic)
        fade_out.finished.connect(self._close_and_notify)
        fade_out.start()
        self._fade_out = fade_out

    def _close_and_notify(self) -> None:
        self.close()
        self.finished.emit()
