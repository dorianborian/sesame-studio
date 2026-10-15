"""First screen: New Project / Open Project / Recent Projects cards."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPixmap, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QFrame, QGraphicsDropShadowEffect,
)

from .theme import BG, PANEL, BORDER, ACCENT, FG_DIM


class Card(QFrame):
    clicked = Signal()

    def __init__(self, title: str, subtitle: str, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(220, 140)
        self.setStyleSheet(
            f"QFrame {{ background-color:{PANEL}; border:1px solid {BORDER}; border-radius:10px; }}"
            f"QFrame:hover {{ border:1px solid {ACCENT}; }}"
        )
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18)
        shadow.setColor(Qt.black)
        shadow.setOffset(0, 4)
        self.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addStretch(1)
        t = QLabel(title)
        t.setStyleSheet("font-size:16px; font-weight:700; border:none;")
        layout.addWidget(t)
        s = QLabel(subtitle)
        s.setWordWrap(True)
        s.setStyleSheet(f"color:{FG_DIM}; font-size:11px; border:none;")
        layout.addWidget(s)
        layout.addStretch(1)

    def mousePressEvent(self, event) -> None:
        self.clicked.emit()
        super().mousePressEvent(event)


class WelcomePage(QWidget):
    newProjectRequested = Signal()
    openProjectRequested = Signal()
    openFaceBitmapsRequested = Signal()
    recentProjectRequested = Signal(str)
    tutorialRequested = Signal()

    def __init__(self, logo_path: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background-color:{BG};")
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignCenter)
        outer.setSpacing(24)

        header = QVBoxLayout()
        header.setAlignment(Qt.AlignCenter)
        if os.path.exists(logo_path):
            logo = QLabel()
            logo.setAlignment(Qt.AlignCenter)
            logo.setPixmap(QPixmap(logo_path).scaled(
                QSize(84, 84), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            header.addWidget(logo)
        title = QLabel("Sesame Studio")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f"color:{ACCENT}; font-size:30px; font-weight:800;")
        header.addWidget(title)
        subtitle = QLabel("Pose, animate, and design faces for your Sesame Robot.")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet(f"color:{FG_DIM}; font-size:13px;")
        header.addWidget(subtitle)
        outer.addLayout(header)

        cards = QHBoxLayout()
        cards.setSpacing(18)
        new_card = Card("New Project", "Start a fresh animation project with default poses.")
        new_card.clicked.connect(self.newProjectRequested.emit)
        open_card = Card("Open Project", "Load an existing .sesproj file from disk.")
        open_card.clicked.connect(self.openProjectRequested.emit)
        header_card = Card("Edit face-bitmaps.h", "Open firmware face-bitmaps.h to edit and save bitmaps directly.")
        header_card.clicked.connect(self.openFaceBitmapsRequested.emit)
        tutorial_card = Card("Getting Started", "Walk through posing, the timeline, and faces.")
        tutorial_card.clicked.connect(self.tutorialRequested.emit)
        cards.addWidget(new_card)
        cards.addWidget(open_card)
        cards.addWidget(header_card)
        cards.addWidget(tutorial_card)
        cards_wrap = QWidget()
        cards_wrap.setLayout(cards)
        outer.addWidget(cards_wrap, alignment=Qt.AlignCenter)

        recent_label = QLabel("Recent Files")
        recent_label.setStyleSheet(f"color:{FG_DIM}; font-size:11px; font-weight:600;")
        outer.addWidget(recent_label, alignment=Qt.AlignCenter)

        self.recent_list = QListWidget()
        self.recent_list.setFixedSize(680, 120)
        self.recent_list.setStyleSheet(
            f"background-color:{PANEL}; border:1px solid {BORDER}; border-radius:6px;"
        )
        self.recent_list.itemDoubleClicked.connect(
            lambda item: self.recentProjectRequested.emit(item.data(Qt.UserRole))
        )
        outer.addWidget(self.recent_list, alignment=Qt.AlignCenter)

    def set_recent(self, paths: list[str]) -> None:
        self.recent_list.clear()
        for path in paths:
            item = QListWidgetItem(os.path.basename(path))
            item.setToolTip(path)
            item.setData(Qt.UserRole, path)
            self.recent_list.addItem(item)
        if not paths:
            placeholder = QListWidgetItem("No recent projects yet")
            placeholder.setFlags(Qt.NoItemFlags)
            self.recent_list.addItem(placeholder)
