"""Read-only built-in sequences transcribed from the firmware's hand-written
poses in firmware/movement-sequences.h (runWavePose(), runDancePose(), ...),
so users can preview/play the robot's existing repertoire in Studio without
recreating it. Each pose function there mutates a shared 8-servo state with
setServoAngle() calls between delayWithFace() calls; _seq() replays that
same accumulation to build full per-frame angle snapshots.

These are templates, not project data -- never mutated in place. Studio
offers "Duplicate to Custom" to copy one into the editable project sequences.
"""

from __future__ import annotations

from .project import Frame, Sequence
from .servo_model import SERVO_ORDER

REST_STATE = {s: 90 for s in SERVO_ORDER}
STAND_STATE = {"R1": 135, "R2": 45, "L1": 45, "L2": 135, "R4": 0, "R3": 180, "L3": 0, "L4": 180}


def _seq(name: str, start: dict, steps: list[tuple[dict | None, int, str | None, str]]) -> Sequence:
    """steps: list of (angle_updates_or_None, delay_ms, face_or_None, face_mode)."""
    state = dict(start)
    frames = []
    for updates, delay, face, mode in steps:
        if updates:
            state.update(updates)
        frames.append(Frame(delay_ms=delay, angles=dict(state), face=face, face_mode=mode))
    return Sequence(name, frames)


REST = _seq("rest", REST_STATE, [
    (None, 300, "rest", "boomerang"),
])

STAND = _seq("stand", STAND_STATE, [
    (None, 300, "stand", "once"),
])

