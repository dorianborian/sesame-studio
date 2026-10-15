"""Read-only built-in faces parsed live from firmware/face-bitmaps.h (bitmap
data) and firmware/sesame-firmware-main.ino (faceFpsEntries, for playback
speed), so Studio's Face Library always reflects whatever faces actually
ship with the firmware. Parsed once at import time; if the firmware files
can't be found or don't parse cleanly, BUILTIN_FACES is simply empty rather
than the app failing to start.
"""

from __future__ import annotations

import re
from pathlib import Path

from .project import Face
from .paths import FIRMWARE_DIR, ASSETS_DIR, BASE_DIR
from .servo_model import RESERVED_FACE_NAMES, FACE_CATEGORY_CUSTOM


def _find_firmware_file(filename: str) -> Path:
    candidates = [
        FIRMWARE_DIR / filename,
        ASSETS_DIR / filename,
        BASE_DIR.parent / "sesame-robot" / "firmware" / filename,
        BASE_DIR.parent.parent / "firmware" / filename,
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


FACE_BITMAPS_PATH = _find_firmware_file("face-bitmaps.h")
MAIN_INO_PATH = _find_firmware_file("sesame-firmware-main.ino")

# face-bitmaps.h/sesame-firmware-main.ino both special-case the typo'd
# "defualt" bitmap symbol as the correctly-spelled "default" face name.
_RENAME = {"defualt": "default"}

_BITMAP_RE = re.compile(
    r"const\s+unsigned\s+char\s+epd_bitmap_(\w+)\s*\[\]\s*PROGMEM\s*=\s*\{(.*?)\};",
    re.DOTALL,
)
_HEX_RE = re.compile(r"0x([0-9A-Fa-f]{2})")
_FPS_ENTRY_RE = re.compile(r'\{\s*"([^"]+)"\s*,\s*(\d+)\s*\}')
_FRAME_SUFFIX_RE = re.compile(r"^(.+)_([1-5])$")


def _parse_bitmaps(text: str) -> dict[str, bytes]:
    bitmaps: dict[str, bytes] = {}
    for m in _BITMAP_RE.finditer(text):
        hex_bytes = _HEX_RE.findall(m.group(2))
        if len(hex_bytes) != 1024:
            continue  # not a 128x64 face bitmap (or malformed) -- skip
        bitmaps[m.group(1)] = bytes(int(h, 16) for h in hex_bytes)
    return bitmaps


def _parse_fps(text: str) -> dict[str, int]:
    match = re.search(r"faceFpsEntries\[\]\s*=\s*\{(.*?)\};", text, re.DOTALL)
    if not match:
        return {}
    return {name: int(fps) for name, fps in _FPS_ENTRY_RE.findall(match.group(1))}


def _load_builtin_faces() -> dict[str, Face]:
    try:
        raw_bitmaps = _parse_bitmaps(FACE_BITMAPS_PATH.read_text(encoding="utf-8"))
    except OSError:
        return {}
    if not raw_bitmaps:
        return {}

    try:
        fps_by_name = _parse_fps(MAIN_INO_PATH.read_text(encoding="utf-8"))
    except OSError:
        fps_by_name = {}

    # Group into {base_name: {frame_index: bytes}} -- root (no suffix) is
    # frame 0, _1.._5 are subsequent frames, matching firmware's
    # MAKE_FACE_FRAMES macro.
    grouped: dict[str, dict[int, bytes]] = {}
    for raw_name, data in raw_bitmaps.items():
        base, idx = raw_name, 0
        m = _FRAME_SUFFIX_RE.match(raw_name)
        if m and m.group(1) in raw_bitmaps:
            base, idx = m.group(1), int(m.group(2))
        base = _RENAME.get(base, base)
        grouped.setdefault(base, {})[idx] = data

    faces: dict[str, Face] = {}
    for name, frames_by_idx in grouped.items():
        # Frames must be contiguous starting at 0 -- a gap (e.g. _2 present
        # without _1) means later frames are unreachable in the firmware
        # too (countFrames() stops at the first missing frame), so stop
        # there rather than presenting a bitmap that can't actually play.
        ordered: list[bytes] = []
        i = 0
        while i in frames_by_idx:
            ordered.append(frames_by_idx[i])
            i += 1
        if not ordered:
            continue
        faces[name] = Face(
            name=name,
            category=RESERVED_FACE_NAMES.get(name, FACE_CATEGORY_CUSTOM),
            fps=fps_by_name.get(name, 4),
            frames=[bytearray(f) for f in ordered],
            is_override=False,
        )
    return faces


BUILTIN_FACES: dict[str, Face] = _load_builtin_faces()
BUILTIN_FACE_ORDER: list[str] = sorted(BUILTIN_FACES.keys())


def reload_builtin_faces() -> None:
    """Reload BUILTIN_FACES and BUILTIN_FACE_ORDER in-place from disk."""
    loaded = _load_builtin_faces()
    BUILTIN_FACES.clear()
    BUILTIN_FACES.update(loaded)
    BUILTIN_FACE_ORDER.clear()
    BUILTIN_FACE_ORDER.extend(sorted(BUILTIN_FACES.keys()))
