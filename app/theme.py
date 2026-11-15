"""Sesame Studio design system: colour/metric tokens + the app-wide stylesheet.

How to use
----------
* At startup call ``theme.apply_theme(app)``. It selects the Fusion base style,
  installs a dark ``QPalette`` (so custom-painted widgets, rich-text links and
  anything QSS cannot reach are dark too), asks the platform for dark window
  frames and applies ``STYLESHEET``. ``app.setStyleSheet(STYLESHEET)`` on its
  own also works (every stock control is styled explicitly) but leaves the
  palette/base style to the OS, so prefer ``apply_theme``.
* Never hard-code hex colours or call ``setStyleSheet`` with literals in
  widgets -- import tokens from here, or better, tag the widget with one of
  the dynamic properties below and let the stylesheet do it. After changing a
  property on a widget that is already shown, call ``theme.repolish(widget)``.
* Icons come from ``app.icons`` (``icon(name)``), shared widgets from
  ``app.widgets``.

Tokens (names are a stable contract; values may be retuned)
-----------------------------------------------------------
Surfaces, darkest to lightest
    ``BG_SUNKEN``   wells: timeline tracks, canvas surround, lists, code views
    ``BG``          window / dock background
    ``BG_ELEVATED`` menu bar, status bar, dock title bars, popups
    ``PANEL``       cards and toolbar strips
    ``INPUT_BG``    text fields, combo boxes, spin boxes
    ``BUTTON_BG``   neutral buttons            ``HOVER`` / ``PRESSED`` fills
Lines
    ``BORDER`` hairlines   ``BORDER_CONTROL`` control outlines
    ``BORDER_STRONG`` hovered / emphasised outlines
Text
    ``FG`` primary   ``FG_DIM`` secondary   ``FG_MUTED`` tertiary / captions
    ``FG_DISABLED`` disabled
Brand + status
    ``ACCENT`` ``ACCENT_HOVER`` ``ACCENT_PRESSED`` (warm Sesame orange)
    ``ACCENT_FG`` text/icon colour ON an accent fill (dark)
    ``ACCENT_SOFT`` low-strength accent fill (selected rows, "on" buttons)
    ``ACCENT_SOFT_BORDER`` outline that goes with ``ACCENT_SOFT``
    ``SELECTION`` secondary blue highlight (multi-select, hover outlines)
    ``SUCCESS`` ``DANGER`` ``WARNING`` ``INFO`` + ``*_SOFT`` tinted fills
Type + metrics
    ``FONT_FAMILY`` ``FONT_MONO`` ``FONT_SIZE`` (12) ``FONT_SIZE_SM`` (11)
    ``FONT_SIZE_LG`` (14) ``RADIUS`` (6) ``RADIUS_SM`` (4) ``RADIUS_LG`` (8)
    ``SPACING`` (8) ``SPACING_XS`` (4) ``SPACING_LG`` (16)
    ``CONTROL_HEIGHT`` (28: buttons, fields, combos, icon buttons)
    ``ICON_SIZE`` (16)

Dynamic-property conventions (set with ``widget.setProperty(name, value)``)
---------------------------------------------------------------------------
``QPushButton`` / ``QToolButton``
    ``variant``: unset = neutral filled; ``"primary"`` = accent filled (use
    ``icons.icon(name, theme.ACCENT_FG)`` for its glyph); ``"success"``;
    ``"danger"`` (quiet red tint, solid red on hover); ``"flat"`` = borderless,
    a fill appears on hover. Checkable buttons show an "on" state
    (accent-soft fill + accent text/icon). ``iconOnly="true"`` = square
    28px icon button. Tool buttons inside a ``QToolBar`` are flat by default.
``QLabel``
    ``role``: ``"heading"`` (18px semibold), ``"title"`` (24px semibold),
    ``"subheading"`` (dim secondary text), ``"caption"`` (11px semibold muted
    section label), ``"mono"`` (monospace), ``"badge"`` (pill; optional
    ``tone`` = ``"accent" | "success" | "danger" | "warning" | "info"``),
    ``"key"`` (keyboard key chip, see ``widgets.KeyHint``).
``QFrame`` (use a QFrame, or set ``WA_StyledBackground`` on a QWidget subclass)
    ``role``: ``"card"`` (panel surface, hairline, 8px radius; add
    ``clickable="true"`` for hover/focus feedback), ``"toolbar"`` (strip at
    the top of a dock: PANEL background + hairline bottom border),
    ``"separator"`` (hairline; give it a fixed height or width of 1),
    ``"well"`` (sunken surface), ``"segmented"`` / ``"toast"`` (used by the
    shared widgets).

Helpers
-------
``apply_theme(app)``, ``build_palette()``, ``repolish(widget)``,
``polish_popup(menu)`` (optional anti-aliased rounded menu corners),
``ui_font(size, weight)``, ``mono_font(size)``, ``qcolor(token, alpha)``,
``mix(a, b, t)``, ``set_variant(button, variant)``, ``set_role(widget, role)``.

Things to know (Qt 6.12 behaviour the stylesheet works around)
--------------------------------------------------------------
* Fonts: the stylesheet sets no global font, so ``widget.setFont(...)`` works
  everywhere (use ``mono_font()`` for code views). ``apply_theme`` sets the
  application font to ``FONT_FAMILY`` 9pt.
* Spin boxes are right-aligned by the stylesheet (Qt ignores their horizontal
  padding) and must keep their step buttons (``NoButtons`` collapses the
  editor of a styled spin box).
* List/tree rows get their height from ``::item`` padding (28px rows). The
  stylesheet forces a re-layout when it is first applied, so views filled
  before they are shown are measured correctly; custom delegates should
  return their own ``sizeHint``.
* Icons default to 16px; ``QToolBar`` is pinned to 16px too.
* A ``role`` frame's QSS border is not part of its layout: keep at least 1px
  of layout margin so children do not paint over the hairline.

Organisation rule: the Comic Sans typeface is never used anywhere.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile


# ---------------------------------------------------------------------------
# small colour helpers (pure python, usable before any Qt import)
# ---------------------------------------------------------------------------
def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def mix(a: str, b: str, t: float) -> str:
    """Blend hex colour ``a`` towards ``b`` by ``t`` (0..1); returns ``#rrggbb``."""
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    t = max(0.0, min(1.0, t))
    return "#{:02x}{:02x}{:02x}".format(
        round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t))


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------
# surfaces
BG_SUNKEN = "#0d0d0f"        # deepest wells: timeline tracks, paint canvas surround
BG = "#141416"               # window + dock background
BG_ELEVATED = "#1a1a1d"      # menu bar, status bar, dock title bars
PANEL = "#1f1f23"            # cards, toolbar strips
INPUT_BG = "#232328"         # text fields, combos, spin boxes
BUTTON_BG = "#2a2a30"        # neutral buttons
HOVER = "#303037"            # hover fill for flat things
PRESSED = "#222227"          # pressed fill
POPUP_BG = "#202024"         # menus, combo popups, tooltips

