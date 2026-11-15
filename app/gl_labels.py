"""Small text "chips" for the 3D viewport, rasterised with QPainter into
premultiplied RGBA images (the renderer uploads them once and draws them as
screen-space sprites, so there are no per-frame text layouts and no child
widgets chasing the joints around)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPen

from . import theme

__all__ = ["chip_image", "chip_key"]


def chip_key(text: str, style: str, dot: str | None, scale: float) -> tuple:
    return (text, style, dot, round(float(scale), 3))


_STYLES = {
    # style: (font px, weight, fg, bg, bg alpha, border, border alpha, pad x, pad y, radius)
    "label":    (11, QFont.Weight.DemiBold, theme.FG_DIM, theme.BG_SUNKEN, 0.72, "#ffffff", 0.10, 6, 2, 8),
    "hover":    (12, QFont.Weight.DemiBold, theme.FG, theme.BG_ELEVATED, 0.92, theme.BORDER_STRONG, 1.0, 8, 4, 9),
    "selected": (11, QFont.Weight.DemiBold, theme.FG, theme.BG_ELEVATED, 0.92, theme.ACCENT, 0.95, 7, 3, 9),
    "readout":  (15, QFont.Weight.Bold, "#ffffff", theme.BG_SUNKEN, 0.86, theme.ACCENT, 0.95, 9, 4, 11),
    "hint":     (12, QFont.Weight.Medium, theme.FG_DIM, theme.BG_SUNKEN, 0.78, "#ffffff", 0.08, 14, 7, 15),
    "note":     (12, QFont.Weight.Medium, theme.WARNING, theme.BG_SUNKEN, 0.86, theme.WARNING, 0.45, 12, 6, 13),
}


def chip_image(text: str, style: str = "label", dot: str | None = None, scale: float = 1.0) -> QImage:
    """Rounded pill with ``text`` (optionally a coloured dot in front).
    ``scale`` is the device pixel ratio; the image is ``scale`` times the
    logical size."""
    px, weight, fg, bg, bg_a, border, border_a, pad_x, pad_y, radius = _STYLES.get(style, _STYLES["label"])
    font = theme.ui_font(px, int(weight))
    if style == "readout":
        font = theme.mono_font(px)
        font.setWeight(QFont.Weight.Bold)
    font.setPixelSize(max(1, int(round(px * scale))))
    fm = QFontMetricsF(font)
    text_w = fm.horizontalAdvance(text)
    dot_d = (7.0 * scale) if dot else 0.0
    gap = (5.0 * scale) if dot else 0.0
    w = int(text_w + dot_d + gap + 2 * pad_x * scale + 2.5)
    h = int(fm.height() + 2 * pad_y * scale + 2.5)
    img = QImage(w, h, QImage.Format.Format_RGBA8888_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    rect = QRectF(0.75, 0.75, w - 1.5, h - 1.5)
    r = min(radius * scale, rect.height() / 2.0)
    p.setPen(QPen(theme.qcolor(border, border_a), max(1.0, 1.0 * scale)))
    p.setBrush(theme.qcolor(bg, bg_a))
    p.drawRoundedRect(rect, r, r)
    x = pad_x * scale + 1.0
    if dot:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(dot))
        p.drawEllipse(QRectF(x, (h - dot_d) / 2.0, dot_d, dot_d))
        x += dot_d + gap
    p.setFont(font)
    p.setPen(QColor(fg))
    p.drawText(QRectF(x, 0, text_w + 2, h), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
    p.end()
    return img
