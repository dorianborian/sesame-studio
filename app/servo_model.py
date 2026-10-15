"""Servo/joint definitions shared by the viewport, inspector, timeline and
exporter. Mirrors firmware/movement-sequences.h (ServoName enum) and
firmware/sesame-firmware-main.ino (servoPins tables, and setServoAngle's
uniform 0-180 constrain()) so generated code and on-screen limits always
match the real hardware.
"""

from __future__ import annotations

from dataclasses import dataclass

# Order matches `enum ServoName` in firmware/movement-sequences.h.
SERVO_ORDER = ["R1", "R2", "L1", "L2", "R4", "R3", "L3", "L4"]

# All 8 servos are the same hardware and share the same full 0-180 sweep
# (firmware's setServoAngle() constrains uniformly, no per-servo limit).
# Individual poses just don't happen to use the full range on every joint.
SERVO_RANGES: dict[str, tuple[int, int]] = {s: (0, 180) for s in SERVO_ORDER}

DEFAULT_ANGLE = 90

# Distinct color per servo, reused across inspector rows, viewport labels and
# generated-code syntax highlighting (matches the original app's palette).
SERVO_COLORS: dict[str, str] = {
    "R1": "#FF69B4",
    "R2": "#9370DB",
    "L1": "#FF2600",
    "L2": "#00CED1",
    "R4": "#FFA500",
    "R3": "#32CD32",
    "L3": "#FFF23B",
    "L4": "#FFFFFF",
}


@dataclass(frozen=True)
class BoardVariant:
    key: str
    label: str
    pins: dict[str, int]
    i2c_sda: int
    i2c_scl: int


BOARD_VARIANTS: dict[str, BoardVariant] = {
    "s2_mini": BoardVariant(
        "s2_mini", "Lolin S2 Mini",
        {"R1": 1, "R2": 2, "L1": 4, "L2": 6, "R4": 8, "R3": 10, "L3": 13, "L4": 14},
        33, 35,
    ),
    "distro_v1": BoardVariant(
        "distro_v1", "Sesame Distro Board V1",
        {"R1": 15, "R2": 2, "L1": 23, "L2": 19, "R4": 4, "R3": 16, "L3": 17, "L4": 18},
        21, 22,
    ),
    "distro_v2": BoardVariant(
        "distro_v2", "Sesame Distro Board V2",
        {"R1": 4, "R2": 5, "L1": 6, "L2": 7, "R4": 15, "R3": 16, "L3": 17, "L4": 18},
        8, 9,
    ),
    "distro_v3": BoardVariant(
        "distro_v3", "Sesame Distro Board V3",
        {"R1": 4, "R2": 5, "L1": 6, "L2": 7, "R4": 10, "R3": 11, "L3": 12, "L4": 13},
        8, 9,
    ),
}

DEFAULT_BOARD_VARIANT = "s2_mini"


def clamp_angle(servo: str, angle: int) -> int:
    lo, hi = SERVO_RANGES[servo]
    return max(lo, min(hi, angle))


def is_valid_angle(servo: str, angle: int) -> bool:
    lo, hi = SERVO_RANGES[servo]
    return lo <= angle <= hi


# ---------------------------------------------------------------------------
# Face categories / reserved names (firmware/face-bitmaps.h FACE_LIST +
# firmware/sesame-firmware-main.ino faceFpsEntries)
# ---------------------------------------------------------------------------

FACE_CATEGORY_MOVEMENT = "movement"
FACE_CATEGORY_CONVERSATIONAL = "conversational"
FACE_CATEGORY_TALK = "talk"
FACE_CATEGORY_SPECIAL = "special"
FACE_CATEGORY_CUSTOM = "custom"

FACE_CATEGORIES = [
    FACE_CATEGORY_MOVEMENT,
    FACE_CATEGORY_CONVERSATIONAL,
    FACE_CATEGORY_TALK,
    FACE_CATEGORY_SPECIAL,
    FACE_CATEGORY_CUSTOM,
]

FACE_CATEGORY_LABELS = {
    FACE_CATEGORY_MOVEMENT: "Movement",
    FACE_CATEGORY_CONVERSATIONAL: "Conversational",
    FACE_CATEGORY_TALK: "Talk",
    FACE_CATEGORY_SPECIAL: "Special",
    FACE_CATEGORY_CUSTOM: "Custom",
}

RESERVED_FACE_NAMES: dict[str, str] = {
    **{n: FACE_CATEGORY_MOVEMENT for n in [
        "walk", "rest", "swim", "dance", "wave", "point", "stand", "cute",
        "pushup", "freaky", "bow", "worm", "shake", "shrug", "dead", "crab",
    ]},
    **{n: FACE_CATEGORY_SPECIAL for n in ["default", "idle", "idle_blink"]},
    **{n: FACE_CATEGORY_CONVERSATIONAL for n in [
        "happy", "sad", "angry", "surprised", "sleepy", "love", "excited",
        "confused", "thinking",
    ]},
    **{f"talk_{n}": FACE_CATEGORY_TALK for n in [
        "happy", "sad", "angry", "surprised", "sleepy", "love", "excited",
        "confused", "thinking",
    ]},
}

FACE_ANIM_MODES = ["loop", "once", "boomerang"]
FACE_WIDTH = 128
FACE_HEIGHT = 64
FACE_MAX_FRAMES = 6