# lines
BORDER = "#2c2c32"           # hairlines between regions
BORDER_CONTROL = "#393941"   # outlines of interactive controls
BORDER_STRONG = "#4a4a54"    # hovered / emphasised outlines

# text
FG = "#e9e9ec"
FG_DIM = "#a3a3ad"           # secondary text
FG_MUTED = "#7b7b86"         # tertiary text, tick labels, captions
FG_DISABLED = "#56565f"

# brand (warm Sesame orange) + status
ACCENT = "#f28c28"
ACCENT_HOVER = "#ffa347"
ACCENT_PRESSED = "#d97716"
ACCENT_FG = "#1c1206"        # text / icons on top of an accent fill
ACCENT_SOFT = mix(BG, ACCENT, 0.20)         # selected-row / "on" fills
ACCENT_SOFT_BORDER = mix(BG, ACCENT, 0.45)
SELECTION = "#4da3ff"        # secondary highlight (multi-select, hover outlines)
SUCCESS = "#3fb673"
DANGER = "#e5534b"
WARNING = "#e6b23e"
INFO = SELECTION
SUCCESS_SOFT = mix(BG, SUCCESS, 0.18)
DANGER_SOFT = mix(BG, DANGER, 0.18)
WARNING_SOFT = mix(BG, WARNING, 0.18)
INFO_SOFT = mix(BG, INFO, 0.18)

# type
FONT_FAMILY = "Segoe UI"
FONT_MONO = "Cascadia Mono"  # falls back to Consolas via mono_font() / QSS
FONT_SIZE = 12               # px; the base UI size (= 9pt at 96 dpi)
FONT_SIZE_SM = 11
FONT_SIZE_LG = 14

# metrics
RADIUS = 6
RADIUS_SM = 4
RADIUS_LG = 8
SPACING = 8
SPACING_XS = 4
SPACING_LG = 16
CONTROL_HEIGHT = 28
ICON_SIZE = 16


# ---------------------------------------------------------------------------
# generated image assets (check marks, chevrons ...) referenced by the QSS
# ---------------------------------------------------------------------------
# QSS can only take sub-control glyphs from image files. They are rasterised
# from app.icons into a per-user temp folder (1x/2x/3x) the first time the
# theme is imported; the folder name is a hash of the inputs so a retuned
# palette never reuses stale images. Nothing is shipped or downloaded.
_ASSET_VERSION = "4"
_ASSETS: dict[str, tuple[str, str, int, float | None]] = {
    # file stem: (icon name, colour, logical px, stroke override in grid units)
    "check": ("check", ACCENT_FG, 14, 2.1),
    "check-disabled": ("check", FG_MUTED, 14, 2.1),
    "dash": ("minus", ACCENT_FG, 14, 2.1),
    "dash-disabled": ("minus", FG_MUTED, 14, 2.1),
    "radio": ("dot", ACCENT_FG, 14, None),
    "radio-disabled": ("dot", FG_MUTED, 14, None),
    "menu-check": ("check", ACCENT, 14, 1.9),
    "menu-dot": ("dot", ACCENT, 14, None),
    "down": ("chevron-down", FG_DIM, 12, 1.7),
    "down-hover": ("chevron-down", FG, 12, 1.7),
    "down-disabled": ("chevron-down", FG_DISABLED, 12, 1.7),
    "up": ("chevron-up", FG_DIM, 12, 1.7),
    "up-hover": ("chevron-up", FG, 12, 1.7),
    "up-disabled": ("chevron-up", FG_DISABLED, 12, 1.7),
    "right": ("chevron-right", FG_DIM, 12, 1.7),
    "right-hover": ("chevron-right", FG, 12, 1.7),
    "right-disabled": ("chevron-right", FG_DISABLED, 12, 1.7),
    "left": ("chevron-left", FG_DIM, 12, 1.7),
    "left-hover": ("chevron-left", FG, 12, 1.7),
    "spin-up": ("chevron-up", FG_DIM, 9, 2.2),
    "spin-up-hover": ("chevron-up", FG, 9, 2.2),
    "spin-up-disabled": ("chevron-up", FG_DISABLED, 9, 2.2),
    "spin-down": ("chevron-down", FG_DIM, 9, 2.2),
    "spin-down-hover": ("chevron-down", FG, 9, 2.2),
    "spin-down-disabled": ("chevron-down", FG_DISABLED, 9, 2.2),
    "close": ("close", FG_DIM, 12, 1.7),
    "close-hover": ("close", FG, 12, 1.7),
    "dock-close": ("close", FG_DIM, 16, 1.5),
    "dock-float": ("maximize", FG_DIM, 16, 1.5),
    "grip": ("grip", FG_DISABLED, 12, None),
}


def _asset_dir() -> str:
    digest = hashlib.sha1(
        (_ASSET_VERSION + repr(sorted(_ASSETS.items()))).encode("utf-8")).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), "sesame-studio", f"theme-{digest}")


