"""Pure-numpy pixel operations for 128x64 1-bit faces (no Qt imports).

Everything here works on ``(64, 128)`` bool arrays indexed ``arr[y, x]``.
Drawing primitives return a *mask* of the pixels they touch rather than
painting directly, so the canvas can symmetrise the mask (mirror mode),
preview it, or apply it as "set" or "erase" without any primitive having to
know about those concerns.
"""

from __future__ import annotations

import numpy as np

from .servo_model import FACE_WIDTH, FACE_HEIGHT

W = FACE_WIDTH
H = FACE_HEIGHT
MAX_BRUSH = 4


def blank() -> np.ndarray:
    return np.zeros((H, W), dtype=bool)


def empty_mask() -> np.ndarray:
    return np.zeros((H, W), dtype=bool)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def line_points(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """Bresenham: every pixel on the segment, both endpoints included, with
    no gaps and no doubled corners (8-connected)."""
    points: list[tuple[int, int]] = []
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    x, y = x0, y0
    while True:
        points.append((x, y))
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy
    return points


def constrain_line(x0: int, y0: int, x1: int, y1: int) -> tuple[int, int]:
    """Snap the end point so the segment is horizontal, vertical or 45 deg."""
    dx, dy = x1 - x0, y1 - y0
    adx, ady = abs(dx), abs(dy)
    if adx == 0 and ady == 0:
        return x1, y1
    # tan(22.5 deg) ~= 0.414 splits the octants between axis and diagonal.
    if ady <= adx * 0.4142:
        return x1, y0
    if adx <= ady * 0.4142:
        return x0, y1
    d = max(adx, ady)
    return x0 + (d if dx >= 0 else -d), y0 + (d if dy >= 0 else -d)


def constrain_square(x0: int, y0: int, x1: int, y1: int) -> tuple[int, int]:
    """Snap the end point so the dragged box is a square."""
    dx, dy = x1 - x0, y1 - y0
    d = max(abs(dx), abs(dy))
    return x0 + (d if dx >= 0 else -d), y0 + (d if dy >= 0 else -d)


def brush_origin(x: int, y: int, size: int) -> tuple[int, int]:
    """Top-left pixel of the size x size brush square centred on (x, y)."""
    o = (size - 1) // 2
    return x - o, y - o


# ---------------------------------------------------------------------------
# Mask builders
# ---------------------------------------------------------------------------

def _stamp(mask: np.ndarray, x: int, y: int, size: int) -> None:
    bx, by = brush_origin(x, y, size)
    x0, y0 = max(0, bx), max(0, by)
    x1, y1 = min(W, bx + size), min(H, by + size)
    if x0 < x1 and y0 < y1:
        mask[y0:y1, x0:x1] = True


def dot_mask(x: int, y: int, size: int = 1) -> np.ndarray:
    mask = empty_mask()
    _stamp(mask, x, y, size)
    return mask


def line_mask(x0: int, y0: int, x1: int, y1: int, size: int = 1) -> np.ndarray:
    mask = empty_mask()
    pts = line_points(x0, y0, x1, y1)
    if size <= 1:
        xs = np.fromiter((p[0] for p in pts), dtype=np.intp, count=len(pts))
        ys = np.fromiter((p[1] for p in pts), dtype=np.intp, count=len(pts))
        ok = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)
        mask[ys[ok], xs[ok]] = True
    else:
        for x, y in pts:
            _stamp(mask, x, y, size)
    return mask


def _norm_box(x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int]:
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def rect_mask(x0: int, y0: int, x1: int, y1: int, filled: bool = False,
              size: int = 1) -> np.ndarray:
    """Rectangle spanning both corners inclusive. Outlines grow inwards so a
    thick border never leaves the dragged box."""
    x0, y0, x1, y1 = _norm_box(x0, y0, x1, y1)
    mask = empty_mask()
    cx0, cy0 = max(0, x0), max(0, y0)
    cx1, cy1 = min(W - 1, x1), min(H - 1, y1)
    if cx0 > cx1 or cy0 > cy1:
        return mask
    mask[cy0:cy1 + 1, cx0:cx1 + 1] = True
    if not filled:
        ix0, iy0, ix1, iy1 = x0 + size, y0 + size, x1 - size, y1 - size
        if ix0 <= ix1 and iy0 <= iy1:
            jx0, jy0 = max(0, ix0), max(0, iy0)
            jx1, jy1 = min(W - 1, ix1), min(H - 1, iy1)
            if jx0 <= jx1 and jy0 <= jy1:
                mask[jy0:jy1 + 1, jx0:jx1 + 1] = False
    return mask


