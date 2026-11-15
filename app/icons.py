"""Vector icon set drawn with QPainter (no icon font / image files needed).

Public API (stable contract)
----------------------------
``icon(name, color=None, size=16) -> QIcon``
    A themed icon. ``color=None`` means "follow the theme": the glyph is drawn
    in ``theme.FG``, brightens on hover (Active), dims when Disabled and turns
    ``theme.ACCENT`` in the On state, so checkable buttons light up by
    themselves. Passing an explicit ``color`` (any ``QColor`` string, e.g.
    ``theme.ACCENT_FG`` for a glyph on a primary button) pins every enabled
    state to that colour; Disabled is always the theme's disabled grey.
``pixmap(name, color=None, size=16) -> QPixmap``
    A single high-DPI pixmap (``size`` is in logical pixels).
``ICON_NAMES``
    Every available glyph name. Unknown names return an empty icon/pixmap
    rather than raising, so callers never crash on a missing glyph.

Design
------
Every glyph is drawn on a 16-unit grid with round-capped ~1.5 unit strokes and
is rasterised straight at the target device resolution, so it stays crisp at
any size or screen scale. Larger sizes use an optically thinner stroke so 32px
icons do not look heavy next to text. Results are cached.
"""

from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QGuiApplication, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap,
)

__all__ = ["icon", "pixmap", "image", "ICON_NAMES"]

ICON_NAMES = [
    # transport
    "play", "pause", "stop", "loop", "skip-start", "skip-end", "step-back", "step-forward",
    # editing
    "plus", "minus", "trash", "copy", "paste", "cut", "duplicate", "undo", "redo", "rename",
    "check", "close", "search", "more", "settings", "save", "folder", "file", "export", "import",
    # paint tools
    "pencil", "eraser", "line", "rect", "rect-fill", "ellipse", "ellipse-fill", "fill", "text",
    "select", "move", "invert", "flip-h", "flip-v", "clear", "onion", "grid",
    # 3D / view
    "cursor", "rotate", "camera", "home", "eye", "eye-off", "cube", "physics", "magnet",
    "zoom-in", "zoom-out", "fit", "lock", "unlock", "pin",
    # misc
    "face", "robot", "timeline", "sliders", "help", "info", "warning", "chevron-left",
    "chevron-right", "chevron-down", "chevron-up", "keyboard", "sparkle",
    # -- added by the design-system pass ---------------------------------
    # status
    "check-circle", "x-circle",
    # files
    "file-plus", "file-code", "folder-open", "image", "code",
    # arrows / layout
    "arrow-left", "arrow-right", "arrow-up", "arrow-down", "maximize", "external",
    "panel-left", "panel-right", "panel-bottom", "menu", "grip", "filter",
    # animation / robot
    "keyframe", "keyframe-fill", "layers", "clock", "speed", "record", "dot",
    "target", "axis", "joint", "chip", "bolt", "star", "compass",
]


def _token(name: str) -> str:
    # Imported lazily: the theme rasterises its QSS glyphs through this module
    # while it is itself being imported, so there must be no import cycle.
    from . import theme
    return getattr(theme, name)