def _build_assets() -> str:
    """Create the QSS image files if they are missing; returns their folder
    (forward slashes). Failures are swallowed: the stylesheet still works,
    indicators just lose their glyph (checked boxes stay accent-filled)."""
    folder = _asset_dir()
    marker = os.path.join(folder, ".complete")
    try:
        if not os.path.exists(marker):
            from . import icons  # late import: icons reads tokens from this module

            os.makedirs(folder, exist_ok=True)
            for stem, (glyph, color, size, stroke) in _ASSETS.items():
                for scale, suffix in ((1, ""), (2, "@2x"), (3, "@3x")):
                    target = os.path.join(folder, f"{stem}{suffix}.png")
                    img = icons.image(glyph, color, size, scale, stroke=stroke)
                    tmp = f"{target}.{os.getpid()}.tmp"
                    if img.save(tmp, "PNG"):
                        os.replace(tmp, target)
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write("ok")
    except Exception:
        pass
    return folder.replace("\\", "/")


ASSET_DIR = _build_assets()


# ---------------------------------------------------------------------------
# stylesheet
# ---------------------------------------------------------------------------
_QSS = r"""
/* ===== base ============================================================ */
QWidget {
    color: $FG;
    selection-background-color: $ACCENT;
    selection-color: $ACCENT_FG;
    outline: none;
}
QWidget:disabled { color: $FG_DISABLED; }
QWidget:window { background-color: $BG; }
QMainWindow, QDialog { background-color: $BG; }

/* ===== labels ========================================================== */
QLabel { background: transparent; }
QLabel[role="title"] { font-size: 24px; font-weight: 600; color: $FG; }
QLabel[role="heading"] { font-size: 18px; font-weight: 600; color: $FG; }
QLabel[role="subheading"] { font-size: 12px; color: $FG_DIM; }
QLabel[role="caption"] { font-size: 11px; font-weight: 600; color: $FG_MUTED; }
QLabel[role="mono"] { font-family: "$FONT_MONO", "Consolas", monospace; font-size: 12px; }
QLabel[role="badge"] {
    background-color: $BUTTON_BG;
    color: $FG_DIM;
    border-radius: 9px;
    padding: 1px 8px;
    min-height: 16px;
    max-height: 16px;
    font-size: 11px;
    font-weight: 600;
}
QLabel[role="badge"][tone="accent"] { background-color: $ACCENT_SOFT; color: $ACCENT; }
QLabel[role="badge"][tone="success"] { background-color: $SUCCESS_SOFT; color: $SUCCESS; }
QLabel[role="badge"][tone="danger"] { background-color: $DANGER_SOFT; color: $DANGER; }
QLabel[role="badge"][tone="warning"] { background-color: $WARNING_SOFT; color: $WARNING; }
QLabel[role="badge"][tone="info"] { background-color: $INFO_SOFT; color: $INFO; }
QLabel[role="iconChip"] { background-color: $ACCENT_SOFT; border-radius: 8px; }
QLabel[role="iconChip"][tone="solid"] { background-color: $ACCENT; }
QLabel[role="iconChip"][tone="neutral"] { background-color: $BUTTON_BG; }
QLabel[role="key"] {
    background-color: $BUTTON_BG;
    color: $FG_DIM;
    border: 1px solid $BORDER_CONTROL;
    border-bottom: 2px solid $BORDER_CONTROL;
    border-radius: 4px;
    padding: 0px 5px;
    min-width: 8px;
    font-size: 11px;
}

/* ===== surfaces ======================================================== */
QFrame[role="card"] {
    background-color: $PANEL;
    border: 1px solid $BORDER;
    border-radius: 8px;
}
QFrame[role="card"][clickable="true"]:hover {
    background-color: $INPUT_BG;
    border-color: $BORDER_STRONG;
}
QFrame[role="card"][clickable="true"]:focus { border-color: $ACCENT; }
QFrame[role="toolbar"], QWidget[role="toolbar"] {
    background-color: $PANEL;
    border: none;
    border-bottom: 1px solid $BORDER;
}
QFrame[role="well"] {
    background-color: $BG_SUNKEN;
    border: 1px solid $BORDER;
    border-radius: 6px;
}
QFrame[role="separator"] {
    background-color: $BORDER;
    border: none;
    min-width: 1px;
    min-height: 1px;
}
QFrame[frameShape="4"], QFrame[frameShape="5"] {
    background-color: $BORDER;
    border: none;
    min-width: 1px;
    min-height: 1px;
}
QFrame[frameShape="4"] { max-height: 1px; }
QFrame[frameShape="5"] { max-width: 1px; }
QFrame[role="toast"] {
    background-color: $BUTTON_BG;
    border: 1px solid $BORDER_STRONG;
    border-radius: 8px;
}

/* ===== push buttons ==================================================== */
QPushButton {
    background-color: $BUTTON_BG;
    color: $FG;
    border: 1px solid $BORDER_CONTROL;
    border-radius: 6px;
    padding: 4px 12px;
    min-height: 18px;
}
QPushButton:hover { background-color: $HOVER; border-color: $BORDER_STRONG; }
QPushButton:pressed { background-color: $PRESSED; }
QPushButton:checked {
    background-color: $ACCENT_SOFT;
    border-color: $ACCENT_SOFT_BORDER;
    color: $ACCENT;
}
QPushButton:default { border-color: $ACCENT_SOFT_BORDER; }
QPushButton:focus { border-color: $ACCENT; }
QPushButton:disabled {
    background-color: $BG_ELEVATED;
    border-color: $BORDER;
    color: $FG_DISABLED;
}
QPushButton::menu-indicator {
    image: url("$IMG/down.png");
    subcontrol-origin: padding;
    subcontrol-position: right center;
    width: 12px;
    height: 12px;
    right: 6px;
}

QPushButton[variant="primary"] {
    background-color: $ACCENT;
    border-color: $ACCENT;
    color: $ACCENT_FG;
    font-weight: 600;
}
QPushButton[variant="primary"]:hover { background-color: $ACCENT; border-color: $ACCENT; }
QPushButton[variant="primary"]:pressed { background-color: $ACCENT_PRESSED; border-color: $ACCENT_PRESSED; }
QPushButton[variant="primary"]:focus { border-color: #ffffff; }

QPushButton[variant="success"] {
    background-color: $SUCCESS;
    border-color: $SUCCESS;
    color: #06170d;
    font-weight: 600;
}
QPushButton[variant="success"]:hover { background-color: $SUCCESS_HOVER; border-color: $SUCCESS_HOVER; }
QPushButton[variant="success"]:pressed { background-color: $SUCCESS_PRESSED; border-color: $SUCCESS_PRESSED; }
QPushButton[variant="success"]:focus { border-color: #ffffff; }

QPushButton[variant="danger"] {
    background-color: $DANGER_SOFT;
    border-color: $DANGER_BORDER;
    color: $DANGER_TEXT;
}
QPushButton[variant="danger"]:hover { background-color: $DANGER_FILL; border-color: $DANGER_FILL; color: #ffffff; }
QPushButton[variant="danger"]:pressed { background-color: $DANGER_PRESSED; border-color: $DANGER_PRESSED; color: #ffffff; }
QPushButton[variant="danger"]:focus { border-color: $DANGER; }

QPushButton[variant="flat"] {
    background-color: transparent;
    border-color: transparent;
    color: $FG_DIM;
}
QPushButton[variant="flat"]:hover { background-color: $HOVER; border-color: transparent; color: $FG; }
QPushButton[variant="flat"]:pressed { background-color: $PRESSED; }
QPushButton[variant="flat"]:checked { background-color: $ACCENT_SOFT; color: $ACCENT; }
QPushButton[variant="flat"]:focus { border-color: $ACCENT; }

QPushButton[variant="primary"]:disabled, QPushButton[variant="success"]:disabled,
QPushButton[variant="danger"]:disabled {
    background-color: $BG_ELEVATED;
    border-color: $BORDER;
    color: $FG_DISABLED;
    font-weight: 400;
}
QPushButton[variant="flat"]:disabled { background-color: transparent; border-color: transparent; color: $FG_DISABLED; }
QPushButton[iconOnly="true"] { padding: 4px; min-width: 18px; max-width: 18px; }

/* ===== tool buttons ==================================================== */
QToolButton {
    background-color: $BUTTON_BG;
    color: $FG;
    border: 1px solid $BORDER_CONTROL;
    border-radius: 6px;
    padding: 3px 8px;      /* Qt adds 3px to tool-button contents: 17 + 3 + 6 + 2 = 28 */
    min-height: 17px;
}
QToolButton:hover { background-color: $HOVER; border-color: $BORDER_STRONG; }
QToolButton:pressed { background-color: $PRESSED; }
QToolButton:checked {
    background-color: $ACCENT_SOFT;
    border-color: $ACCENT_SOFT_BORDER;
    color: $ACCENT;
}
QToolButton:checked:hover { background-color: $ACCENT_SOFT_HOVER; }
QToolButton:focus { border-color: $ACCENT; }
QToolButton:disabled {
    background-color: $BG_ELEVATED;
    border-color: $BORDER;
    color: $FG_DISABLED;
}

QToolButton[variant="primary"] {
    background-color: $ACCENT;
    border-color: $ACCENT;
    color: $ACCENT_FG;
    font-weight: 600;
}
QToolButton[variant="primary"]:hover { background-color: $ACCENT; border-color: $ACCENT; }
QToolButton[variant="primary"]:pressed { background-color: $ACCENT_PRESSED; border-color: $ACCENT_PRESSED; }
QToolButton[variant="primary"]:focus { border-color: #ffffff; }

QToolButton[variant="success"] {
    background-color: $SUCCESS;
    border-color: $SUCCESS;
    color: #06170d;
    font-weight: 600;
}
QToolButton[variant="success"]:hover { background-color: $SUCCESS_HOVER; border-color: $SUCCESS_HOVER; }
QToolButton[variant="success"]:pressed { background-color: $SUCCESS_PRESSED; border-color: $SUCCESS_PRESSED; }
QToolButton[variant="success"]:focus { border-color: #ffffff; }

QToolButton[variant="danger"] {
    background-color: $DANGER_SOFT;
    border-color: $DANGER_BORDER;
    color: $DANGER_TEXT;
}
QToolButton[variant="danger"]:hover { background-color: $DANGER_FILL; border-color: $DANGER_FILL; color: #ffffff; }
QToolButton[variant="danger"]:pressed { background-color: $DANGER_PRESSED; border-color: $DANGER_PRESSED; color: #ffffff; }
QToolButton[variant="danger"]:focus { border-color: $DANGER; }

QToolButton[variant="flat"], QToolBar QToolButton {
    background-color: transparent;
    border-color: transparent;
    color: $FG_DIM;
}
QToolButton[variant="flat"]:hover, QToolBar QToolButton:hover {
    background-color: $HOVER;
    border-color: transparent;
    color: $FG;
}
QToolButton[variant="flat"]:pressed, QToolBar QToolButton:pressed { background-color: $PRESSED; }
QToolButton[variant="flat"]:checked, QToolBar QToolButton:checked {
    background-color: $ACCENT_SOFT;
    border-color: transparent;
    color: $ACCENT;
}
QToolButton[variant="flat"]:checked:hover, QToolBar QToolButton:checked:hover {
    background-color: $ACCENT_SOFT_HOVER;
}
QToolButton[variant="flat"]:focus, QToolBar QToolButton:focus { border-color: $ACCENT; }

QToolButton[variant="primary"]:disabled, QToolButton[variant="success"]:disabled,
QToolButton[variant="danger"]:disabled {
    background-color: $BG_ELEVATED;
    border-color: $BORDER;
    color: $FG_DISABLED;
    font-weight: 400;
}
QToolButton[variant="flat"]:disabled, QToolBar QToolButton:disabled {
    background-color: transparent;
    border-color: transparent;
    color: $FG_DISABLED;
}
QToolButton[iconOnly="true"] { padding: 3px; min-width: 17px; min-height: 17px; }

QToolButton[popupMode="1"] { padding-right: 20px; }
QToolButton[popupMode="2"] { padding-right: 18px; }
QToolButton::menu-button {
    border: none;
    border-left: 1px solid $BORDER_CONTROL;
    border-top-right-radius: 5px;
    border-bottom-right-radius: 5px;
    width: 16px;
}
QToolButton::menu-button:hover { background-color: $BORDER_CONTROL; }
QToolButton::menu-arrow { image: url("$IMG/down.png"); width: 12px; height: 12px; }
QToolButton::menu-indicator {
    image: url("$IMG/down.png");
    subcontrol-origin: padding;
    subcontrol-position: right center;
    width: 12px;
    height: 12px;
    right: 3px;
}

/* collapsible section header (app.widgets.CollapsibleSection) */
QToolButton[role="sectionHeader"], QToolButton[role="sectionHeader"]:checked {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    color: $FG_DIM;
    font-weight: 600;
    padding: 5px 4px;
}
QToolButton[role="sectionHeader"]:hover, QToolButton[role="sectionHeader"]:checked:hover {
    background-color: transparent;
    color: $FG;
}
QToolButton[role="sectionHeader"]:focus { border-color: $ACCENT; }

/* ===== segmented control (app.widgets.SegmentedControl) ================ */
QFrame[role="segmented"] {
    background-color: $BG_SUNKEN;
    border: 1px solid $BORDER;
    border-radius: 7px;
}
QFrame[role="segmented"] > QToolButton {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 5px;
    color: $FG_DIM;
    padding: 0px 10px;
    min-height: 12px;      /* SegmentedControl fixes the row at CONTROL_HEIGHT */
}
QFrame[role="segmented"] > QToolButton[iconOnly="true"] { padding: 0px 3px; min-width: 17px; }
QFrame[role="segmented"] > QToolButton:hover { background-color: $PANEL; color: $FG; }
QFrame[role="segmented"] > QToolButton:checked {
    background-color: $ACCENT_SOFT;
    border-color: transparent;
    color: $ACCENT;
}
QFrame[role="segmented"] > QToolButton:focus { border-color: $ACCENT; }
QFrame[role="segmented"] > QToolButton:disabled { background-color: transparent; border-color: transparent; color: $FG_DISABLED; }

/* ===== text inputs ===================================================== */
QLineEdit, QAbstractSpinBox, QComboBox {
    background-color: $INPUT_BG;
    color: $FG;
    border: 1px solid $BORDER_CONTROL;
    border-radius: 6px;
    padding: 4px 8px;
    min-height: 18px;
    selection-background-color: $ACCENT;
    selection-color: $ACCENT_FG;
}
QLineEdit:hover, QAbstractSpinBox:hover, QComboBox:hover { border-color: $BORDER_STRONG; }
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus, QComboBox:on { border-color: $ACCENT; }
QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled {
    background-color: $BG_ELEVATED;
    border-color: $BORDER;
    color: $FG_DISABLED;
}
QLineEdit:read-only { color: $FG_DIM; }
QLineEdit { placeholder-text-color: $FG_MUTED; }

QTextEdit, QPlainTextEdit, QTextBrowser {
    background-color: $BG_SUNKEN;
    color: $FG;
    border: 1px solid $BORDER;
    border-radius: 6px;
    padding: 6px;
    selection-background-color: $ACCENT;
    selection-color: $ACCENT_FG;
}
QTextEdit:focus, QPlainTextEdit:focus { border-color: $ACCENT; }
QTextEdit:disabled, QPlainTextEdit:disabled { color: $FG_DISABLED; }

/* ----- spin boxes ------------------------------------------------------ */
/* Qt (6.12) lays the embedded editor out from the widget's left edge and
   ignores horizontal padding, so left-aligned digits would touch the border.
   Values are therefore right-aligned next to the steppers. (Do not use
   QAbstractSpinBox.NoButtons on a styled spin box: the editor collapses.) */
QAbstractSpinBox {
    padding: 2px 22px 2px 8px;
    min-height: 19px;      /* Qt adds 3px: 19 + 3 + 4 + 2 = 28 */
    qproperty-alignment: AlignRight;
}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    subcontrol-origin: border;
    width: 18px;
    border: none;
    background-color: transparent;
}
QAbstractSpinBox::up-button {
    subcontrol-position: top right;
    margin: 2px 2px 0px 0px;
    border-top-right-radius: 4px;
}
QAbstractSpinBox::down-button {
    subcontrol-position: bottom right;
    margin: 0px 2px 2px 0px;
    border-bottom-right-radius: 4px;
}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover { background-color: $BORDER_CONTROL; }
QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed { background-color: $BORDER_STRONG; }
QAbstractSpinBox::up-arrow { image: url("$IMG/spin-up.png"); width: 9px; height: 9px; }
QAbstractSpinBox::up-arrow:hover { image: url("$IMG/spin-up-hover.png"); }
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off { image: url("$IMG/spin-up-disabled.png"); }
QAbstractSpinBox::down-arrow { image: url("$IMG/spin-down.png"); width: 9px; height: 9px; }
QAbstractSpinBox::down-arrow:hover { image: url("$IMG/spin-down-hover.png"); }
QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off { image: url("$IMG/spin-down-disabled.png"); }

/* ----- combo boxes ----------------------------------------------------- */
QComboBox { padding-right: 4px; combobox-popup: 0; }
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 22px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow { image: url("$IMG/down.png"); width: 12px; height: 12px; }
QComboBox::down-arrow:hover, QComboBox::down-arrow:on { image: url("$IMG/down-hover.png"); }
QComboBox::down-arrow:disabled { image: url("$IMG/down-disabled.png"); }
QComboBox QAbstractItemView {
    background-color: $POPUP_BG;
    border: 1px solid $BORDER_STRONG;
    border-radius: 0px;
    padding: 4px;
    outline: none;
    selection-background-color: $HOVER;
    selection-color: #ffffff;
}
QComboBox QAbstractItemView::item {
    min-height: 24px;
    padding: 0px 8px;
    border-radius: 4px;
    color: $FG;
}
QComboBox QAbstractItemView::item:selected { background-color: $HOVER; color: #ffffff; }
QComboBox QAbstractItemView::item:disabled { color: $FG_DISABLED; }

/* ===== check boxes + radio buttons ===================================== */
QCheckBox, QRadioButton { background: transparent; spacing: 8px; min-height: 20px; }
QCheckBox::indicator, QRadioButton::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid $BORDER_STRONG;
    background-color: $INPUT_BG;
}
QCheckBox::indicator { border-radius: 4px; }
QRadioButton::indicator { border-radius: 8px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: $FG_MUTED; }
QCheckBox::indicator:focus, QRadioButton::indicator:focus { border-color: $ACCENT; }
QCheckBox::indicator:checked, QCheckBox::indicator:indeterminate, QRadioButton::indicator:checked {
    background-color: $ACCENT;
    border-color: $ACCENT;
}
QCheckBox::indicator:checked { image: url("$IMG/check.png"); }
QCheckBox::indicator:indeterminate { image: url("$IMG/dash.png"); }
QRadioButton::indicator:checked { image: url("$IMG/radio.png"); }
QCheckBox::indicator:checked:hover, QCheckBox::indicator:indeterminate:hover,
QRadioButton::indicator:checked:hover { background-color: $ACCENT; border-color: $ACCENT; }
QCheckBox::indicator:checked:focus, QCheckBox::indicator:indeterminate:focus,
QRadioButton::indicator:checked:focus { border-color: #ffffff; }
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {
    background-color: $BG_ELEVATED;
    border-color: $BORDER_CONTROL;
}
QCheckBox::indicator:checked:disabled, QCheckBox::indicator:indeterminate:disabled,
QRadioButton::indicator:checked:disabled {
    background-color: $BORDER_CONTROL;
    border-color: $BORDER_CONTROL;
}
QCheckBox::indicator:checked:disabled { image: url("$IMG/check-disabled.png"); }
QCheckBox::indicator:indeterminate:disabled { image: url("$IMG/dash-disabled.png"); }
QRadioButton::indicator:checked:disabled { image: url("$IMG/radio-disabled.png"); }

/* ===== sliders ========================================================= */
QSlider { background: transparent; }
QSlider:horizontal { min-height: 20px; }
QSlider:vertical { min-width: 20px; }
QSlider::groove:horizontal { height: 4px; background-color: $BORDER_CONTROL; border-radius: 2px; }
QSlider::groove:vertical { width: 4px; background-color: $BORDER_CONTROL; border-radius: 2px; }
QSlider::sub-page:horizontal { background-color: $ACCENT; border-radius: 2px; }
QSlider::add-page:horizontal { background-color: $BORDER_CONTROL; border-radius: 2px; }
QSlider::add-page:vertical { background-color: $ACCENT; border-radius: 2px; }
QSlider::sub-page:vertical { background-color: $BORDER_CONTROL; border-radius: 2px; }
QSlider::handle {
    background-color: #ededf0;
    border: 2px solid #ededf0;
    width: 10px;
    height: 10px;
    border-radius: 7px;
}
QSlider::handle:horizontal { margin: -5px 0px; }
QSlider::handle:vertical { margin: 0px -5px; }
QSlider::handle:hover { background-color: #ffffff; border-color: $ACCENT; }
QSlider::handle:pressed, QSlider::handle:focus { background-color: #ffffff; border-color: $ACCENT; }
QSlider::sub-page:horizontal:disabled, QSlider::add-page:vertical:disabled { background-color: $BORDER_STRONG; }
QSlider::handle:disabled { background-color: $FG_DISABLED; border-color: $FG_DISABLED; }

/* ===== progress ======================================================== */
QProgressBar {
    background-color: $BUTTON_BG;
    border: none;
    border-radius: 3px;
    min-height: 6px;
    max-height: 6px;
    color: transparent;
    text-align: center;
}
QProgressBar::chunk { background-color: $ACCENT; border-radius: 3px; }

/* ===== item views ====================================================== */
QAbstractItemView {
    /* font-style is a deliberate no-op: giving views *a* font property makes
       Qt send FontChange when the stylesheet is first polished, which
       re-lays-out rows that were measured before the item padding applied
       (otherwise views filled before being shown get overlapping rows). */
    font-style: normal;
    background-color: $BG_SUNKEN;
    alternate-background-color: $BG;
    color: $FG;
    border: 1px solid $BORDER;
    border-radius: 6px;
    padding: 3px;
    outline: none;
    selection-background-color: $ACCENT_SOFT;
    selection-color: #ffffff;
}
QAbstractItemView:focus { border-color: $BORDER_STRONG; }
QListView::item, QTreeView::item {
    padding: 5px 8px;
    color: $FG;
}
QListView::item { border-radius: 4px; }
QTreeView { show-decoration-selected: 1; }
QListView::item:hover, QTreeView::item:hover { background-color: $PANEL; }
QListView::item:selected, QTreeView::item:selected { background-color: $ACCENT_SOFT; color: #ffffff; }
QListView::item:selected:hover, QTreeView::item:selected:hover { background-color: $ACCENT_SOFT_HOVER; }
QListView::item:selected:!active, QTreeView::item:selected:!active { background-color: $HOVER; color: $FG; }
QListView::item:disabled, QTreeView::item:disabled { color: $FG_DISABLED; }
QTreeView::branch { background: transparent; }
QTreeView::branch:hover { background-color: $PANEL; }
QTreeView::branch:selected { background-color: $ACCENT_SOFT; }
QTreeView::branch:selected:hover { background-color: $ACCENT_SOFT_HOVER; }
QTreeView::branch:selected:!active { background-color: $HOVER; }
QTreeView::branch:has-children:closed { image: url("$IMG/right.png"); }
QTreeView::branch:has-children:open { image: url("$IMG/down.png"); }
QTableView { gridline-color: $BORDER; }
QTableView::item { padding: 4px 8px; }
QTableView::item:selected { background-color: $ACCENT_SOFT; color: #ffffff; }
QHeaderView { background-color: transparent; border: none; }
QHeaderView::section {
    background-color: $BG_ELEVATED;
    color: $FG_DIM;
    border: none;
    border-bottom: 1px solid $BORDER;
    padding: 5px 8px;
    font-weight: 600;
}
QTableCornerButton::section { background-color: $BG_ELEVATED; border: none; border-bottom: 1px solid $BORDER; }

/* ===== scroll areas + scroll bars ====================================== */
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QGraphicsView { background-color: $BG_SUNKEN; border: 1px solid $BORDER; }
QAbstractScrollArea::corner { background: transparent; border: none; }

QScrollBar { background: transparent; border: none; }
QScrollBar:vertical { width: 10px; margin: 0px; }
QScrollBar:horizontal { height: 10px; margin: 0px; }
QScrollBar::handle { background-color: $BORDER_CONTROL; border-radius: 3px; }
QScrollBar::handle:vertical { min-height: 28px; margin: 2px; }
QScrollBar::handle:horizontal { min-width: 28px; margin: 2px; }
QScrollBar::handle:hover { background-color: $FG_DISABLED; }
QScrollBar::handle:pressed { background-color: $FG_MUTED; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ===== tabs ============================================================ */
QTabWidget::pane { border: none; border-top: 1px solid $BORDER; }
QTabWidget::pane:bottom { border: none; border-bottom: 1px solid $BORDER; }
QTabBar { background: transparent; border: none; qproperty-drawBase: 0; }
QTabBar::tab {
    background: transparent;
    color: $FG_DIM;
    border: none;
    padding: 6px 12px;
}
QTabBar::tab:top { border-bottom: 2px solid transparent; }
QTabBar::tab:bottom { border-top: 2px solid transparent; }
QTabBar::tab:left { border-right: 2px solid transparent; }
QTabBar::tab:right { border-left: 2px solid transparent; }
QTabBar::tab:hover { color: $FG; }
QTabBar::tab:selected { color: $FG; }
QTabBar::tab:top:selected { border-bottom-color: $ACCENT; }
QTabBar::tab:bottom:selected { border-top-color: $ACCENT; }
QTabBar::tab:left:selected { border-right-color: $ACCENT; }
QTabBar::tab:right:selected { border-left-color: $ACCENT; }
QTabBar::tab:disabled { color: $FG_DISABLED; }
QTabBar::close-button { image: url("$IMG/close.png"); subcontrol-position: right; border-radius: 4px; }
QTabBar::close-button:hover { image: url("$IMG/close-hover.png"); background-color: $HOVER; }
QTabBar QToolButton {
    background-color: $BG;
    border: none;
    border-radius: 0px;
    padding: 2px;
}
QTabBar QToolButton::left-arrow { image: url("$IMG/left.png"); }
QTabBar QToolButton::right-arrow { image: url("$IMG/right.png"); }

/* ===== docks =========================================================== */
QDockWidget {
    color: $FG_DIM;
    font-weight: 600;
    titlebar-close-icon: url("$IMG/dock-close.png");
    titlebar-normal-icon: url("$IMG/dock-float.png");
}
QDockWidget::title {
    background-color: $BG_ELEVATED;
    border-bottom: 1px solid $BORDER;
    padding: 7px 10px;
    text-align: left;
}
QDockWidget::close-button, QDockWidget::float-button {
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 0px;
    icon-size: 16px;
    subcontrol-origin: margin;
    subcontrol-position: right center;
    position: absolute;
    width: 20px;
    height: 20px;
}
QDockWidget::close-button { right: 5px; }
QDockWidget::float-button { right: 27px; }
QDockWidget::close-button:hover, QDockWidget::float-button:hover { background-color: $HOVER; }
QDockWidget::close-button:pressed, QDockWidget::float-button:pressed { background-color: $PRESSED; }
QMainWindow::separator { background-color: $BG_SUNKEN; width: 4px; height: 4px; }
QMainWindow::separator:hover { background-color: $ACCENT_SOFT_BORDER; }
QMainWindow > QTabBar { background-color: $BG_ELEVATED; }

/* ===== menus =========================================================== */
QMenuBar {
    background-color: $BG_ELEVATED;
    border: none;
    border-bottom: 1px solid $BORDER;
    padding: 3px 6px;
}
QMenuBar::item {
    background: transparent;
    color: $FG_DIM;
    padding: 5px 10px;
    border-radius: 5px;
}
QMenuBar::item:selected { background-color: $HOVER; color: $FG; }
QMenuBar::item:pressed { background-color: $BORDER_CONTROL; color: $FG; }
QMenuBar::item:disabled { color: $FG_DISABLED; }
QMenu, QMenu:window {
    background-color: $POPUP_BG;
    border: 1px solid $BORDER_STRONG;
    border-radius: 8px;
    padding: 5px;
}
QMenu::item {
    background: transparent;
    color: $FG;
    padding: 6px 28px 6px 12px;
    border-radius: 5px;
    margin: 1px 0px;
}
QMenu::item:selected { background-color: $HOVER; color: #ffffff; }
QMenu::item:disabled { background: transparent; color: $FG_DISABLED; }
QMenu::separator { height: 1px; background-color: $BORDER_CONTROL; margin: 5px 8px; }
QMenu::icon { padding-left: 8px; }
QMenu::indicator { width: 14px; height: 14px; left: 8px; }
QMenu::indicator:non-exclusive:checked { image: url("$IMG/menu-check.png"); }
QMenu::indicator:exclusive:checked { image: url("$IMG/menu-dot.png"); }
QMenu::right-arrow { image: url("$IMG/right.png"); width: 12px; height: 12px; right: 8px; }
QMenu::right-arrow:selected { image: url("$IMG/right-hover.png"); }

/* ===== tool bars + status bar ========================================== */
QToolBar {
    background-color: $BG_ELEVATED;
    border: none;
    border-bottom: 1px solid $BORDER;
    padding: 4px 8px;
    spacing: 4px;
    icon-size: 16px;
}
QToolBar::separator { background-color: $BORDER_CONTROL; width: 1px; height: 1px; margin: 5px 6px; }
QToolBar::handle { image: url("$IMG/grip.png"); width: 12px; }
QToolBar QLabel { color: $FG_DIM; }
QStatusBar {
    background-color: $BG_ELEVATED;
    color: $FG_DIM;
    border-top: 1px solid $BORDER;
}
QStatusBar::item { border: none; }
QStatusBar QLabel { color: $FG_DIM; padding: 0px 6px; }
QSizeGrip { background: transparent; width: 12px; height: 12px; }

/* ===== group boxes: quiet captioned sections =========================== */
QGroupBox {
    background: transparent;
    border: none;
    border-top: 1px solid $BORDER;
    margin-top: 24px;
    padding: 4px 0px 0px 0px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 0px;
    top: 3px;
    padding: 0px;
    color: $FG_DIM;
}
QGroupBox::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid $BORDER_STRONG;
    border-radius: 4px;
    background-color: $INPUT_BG;
}
QGroupBox::indicator:checked { background-color: $ACCENT; border-color: $ACCENT; image: url("$IMG/check.png"); }

/* ===== splitters ======================================================= */
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 6px; }
QSplitter::handle:vertical { height: 6px; }
QSplitter::handle:hover { background-color: $BORDER_CONTROL; }
QSplitter::handle:pressed { background-color: $ACCENT_SOFT_BORDER; }

/* ===== tooltips ======================================================== */
QToolTip, QToolTip:window {
    background-color: $BUTTON_BG;
    color: $FG;
    border: 1px solid $BORDER_STRONG;
    border-radius: 0px;
    padding: 2px 5px;
}

/* ===== dialogs ========================================================= */
QDialogButtonBox QPushButton, QMessageBox QPushButton, QInputDialog QPushButton { min-width: 64px; }
QMessageBox QLabel { color: $FG; }

/* ===== app-specific canvases =========================================== */
#TimelineView { background-color: $BG_SUNKEN; border: none; }
#Viewport3D { background-color: #000000; border: none; }
"""