WAVE = _seq("wave", STAND_STATE, [
    (None, 200, "wave", "once"),
    ({"R4": 80, "L3": 180, "L2": 90, "R1": 100}, 200, None, "once"),
    ({"L3": 180}, 300, None, "once"),
    ({"L3": 180}, 300, None, "once"),
    ({"L3": 100}, 300, None, "once"),
    ({"L3": 180}, 300, None, "once"),
    ({"L3": 100}, 300, None, "once"),
    ({"L3": 180}, 300, None, "once"),
    ({"L3": 100}, 300, None, "once"),
    ({"L3": 180}, 300, None, "once"),
    ({"L3": 100}, 300, None, "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

DANCE = _seq("dance", REST_STATE, [
    ({"R1": 90, "R2": 90, "L1": 90, "L2": 90, "R4": 160, "R3": 160, "L3": 10, "L4": 10}, 300, "dance", "loop"),
    ({"R4": 115, "R3": 115, "L3": 10, "L4": 10}, 300, None, "loop"),
    ({"R4": 160, "R3": 160, "L3": 65, "L4": 65}, 300, None, "loop"),
    ({"R4": 115, "R3": 115, "L3": 10, "L4": 10}, 300, None, "loop"),
    ({"R4": 160, "R3": 160, "L3": 65, "L4": 65}, 300, None, "loop"),
    ({"R4": 115, "R3": 115, "L3": 10, "L4": 10}, 300, None, "loop"),
    ({"R4": 160, "R3": 160, "L3": 65, "L4": 65}, 300, None, "loop"),
    ({"R4": 115, "R3": 115, "L3": 10, "L4": 10}, 300, None, "loop"),
    ({"R4": 160, "R3": 160, "L3": 65, "L4": 65}, 300, None, "loop"),
    ({"R4": 115, "R3": 115, "L3": 10, "L4": 10}, 300, None, "loop"),
    ({"R4": 160, "R3": 160, "L3": 65, "L4": 65}, 300, None, "loop"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

SWIM = _seq("swim", REST_STATE, [
    (None, 200, "swim", "once"),
    ({"R1": 135, "R2": 45, "L1": 45, "L2": 135}, 400, None, "once"),
    ({"R1": 90, "R2": 90, "L1": 90, "L2": 90}, 400, None, "once"),
    ({"R1": 135, "R2": 45, "L1": 45, "L2": 135}, 400, None, "once"),
    ({"R1": 90, "R2": 90, "L1": 90, "L2": 90}, 400, None, "once"),
    ({"R1": 135, "R2": 45, "L1": 45, "L2": 135}, 400, None, "once"),
    ({"R1": 90, "R2": 90, "L1": 90, "L2": 90}, 400, None, "once"),
    ({"R1": 135, "R2": 45, "L1": 45, "L2": 135}, 400, None, "once"),
    ({"R1": 90, "R2": 90, "L1": 90, "L2": 90}, 400, None, "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

POINT = _seq("point", REST_STATE, [
    ({"L2": 90, "R1": 135, "R2": 100, "L4": 180, "L1": 25, "L3": 145, "R4": 80, "R3": 170}, 2000, "point", "boomerang"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

PUSHUP = _seq("pushup", STAND_STATE, [
    (None, 200, "pushup", "once"),
    ({"L1": 0, "R1": 180, "L3": 90, "R3": 90}, 500, None, "once"),
    ({"L3": 0, "R3": 180}, 600, None, "once"),
    ({"L3": 90, "R3": 90}, 500, None, "once"),
    ({"L3": 0, "R3": 180}, 600, None, "once"),
    ({"L3": 90, "R3": 90}, 500, None, "once"),
    ({"L3": 0, "R3": 180}, 600, None, "once"),
    ({"L3": 90, "R3": 90}, 500, None, "once"),
    ({"L3": 0, "R3": 180}, 600, None, "once"),
    ({"L3": 90, "R3": 90}, 500, None, "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

BOW = _seq("bow", STAND_STATE, [
    (None, 200, "bow", "once"),
    ({"L1": 0, "R1": 180, "L3": 0, "R3": 180, "L2": 180, "R2": 0, "R4": 0, "L4": 180}, 600, None, "once"),
    ({"L3": 90, "R3": 90}, 3000, None, "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

CUTE = _seq("cute", STAND_STATE, [
    (None, 200, "cute", "once"),
    ({"L2": 160, "R2": 20, "R4": 180, "L4": 0, "L1": 0, "R1": 180, "L3": 180, "R3": 0}, 200, None, "once"),
    ({"R4": 180, "L4": 45}, 300, None, "once"),
    ({"R4": 135, "L4": 0}, 300, None, "once"),
    ({"R4": 180, "L4": 45}, 300, None, "once"),
    ({"R4": 135, "L4": 0}, 300, None, "once"),
    ({"R4": 180, "L4": 45}, 300, None, "once"),
    ({"R4": 135, "L4": 0}, 300, None, "once"),
    ({"R4": 180, "L4": 45}, 300, None, "once"),
    ({"R4": 135, "L4": 0}, 300, None, "once"),
    ({"R4": 180, "L4": 45}, 300, None, "once"),
    ({"R4": 135, "L4": 0}, 300, None, "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

FREAKY = _seq("freaky", STAND_STATE, [
    (None, 200, "freaky", "once"),
    ({"L1": 0, "R1": 180, "L2": 180, "R2": 0, "R4": 90, "R3": 0}, 200, None, "once"),
    ({"R3": 25}, 400, None, "once"),
    ({"R3": 0}, 400, None, "once"),
    ({"R3": 25}, 400, None, "once"),
    ({"R3": 0}, 400, None, "once"),
    ({"R3": 25}, 400, None, "once"),
    ({"R3": 0}, 400, None, "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

WORM = _seq("worm", STAND_STATE, [
    (None, 200, "worm", "once"),
    ({"R1": 180, "R2": 0, "L1": 0, "L2": 180, "R4": 90, "R3": 90, "L3": 90, "L4": 90}, 200, None, "once"),
    ({"R3": 45, "L3": 135, "R4": 45, "L4": 135}, 300, None, "once"),
    ({"R3": 135, "L3": 45, "R4": 135, "L4": 45}, 300, None, "once"),
    ({"R3": 45, "L3": 135, "R4": 45, "L4": 135}, 300, None, "once"),
    ({"R3": 135, "L3": 45, "R4": 135, "L4": 45}, 300, None, "once"),
    ({"R3": 45, "L3": 135, "R4": 45, "L4": 135}, 300, None, "once"),
    ({"R3": 135, "L3": 45, "R4": 135, "L4": 45}, 300, None, "once"),
    ({"R3": 45, "L3": 135, "R4": 45, "L4": 135}, 300, None, "once"),
    ({"R3": 135, "L3": 45, "R4": 135, "L4": 45}, 300, None, "once"),
    ({"R3": 45, "L3": 135, "R4": 45, "L4": 135}, 300, None, "once"),
    ({"R3": 135, "L3": 45, "R4": 135, "L4": 45}, 300, None, "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

SHAKE = _seq("shake", STAND_STATE, [
    (None, 200, "shake", "once"),
    ({"R1": 135, "L1": 45, "L3": 90, "R3": 90, "L2": 90, "R2": 90}, 200, None, "once"),
    ({"R4": 45, "L4": 135}, 300, None, "once"),
    ({"R4": 0, "L4": 180}, 300, None, "once"),
    ({"R4": 45, "L4": 135}, 300, None, "once"),
    ({"R4": 0, "L4": 180}, 300, None, "once"),
    ({"R4": 45, "L4": 135}, 300, None, "once"),
    ({"R4": 0, "L4": 180}, 300, None, "once"),
    ({"R4": 45, "L4": 135}, 300, None, "once"),
    ({"R4": 0, "L4": 180}, 300, None, "once"),
    ({"R4": 45, "L4": 135}, 300, None, "once"),
    ({"R4": 0, "L4": 180}, 300, None, "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

SHRUG = _seq("shrug", STAND_STATE, [
    (None, 200, "dead", "once"),
    ({"R3": 90, "R4": 90, "L3": 90, "L4": 90}, 1000, None, "once"),
    ({"R3": 0, "R4": 180, "L3": 180, "L4": 0}, 1500, "shrug", "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

DEAD = _seq("dead", STAND_STATE, [
    (None, 200, "dead", "boomerang"),
    ({"R3": 90, "R4": 90, "L3": 90, "L4": 90}, 800, None, "boomerang"),
])

CRAB = _seq("crab", STAND_STATE, [
    (None, 200, "crab", "once"),
    ({"R1": 90, "R2": 90, "L1": 90, "L2": 90, "R4": 45, "R3": 135, "L3": 0, "L4": 180}, 300, None, "once"),
    ({"R4": 0, "R3": 180, "L3": 45, "L4": 135}, 300, None, "once"),
    ({"R4": 45, "R3": 135, "L3": 0, "L4": 180}, 300, None, "once"),
    ({"R4": 0, "R3": 180, "L3": 45, "L4": 135}, 300, None, "once"),
    ({"R4": 45, "R3": 135, "L3": 0, "L4": 180}, 300, None, "once"),
    ({"R4": 0, "R3": 180, "L3": 45, "L4": 135}, 300, None, "once"),
    ({"R4": 45, "R3": 135, "L3": 0, "L4": 180}, 300, None, "once"),
    ({"R4": 0, "R3": 180, "L3": 45, "L4": 135}, 300, None, "once"),
    ({"R4": 45, "R3": 135, "L3": 0, "L4": 180}, 300, None, "once"),
    ({"R4": 0, "R3": 180, "L3": 45, "L4": 135}, 300, None, "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

# --- Movement animations: one representative cycle (firmware loops these
# for as long as the direction button is held; here they loop in the
# Timeline preview instead). ---

WALK_FORWARD = _seq("walk_forward", STAND_STATE, [
    ({"R3": 135, "L3": 45, "R2": 100, "L1": 25}, 100, "walk", "once"),
    ({"R3": 135, "L3": 0}, 100, None, "once"),
    ({"L4": 135, "L2": 90, "R4": 0, "R1": 180}, 100, None, "once"),
    ({"R2": 45, "L1": 90}, 100, None, "once"),
    ({"R4": 45, "L4": 180}, 100, None, "once"),
    ({"R3": 180, "L3": 45, "R2": 90, "L1": 0}, 100, None, "once"),
    ({"L2": 135, "R1": 90}, 100, None, "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

WALK_BACKWARD = _seq("walk_backward", STAND_STATE, [
    (None, 100, "walk", "once"),
    ({"R3": 135, "L3": 0}, 100, None, "once"),
    ({"L4": 135, "L2": 135, "R4": 0, "R1": 90}, 100, None, "once"),
    ({"R2": 90, "L1": 0}, 100, None, "once"),
    ({"R4": 45, "L4": 180}, 100, None, "once"),
    ({"R3": 180, "L3": 45, "R2": 45, "L1": 90}, 100, None, "once"),
    ({"L2": 90, "R1": 180}, 100, None, "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

TURN_LEFT = _seq("turn_left", STAND_STATE, [
    (None, 100, "walk", "once"),
    ({"R3": 135, "L4": 135}, 100, None, "once"),
    ({"R1": 180, "L2": 180}, 100, None, "once"),
    ({"R3": 180, "L4": 180}, 100, None, "once"),
    ({"R1": 135, "L2": 135}, 100, None, "once"),
    ({"R4": 45, "L3": 45}, 100, None, "once"),
    ({"R2": 90, "L1": 90}, 100, None, "once"),
    ({"R4": 0, "L3": 0}, 100, None, "once"),
    ({"R2": 45, "L1": 45}, 100, None, "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

TURN_RIGHT = _seq("turn_right", STAND_STATE, [
    (None, 100, "walk", "once"),
    ({"R4": 45, "L3": 45}, 100, None, "once"),
    ({"R2": 0, "L1": 0}, 100, None, "once"),
    ({"R4": 0, "L3": 0}, 100, None, "once"),
    ({"R2": 45, "L1": 45}, 100, None, "once"),
    ({"R3": 135, "L4": 135}, 100, None, "once"),
    ({"R1": 90, "L2": 90}, 100, None, "once"),
    ({"R3": 180, "L4": 180}, 100, None, "once"),
    ({"R1": 135, "L2": 135}, 100, None, "once"),
    (dict(STAND_STATE), 400, "stand", "once"),
])

BUILTIN_ORDER = [
    "rest", "stand", "wave", "dance", "swim", "point", "pushup", "bow",
    "cute", "freaky", "worm", "shake", "shrug", "dead", "crab",
    "walk_forward", "walk_backward", "turn_left", "turn_right",
]

BUILTIN_SEQUENCES: dict[str, Sequence] = {
    seq.name: seq for seq in [
        REST, STAND, WAVE, DANCE, SWIM, POINT, PUSHUP, BOW, CUTE, FREAKY,
        WORM, SHAKE, SHRUG, DEAD, CRAB, WALK_FORWARD, WALK_BACKWARD,
        TURN_LEFT, TURN_RIGHT,
    ]
}
