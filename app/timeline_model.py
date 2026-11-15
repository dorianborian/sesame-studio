"""Pure-Python timeline math (no widgets): the time<->pixel map, pose
interpolation, ruler tick selection and the frames clipboard format.

Semantics shared by everything here: frame ``i`` occupies the half-open
interval ``(start_i, end_i]`` where ``end_i - start_i == max(1, delay_ms)``.
A frame's delay is the *transition into* its pose, so the pose is reached at
the block's END -- that is where a selected frame's playhead sits and where
the key markers / servo-track vertices are drawn.
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right

from .project import Frame

PPM_MIN = 0.004          # px per ms (1 minute = 240 px)
PPM_MAX = 4.0            # 1 ms = 4 px
PPM_DEFAULT = 0.12
FIT_PPM_MAX = 0.6        # zoom-to-fit never blows a tiny sequence up past this

# Very short frames are drawn as if they lasted MIN_BLOCK_REF_MS, clamped to
# [FLOOR, CEIL] px, so a 10 ms frame is still a grabbable block at normal
# zooms while a zoomed-out overview can still squeeze hundreds of frames in.
MIN_BLOCK_REF_MS = 150.0
MIN_BLOCK_W_FLOOR = 3.0
MIN_BLOCK_W_CEIL = 22.0

CLIPBOARD_TYPE = "sesame-studio/frames"


def lerp_angles(a: dict, b: dict, t: float) -> dict:
    t = max(0.0, min(1.0, t))
    return {k: a[k] + (b.get(k, a[k]) - a[k]) * t for k in a}


def clamp_ppm(ppm: float) -> float:
    return max(PPM_MIN, min(PPM_MAX, ppm))


def min_block_w(ppm: float) -> float:
    return max(MIN_BLOCK_W_FLOOR, min(MIN_BLOCK_W_CEIL, ppm * MIN_BLOCK_REF_MS))


def frame_duration(frame: Frame) -> int:
    return frame.delay_ms if frame.delay_ms > 1 else 1


def content_width(durations, ppm: float) -> float:
    mw = min_block_w(ppm)
    return sum(max(mw, d * ppm) for d in durations)


def fit_ppm(durations, avail_px: float) -> float:
    """Largest zoom at which every block fits in ``avail_px`` (bisection:
    content_width is monotonic in ppm)."""
    durations = list(durations)
    if not durations or avail_px <= 10:
        return PPM_DEFAULT
    lo, hi = PPM_MIN, FIT_PPM_MAX
    if content_width(durations, hi) <= avail_px:
        return hi
    if content_width(durations, lo) >= avail_px:
        return lo
    for _ in range(36):
        mid = (lo * hi) ** 0.5
        if content_width(durations, mid) > avail_px:
            hi = mid
        else:
            lo = mid
    return lo


class TimeMap:
    """Piecewise-linear time<->x map for one sequence at one zoom.

    ``xs`` has n+1 entries (content-space left edge of each block, plus the
    right edge of the last one); ``starts``/``ends`` are cumulative ms.
    Rebuilding is O(n) float math; every lookup is a bisect.
    """

    __slots__ = ("n", "ppm", "starts", "ends", "xs", "total_ms", "total_w")

    def __init__(self):
        self.n = 0
        self.ppm = PPM_DEFAULT
        self.starts: list[float] = []
        self.ends: list[float] = []
        self.xs: list[float] = [0.0]
        self.total_ms = 0.0
        self.total_w = 0.0

    def rebuild(self, frames, ppm: float) -> None:
        mw = min_block_w(ppm)
        starts, ends, xs = [], [], [0.0]
        t = 0.0
        x = 0.0
        for f in frames:
            d = f.delay_ms if f.delay_ms > 1 else 1
            starts.append(t)
            t += d
            ends.append(t)
            w = d * ppm
            x += w if w > mw else mw
            xs.append(x)
        self.n = len(starts)
        self.ppm = ppm
        self.starts, self.ends, self.xs = starts, ends, xs
        self.total_ms = t
        self.total_w = x

    # -- lookups ---------------------------------------------------------
    def index_at_time(self, ms: float) -> int:
        """Frame whose (start, end] contains ms; clamped. -1 when empty."""
        if self.n == 0:
            return -1
        i = bisect_left(self.ends, ms)
        return i if i < self.n else self.n - 1

    def time_to_x(self, ms: float) -> float:
        if self.n == 0 or ms <= 0.0:
            return ms * self.ppm
        if ms >= self.total_ms:
            return self.total_w + (ms - self.total_ms) * self.ppm
        i = bisect_right(self.ends, ms)
        s = self.starts[i]
        x0 = self.xs[i]
        return x0 + (ms - s) / (self.ends[i] - s) * (self.xs[i + 1] - x0)

    def x_to_time(self, x: float) -> float:
        if x <= 0.0:
            return 0.0
        if self.n == 0 or x >= self.total_w:
            return self.total_ms + (x - self.total_w) / self.ppm
        i = bisect_right(self.xs, x) - 1
        if i >= self.n:
            i = self.n - 1
        x0 = self.xs[i]
        s = self.starts[i]
        return s + (x - x0) / (self.xs[i + 1] - x0) * (self.ends[i] - s)

    def index_at_x(self, x: float):
        if self.n == 0 or x < 0.0 or x >= self.total_w:
            return None
        i = bisect_right(self.xs, x) - 1
        return i if i < self.n else self.n - 1

    def nearest_key(self, x: float) -> int:
        """Index of the frame whose END (its key) is closest to content x."""
        if self.n == 0:
            return -1
        j = bisect_left(self.xs, x)          # xs[j] >= x ; key of frame j-1 is xs[j]
        if j <= 1:
            return 0
        if j > self.n:
            return self.n - 1
        return j - 1 if (self.xs[j] - x) <= (x - self.xs[j - 1]) else j - 2


def pose_at(frames, tm: TimeMap, ms: float):
    """-> (angles {servo: number}, frame index). Linear interpolation from the
    previous frame's pose to this frame's pose across this frame's delay; the
    first frame has nothing to come from so it holds its pose."""
    i = tm.index_at_time(ms)
    if i < 0:
        return {}, -1
    cur = frames[i].angles
    if i == 0:
        return dict(cur), 0
    s = tm.starts[i]
    u = (ms - s) / (tm.ends[i] - s)
    if u >= 1.0:
        return dict(cur), i
    if u < 0.0:
        u = 0.0
    prev = frames[i - 1].angles
    return {k: a + (cur.get(k, a) - a) * u for k, a in prev.items()}, i


# -- ruler ---------------------------------------------------------------

_NICE_STEPS = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000,
               15000, 30000, 60000, 120000, 300000, 600000)
_MINOR_DIV = {15000: 3, 30000: 3, 60000: 4, 120000: 4, 300000: 5, 600000: 5}


def tick_step(ms_per_px: float, min_px: float = 84.0) -> int:
    """Smallest 'nice' label interval (ms) that is at least min_px wide."""
    target = ms_per_px * min_px
    for s in _NICE_STEPS:
        if s >= target:
            return s
    return _NICE_STEPS[-1]


def minor_divisions(step: int) -> int:
    if step in _MINOR_DIV:
        return _MINOR_DIV[step]
    if step == 1:
        return 1
    lead = int(str(step)[0])
    return 4 if lead == 2 else 5


def format_tick(ms: float, step: int) -> str:
    ms = int(round(ms))
    if ms >= 60000 and step >= 1000:
        return f"{ms // 60000}:{(ms % 60000) // 1000:02d}"
    if step >= 1000:
        return f"{ms // 1000}s"
    if step >= 100:
        return f"{ms / 1000:.1f}s"
    if step >= 10:
        return f"{ms / 1000:.2f}s"
    return f"{ms / 1000:.3f}s"


def format_clock(ms: float) -> str:
    """s.ms readout, e.g. 1.250"""
    return f"{max(0.0, ms) / 1000.0:.3f}"


# -- derived per-frame data ------------------------------------------------

def changed_mask(prev_angles, angles, order) -> int:
    """Bit k set when servo order[k] differs from the previous frame (all
    bits set for the first frame: it establishes the pose)."""
    if prev_angles is None:
        return (1 << len(order)) - 1
    m = 0
    for k, s in enumerate(order):
        if angles.get(s) != prev_angles.get(s):
            m |= 1 << k
    return m


def effective_faces(frames) -> list:
    """Per frame: (face, face_mode) actually on the OLED during that frame.
    The firmware sets a face when a frame starts and it stays until another
    frame sets a different one, so faces carry forward."""
    out = []
    cur = (None, "loop")
    for f in frames:
        if f.face:
            cur = (f.face, f.face_mode)
        out.append(cur)
    return out


# -- clipboard -------------------------------------------------------------

def frames_to_text(frames) -> str:
    return json.dumps({
        "type": CLIPBOARD_TYPE,
        "version": 1,
        "frames": [f.to_dict() for f in frames],
    }, indent=1)


def frames_from_text(text: str):
    """Parse frames_to_text() output; None if the text isn't ours."""
    if not text or CLIPBOARD_TYPE not in text:
        return None
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("type") != CLIPBOARD_TYPE:
            return None
        frames = [Frame.from_dict(d) for d in data.get("frames", [])]
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return frames or None