def _render_stylesheet() -> str:
    values = {
        name: str(value) for name, value in globals().items()
        if name.isupper() and not name.startswith("_") and isinstance(value, (str, int))
    }
    values.update({
        "IMG": ASSET_DIR,
        "SUCCESS_HOVER": mix(SUCCESS, "#ffffff", 0.14),
        "SUCCESS_PRESSED": mix(SUCCESS, "#000000", 0.16),
        "DANGER_BORDER": mix(BG, DANGER, 0.42),
        "DANGER_TEXT": mix(DANGER, "#ffffff", 0.22),
        "DANGER_FILL": mix(DANGER, "#000000", 0.08),
        "DANGER_PRESSED": mix(DANGER, "#000000", 0.24),
        "ACCENT_SOFT_HOVER": mix(BG, ACCENT, 0.28),
    })

    def sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"theme: unknown stylesheet token ${key}")
        return values[key]

    return re.sub(r"\$([A-Z][A-Z0-9_]*)", sub, _QSS)


STYLESHEET = _render_stylesheet()


# ---------------------------------------------------------------------------
# runtime helpers (Qt imported lazily so tokens stay importable anywhere)
# ---------------------------------------------------------------------------
def qcolor(color: str, alpha: float | None = None):
    """``QColor`` for a token / hex string, optionally with 0..1 alpha."""
    from PySide6.QtGui import QColor

    c = QColor(color)
    if alpha is not None:
        c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def ui_font(size_px: int | None = None, weight: int | None = None):
    """The UI font at ``size_px`` pixels (default ``FONT_SIZE``)."""
    from PySide6.QtGui import QFont

    font = QFont(FONT_FAMILY)
    font.setPixelSize(int(size_px or FONT_SIZE))
    if weight is not None:
        font.setWeight(QFont.Weight(int(weight)))
    return font


