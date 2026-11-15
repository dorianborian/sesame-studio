"""Qt-side rendering helpers for 1-bit faces: fast QImage <-> numpy
conversions, the shared OLED look (palette), text rasterising and a
thumbnail cache keyed by the packed frame bytes."""

from __future__ import annotations

from collections import OrderedDict

import numpy as np
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QFontMetrics, QImage, QPainter, QPixmap,
)

from .project import BYTES_PER_FRAME, frame_to_array
from .servo_model import FACE_WIDTH, FACE_HEIGHT

# -- OLED look ---------------------------------------------------------------
# An SSD1306 "white" panel is really a cool blue-white on a near-black glass.
OLED_ON = "#cdefff"
OLED_OFF = "#05080c"
OLED_ONION = "#1f4a5e"      # previous frame ghost (dim teal)
OLED_GHOST = "#ffb066"      # un-committed preview (text stamp before click)
OLED_GLASS = "#0a0f15"      # bezel / letterbox around the panel

# Palette indices used by indexed canvas images.
IDX_OFF, IDX_ON, IDX_ONION, IDX_GHOST = 0, 1, 2, 3


def _rgb(hex_color: str) -> int:
    return QColor(hex_color).rgb()


def oled_color_table() -> list[int]:
    return [_rgb(OLED_OFF), _rgb(OLED_ON), _rgb(OLED_ONION), _rgb(OLED_GHOST)]


# -- conversions -------------------------------------------------------------

def blank_image() -> QImage:
    img = QImage(FACE_WIDTH, FACE_HEIGHT, QImage.Format_RGB32)
    img.fill(QColor("black"))
    return img


