"""Single dark QSS theme applied app-wide: #121212 base with a single
orange accent used everywhere a highlight color is needed."""

BG = "#121212"
BG_ELEVATED = "#1a1a1a"
PANEL = "#1e1e1e"
BORDER = "#2c2c2c"
INPUT_BG = "#262626"
FG = "#e6e6e6"
FG_DIM = "#9a9a9a"
ACCENT = "#E67E22"
ACCENT_HOVER = "#F0954A"
ACCENT_PRESSED = "#C9691A"
SUCCESS = "#27ae60"
DANGER = "#c0392b"
WARNING = "#e0a020"

FONT_FAMILY = "Segoe UI"

STYLESHEET = f"""
* {{
    font-family: '{FONT_FAMILY}';
    color: {FG};
    outline: none;
}}

QWidget {{
    background-color: {BG};
}}

QMainWindow, QDialog {{
    background-color: {BG};
}}

QLabel {{
    background: transparent;
}}

QLabel[role="heading"] {{
    font-size: 18px;
    font-weight: 600;
    color: {FG};
}}

QLabel[role="subheading"] {{
    font-size: 12px;
    color: {FG_DIM};
}}

QMenuBar {{
    background-color: {BG_ELEVATED};
    border-bottom: 1px solid {BORDER};
    padding: 2px;
}}
QMenuBar::item {{
    padding: 6px 10px;
    background: transparent;
    border-radius: 4px;
}}
QMenuBar::item:selected {{
    background-color: {ACCENT};
}}
QMenu {{
    background-color: {BG_ELEVATED};
    border: 1px solid {BORDER};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 24px 6px 12px;
    border-radius: 3px;
}}
QMenu::item:selected {{
    background-color: {ACCENT};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
}}

QToolBar {{
    background-color: {BG_ELEVATED};
    border: none;
    padding: 4px;
    spacing: 4px;
}}

QStatusBar {{
    background-color: {BG_ELEVATED};
    color: {FG_DIM};
    border-top: 1px solid {BORDER};
}}

QDockWidget {{
    color: {FG};
    titlebar-close-icon: none;
    font-weight: 600;
}}
QDockWidget::title {{
    background-color: {PANEL};
    padding: 6px 8px;
    border-bottom: 1px solid {BORDER};
}}

QPushButton {{
    background-color: {ACCENT};
    color: white;
    border: none;
    border-radius: 5px;
    padding: 7px 16px;
    font-weight: 600;
}}
QPushButton:hover {{ background-color: {ACCENT_HOVER}; }}
QPushButton:pressed {{ background-color: {ACCENT_PRESSED}; }}
QPushButton:disabled {{ background-color: #3a3a3a; color: #777; }}

QPushButton[variant="flat"] {{
    background-color: transparent;
    border: 1px solid {BORDER};
    color: {FG};
}}
QPushButton[variant="flat"]:hover {{ background-color: {INPUT_BG}; }}

QPushButton[variant="danger"] {{ background-color: {DANGER}; }}
QPushButton[variant="danger"]:hover {{ background-color: #d84a3a; }}

QPushButton[variant="success"] {{ background-color: {SUCCESS}; }}
QPushButton[variant="success"]:hover {{ background-color: #2ecc71; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QPlainTextEdit {{
    background-color: {INPUT_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 6px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTextEdit:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background-color: {BG_ELEVATED};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {ACCENT};
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT_HOVER}; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}

QScrollBar:vertical {{
    background: {BG};
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: #444; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QScrollBar:horizontal {{
    background: {BG};
    height: 12px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER};
    border-radius: 5px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{ background: #444; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QTabWidget::pane {{
    border: 1px solid {BORDER};
    background: {PANEL};
}}
QTabBar::tab {{
    background: {BG_ELEVATED};
    padding: 6px 14px;
    border: 1px solid {BORDER};
    border-bottom: none;
}}
QTabBar::tab:selected {{
    background: {PANEL};
    border-bottom: 2px solid {ACCENT};
}}

QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}}

QSplitter::handle {{
    background: {BORDER};
}}

QToolTip {{
    background-color: {BG_ELEVATED};
    color: {FG};
    border: 1px solid {BORDER};
    padding: 4px 6px;
}}

QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px;
    height: 14px;
}}

#TimelineView, #Viewport3D {{
    background-color: #000000;
    border: 1px solid {BORDER};
}}
"""