def mono_font(size_px: int | None = None):
    """Monospace font (``FONT_MONO`` with a Consolas / system fallback)."""
    from PySide6.QtGui import QFont

    font = QFont()
    font.setFamilies([FONT_MONO, "Consolas", "Courier New"])
    font.setStyleHint(QFont.Monospace)
    font.setPixelSize(int(size_px or FONT_SIZE))
    return font


def build_palette():
    """Dark ``QPalette`` matching the tokens (for everything QSS cannot reach)."""
    from PySide6.QtGui import QColor, QPalette

    pal = QPalette()
    roles = {
        QPalette.Window: BG, QPalette.WindowText: FG,
        QPalette.Base: BG_SUNKEN, QPalette.AlternateBase: BG,
        QPalette.Text: FG, QPalette.Button: BUTTON_BG, QPalette.ButtonText: FG,
        QPalette.BrightText: "#ffffff", QPalette.ToolTipBase: BUTTON_BG,
        QPalette.ToolTipText: FG, QPalette.Highlight: ACCENT,
        QPalette.HighlightedText: ACCENT_FG, QPalette.PlaceholderText: FG_MUTED,
        QPalette.Link: ACCENT_HOVER, QPalette.LinkVisited: ACCENT,
        QPalette.Light: BORDER_STRONG, QPalette.Midlight: BORDER_CONTROL,
        QPalette.Mid: BORDER, QPalette.Dark: BG_SUNKEN, QPalette.Shadow: "#000000",
    }
    for role, value in roles.items():
        pal.setColor(role, QColor(value))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(FG_DISABLED))
    pal.setColor(QPalette.Disabled, QPalette.Highlight, QColor(BORDER_STRONG))
    pal.setColor(QPalette.Disabled, QPalette.HighlightedText, QColor(FG_DIM))
    return pal