def frame_bytes_to_image(frame: bytes) -> QImage:
    """Packed frame -> 128x64 RGB32 image, white-on-black (legacy look, used
    by anything that composites or paints on the result)."""
    data = bytes(frame)
    if len(data) != BYTES_PER_FRAME:
        data = data[:BYTES_PER_FRAME].ljust(BYTES_PER_FRAME, b"\x00")
    # The packed layout (16 bytes/row, MSB first) *is* QImage.Format_Mono.
    mono = QImage(data, FACE_WIDTH, FACE_HEIGHT, FACE_WIDTH // 8, QImage.Format_Mono)
    mono.setColorTable([0xFF000000, 0xFFFFFFFF])
    return mono.convertToFormat(QImage.Format_RGB32)  # deep copy, owns its bits


def array_to_image(arr: np.ndarray) -> QImage:
    """(64,128) bool -> RGB32 white-on-black QImage."""
    return frame_bytes_to_image(np.packbits(np.asarray(arr, dtype=bool), axis=None).tobytes())


def indexed_image(index: np.ndarray, color_table: list[int] | None = None) -> QImage:
    """uint8 palette-index array (h, w) -> Indexed8 QImage (deep copy)."""
    idx = np.ascontiguousarray(index, dtype=np.uint8)
    h, w = idx.shape
    if w % 4:  # QImage scanlines are 32-bit aligned
        padded = np.zeros((h, (w + 3) // 4 * 4), dtype=np.uint8)
        padded[:, :w] = idx
        idx = padded
    img = QImage(idx.tobytes(), w, h, idx.shape[1], QImage.Format_Indexed8)
    img.setColorTable(color_table or oled_color_table())
    return img.copy()


def oled_image(arr: np.ndarray, onion: np.ndarray | None = None,
               ghost: np.ndarray | None = None) -> QImage:
    """Compose a frame (+ optional onion-skin and ghost layers) into an
    Indexed8 image using the OLED palette."""
    index = np.asarray(arr, dtype=np.uint8)
    if onion is not None or ghost is not None:
        index = index.copy()
        if onion is not None:
            index[onion & ~arr] = IDX_ONION
        if ghost is not None:
            index[ghost] = IDX_GHOST
    return indexed_image(index)


def image_to_array(image: QImage) -> np.ndarray:
    """Any QImage -> (64,128) bool array of "lit" pixels.

    A pixel is lit when its HSL lightness is above 127, exactly like the old
    per-pixel ``QColor(image.pixel(x, y)).lightness() > 127`` test (which
    works out to max(r,g,b) + min(r,g,b) >= 255), but vectorised. Images of
    another size are cropped/padded from the top-left corner."""
    if image.isNull():
        return np.zeros((FACE_HEIGHT, FACE_WIDTH), dtype=bool)
    img = image.convertToFormat(QImage.Format_RGB32)
    w, h = img.width(), img.height()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.sizeInBytes())
    px = buf.reshape(h, img.bytesPerLine() // 4, 4)[:, :w, :3].astype(np.int16)
    lit = (px.max(axis=2) + px.min(axis=2)) >= 255
    if (h, w) == (FACE_HEIGHT, FACE_WIDTH):
        return lit
    out = np.zeros((FACE_HEIGHT, FACE_WIDTH), dtype=bool)
    ch, cw = min(h, FACE_HEIGHT), min(w, FACE_WIDTH)
    out[:ch, :cw] = lit[:ch, :cw]
    return out


def image_to_gray(image: QImage) -> np.ndarray:
    """QImage -> uint8 luminance array (h, w); transparency is treated as
    black, which is what an unlit OLED shows."""
    if image.isNull():
        return np.zeros((0, 0), dtype=np.uint8)
    if image.hasAlphaChannel():
        flat = QImage(image.size(), QImage.Format_RGB32)
        flat.fill(QColor("black"))
        p = QPainter(flat)
        p.drawImage(0, 0, image)
        p.end()
        image = flat
    img = image.convertToFormat(QImage.Format_Grayscale8)
    w, h = img.width(), img.height()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.sizeInBytes())
    return buf.reshape(h, img.bytesPerLine())[:, :w].copy()


FIT_CONTAIN = "fit"     # whole image visible, letterboxed with black
FIT_COVER = "fill"      # fills the panel, cropping the overflow
FIT_STRETCH = "stretch"  # ignores aspect ratio


def fit_image_gray(image: QImage, mode: str = FIT_CONTAIN) -> np.ndarray:
    """Scale an arbitrary image to the 128x64 panel -> uint8 gray (64,128)."""
    out = np.zeros((FACE_HEIGHT, FACE_WIDTH), dtype=np.uint8)
    if image.isNull() or image.width() == 0 or image.height() == 0:
        return out
    if mode == FIT_STRETCH:
        aspect = Qt.IgnoreAspectRatio
    elif mode == FIT_COVER:
        aspect = Qt.KeepAspectRatioByExpanding
    else:
        aspect = Qt.KeepAspectRatio
    if image.width() == FACE_WIDTH and image.height() == FACE_HEIGHT:
        scaled = image
    else:
        scaled = image.scaled(FACE_WIDTH, FACE_HEIGHT, aspect, Qt.SmoothTransformation)
    gray = image_to_gray(scaled)
    gh, gw = gray.shape
    # centre-crop (cover) or centre-pad (contain)
    sx = max(0, (gw - FACE_WIDTH) // 2)
    sy = max(0, (gh - FACE_HEIGHT) // 2)
    crop = gray[sy:sy + FACE_HEIGHT, sx:sx + FACE_WIDTH]
    ch, cw = crop.shape
    dx = (FACE_WIDTH - cw) // 2
    dy = (FACE_HEIGHT - ch) // 2
    out[dy:dy + ch, dx:dx + cw] = crop
    return out


# -- thumbnails --------------------------------------------------------------

_THUMB_CACHE: "OrderedDict[tuple, QPixmap]" = OrderedDict()
_THUMB_CACHE_LIMIT = 768


def clear_thumbnail_cache() -> None:
    _THUMB_CACHE.clear()


def oled_pixmap(frame, width: int, height: int, dpr: float = 1.0) -> QPixmap:
    """OLED-styled pixmap of one packed frame, cached by (bytes, size).

    Library grids, frame strips and hover playback all hit this, so a face
    is rasterised once and every later paint is a dictionary lookup."""
    data = bytes(frame)
    key = (data, width, height, round(dpr * 100))
    pm = _THUMB_CACHE.get(key)
    if pm is not None:
        _THUMB_CACHE.move_to_end(key)
        return pm
    if len(data) != BYTES_PER_FRAME:
        data = data[:BYTES_PER_FRAME].ljust(BYTES_PER_FRAME, b"\x00")
    mono = QImage(data, FACE_WIDTH, FACE_HEIGHT, FACE_WIDTH // 8, QImage.Format_Mono)
    mono.setColorTable([_rgb(OLED_OFF), _rgb(OLED_ON)])
    pw, ph = max(1, round(width * dpr)), max(1, round(height * dpr))
    # Integer up-scales stay crisp with nearest-neighbour; down-scales need
    # smoothing or thin strokes vanish.
    crisp = pw >= FACE_WIDTH and pw % FACE_WIDTH == 0 and ph % FACE_HEIGHT == 0
    scaled = mono.convertToFormat(QImage.Format_RGB32).scaled(
        pw, ph, Qt.IgnoreAspectRatio,
        Qt.FastTransformation if crisp else Qt.SmoothTransformation,
    )
    pm = QPixmap.fromImage(scaled)
    pm.setDevicePixelRatio(dpr)
    _THUMB_CACHE[key] = pm
    if len(_THUMB_CACHE) > _THUMB_CACHE_LIMIT:
        _THUMB_CACHE.popitem(last=False)
    return pm


def array_pixmap(arr: np.ndarray, width: int, height: int, dpr: float = 1.0) -> QPixmap:
    return oled_pixmap(np.packbits(np.asarray(arr, dtype=bool), axis=None).tobytes(),
                       width, height, dpr)


# -- text --------------------------------------------------------------------

_BANNED_FONT_MARKERS = ("comic sans",)
_FALLBACK_FONTS = ("Cascadia Mono", "Consolas", "Segoe UI", "Arial")


def is_banned_font(family: str) -> bool:
    """Comic Sans is never offered or used (organisation rule)."""
    return any(marker in family.lower() for marker in _BANNED_FONT_MARKERS)


def text_font_families() -> list[str]:
    """Installed font families suitable for the text tool, sorted, without
    vertical-writing aliases ('@...') or banned fonts."""
    seen: set[str] = set()
    out: list[str] = []
    for fam in QFontDatabase.families():
        if not fam or fam.startswith("@") or is_banned_font(fam) or fam in seen:
            continue
        seen.add(fam)
        out.append(fam)
    return sorted(out, key=str.lower)


def default_text_font(families: list[str] | None = None) -> str:
    fams = families if families is not None else text_font_families()
    for name in _FALLBACK_FONTS:
        if name in fams:
            return name
    return fams[0] if fams else ""


def safe_font_family(family: str) -> str:
    return default_text_font() if (not family or is_banned_font(family)) else family


def render_text_mask(text: str, family: str, pixel_size: int,
                     bold: bool = False) -> tuple[np.ndarray, int]:
    """Rasterise text without antialiasing -> (bool mask (h, w), baseline
    row). The mask is tight on the left edge so the caret sits where the
    first glyph starts."""
    if not text:
        return np.zeros((0, 0), dtype=bool), 0
    font = QFont(safe_font_family(family))
    font.setPixelSize(max(4, int(pixel_size)))
    font.setBold(bold)
    font.setStyleStrategy(QFont.NoAntialias)
    font.setHintingPreference(QFont.PreferFullHinting)
    fm = QFontMetrics(font)
    pad = 2
    w = max(1, fm.horizontalAdvance(text) + fm.maxWidth() // 2 + pad * 2)
    h = max(1, fm.height() + pad * 2)
    w4 = (w + 3) // 4 * 4
    img = QImage(w4, h, QImage.Format_Grayscale8)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, False)
    p.setRenderHint(QPainter.TextAntialiasing, False)
    p.setFont(font)
    p.setPen(QColor("white"))
    baseline = pad + fm.ascent()
    p.drawText(pad, baseline, text)
    p.end()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.sizeInBytes())
    mask = buf.reshape(h, img.bytesPerLine())[:, :w4] > 127
    cols = np.nonzero(mask.any(axis=0))[0]
    if len(cols) == 0:
        return np.zeros((0, 0), dtype=bool), 0
    return mask[:, cols[0]:cols[-1] + 1].copy(), baseline


def text_stamp_mask(text_mask: np.ndarray, baseline: int, x: int, y: int) -> np.ndarray:
    """Place a rendered text mask on the canvas with its left baseline at
    (x, y) -> full (64,128) mask."""
    out = np.zeros((FACE_HEIGHT, FACE_WIDTH), dtype=bool)
    th, tw = text_mask.shape
    if th == 0 or tw == 0:
        return out
    top = y - baseline
    x0, y0 = max(0, x), max(0, top)
    x1, y1 = min(FACE_WIDTH, x + tw), min(FACE_HEIGHT, top + th)
    if x0 < x1 and y0 < y1:
        out[y0:y1, x0:x1] = text_mask[y0 - top:y1 - top, x0 - x:x1 - x]
    return out


def panel_rect(outer: QRect, zoom: int) -> QRect:
    """Rect of a zoom-x panel centred in ``outer``."""
    w, h = FACE_WIDTH * zoom, FACE_HEIGHT * zoom
    return QRect(outer.x() + (outer.width() - w) // 2,
                 outer.y() + (outer.height() - h) // 2, w, h)


__all__ = [
    "OLED_ON", "OLED_OFF", "OLED_ONION", "OLED_GHOST", "OLED_GLASS",
    "blank_image", "frame_bytes_to_image", "array_to_image", "indexed_image",
    "oled_image", "image_to_array", "image_to_gray", "fit_image_gray",
    "oled_pixmap", "array_pixmap", "clear_thumbnail_cache",
    "text_font_families", "default_text_font", "safe_font_family",
    "is_banned_font", "render_text_mask", "text_stamp_mask", "frame_to_array",
]
