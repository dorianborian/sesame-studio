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

# --- Movement animations ---------------------------------------------------
# The remastered "V5" gaits by @Saireous (sesame-robot commit bb87021,
# firmware/movement-sequences.h: runWalkPose / runWalkBackward / runTurnLeft /
# runTurnRight). On the robot each one runs for as long as its direction
# button is held:
#
#   set the "walk" face (looping), wait one frameDelay in the stand pose
#   lift diagonal pair A (R3 + L4), wait
#   repeat while held:
#       swing the lifted pair / push with the grounded pair      (4 hips)
#       soft support exchange: A lands while B (L3 + R4) lifts   (4 knees)
#       swing / push the other way                               (4 hips)
#       B lands while A lifts -- straight into the next cycle,
#       without returning to the rigid stand pose                (4 knees)
#   stand
#
# Only the two hip steps differ between forward, backward and the turns. A
# held button has no fixed length, so each sequence here carries
# GAIT_PREVIEW_CYCLES passes of the loop between the lead-in and the closing
# stand frame -- enough to see the continuous cycle-to-cycle hand-over.
# Every step waits the firmware's default frameDelay (100 ms).

GAIT_PREVIEW_CYCLES = 3
GAIT_LEAD_IN_FRAMES = 2      # stand + lift pair A
GAIT_FRAMES_PER_CYCLE = 4
GAIT_FRAME_DELAY_MS = 100

_GAIT_LIFT_A = {"R3": 143, "L4": 148}
_GAIT_A_LANDS_B_LIFTS = {"R3": 171, "L4": 176, "L3": 38, "R4": 42}
_GAIT_B_LANDS_A_LIFTS = {"L3": 10, "R4": 14, "R3": 143, "L4": 148}


def _gait(name: str, hips_first: dict, hips_second: dict,
          cycles: int = GAIT_PREVIEW_CYCLES) -> Sequence:
    d = GAIT_FRAME_DELAY_MS
    steps: list[tuple[dict | None, int, str | None, str]] = [
        (None, d, "walk", "loop"),
        (dict(_GAIT_LIFT_A), d, None, "loop"),
    ]
    for _ in range(cycles):
        steps += [
            (dict(hips_first), d, None, "loop"),
            (dict(_GAIT_A_LANDS_B_LIFTS), d, None, "loop"),
            (dict(hips_second), d, None, "loop"),
            (dict(_GAIT_B_LANDS_A_LIFTS), d, None, "loop"),
        ]
    steps.append((dict(STAND_STATE), 400, "stand", "once"))
    return _seq(name, STAND_STATE, steps)


# Pair A swings forward while grounded pair B pushes the body forward (front
# legs take a slightly longer stride than the rear ones), then the reverse.
_WALK_A_SWINGS = {"R1": 170, "L2": 106, "L1": 78, "R2": 18}
_WALK_B_SWINGS = {"L1": 10, "R2": 76, "R1": 102, "L2": 164}

WALK_FORWARD = _gait("walk_forward", _WALK_A_SWINGS, _WALK_B_SWINGS)
# Backward is the same two hip steps in the opposite order.
WALK_BACKWARD = _gait("walk_backward", {"R1": 102, "L2": 164, "L1": 10, "R2": 76},
                      {"L1": 78, "R2": 18, "R1": 170, "L2": 106})

# Turning in place: the grounded pair twists the body (right side pushes
# forward and left side backward for a left turn) while the lifted pair resets.
TURN_LEFT = _gait("turn_left", {"R1": 166, "L2": 160, "L1": 12, "R2": 18},
                  {"L1": 78, "R2": 76, "R1": 102, "L2": 106})
TURN_RIGHT = _gait("turn_right", {"R1": 102, "L2": 106, "L1": 78, "R2": 76},
                   {"L1": 10, "R2": 18, "R1": 170, "L2": 164})

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