def apply_theme(app=None) -> None:
    """Make ``app`` (default: the running QApplication) fully themed: Fusion
    base style, dark palette + window frames, UI font and ``STYLESHEET``."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    app = app or QApplication.instance()
    if app is None:
        return
    try:  # dark title bars / native dialogs chrome (Qt >= 6.8)
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    except Exception:
        pass
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    app.setPalette(build_palette())
    font = QFont(FONT_FAMILY)
    font.setPointSizeF(9.0)
    app.setFont(font)
    app.setStyleSheet(STYLESHEET)


def repolish(widget) -> None:
    """Re-evaluate the stylesheet for ``widget`` after a dynamic property
    (``variant``, ``role``, ``tone`` ...) changed while it was visible."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_variant(button, variant: str | None):
    """Set a button's ``variant`` property (None = neutral) and restyle it."""
    button.setProperty("variant", variant)
    repolish(button)
    return button


def set_role(widget, role: str | None):
    """Set a label's / frame's ``role`` property and restyle it."""
    widget.setProperty("role", role)
    repolish(widget)
    return widget


def polish_popup(menu) -> None:
    """Optional: give a ``QMenu`` perfectly anti-aliased rounded corners by
    making its window translucent and dropping the square native shadow.
    Menus already look right over the app's dark surfaces without it; use it
    for menus that can open over bright content. Anything that is not a
    ``QMenu`` is left untouched (combo popups are intentionally square)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMenu

    if not isinstance(menu, QMenu):
        return
    menu.setWindowFlags(
        menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
    menu.setAttribute(Qt.WA_TranslucentBackground, True)
