"""Project data model and .sesproj JSON persistence.

Bitmap storage format: each face frame is 1024 bytes (128x64 / 8), packed
horizontal-scan MSB-first -- the same byte order image2cpp produces and that
firmware/face-bitmaps.h's epd_bitmap_* arrays already use. Frames are stored
as hex strings in the JSON file.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field, asdict

from .servo_model import (
    SERVO_ORDER, DEFAULT_ANGLE, DEFAULT_BOARD_VARIANT,
    FACE_CATEGORY_CUSTOM, FACE_WIDTH, FACE_HEIGHT, FACE_MAX_FRAMES,
)

PROJECT_VERSION = 1
PROJECT_EXTENSION = ".sesproj"

BYTES_PER_FRAME = (FACE_WIDTH * FACE_HEIGHT) // 8


# ---------------------------------------------------------------------------
# Bitmap helpers
# ---------------------------------------------------------------------------

def blank_frame() -> bytearray:
    return bytearray(BYTES_PER_FRAME)


def pixels_to_frame(pixels: list[list[bool]]) -> bytearray:
    """128x64 bool grid (row-major, pixels[y][x]) -> packed 1024-byte frame."""
    buf = bytearray(BYTES_PER_FRAME)
    for y in range(FACE_HEIGHT):
        row = pixels[y]
        for bx in range(FACE_WIDTH // 8):
            byte = 0
            for bit in range(8):
                x = bx * 8 + bit
                if row[x]:
                    byte |= 0x80 >> bit
            buf[y * (FACE_WIDTH // 8) + bx] = byte
    return buf


def frame_to_pixels(frame: bytes) -> list[list[bool]]:
    pixels = [[False] * FACE_WIDTH for _ in range(FACE_HEIGHT)]
    stride = FACE_WIDTH // 8
    for y in range(FACE_HEIGHT):
        for bx in range(stride):
            byte = frame[y * stride + bx]
            for bit in range(8):
                if byte & (0x80 >> bit):
                    pixels[y][bx * 8 + bit] = True
    return pixels


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Face:
    name: str
    category: str = FACE_CATEGORY_CUSTOM
    fps: int = 4
    frames: list[bytearray] = field(default_factory=lambda: [blank_frame()])
    is_override: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "fps": self.fps,
            "frames": [f.hex() for f in self.frames],
            "is_override": self.is_override,
        }

    @staticmethod
    def from_dict(d: dict) -> "Face":
        frames = [bytearray.fromhex(h) for h in d.get("frames", [])] or [blank_frame()]
        return Face(
            name=d["name"],
            category=d.get("category", FACE_CATEGORY_CUSTOM),
            fps=int(d.get("fps", 4)),
            frames=frames,
            is_override=bool(d.get("is_override", False)),
        )


@dataclass
class Frame:
    delay_ms: int = 200
    angles: dict[str, int] = field(default_factory=lambda: {s: DEFAULT_ANGLE for s in SERVO_ORDER})
    face: str | None = None
    face_mode: str = "loop"

    def to_dict(self) -> dict:
        return {
            "delay_ms": self.delay_ms,
            "angles": dict(self.angles),
            "face": self.face,
            "face_mode": self.face_mode,
        }

    @staticmethod
    def from_dict(d: dict) -> "Frame":
        angles = {s: DEFAULT_ANGLE for s in SERVO_ORDER}
        angles.update(d.get("angles", {}))
        return Frame(
            delay_ms=int(d.get("delay_ms", 200)),
            angles=angles,
            face=d.get("face"),
            face_mode=d.get("face_mode", "loop"),
        )

    def clone(self) -> "Frame":
        return Frame(self.delay_ms, dict(self.angles), self.face, self.face_mode)


@dataclass
class Sequence:
    name: str
    frames: list[Frame] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "frames": [f.to_dict() for f in self.frames]}

    @staticmethod
    def from_dict(d: dict) -> "Sequence":
        return Sequence(d["name"], [Frame.from_dict(f) for f in d.get("frames", [])])


@dataclass
class ProjectSettings:
    frame_delay: int = 100
    walk_cycles: int = 10
    motor_current_delay: int = 20
    face_fps: int = 8
    board_variant: str = DEFAULT_BOARD_VARIANT

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ProjectSettings":
        base = ProjectSettings()
        for k, v in d.items():
            if hasattr(base, k):
                setattr(base, k, v)
        return base


class Project:
    def __init__(self, name: str = "Untitled Sesame Project"):
        self.name = name
        self.created = time.time()
        self.modified = time.time()
        self.path: str | None = None
        self.settings = ProjectSettings()
        self.sequences: dict[str, Sequence] = {"my_sequence": Sequence("my_sequence", [Frame()])}
        self.faces: dict[str, Face] = {}
        self.dirty = False

    # -- mutation helpers -------------------------------------------------
    def touch(self) -> None:
        self.modified = time.time()
        self.dirty = True

    def add_sequence(self, name: str) -> Sequence:
        seq = Sequence(name, [Frame()])
        self.sequences[name] = seq
        self.touch()
        return seq

    def add_face(self, face: Face) -> None:
        self.faces[face.name] = face
        self.touch()

    # -- serialization ------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": PROJECT_VERSION,
            "meta": {
                "name": self.name,
                "created": self.created,
                "modified": self.modified,
            },
            "settings": self.settings.to_dict(),
            "sequences": {k: v.to_dict() for k, v in self.sequences.items()},
            "faces": {k: v.to_dict() for k, v in self.faces.items()},
        }

    @staticmethod
    def from_dict(d: dict) -> "Project":
        p = Project(d.get("meta", {}).get("name", "Untitled Sesame Project"))
        p.created = d.get("meta", {}).get("created", time.time())
        p.modified = d.get("meta", {}).get("modified", time.time())
        p.settings = ProjectSettings.from_dict(d.get("settings", {}))
        p.sequences = {k: Sequence.from_dict(v) for k, v in d.get("sequences", {}).items()}
        p.faces = {k: Face.from_dict(v) for k, v in d.get("faces", {}).items()}
        if not p.sequences:
            p.sequences = {"my_sequence": Sequence("my_sequence", [Frame()])}
        p.dirty = False
        return p

    def save(self, path: str) -> None:
        self.touch()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        self.path = path
        self.dirty = False

    @staticmethod
    def load(path: str) -> "Project":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        proj = Project.from_dict(data)
        proj.path = path
        return proj

    def clone(self) -> "Project":
        return copy.deepcopy(self)