def ellipse_points(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """Outline pixels of the ellipse inscribed in the inclusive box, using
    Zingl's integer midpoint algorithm (handles even and odd diameters)."""
    if x0 == x1 or y0 == y1:  # degenerate box: a 1px-wide ellipse is a line
        return line_points(x0, y0, x1, y1)
    pts: list[tuple[int, int]] = []
    a = abs(x1 - x0)
    b = abs(y1 - y0)
    b1 = b & 1
    dx = 4 * (1 - a) * b * b
    dy = 4 * (b1 + 1) * a * a
    err = dx + dy + b1 * a * a
    if x0 > x1:
        x0 = x1
        x1 += a
    if y0 > y1:
        y0 = y1
    y0 += (b + 1) // 2
    y1 = y0 - b1
    a8 = 8 * a * a
    b8 = 8 * b * b
    while True:
        pts += [(x1, y0), (x0, y0), (x0, y1), (x1, y1)]
        e2 = 2 * err
        if e2 <= dy:
            y0 += 1
            y1 -= 1
            dy += a8
            err += dy
        if e2 >= dx or 2 * err > dy:
            x0 += 1
            x1 -= 1
            dx += b8
            err += dx
        if x0 > x1:
            break
    while y0 - y1 <= b:  # finish the tips of very flat ellipses
        pts += [(x0 - 1, y0), (x1 + 1, y0)]
        y0 += 1
        pts += [(x0 - 1, y1), (x1 + 1, y1)]
        y1 -= 1
    return pts


def _points_mask(pts: list[tuple[int, int]]) -> np.ndarray:
    mask = empty_mask()
    for x, y in pts:
        if 0 <= x < W and 0 <= y < H:
            mask[y, x] = True
    return mask


def _ellipse_fill(x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    spans: dict[int, list[int]] = {}
    for x, y in ellipse_points(x0, y0, x1, y1):
        s = spans.get(y)
        if s is None:
            spans[y] = [x, x]
        elif x < s[0]:
            s[0] = x
        elif x > s[1]:
            s[1] = x
    mask = empty_mask()
    for y, (lo, hi) in spans.items():
        if 0 <= y < H:
            lo, hi = max(0, lo), min(W - 1, hi)
            if lo <= hi:
                mask[y, lo:hi + 1] = True
    return mask


def ellipse_mask(x0: int, y0: int, x1: int, y1: int, filled: bool = False,
                 size: int = 1) -> np.ndarray:
    """Ellipse inscribed in the box spanning both corners inclusive. Thick
    outlines grow inwards, like rect_mask."""
    x0, y0, x1, y1 = _norm_box(x0, y0, x1, y1)
    if filled:
        return _ellipse_fill(x0, y0, x1, y1)
    if size <= 1:
        return _points_mask(ellipse_points(x0, y0, x1, y1))
    full = _ellipse_fill(x0, y0, x1, y1)
    ix0, iy0, ix1, iy1 = x0 + size, y0 + size, x1 - size, y1 - size
    if ix0 > ix1 or iy0 > iy1:
        return full
    return full & ~_ellipse_fill(ix0, iy0, ix1, iy1)


def flood_mask(arr: np.ndarray, x: int, y: int) -> np.ndarray:
    """Mask of the 4-connected region around (x, y) sharing its value.

    Span fill over per-row bytearrays: C-speed ``find`` walks each run, so a
    full-canvas fill costs ~64 span pushes instead of 8192 pixel visits."""
    mask = empty_mask()
    if not (0 <= x < W and 0 <= y < H):
        return mask
    target = bool(arr[y, x])
    same = (arr == target).astype(np.uint8)
    rows = [bytearray(same[r].tobytes()) for r in range(H)]
    stack = [(x, y)]
    zero = b"\x00"
    one = b"\x01"
    while stack:
        sx, sy = stack.pop()
        row = rows[sy]
        if not row[sx]:
            continue
        left = row.rfind(zero, 0, sx) + 1
        right = row.find(zero, sx)
        if right < 0:
            right = W
        row[left:right] = bytes(right - left)
        mask[sy, left:right] = True
        for ny in (sy - 1, sy + 1):
            if not (0 <= ny < H):
                continue
            nrow = rows[ny]
            pos = nrow.find(one, left, right)
            while pos >= 0:
                stack.append((pos, ny))
                end = nrow.find(zero, pos, right)
                if end < 0:
                    break
                pos = nrow.find(one, end, right)
    return mask


def symmetrize(mask: np.ndarray, mirror_x: bool = False, mirror_y: bool = False) -> np.ndarray:
    """OR the mask with its reflection(s). ``mirror_x`` reflects across the
    vertical centre line (left <-> right), ``mirror_y`` across the horizontal
    one (top <-> bottom)."""
    out = mask
    if mirror_x:
        out = out | out[:, ::-1]
    if mirror_y:
        out = out | out[::-1, :]
    return out


def mirror_points(x: int, y: int, mirror_x: bool, mirror_y: bool) -> list[tuple[int, int]]:
    """The seed plus its reflections, de-duplicated."""
    pts = [(x, y)]
    if mirror_x:
        pts.append((W - 1 - x, y))
    if mirror_y:
        pts.extend([(px, H - 1 - py) for px, py in list(pts)])
    seen: list[tuple[int, int]] = []
    for p in pts:
        if p not in seen:
            seen.append(p)
    return seen


def apply_mask(arr: np.ndarray, mask: np.ndarray, value: bool) -> np.ndarray:
    """Return a copy of ``arr`` with the masked pixels set to ``value``."""
    out = arr.copy()
    out[mask] = value
    return out


# ---------------------------------------------------------------------------
# Whole-image operations (all return new arrays)
# ---------------------------------------------------------------------------

def invert(arr: np.ndarray) -> np.ndarray:
    return ~arr


def flip_h(arr: np.ndarray) -> np.ndarray:
    return arr[:, ::-1].copy()


def flip_v(arr: np.ndarray) -> np.ndarray:
    return arr[::-1, :].copy()


def shift(arr: np.ndarray, dx: int, dy: int, wrap: bool = True) -> np.ndarray:
    if wrap:
        return np.roll(arr, (dy, dx), axis=(0, 1))
    out = blank()
    sx0, sx1 = max(0, -dx), min(W, W - dx)
    sy0, sy1 = max(0, -dy), min(H, H - dy)
    if sx0 < sx1 and sy0 < sy1:
        out[sy0 + dy:sy1 + dy, sx0 + dx:sx1 + dx] = arr[sy0:sy1, sx0:sx1]
    return out


def content_bounds(arr: np.ndarray) -> tuple[int, int, int, int] | None:
    """Inclusive (x0, y0, x1, y1) of lit pixels, or None if blank."""
    ys, xs = np.nonzero(arr)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def center_content(arr: np.ndarray) -> np.ndarray:
    b = content_bounds(arr)
    if b is None:
        return arr.copy()
    x0, y0, x1, y1 = b
    dx = (W - (x1 - x0 + 1)) // 2 - x0
    dy = (H - (y1 - y0 + 1)) // 2 - y0
    return shift(arr, dx, dy, wrap=False)


def paste(arr: np.ndarray, clip: np.ndarray, x: int, y: int,
          transparent: bool = False) -> np.ndarray:
    """Copy of ``arr`` with ``clip`` stamped with its top-left at (x, y),
    clipped to the canvas. ``transparent`` only ORs lit pixels in."""
    out = arr.copy()
    ch, cw = clip.shape
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + cw), min(H, y + ch)
    if x0 >= x1 or y0 >= y1:
        return out
    src = clip[y0 - y:y1 - y, x0 - x:x1 - x]
    if transparent:
        out[y0:y1, x0:x1] |= src
    else:
        out[y0:y1, x0:x1] = src
    return out


# ---------------------------------------------------------------------------
# Image import
# ---------------------------------------------------------------------------

def threshold_gray(gray: np.ndarray, threshold: int = 128, dither: bool = False,
                   invert_result: bool = False) -> np.ndarray:
    """uint8 grayscale (any shape) -> bool array of lit pixels.

    Without dithering a pixel is lit when ``gray >= threshold``. With
    Floyd-Steinberg the threshold still biases overall brightness (128 is
    neutral), which is what the import slider previews live."""
    g = np.asarray(gray)
    if not dither:
        out = g >= threshold
    else:
        h, w = g.shape
        # Error-diffuse on plain Python floats: for 8192 pixels this beats
        # numpy scalar indexing by ~10x and stays well under one frame.
        buf = [[float(v) for v in row] for row in g.tolist()]
        buf.append([0.0] * w)  # sentinel row so y+1 never needs a bounds test
        t = float(threshold)
        res = np.zeros((h, w), dtype=bool)
        for yy in range(h):
            row = buf[yy]
            nxt = buf[yy + 1]
            lit = res[yy]
            bits = [False] * w
            for xx in range(w):
                old = row[xx]
                on = old >= t
                bits[xx] = on
                err = old - (255.0 if on else 0.0)
                if xx + 1 < w:
                    row[xx + 1] += err * 0.4375
                    nxt[xx + 1] += err * 0.0625
                if xx > 0:
                    nxt[xx - 1] += err * 0.1875
                nxt[xx] += err * 0.3125
            lit[:] = bits
        out = res
    return ~out if invert_result else out


# ---------------------------------------------------------------------------
# Undo history
# ---------------------------------------------------------------------------

class History:
    """Undo/redo stack for one frame. Snapshots are stored bit-packed
    (1 KB each), so a deep history is cheap."""

    def __init__(self, limit: int = 256):
        self.limit = limit
        self._undo: list[bytes] = []
        self._redo: list[bytes] = []

    @staticmethod
    def _pack(arr: np.ndarray) -> bytes:
        return np.packbits(arr, axis=None).tobytes()

    @staticmethod
    def _unpack(data: bytes) -> np.ndarray:
        return np.unpackbits(np.frombuffer(data, dtype=np.uint8)).reshape(H, W).astype(bool)

    def push(self, before: np.ndarray) -> None:
        """Record the state that existed *before* an edit."""
        self._undo.append(self._pack(before))
        if len(self._undo) > self.limit:
            del self._undo[0]
        self._redo.clear()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self, current: np.ndarray) -> np.ndarray | None:
        if not self._undo:
            return None
        self._redo.append(self._pack(current))
        return self._unpack(self._undo.pop())

    def redo(self, current: np.ndarray) -> np.ndarray | None:
        if not self._redo:
            return None
        self._undo.append(self._pack(current))
        return self._unpack(self._redo.pop())

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