# ---------------------------------------------------------------------------
# drawing helper: a tiny pen-plotter working in 16-unit grid coordinates
# ---------------------------------------------------------------------------
class _G:
    def __init__(self, painter: QPainter, color: QColor, stroke: float):
        self.p = painter
        self.color = color
        self.w = stroke
        self._set_pen(stroke)

    def _set_pen(self, width: float) -> None:
        pen = QPen(self.color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        self.p.setPen(pen)
        self.p.setBrush(Qt.NoBrush)

    # -- strokes ---------------------------------------------------------
    def line(self, x1, y1, x2, y2) -> None:
        self.p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def poly(self, pts, close: bool = False) -> None:
        self.p.drawPath(_poly(pts, close))

    def path(self, path: QPainterPath) -> None:
        self.p.drawPath(path)

    def rect(self, x, y, w, h, r=0.0) -> None:
        self.p.drawRoundedRect(QRectF(x, y, w, h), r, r)

    def circle(self, cx, cy, r) -> None:
        self.p.drawEllipse(QPointF(cx, cy), r, r)

    def ellipse(self, cx, cy, rx, ry) -> None:
        self.p.drawEllipse(QPointF(cx, cy), rx, ry)

    def arc(self, cx, cy, r, start, span) -> None:
        """Angles in degrees, counter-clockwise, 0 = 3 o'clock."""
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        path = QPainterPath()
        path.arcMoveTo(rect, start)
        path.arcTo(rect, start, span)
        self.p.drawPath(path)

    # -- fills -----------------------------------------------------------
    def solid(self, shape, soften: float = 1.0) -> None:
        """Fill a path/point list; a thin same-colour outline rounds corners."""
        path = shape if isinstance(shape, QPainterPath) else _poly(shape, True)
        self.p.save()
        self.p.setBrush(self.color)
        if soften > 0:
            self.p.setPen(QPen(self.color, soften, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        else:
            self.p.setPen(Qt.NoPen)
        self.p.drawPath(path)
        self.p.restore()

    def solid_rect(self, x, y, w, h, r=0.0) -> None:
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, w, h), r, r)
        self.solid(path, 0)

    def dot(self, cx, cy, r) -> None:
        self.p.save()
        self.p.setPen(Qt.NoPen)
        self.p.setBrush(self.color)
        self.p.drawEllipse(QPointF(cx, cy), r, r)
        self.p.restore()

    # -- modifiers -------------------------------------------------------
    def erase(self, path: QPainterPath, width: float) -> None:
        """Knock a gap out of what has been drawn so far (e.g. under a slash)."""
        self.p.save()
        self.p.setCompositionMode(QPainter.CompositionMode_Clear)
        self.p.setPen(QPen(Qt.black, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        self.p.setBrush(Qt.NoBrush)
        self.p.drawPath(path)
        self.p.restore()

    def erase_disc(self, cx, cy, r) -> None:
        self.p.save()
        self.p.setCompositionMode(QPainter.CompositionMode_Clear)
        self.p.setPen(Qt.NoPen)
        self.p.setBrush(Qt.black)
        self.p.drawEllipse(QPointF(cx, cy), r, r)
        self.p.restore()

    def opacity(self, value: float) -> None:
        self.p.setOpacity(value)

    def width(self, w: float | None = None) -> None:
        self._set_pen(self.w if w is None else w)


def _poly(pts, close: bool = False) -> QPainterPath:
    path = QPainterPath(QPointF(*pts[0]))
    for x, y in pts[1:]:
        path.lineTo(x, y)
    if close:
        path.closeSubpath()
    return path


def _line_path(x1, y1, x2, y2) -> QPainterPath:
    path = QPainterPath(QPointF(x1, y1))
    path.lineTo(x2, y2)
    return path


def _mirror_x(fn: Callable[[_G], None]) -> Callable[[_G], None]:
    def draw(g: _G) -> None:
        g.p.save()
        g.p.translate(16, 0)
        g.p.scale(-1, 1)
        fn(g)
        g.p.restore()
    return draw


def _transpose(fn: Callable[[_G], None]) -> Callable[[_G], None]:
    """Swap x/y (mirror across the main diagonal)."""
    def draw(g: _G) -> None:
        g.p.save()
        g.p.rotate(90)
        g.p.scale(1, -1)
        fn(g)
        g.p.restore()
    return draw


# ---------------------------------------------------------------------------
# glyphs
# ---------------------------------------------------------------------------
# -- transport ---------------------------------------------------------------
def _play(g):
    g.solid([(5.4, 3.7), (12.2, 8), (5.4, 12.3)], 1.4)


def _pause(g):
    g.solid_rect(4, 3.25, 2.9, 9.5, 0.9)
    g.solid_rect(9.1, 3.25, 2.9, 9.5, 0.9)


def _stop(g):
    g.solid_rect(3.75, 3.75, 8.5, 8.5, 1.6)


def _loop(g):
    top = QPainterPath(QPointF(2.5, 7.4))
    top.lineTo(2.5, 6.6)
    top.quadTo(2.5, 4.5, 4.6, 4.5)
    top.lineTo(13, 4.5)
    g.path(top)
    g.poly([(11, 2.4), (13.1, 4.5), (11, 6.6)])
    bot = QPainterPath(QPointF(13.5, 8.6))
    bot.lineTo(13.5, 9.4)
    bot.quadTo(13.5, 11.5, 11.4, 11.5)
    bot.lineTo(3, 11.5)
    g.path(bot)
    g.poly([(5, 9.4), (2.9, 11.5), (5, 13.6)])


def _skip_start(g):
    g.line(3.75, 3.5, 3.75, 12.5)
    g.solid([(12.3, 3.9), (6.6, 8), (12.3, 12.1)], 1.4)


def _step_back(g):
    g.solid([(8.9, 3.9), (3.4, 8), (8.9, 12.1)], 1.4)
    g.line(12.4, 3.5, 12.4, 12.5)


# -- editing -----------------------------------------------------------------
def _plus(g):
    g.line(8, 3.25, 8, 12.75)
    g.line(3.25, 8, 12.75, 8)


def _minus(g):
    g.line(3.25, 8, 12.75, 8)


def _trash(g):
    g.line(2.6, 4.5, 13.4, 4.5)
    g.poly([(6, 4.4), (6, 3.2), (6.7, 2.5), (9.3, 2.5), (10, 3.2), (10, 4.4)])
    g.poly([(3.9, 4.6), (4.5, 12.4), (5.6, 13.5), (10.4, 13.5), (11.5, 12.4), (12.1, 4.6)])
    g.line(6.6, 7.2, 6.6, 10.9)
    g.line(9.4, 7.2, 9.4, 10.9)


def _copy_back() -> QPainterPath:
    path = QPainterPath(QPointF(10.5, 5.1))
    path.lineTo(10.5, 4)
    path.quadTo(10.5, 2.5, 9, 2.5)
    path.lineTo(4, 2.5)
    path.quadTo(2.5, 2.5, 2.5, 4)
    path.lineTo(2.5, 9)
    path.quadTo(2.5, 10.5, 4, 10.5)
    path.lineTo(5.1, 10.5)
    return path


def _copy(g):
    g.path(_copy_back())
    g.rect(5.5, 5.5, 8, 8, 1.5)


def _duplicate(g):
    _copy(g)
    g.line(9.5, 7.6, 9.5, 11.4)
    g.line(7.6, 9.5, 11.4, 9.5)


def _paste(g):
    g.rect(3, 3.5, 10, 10.25, 1.6)
    g.erase(_line_path(6.2, 3.5, 9.8, 3.5), 2.6)
    g.rect(5.75, 2, 4.5, 2.9, 1)
    g.line(5.9, 8.2, 10.1, 8.2)
    g.line(5.9, 10.8, 8.6, 10.8)


def _cut(g):
    g.circle(4.1, 4.4, 1.85)
    g.circle(4.1, 11.6, 1.85)
    g.line(5.6, 5.5, 13.5, 12.4)
    g.line(5.6, 10.5, 13.5, 3.6)


def _undo(g):
    path = QPainterPath(QPointF(3, 6.5))
    path.lineTo(9.75, 6.5)
    path.arcTo(QRectF(6.25, 6.5, 7, 7), 90, -180)
    path.lineTo(6.5, 13.5)
    g.path(path)
    g.poly([(5.75, 3.75), (3, 6.5), (5.75, 9.25)])


def _rename(g):
    left = QPainterPath(QPointF(8.4, 5))
    left.lineTo(3.5, 5)
    left.quadTo(2, 5, 2, 6.5)
    left.lineTo(2, 9.5)
    left.quadTo(2, 11, 3.5, 11)
    left.lineTo(8.4, 11)
    g.path(left)
    right = QPainterPath(QPointF(12.6, 5))
    right.quadTo(14, 5, 14, 6.5)
    right.lineTo(14, 9.5)
    right.quadTo(14, 11, 12.6, 11)
    g.path(right)
    g.line(10.5, 3.2, 10.5, 12.8)
    g.line(9.1, 2.6, 10.5, 3.2)
    g.line(11.9, 2.6, 10.5, 3.2)
    g.line(9.1, 13.4, 10.5, 12.8)
    g.line(11.9, 13.4, 10.5, 12.8)
    g.line(4.6, 8, 7, 8)


def _check(g):
    g.poly([(3.25, 8.4), (6.5, 11.6), (12.75, 4.6)])


def _close(g):
    g.line(4, 4, 12, 12)
    g.line(12, 4, 4, 12)


def _search(g):
    g.circle(7, 7, 4.25)
    g.line(10.2, 10.2, 13.5, 13.5)


def _zoom_in(g):
    _search(g)
    g.line(7, 5.2, 7, 8.8)
    g.line(5.2, 7, 8.8, 7)


def _zoom_out(g):
    _search(g)
    g.line(5.2, 7, 8.8, 7)


def _more(g):
    for x in (3.4, 8, 12.6):
        g.dot(x, 8, 1.2)


def _settings(g):
    path = QPainterPath()
    teeth, r_out, r_in = 8, 6.3, 4.7
    first = True
    for k in range(teeth):
        base = k * 360.0 / teeth
        for radius, ang in ((r_in, base - 14.5), (r_out, base - 8.5),
                            (r_out, base + 8.5), (r_in, base + 14.5)):
            a = math.radians(ang)
            pt = QPointF(8 + radius * math.cos(a), 8 + radius * math.sin(a))
            if first:
                path.moveTo(pt)
                first = False
            else:
                path.lineTo(pt)
    path.closeSubpath()
    g.width(min(g.w, 1.3))
    g.path(path)
    g.circle(8, 8, 1.9)
    g.width()


def _save(g):
    g.poly([(3.9, 2.75), (10.4, 2.75), (13.25, 5.6), (13.25, 12.1), (12.1, 13.25),
            (3.9, 13.25), (2.75, 12.1), (2.75, 3.9)], True)
    g.poly([(5.6, 2.9), (5.6, 5.7), (9.4, 5.7), (9.4, 2.9)])
    g.poly([(5.1, 13.1), (5.1, 9.3), (10.9, 9.3), (10.9, 13.1)])


def _folder(g):
    g.poly([(2, 4.3), (3.3, 3), (6.2, 3), (7.8, 4.75), (12.7, 4.75), (14, 6.05),
            (14, 11.7), (12.7, 13), (3.3, 13), (2, 11.7)], True)


def _folder_open(g):
    g.poly([(2.2, 11.4), (2.2, 4.3), (3.4, 3.1), (6.1, 3.1), (7.6, 4.75), (11.6, 4.75),
            (12.8, 5.95), (12.8, 7)])
    g.poly([(2.4, 12.9), (4.2, 7.9), (5.4, 7), (14.2, 7), (12.4, 12), (11.2, 12.9)], True)


def _file_outline(g):
    g.poly([(4.3, 2), (9.4, 2), (12.75, 5.35), (12.75, 12.9), (11.65, 14), (4.35, 14),
            (3.25, 12.9), (3.25, 3.05)], True)
    g.poly([(9.4, 2.2), (9.4, 5.35), (12.55, 5.35)])


def _file(g):
    _file_outline(g)


def _file_plus(g):
    _file_outline(g)
    g.line(8, 7.9, 8, 11.7)
    g.line(6.1, 9.8, 9.9, 9.8)


def _file_code(g):
    _file_outline(g)
    g.poly([(6.9, 8.2), (5.4, 9.8), (6.9, 11.4)])
    g.poly([(9.1, 8.2), (10.6, 9.8), (9.1, 11.4)])


def _tray(g):
    g.poly([(2.75, 10.2), (2.75, 12.1), (3.9, 13.25), (12.1, 13.25), (13.25, 12.1),
            (13.25, 10.2)])


def _export(g):
    _tray(g)
    g.line(8, 10, 8, 2.9)
    g.poly([(5, 5.6), (8, 2.6), (11, 5.6)])


def _import(g):
    _tray(g)
    g.line(8, 2.6, 8, 9.8)
    g.poly([(5, 7), (8, 10), (11, 7)])


# -- paint tools -------------------------------------------------------------
def _pencil(g):
    g.poly([(10.75, 2.6), (13.4, 5.25), (5.6, 13.05), (2.5, 13.5), (2.95, 10.4)], True)
    g.line(9.1, 4.25, 11.75, 6.9)


def _eraser(g):
    g.poly([(9.25, 2.5), (13.5, 6.75), (7.25, 13), (4.75, 13), (2.5, 10.75), (2.5, 9.25)], True)
    g.line(5.9, 5.9, 10.1, 10.1)
    g.line(7.25, 13, 13.5, 13)


def _line(g):
    g.circle(3.6, 12.4, 1.6)
    g.circle(12.4, 3.6, 1.6)
    g.line(4.8, 11.2, 11.2, 4.8)


def _rect(g):
    g.rect(2.5, 3.75, 11, 8.5, 1.6)


def _rect_fill(g):
    g.solid_rect(2, 3.25, 12, 9.5, 2.2)


def _ellipse(g):
    g.ellipse(8, 8, 5.6, 4.5)


def _ellipse_fill(g):
    path = QPainterPath()
    path.addEllipse(QPointF(8, 8), 6.1, 5)
    g.solid(path, 0)


def _fill(g):
    g.poly([(7, 2.9), (12.4, 8.3), (7.7, 13), (2.6, 7.9), (7, 3.5)], True)
    g.line(3.4, 8.3, 12, 8.3)
    g.line(7, 2.9, 5.4, 1.5)
    drop = QPainterPath(QPointF(13.3, 9.9))
    drop.cubicTo(12.4, 11.1, 12, 11.8, 12, 12.4)
    drop.cubicTo(12, 14.1, 14.6, 14.1, 14.6, 12.4)
    drop.cubicTo(14.6, 11.8, 14.2, 11.1, 13.3, 9.9)
    g.solid(drop, 0)


def _text(g):
    g.line(3.5, 3.5, 12.5, 3.5)
    g.line(3.5, 3.5, 3.5, 5.1)
    g.line(12.5, 3.5, 12.5, 5.1)
    g.line(8, 3.5, 8, 12.7)
    g.line(6.2, 12.7, 9.8, 12.7)


def _corners(g, lo=2.75, hi=13.25, arm=2.6):
    g.poly([(lo, lo + arm), (lo, lo), (lo + arm, lo)])
    g.poly([(hi - arm, lo), (hi, lo), (hi, lo + arm)])
    g.poly([(hi, hi - arm), (hi, hi), (hi - arm, hi)])
    g.poly([(lo + arm, hi), (lo, hi), (lo, hi - arm)])


def _select(g):
    _corners(g, 2.75, 13.25, 2.1)
    for edge in (2.75, 13.25):
        g.line(7, edge, 9, edge)
        g.line(edge, 7, edge, 9)


def _move(g):
    g.line(8, 2.6, 8, 13.4)
    g.line(2.6, 8, 13.4, 8)
    g.poly([(6.1, 4.5), (8, 2.6), (9.9, 4.5)])
    g.poly([(6.1, 11.5), (8, 13.4), (9.9, 11.5)])
    g.poly([(4.5, 6.1), (2.6, 8), (4.5, 9.9)])
    g.poly([(11.5, 6.1), (13.4, 8), (11.5, 9.9)])


def _invert(g):
    g.circle(8, 8, 5.6)
    half = QPainterPath(QPointF(8, 2.4))
    half.arcTo(QRectF(2.4, 2.4, 11.2, 11.2), 90, -180)
    half.closeSubpath()
    g.solid(half, 0)


def _flip_h(g):
    g.line(8, 2.2, 8, 4)
    g.line(8, 7.1, 8, 8.9)
    g.line(8, 12, 8, 13.8)
    g.poly([(5.7, 4), (5.7, 12), (1.9, 8)], True)
    g.solid([(10.3, 4), (10.3, 12), (14.1, 8)], g.w)


def _clear(g):
    g.rect(2.75, 2.75, 10.5, 10.5, 2)
    g.line(6, 6, 10, 10)
    g.line(10, 6, 6, 10)


def _layers_shape(g, fade: bool):
    g.poly([(8, 2.4), (14, 5.6), (8, 8.8), (2, 5.6)], True)
    if fade:
        g.opacity(0.6)
    g.poly([(2, 8.4), (8, 11.6), (14, 8.4)])
    if fade:
        g.opacity(0.32)
    g.poly([(2, 11.2), (8, 14.4), (14, 11.2)])
    g.opacity(1.0)


def _onion(g):
    _layers_shape(g, True)


def _layers(g):
    _layers_shape(g, False)


def _grid(g):
    g.rect(2.5, 2.5, 11, 11, 1.6)
    g.width(min(g.w, 1.2))
    for v in (6.2, 9.8):
        g.line(v, 2.9, v, 13.1)
        g.line(2.9, v, 13.1, v)
    g.width()


# -- 3D / view ---------------------------------------------------------------
def _cursor(g):
    g.poly([(4, 2.5), (4, 12.4), (6.7, 10), (8.5, 13.8), (10.3, 13), (8.5, 9.3),
            (12.1, 9.3)], True)


def _rotate(g):
    rect = QRectF(2.5, 2.75, 11, 11)
    path = QPainterPath()
    path.arcMoveTo(rect, 0)
    path.arcTo(rect, 0, -315)
    path.lineTo(13.5, 6)
    g.path(path)
    g.poly([(13.5, 2.75), (13.5, 6), (10.25, 6)])


def _camera(g):
    g.rect(1.75, 4.75, 12.5, 8.5, 1.8)
    g.poly([(5.4, 4.6), (6.4, 2.9), (9.6, 2.9), (10.6, 4.6)])
    g.circle(8, 9, 2.4)


def _home(g):
    g.poly([(2.4, 7.6), (8, 2.6), (13.6, 7.6)])
    g.poly([(3.9, 6.6), (3.9, 13.3), (12.1, 13.3), (12.1, 6.6)])
    g.poly([(6.5, 13.2), (6.5, 9.6), (9.5, 9.6), (9.5, 13.2)])


def _eye_path() -> QPainterPath:
    path = QPainterPath(QPointF(1.6, 8))
    path.cubicTo(3.3, 5, 5.6, 3.6, 8, 3.6)
    path.cubicTo(10.4, 3.6, 12.7, 5, 14.4, 8)
    path.cubicTo(12.7, 11, 10.4, 12.4, 8, 12.4)
    path.cubicTo(5.6, 12.4, 3.3, 11, 1.6, 8)
    path.closeSubpath()
    return path


def _eye(g):
    g.path(_eye_path())
    g.circle(8, 8, 2.05)


def _eye_off(g):
    _eye(g)
    slash = _line_path(2.9, 2.9, 13.1, 13.1)
    g.erase(slash, g.w * 2.6)
    g.path(slash)


def _cube(g):
    g.poly([(8, 2), (13.3, 5), (13.3, 11), (8, 14), (2.7, 11), (2.7, 5)], True)
    g.poly([(2.9, 5.1), (8, 8), (13.1, 5.1)])
    g.line(8, 8, 8, 13.8)


def _physics(g):
    # a ball dropping onto the ground, with its bounce trail
    g.line(2.5, 13.4, 13.5, 13.4)
    g.circle(10.4, 7.2, 2.7)
    g.dot(2.9, 10.9, 0.85)
    g.dot(4.3, 7.4, 0.85)
    g.dot(6.5, 4.6, 0.85)


def _magnet(g):
    outer = QPainterPath(QPointF(3.4, 2.75))
    outer.lineTo(3.4, 8.6)
    outer.arcTo(QRectF(3.4, 4, 9.2, 9.2), 180, 180)
    outer.lineTo(12.6, 2.75)
    outer.lineTo(9.8, 2.75)
    outer.lineTo(9.8, 8.6)
    outer.arcTo(QRectF(6.2, 6.8, 3.6, 3.6), 0, -180)
    outer.lineTo(6.2, 2.75)
    outer.closeSubpath()
    g.path(outer)
    g.line(3.6, 5.4, 6, 5.4)
    g.line(10, 5.4, 12.4, 5.4)


def _fit(g):
    _corners(g, 2.5, 13.5, 2.8)
    g.rect(5.75, 5.75, 4.5, 4.5, 1)


def _lock_body(g):
    g.rect(3.4, 7.1, 9.2, 6.6, 1.6)
    g.dot(8, 10.4, 0.95)


def _lock(g):
    _lock_body(g)
    path = QPainterPath(QPointF(5.5, 7))
    path.lineTo(5.5, 5.1)
    path.arcTo(QRectF(5.5, 2.5, 5, 5.2), 180, -180)
    path.lineTo(10.5, 7)
    g.path(path)


def _unlock(g):
    _lock_body(g)
    path = QPainterPath(QPointF(5.5, 7))
    path.lineTo(5.5, 5.1)
    path.arcTo(QRectF(5.5, 2.5, 5, 5.2), 180, -165)
    g.path(path)


def _pin(g):
    g.line(4.9, 2.6, 11.1, 2.6)
    g.poly([(6.1, 2.7), (6.1, 6.9), (4.1, 9.75), (11.9, 9.75), (9.9, 6.9), (9.9, 2.7)])
    g.line(8, 9.9, 8, 14)


# -- misc ----------------------------------------------------------------------
def _face(g):
    g.rect(1.6, 3.4, 12.8, 9.2, 2.2)
    g.line(5.4, 6.6, 5.4, 8)
    g.line(10.6, 6.6, 10.6, 8)
    mouth = QPainterPath(QPointF(6.4, 9.7))
    mouth.quadTo(8, 11.1, 9.6, 9.7)
    g.path(mouth)


def _robot(g):
    g.rect(3.4, 5.6, 9.2, 7.6, 2)
    g.line(8, 5.4, 8, 3.6)
    g.dot(8, 2.7, 1.05)
    g.dot(6.2, 9, 0.95)
    g.dot(9.8, 9, 0.95)
    g.line(1.6, 8.4, 1.6, 10.6)
    g.line(14.4, 8.4, 14.4, 10.6)


def _timeline(g):
    g.solid_rect(2.25, 3.1, 7, 2.6, 1.3)
    g.solid_rect(5.5, 6.7, 8.25, 2.6, 1.3)
    g.solid_rect(3.5, 10.3, 5.5, 2.6, 1.3)


def _sliders(g):
    for y, knob in ((4.4, 10.2), (8, 5.6), (11.6, 9)):
        g.line(2.5, y, 13.5, y)
        g.erase_disc(knob, y, 2.55)
        g.circle(knob, y, 1.55)


def _help(g):
    g.circle(8, 8, 5.75)
    hook = QPainterPath(QPointF(6.25, 6.6))
    hook.cubicTo(6.25, 4.5, 9.75, 4.5, 9.75, 6.6)
    hook.cubicTo(9.75, 7.9, 8, 7.8, 8, 9.2)
    g.path(hook)
    g.dot(8, 11.3, 0.9)


def _info(g):
    g.circle(8, 8, 5.75)
    g.line(8, 7.6, 8, 11)
    g.dot(8, 5.2, 0.9)


def _warning(g):
    g.poly([(8, 2.6), (14.2, 12.9), (1.8, 12.9)], True)
    g.line(8, 6.6, 8, 9.4)
    g.dot(8, 11.2, 0.85)


def _check_circle(g):
    g.circle(8, 8, 5.75)
    g.poly([(5.4, 8.2), (7.3, 10.1), (10.7, 6.1)])


def _x_circle(g):
    g.circle(8, 8, 5.75)
    g.line(6, 6, 10, 10)
    g.line(10, 6, 6, 10)


def _chevron_right(g):
    g.poly([(6.25, 3.75), (10.5, 8), (6.25, 12.25)])


def _chevron_down(g):
    g.poly([(3.75, 6.25), (8, 10.5), (12.25, 6.25)])


def _chevron_up(g):
    g.poly([(3.75, 9.75), (8, 5.5), (12.25, 9.75)])


def _keyboard(g):
    g.rect(1.6, 3.9, 12.8, 8.2, 1.8)
    for x in (4.4, 6.8, 9.2, 11.6):
        g.dot(x, 6.7, 0.75)
    g.line(5.3, 9.5, 10.7, 9.5)


def _star4(cx, cy, r, pinch=0.16) -> QPainterPath:
    path = QPainterPath(QPointF(cx, cy - r))
    k = r * pinch
    path.quadTo(cx + k, cy - k, cx + r, cy)
    path.quadTo(cx + k, cy + k, cx, cy + r)
    path.quadTo(cx - k, cy + k, cx - r, cy)
    path.quadTo(cx - k, cy - k, cx, cy - r)
    path.closeSubpath()
    return path


def _sparkle(g):
    g.solid(_star4(6.75, 9, 5.4), 0.9)
    g.solid(_star4(12.25, 3.9, 2.5), 0.6)


def _image(g):
    g.rect(2.1, 2.75, 11.8, 10.5, 1.8)
    g.dot(5.7, 6.1, 1.15)
    g.poly([(2.6, 11.7), (6, 8.5), (8.4, 10.7), (10.3, 8.9), (13.4, 11.7)])


def _code(g):
    g.poly([(5.75, 4.25), (2.25, 8), (5.75, 11.75)])
    g.poly([(10.25, 4.25), (13.75, 8), (10.25, 11.75)])


def _arrow_right(g):
    g.line(2.75, 8, 13, 8)
    g.poly([(9, 3.9), (13.1, 8), (9, 12.1)])


def _maximize(g):
    g.poly([(9.4, 2.75), (13.25, 2.75), (13.25, 6.6)])
    g.poly([(6.6, 13.25), (2.75, 13.25), (2.75, 9.4)])
    g.line(13.1, 2.9, 9.2, 6.8)
    g.line(2.9, 13.1, 6.8, 9.2)


def _external(g):
    g.poly([(7, 3.75), (4, 3.75), (2.75, 5), (2.75, 12), (4, 13.25), (11, 13.25),
            (12.25, 12), (12.25, 9)])
    g.poly([(9.6, 2.75), (13.25, 2.75), (13.25, 6.4)])
    g.line(13.1, 2.9, 7.6, 8.4)


def _panel(g):
    g.rect(2.1, 2.75, 11.8, 10.5, 1.8)


def _panel_left(g):
    _panel(g)
    g.line(6.2, 3, 6.2, 13)


def _panel_right(g):
    _panel(g)
    g.line(9.8, 3, 9.8, 13)


def _panel_bottom(g):
    _panel(g)
    g.line(2.4, 9.6, 13.6, 9.6)


def _menu(g):
    for y in (4.25, 8, 11.75):
        g.line(2.75, y, 13.25, y)


def _grip(g):
    for x in (5.9, 10.1):
        for y in (4, 8, 12):
            g.dot(x, y, 1.05)


def _filter(g):
    g.poly([(2.5, 3.25), (13.5, 3.25), (9.4, 8.3), (9.4, 12.4), (6.6, 13.5), (6.6, 8.3)], True)


def _keyframe(g):
    g.poly([(8, 2.6), (13.4, 8), (8, 13.4), (2.6, 8)], True)


def _keyframe_fill(g):
    g.solid([(8, 2.6), (13.4, 8), (8, 13.4), (2.6, 8)], g.w)


def _clock(g):
    g.circle(8, 8, 5.75)
    g.poly([(8, 4.7), (8, 8), (10.2, 9.4)])


def _speed(g):
    g.arc(8, 9.75, 5.75, -8, 196)
    g.line(8, 9.75, 10.7, 6.4)
    g.dot(8, 9.75, 1.0)


def _record(g):
    g.dot(8, 8, 4.75)


def _dot(g):
    g.dot(8, 8, 2.6)


def _target(g):
    g.circle(8, 8, 4.1)
    g.dot(8, 8, 1.0)
    g.line(8, 1.75, 8, 3.9)
    g.line(8, 12.1, 8, 14.25)
    g.line(1.75, 8, 3.9, 8)
    g.line(12.1, 8, 14.25, 8)


def _axis(g):
    g.line(6.5, 9.5, 6.5, 2.75)
    g.poly([(4.6, 4.6), (6.5, 2.7), (8.4, 4.6)])
    g.line(6.5, 9.5, 13.25, 9.5)
    g.poly([(11.4, 7.6), (13.3, 9.5), (11.4, 11.4)])
    g.line(6.5, 9.5, 2.6, 13.4)
    g.poly([(2.6, 10.8), (2.6, 13.4), (5.2, 13.4)])


def _joint(g):
    g.circle(4.2, 11.8, 2.2)
    g.circle(11.8, 4.2, 2.2)
    g.line(5.8, 10.2, 10.2, 5.8)


def _chip(g):
    g.rect(4.25, 4.25, 7.5, 7.5, 1.4)
    for v in (6.4, 9.6):
        g.line(v, 2.1, v, 4)
        g.line(v, 12, v, 13.9)
        g.line(2.1, v, 4, v)
        g.line(12, v, 13.9, v)
    g.solid_rect(6.75, 6.75, 2.5, 2.5, 0.5)


def _bolt(g):
    g.poly([(9, 1.9), (3.7, 9), (7.8, 9), (7, 14.1), (12.3, 7), (8.2, 7)], True)


def _star(g):
    pts = []
    for i in range(10):
        radius = 6.2 if i % 2 == 0 else 2.7
        a = math.radians(-90 + i * 36)
        pts.append((8 + radius * math.cos(a), 8.5 + radius * math.sin(a)))
    g.poly(pts, True)


def _compass(g):
    g.circle(8, 8, 5.75)
    g.poly([(10.5, 5.5), (9, 9), (5.5, 10.5), (7, 7)], True)


_GLYPHS: dict[str, Callable[[_G], None]] = {
    "play": _play, "pause": _pause, "stop": _stop, "loop": _loop,
    "skip-start": _skip_start, "skip-end": _mirror_x(_skip_start),
    "step-back": _step_back, "step-forward": _mirror_x(_step_back),
    "plus": _plus, "minus": _minus, "trash": _trash, "copy": _copy, "paste": _paste,
    "cut": _cut, "duplicate": _duplicate, "undo": _undo, "redo": _mirror_x(_undo),
    "rename": _rename, "check": _check, "close": _close, "search": _search,
    "more": _more, "settings": _settings, "save": _save, "folder": _folder,
    "file": _file, "export": _export, "import": _import,
    "pencil": _pencil, "eraser": _eraser, "line": _line, "rect": _rect,
    "rect-fill": _rect_fill, "ellipse": _ellipse, "ellipse-fill": _ellipse_fill,
    "fill": _fill, "text": _text, "select": _select, "move": _move, "invert": _invert,
    "flip-h": _flip_h, "flip-v": _transpose(_flip_h), "clear": _clear, "onion": _onion,
    "grid": _grid,
    "cursor": _cursor, "rotate": _rotate, "camera": _camera, "home": _home, "eye": _eye,
    "eye-off": _eye_off, "cube": _cube, "physics": _physics, "magnet": _magnet,
    "zoom-in": _zoom_in, "zoom-out": _zoom_out, "fit": _fit, "lock": _lock,
    "unlock": _unlock, "pin": _pin,
    "face": _face, "robot": _robot, "timeline": _timeline, "sliders": _sliders,
    "help": _help, "info": _info, "warning": _warning,
    "chevron-left": _mirror_x(_chevron_right), "chevron-right": _chevron_right,
    "chevron-down": _chevron_down, "chevron-up": _chevron_up,
    "keyboard": _keyboard, "sparkle": _sparkle,
    "check-circle": _check_circle, "x-circle": _x_circle,
    "file-plus": _file_plus, "file-code": _file_code, "folder-open": _folder_open,
    "image": _image, "code": _code,
    "arrow-right": _arrow_right, "arrow-left": _mirror_x(_arrow_right),
    "arrow-down": _transpose(_arrow_right),
    "arrow-up": _transpose(_mirror_x(_arrow_right)),
    "maximize": _maximize, "external": _external,
    "panel-left": _panel_left, "panel-right": _panel_right, "panel-bottom": _panel_bottom,
    "menu": _menu, "grip": _grip, "filter": _filter,
    "keyframe": _keyframe, "keyframe-fill": _keyframe_fill, "layers": _layers,
    "clock": _clock, "speed": _speed, "record": _record, "dot": _dot,
    "target": _target, "axis": _axis, "joint": _joint, "chip": _chip, "bolt": _bolt,
    "star": _star, "compass": _compass,
}


# ---------------------------------------------------------------------------
# rasterising + caching
# ---------------------------------------------------------------------------
_IMAGE_CACHE: dict[tuple, QImage] = {}
_PIXMAP_CACHE: dict[tuple, QPixmap] = {}
_ICON_CACHE: dict[tuple, QIcon] = {}


def _stroke_for(logical_size: float) -> float:
    """Stroke width in grid units: 1.5 at 16px, optically thinner when large."""
    if logical_size <= 0:
        return 1.5
    return max(0.85, min(1.6, 1.5 * math.sqrt(16.0 / logical_size)))


def image(name: str, color: str | QColor | None = None, size: int = 16,
          scale: float = 1.0, stroke: float | None = None) -> QImage:
    """Rasterise a glyph into a transparent ``QImage`` of ``size * scale``
    device pixels. Needs no ``QApplication`` (used by the theme at import).
    ``stroke`` overrides the line weight (in 16-grid units)."""
    col = QColor(color) if color is not None else QColor(_token("FG"))
    px = max(1, int(round(size * scale)))
    key = (name, col.rgba(), size, px, stroke)
    cached = _IMAGE_CACHE.get(key)
    if cached is not None:
        return cached
    img = QImage(px, px, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    draw = _GLYPHS.get(name)
    if draw is not None:
        painter = QPainter(img)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.scale(px / 16.0, px / 16.0)
            draw(_G(painter, col, stroke if stroke else _stroke_for(size)))
        except Exception:  # a broken glyph must never take the UI down
            pass
        finally:
            painter.end()
    _IMAGE_CACHE[key] = img
    return img


def _device_ratios() -> list[float]:
    ratios = {1.0, 2.0}
    app = QGuiApplication.instance()
    if app is not None:
        try:
            for screen in app.screens():
                ratios.add(round(float(screen.devicePixelRatio()), 2))
        except Exception:
            pass
    return sorted(ratios)


def _max_ratio() -> float:
    app = QGuiApplication.instance()
    if app is None:
        return 1.0
    try:
        return max(1.0, float(app.devicePixelRatio()))
    except Exception:
        return 1.0


def _pixmap_at(name: str, color: QColor, size: int, ratio: float) -> QPixmap:
    key = (name, color.rgba(), size, ratio)
    cached = _PIXMAP_CACHE.get(key)
    if cached is not None:
        return cached
    pm = QPixmap.fromImage(image(name, color, size, ratio))
    pm.setDevicePixelRatio(ratio)
    _PIXMAP_CACHE[key] = pm
    return pm


def pixmap(name: str, color: str | None = None, size: int = 16) -> QPixmap:
    """A crisp pixmap of the glyph, ``size`` logical pixels square."""
    if name not in _GLYPHS:
        return QPixmap()
    col = QColor(color) if color is not None else QColor(_token("FG"))
    return _pixmap_at(name, col, int(size), _max_ratio())


def icon(name: str, color: str | None = None, size: int = 16) -> QIcon:
    """A multi-state, multi-resolution ``QIcon`` (see module docstring)."""
    if name not in _GLYPHS:
        return QIcon()
    size = int(size)
    ratios = tuple(_device_ratios())
    key = (name, color, size, ratios)
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached

    if color is None:
        normal = QColor(_token("FG"))
        active = QColor("#ffffff")
        on = QColor(_token("ACCENT"))
        on_active = QColor(_token("ACCENT_HOVER"))
        disabled = QColor(_token("FG_DISABLED"))
    else:
        normal = active = on = on_active = QColor(color)
        disabled = QColor(_token("FG_DISABLED"))

    result = QIcon()
    for ratio in ratios:
        def add(col: QColor, mode: QIcon.Mode, state: QIcon.State) -> None:
            result.addPixmap(_pixmap_at(name, col, size, ratio), mode, state)

        add(normal, QIcon.Normal, QIcon.Off)
        add(active, QIcon.Active, QIcon.Off)
        add(active, QIcon.Selected, QIcon.Off)
        add(disabled, QIcon.Disabled, QIcon.Off)
        add(on, QIcon.Normal, QIcon.On)
        add(on_active, QIcon.Active, QIcon.On)
        add(on, QIcon.Selected, QIcon.On)
        add(disabled, QIcon.Disabled, QIcon.On)
    _ICON_CACHE[key] = result
    return result
