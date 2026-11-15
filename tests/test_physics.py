"""Tests for app.physics.RobotSim.

Every behavioural test runs against a synthetic stand-in model (box-shaped
collision hulls built from the CAD extents) and, when ``app.robot_model`` and
its generated assets are available, against the real CAD-derived model too.

Run:  .venv/Scripts/python.exe -m pytest tests/test_physics.py -q
      (add -s to see the measured numbers)
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

STUDIO_DIR = Path(__file__).resolve().parent.parent
if str(STUDIO_DIR) not in sys.path:
    sys.path.insert(0, str(STUDIO_DIR))

from app.builtin_sequences import (  # noqa: E402
    BUILTIN_SEQUENCES, GAIT_FRAMES_PER_CYCLE, GAIT_LEAD_IN_FRAMES, GAIT_PREVIEW_CYCLES, REST_STATE, STAND_STATE,
)
from app.kinematics import JointDef, link_transforms, transform_points  # noqa: E402
from app.physics import RobotSim  # noqa: E402
from app.servo_model import SERVO_ORDER  # noqa: E402

DT = 1.0 / 60.0
FEET = {"R3", "R4", "L3", "L4"}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def _box(lo, hi, nx=2, ny=2, nz=2) -> np.ndarray:
    """Surface lattice points of an axis-aligned box."""
    xs, ys, zs = np.linspace(lo[0], hi[0], nx), np.linspace(lo[1], hi[1], ny), np.linspace(lo[2], hi[2], nz)
    pts = [(x, y, z) for x in xs for y in ys for z in zs
           if x in (xs[0], xs[-1]) or y in (ys[0], ys[-1]) or z in (zs[0], zs[-1])]
    return np.array(pts, dtype=float)


def synthetic_model(with_link_masses: bool = False) -> SimpleNamespace:
    """Stand-in for app.robot_model.RobotModel: CAD-derived joint frames
    (bind pose = firmware STAND pose) and box hulls from the CAD extents."""
    s = 0.7071067811865476
    joints = {
        "R1": JointDef("R1", None, (1.5, 30.0, -23.0), (0, 1, 0), 1.0, 135.0),
        "L1": JointDef("L1", None, (1.5, 30.0, 27.8), (0, 1, 0), 1.0, 45.0),
        "R2": JointDef("R2", None, (49.9, 30.0, -23.0), (0, 1, 0), 1.0, 45.0),
        "L2": JointDef("L2", None, (49.9, 30.0, 27.8), (0, 1, 0), 1.0, 135.0),
        "R3": JointDef("R3", "R1", (-15.5, 15.2, -58.1), (s, 0, -s), -1.0, 180.0),
        "L3": JointDef("L3", "L1", (-15.5, 15.2, 62.9), (s, 0, s), -1.0, 0.0),
        "R4": JointDef("R4", "R2", (66.9, 15.2, -58.1), (s, 0, s), 1.0, 0.0),
        "L4": JointDef("L4", "L2", (66.9, 15.2, 62.9), (s, 0, -s), 1.0, 180.0),
    }

    def mirror_z(p):   # right side -> left side
        q = p.copy()
        q[:, 2] = 4.8 - q[:, 2]
        return q

    def mirror_x(p):   # front -> rear
        q = p.copy()
        q[:, 0] = 51.4 - q[:, 0]
        return q

    body = _box((-13.6, -1.0, -31.7), (65.6, 60.5, 36.4), 4, 3, 4)
    femur = _box((-22.0, 8.9, -62.9), (14.6, 39.9, -16.3))
    foot_front = np.concatenate([_box((-39.4, -32.2, -53.3), (-20.3, 26.8, -34.2), 2, 3, 2),
                                 [[-29.85, -32.2, -43.75]]])
    foot_rear = np.concatenate([_box((71.3, -32.4, -53.7), (90.5, 26.6, -34.5), 2, 3, 2),
                                [[80.9, -32.4, -44.1]]])
    points = {
        "body": body,
        "R1": femur, "L1": mirror_z(femur), "R2": mirror_x(femur), "L2": mirror_z(mirror_x(femur)),
        "R3": foot_front, "L3": mirror_z(foot_front), "R4": foot_rear, "L4": mirror_z(foot_rear),
    }
    model = SimpleNamespace(joints=joints, collision_points=points, mass_kg=0.3,
                            com=np.array([26.0, 24.0, 2.4]))
    if with_link_masses:
        mass = {"body": 0.2, **{k: 0.015 for k in ("R1", "L1", "R2", "L2")},
                **{k: 0.010 for k in FEET}}
        model.link_mass_kg = mass
        model.link_com = {k: 0.5 * (points[k].min(axis=0) + points[k].max(axis=0)) for k in mass}
    return model


def _real_model():
    robot_model = pytest.importorskip("app.robot_model", reason="real robot model module not present")
    try:
        return robot_model.RobotModel.load()
    except Exception as exc:  # noqa: BLE001 - assets not generated yet, bad format, ...
        pytest.skip(f"real robot model not loadable: {exc!r}")


@pytest.fixture(scope="module", params=["synthetic", "real"])
def model(request):
    return synthetic_model() if request.param == "synthetic" else _real_model()


@pytest.fixture()
def sim(model) -> RobotSim:
    return RobotSim.from_model(model)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(sim: RobotSim, seconds: float, record=None, dt: float = DT) -> None:
    for _ in range(int(round(seconds / dt))):
        sim.step(dt)
        if record is not None:
            record(sim)


def play(sim: RobotSim, frames, record=None, dt: float = DT, stagger_ms: float = 0.0) -> None:
    """Feed frame targets and wait each frame's delay, like the firmware.
    ``stagger_ms`` > 0 also reproduces the firmware's motorCurrentDelay: the
    changed servos are written one after another, that many ms apart."""
    acc = 0.0

    def wait(seconds):
        nonlocal acc
        acc += seconds
        while acc >= dt - 1e-12:
            sim.step(dt)
            acc -= dt
            if record is not None:
                record(sim)

    for frame in frames:
        if stagger_ms > 0.0:
            current = sim.targets
            for name in SERVO_ORDER:
                if name in frame.angles and current.get(name) != float(frame.angles[name]):
                    sim.set_targets({name: frame.angles[name]})
                    wait(stagger_ms / 1000.0)
        else:
            sim.set_targets(frame.angles)
        wait(frame.delay_ms / 1000.0)


def tilt_deg(sim: RobotSim) -> float:
    """Angle between the body's up axis and world up."""
    return math.degrees(math.acos(max(-1.0, min(1.0, float(sim.root[1, 1])))))


def lowest_point(sim: RobotSim, model, links=None) -> float:
    xf = sim.link_world_transforms()
    return min(float(transform_points(xf[k], p)[:, 1].min())
               for k, p in model.collision_points.items() if links is None or k in links)


class Tracker:
    """Integrates the path in the robot's own frame (forward = body -X,
    left = body +Z), the unwrapped heading, and records extremes."""

    def __init__(self, sim: RobotSim):
        self._last = sim.planar_pose()
        self.forward = self.left = self.yaw = 0.0
        self.max_speed = self.max_spin = self.max_tilt = 0.0

    def __call__(self, sim: RobotSim) -> None:
        x, z, yaw = sim.planar_pose()
        lx, lz, lyaw = self._last
        c, s = math.cos(math.radians(lyaw)), math.sin(math.radians(lyaw))
        dx, dz = x - lx, z - lz
        self.forward += -c * dx + s * dz
        self.left += s * dx + c * dz
        self.yaw += (yaw - lyaw + 180.0) % 360.0 - 180.0
        self._last = (x, z, yaw)
        self.max_speed = max(self.max_speed, float(np.linalg.norm(sim.linear_velocity)))
        self.max_spin = max(self.max_spin, float(np.linalg.norm(sim.angular_velocity)))
        self.max_tilt = max(self.max_tilt, tilt_deg(sim))


def run_gait(sim: RobotSim, name: str, cycles: int = 6, style: str = "studio") -> dict:
    """Play a built-in movement for about ``cycles`` gait cycles and return the
    travel per gait cycle measured along the robot's own heading.

    A built-in gait is: lead-in frames, GAIT_PREVIEW_CYCLES passes of the
    4-frame loop, then a closing stand frame.

    style "studio":   loop the whole Sequence, closing stand frame included --
                      what the Timeline preview does with Loop on.
    style "firmware": what the robot does while the button is held: the
                      lead-in once, the 4-frame loop over and over without
                      returning to stand, the stand frame once at the end."""
    frames = BUILTIN_SEQUENCES[name].frames
    lead, per, passes = GAIT_LEAD_IN_FRAMES, GAIT_FRAMES_PER_CYCLE, GAIT_PREVIEW_CYCLES
    assert len(frames) == lead + per * passes + 1
    sim.reset(STAND_STATE)
    tracker = Tracker(sim)
    if style == "studio":
        loops = max(1, round(cycles / passes))
        for _ in range(loops):
            play(sim, frames, tracker)
        cycles = loops * passes
    else:
        play(sim, frames[:lead + per], tracker)                 # lead-in + first pass
        steady = frames[lead + per:lead + 2 * per]              # servo state has settled from pass 2 on
        for _ in range(cycles - 1):
            play(sim, steady, tracker)
        play(sim, frames[-1:], tracker)
    run(sim, 1.5, tracker)
    return {"forward": tracker.forward / cycles, "left": tracker.left / cycles,
            "yaw": tracker.yaw / cycles, "max_tilt": tracker.max_tilt,
            "max_speed": tracker.max_speed, "resting": sim.is_resting}


# ---------------------------------------------------------------------------
# Kinematics / API contract
# ---------------------------------------------------------------------------

def test_forward_kinematics_matches_kinematics_module(model, sim):
    rng = np.random.default_rng(7)
    for _ in range(5):
        angles = {n: float(rng.uniform(0.0, 180.0)) for n in model.joints}
        sim.reset(angles)
        xf = sim.link_world_transforms()
        root = sim.root
        ref = link_transforms(model.joints, angles)
        assert set(xf) == set(ref)
        for link in ref:
            np.testing.assert_allclose(xf[link], root @ ref[link], atol=1e-9)
        # the solver's own (batched) kinematics put every contact on a real collision point
        for pos, link in sim.contacts:
            pts = transform_points(xf[link], model.collision_points[link])
            assert np.min(np.linalg.norm(pts - pos, axis=1)) < 1e-6
            assert abs(pos[1]) < 0.5


def test_api_shapes(model, sim):
    sim.reset(STAND_STATE)
    root = sim.root
    assert root.shape == (4, 4) and root.dtype == np.float64
    np.testing.assert_allclose(root[3], [0.0, 0.0, 0.0, 1.0])
    np.testing.assert_allclose(root[:3, :3] @ root[:3, :3].T, np.eye(3), atol=1e-9)
    assert sim.angles == {k: float(v) for k, v in STAND_STATE.items()}
    assert set(sim.link_world_transforms()) == {"body", *model.joints}
    contacts = sim.contacts
    assert contacts and all(isinstance(p, np.ndarray) and p.shape == (3,) and isinstance(l, str)
                            for p, l in contacts)
    assert sim.is_resting is True
    sim.servo_speed_dps = 300.0
    sim.friction = 0.5
    assert sim.servo_speed_dps == 300.0 and sim.friction == 0.5
    assert sim.is_resting is False          # changing friction wakes the body


def test_reset_places_robot_at_xz_yaw(model, sim):
    sim.reset(STAND_STATE)
    base = sim.root
    sim.reset(STAND_STATE, x=120.0, z=-45.0, yaw_deg=70.0)
    root = sim.root
    assert root[0, 3] == pytest.approx(120.0, abs=1e-9)
    assert root[2, 3] == pytest.approx(-45.0, abs=1e-9)
    assert root[1, 3] == pytest.approx(base[1, 3], abs=1e-6)
    x, z, yaw = sim.planar_pose()
    assert (x, z) == pytest.approx((120.0, -45.0), abs=1e-9)
    assert yaw == pytest.approx(70.0, abs=1e-6)
    # front (-X of the body) now points along R_y(70 deg) @ (-1, 0, 0)
    c, s = math.cos(math.radians(70.0)), math.sin(math.radians(70.0))
    np.testing.assert_allclose(root[:3, :3] @ [-1.0, 0.0, 0.0], [-c, 0.0, s], atol=5e-3)
    # and it is still a rest state there
    before = sim.root
    sim.wake()
    run(sim, 1.0)
    assert np.abs(sim.root - before).max() < 0.05
    # re-seating with planar_pose() is the identity
    sim.reset(STAND_STATE, x=x, z=z, yaw_deg=yaw)
    np.testing.assert_allclose(sim.root, before, atol=1e-6)


# ---------------------------------------------------------------------------
# (a) (b) (c)  resting poses
# ---------------------------------------------------------------------------

def test_stand_stays_put_and_sleeps(model, sim, capsys):
    sim.reset(STAND_STATE)
    assert sim.is_resting
    start = sim.root
    assert {link for _, link in sim.contacts} <= FEET
    assert len({link for _, link in sim.contacts}) >= 3
    assert lowest_point(sim, model, {"body"}) > 15.0            # body clear of the ground
    assert abs(lowest_point(sim, model, FEET)) < 0.2             # feet on the ground
    assert tilt_deg(sim) < 3.0

    sim.wake()
    slept_after = None
    for i in range(180):                                         # 3 simulated seconds
        sim.step(DT)
        if sim.is_resting and slept_after is None:
            slept_after = (i + 1) * DT
    end = sim.root
    drift = float(np.linalg.norm(end[:3, 3] - start[:3, 3]))
    turn = math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(start[:3, :3].T @ end[:3, :3]) - 1.0) / 2.0))))
    with capsys.disabled():
        print(f"\n[stand] height {start[1, 3]:.2f} mm, drift {drift:.2e} mm, rotation {turn:.2e} deg, "
              f"asleep after {slept_after} s")
    assert drift < 0.5 and turn < 0.5
    assert sim.is_resting and slept_after is not None and slept_after < 1.0


def test_rest_pose_lies_flat(model, sim):
    sim.reset(STAND_STATE)
    stand_height = sim.root[1, 3]
    sim.reset(REST_STATE)
    start = sim.root
    assert stand_height - start[1, 3] > 15.0                     # dropped onto belly / legs
    assert abs(lowest_point(sim, model)) < 0.2
    assert len(sim.contacts) >= 3
    sim.wake()
    run(sim, 3.0)
    assert np.abs(sim.root - start).max() < 0.05
    assert sim.is_resting


def test_stand_to_rest_ends_lying_flat(model, sim, capsys):
    sim.reset(REST_STATE)
    rest_height = sim.root[1, 3]
    rest_tilt = tilt_deg(sim)
    sim.reset(STAND_STATE)
    sim.set_targets(REST_STATE)
    heights = []
    tracker = Tracker(sim)

    def record(s):
        tracker(s)
        heights.append(float(s.root[1, 3]))
        assert np.all(np.isfinite(s.root))

    run(sim, 3.0, record)
    with capsys.disabled():
        print(f"\n[stand->rest] final height {heights[-1]:.2f} (rest {rest_height:.2f}), "
              f"peak speed {tracker.max_speed:.0f} mm/s, asleep {sim.is_resting}")
    assert sim.is_resting                                        # no bouncing forever
    assert heights[-1] == pytest.approx(rest_height, abs=1.0)
    assert abs(tilt_deg(sim) - rest_tilt) < 2.0
    assert max(heights) < heights[0] + 6.0                       # never launched upwards
    assert np.ptp(heights[-60:]) < 1e-3                          # dead still for the last second
    assert sim.angles == pytest.approx({k: float(v) for k, v in REST_STATE.items()})


# ---------------------------------------------------------------------------
# (d) gaits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("style", ["studio", "firmware"])
def test_walk_forward_and_backward(model, sim, style, capsys):
    fwd = run_gait(sim, "walk_forward", style=style)
    back = run_gait(sim, "walk_backward", style=style)
    with capsys.disabled():
        for name, r in (("walk_forward", fwd), ("walk_backward", back)):
            print(f"\n[{name} / {style} loop] per cycle: forward {r['forward']:+.1f} mm, "
                  f"left {r['left']:+.1f} mm, yaw {r['yaw']:+.1f} deg; max tilt {r['max_tilt']:.1f} deg, "
                  f"peak speed {r['max_speed']:.0f} mm/s", end="")
        print()
    assert fwd["forward"] > 10.0                                 # clearly towards the front (-X)
    assert abs(fwd["left"]) < fwd["forward"]                     # mostly straight ahead...
    assert abs(fwd["yaw"]) < 20.0                                # ...on a gentle curve at most
    assert back["forward"] < -10.0                               # the other way
    assert abs(back["left"]) < -back["forward"]
    for r in (fwd, back):
        assert r["max_tilt"] < 35.0                              # never fell over
        assert r["max_speed"] < 1000.0
        assert r["resting"]


def test_turns_yaw_in_opposite_directions(model, sim, capsys):
    left = run_gait(sim, "turn_left")
    right = run_gait(sim, "turn_right")
    with capsys.disabled():
        print(f"\n[turn_left] {left['yaw']:+.1f} deg/cycle   [turn_right] {right['yaw']:+.1f} deg/cycle "
              f"(travel {math.hypot(left['forward'], left['left']):.1f} / "
              f"{math.hypot(right['forward'], right['left']):.1f} mm per cycle)")
    assert left["yaw"] > 15.0                                    # +yaw about +Y = front swings to +Z (left)
    assert right["yaw"] < -15.0
    assert left["max_tilt"] < 35.0 and right["max_tilt"] < 35.0
    assert left["resting"] and right["resting"]


# ---------------------------------------------------------------------------
# (e) push-up
# ---------------------------------------------------------------------------

def test_pushup_body_height_oscillates(model, sim, capsys):
    sim.reset(STAND_STATE)
    nose = np.array([float(min(p[:, 0].min() for p in model.collision_points.values())), 30.0, 0.0, 1.0])
    heights = []
    play(sim, BUILTIN_SEQUENCES["pushup"].frames, lambda s: heights.append(float((s.root @ nose)[1])))
    run(sim, 1.0, lambda s: heights.append(float((s.root @ nose)[1])))
    h = np.array(heights)
    mid = 0.5 * (h.max() + h.min())
    downs = int(np.sum((h[:-1] >= mid) & (h[1:] < mid)))         # downward crossings of the mid height
    with capsys.disabled():
        print(f"\n[pushup] front-of-body height {h.min():.1f} .. {h.max():.1f} mm, {downs} dips")
    assert h.max() - h.min() > 8.0
    assert downs >= 4                                            # the sequence has 5 dips
    assert sim.is_resting
    assert h[-1] == pytest.approx(h[0], abs=2.0)                 # back up in the stand pose


# ---------------------------------------------------------------------------
# (f) determinism
# ---------------------------------------------------------------------------

def test_deterministic(model):
    def trace():
        s = RobotSim.from_model(model)
        s.reset(STAND_STATE, x=3.0, z=-2.0, yaw_deg=11.0)
        out = []
        rng = np.random.default_rng(3)
        play(s, BUILTIN_SEQUENCES["walk_forward"].frames, lambda q: out.append(q.root.copy()))
        for i in range(120):
            if i % 10 == 0:
                s.set_targets({n: float(rng.uniform(0, 180)) for n in SERVO_ORDER})
            if i == 60:
                s.set_angles_immediate({n: float(rng.uniform(0, 180)) for n in SERVO_ORDER})
            s.step(DT if i % 3 else 0.021)
            out.append(s.root.copy())
        return np.array(out)

    a, b = trace(), trace()
    assert np.array_equal(a, b)                                  # bit-identical


# ---------------------------------------------------------------------------
# (g) robustness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("immediate, friction, dt", [
    (False, 0.9, DT), (False, 0.2, DT), (False, 2.0, 0.05), (True, 0.9, DT), (True, 0.4, 1.0 / 30.0),
])
def test_random_thrashing_stays_sane(model, immediate, friction, dt, capsys):
    sim = RobotSim.from_model(model, friction=friction)
    sim.reset(STAND_STATE)
    rng = np.random.default_rng(1234)
    every = max(1, int(round(0.1 / dt)))
    max_speed = max_spin = 0.0
    lowest = 0.0
    below = 0
    for i in range(int(30.0 / dt)):                              # 30 simulated seconds
        if i % every == 0:
            target = {n: float(rng.uniform(0.0, 180.0)) for n in SERVO_ORDER}
            if immediate:
                sim.set_angles_immediate(target)
            else:
                sim.set_targets(target)
        sim.step(dt)
        root = sim.root
        assert np.all(np.isfinite(root)), f"non-finite state at step {i}"
        max_speed = max(max_speed, float(np.linalg.norm(sim.linear_velocity)))
        max_spin = max(max_spin, float(np.linalg.norm(sim.angular_velocity)))
        low = lowest_point(sim, model)
        lowest = min(lowest, low)
        below += low < -2.0
        assert root[1, 3] < 400.0                                # never launched into orbit
    with capsys.disabled():
        print(f"\n[thrash immediate={immediate} mu={friction} dt={dt:.4f}] peak speed {max_speed:.0f} mm/s, "
              f"peak spin {max_spin:.1f} rad/s, deepest point {lowest:.2f} mm, frames below -2 mm: {below}")
    assert max_speed < 2500.0 and max_spin < 40.0
    assert below <= 3 and lowest > -8.0                          # no tunnelling
    # and it calms down again
    sim.set_targets(STAND_STATE)
    run(sim, 4.0)
    assert sim.is_resting


def test_big_immediate_jumps_do_not_launch(model, sim, capsys):
    sim.reset(REST_STATE)
    peak = 0.0
    for target in (STAND_STATE, REST_STATE, {n: 0.0 for n in SERVO_ORDER}, {n: 180.0 for n in SERVO_ORDER},
                   STAND_STATE):
        before = (sim.linear_velocity, sim.angular_velocity)
        sim.set_angles_immediate(target)
        assert sim.angles == pytest.approx({k: float(v) for k, v in target.items()})
        assert np.all(np.isfinite(sim.root))
        assert np.array_equal(sim.linear_velocity, before[0])    # the jump injects no velocity
        assert np.array_equal(sim.angular_velocity, before[1])
        assert lowest_point(sim, model) > -2.0
        top = sim.root[1, 3]
        for _ in range(60):
            sim.step(DT)
            peak = max(peak, float(sim.root[1, 3]) - top)
            assert np.all(np.isfinite(sim.root))
    with capsys.disabled():
        print(f"\n[immediate] highest rise after a jump: {peak:.2f} mm")
    assert peak < 3.0
    run(sim, 3.0)
    assert sim.is_resting
    assert lowest_point(sim, model, {"body"}) > 15.0             # standing again


def test_non_finite_and_unknown_inputs_are_ignored(model, sim):
    sim.reset(STAND_STATE)
    sim.set_targets({"R1": float("nan"), "nope": 12.0})
    sim.set_angles_immediate({"L1": float("inf")})
    sim.step(float("nan"))
    sim.step(-1.0)
    sim.step(10.0)                                               # clamped to 0.05 s
    assert np.all(np.isfinite(sim.root))
    assert sim.angles == {k: float(v) for k, v in STAND_STATE.items()}


# ---------------------------------------------------------------------------
# Servo model, friction, sleeping
# ---------------------------------------------------------------------------

def test_servo_slew_rate_and_easing(model, sim):
    sim.reset(REST_STATE)
    sim.set_targets({"R1": 180.0})
    trace = []
    run(sim, 0.6, lambda s: trace.append(s.angles["R1"]))
    trace = np.array(trace)
    rate = np.diff(trace) / DT
    assert rate.max() <= sim.servo_speed_dps * 1.01              # never faster than the servo
    assert rate.max() > 0.8 * sim.servo_speed_dps                # but it does get up to speed
    assert np.all(np.diff(trace) >= -1e-9)                       # monotonic, no overshoot
    assert trace[-1] == 180.0
    t90 = (int(np.argmax(trace >= 179.0)) + 1) * DT              # 90 degrees at 600 deg/s = 0.15 s + easing
    assert 0.15 <= t90 <= 0.30
    assert rate[0] < rate.max() and rate[int(t90 / DT) - 1] < 0.5 * rate.max()   # eased start and stop

    sim.reset(REST_STATE)
    sim.servo_speed_dps = 120.0
    sim.set_targets({"R1": 180.0})
    run(sim, 0.5)
    assert 135.0 < sim.angles["R1"] < 160.0                      # ~120 deg/s


def test_sliding_friction_matches_coulomb(model, capsys):
    results = []
    for mu in (0.9, 0.4):
        sim = RobotSim.from_model(model, friction=mu)
        sim.reset(REST_STATE)
        x0 = sim.root[0, 3]
        sim._v = np.array([-500.0, 0.0, 0.0])                     # shove it along the ground
        sim.wake()
        for _ in range(600):
            sim.step(DT)
            if sim.is_resting:
                break
        slid = x0 - sim.root[0, 3]
        ideal = 500.0 ** 2 / (2.0 * mu * 9810.0)
        results.append((mu, slid, ideal))
        assert sim.is_resting
        assert slid == pytest.approx(ideal, rel=0.2)
    with capsys.disabled():
        print("\n[friction] " + ", ".join(f"mu={m}: slid {s:.1f} mm (ideal {i:.1f})" for m, s, i in results))


def test_static_friction_holds_a_small_push(model, sim):
    sim.reset(REST_STATE)
    x0 = sim.root[0, 3]
    sim._v = np.array([8.0, 0.0, 0.0])                            # well inside what the bristles absorb
    sim.wake()
    run(sim, 1.0)
    assert sim.is_resting
    assert abs(sim.root[0, 3] - x0) < 0.2


def test_asleep_steps_are_free_and_wake_works(model, sim, capsys):
    sim.reset(STAND_STATE)
    before = sim.root
    t0 = time.perf_counter()
    for _ in range(20000):
        sim.step(DT)
    per_step = (time.perf_counter() - t0) / 20000
    with capsys.disabled():
        print(f"\n[perf] asleep step: {per_step * 1e6:.2f} us")
    assert np.array_equal(sim.root, before)
    assert per_step < 20e-6
    sim.set_targets(STAND_STATE)                                  # same targets: stays asleep
    assert sim.is_resting
    sim.wake()
    assert not sim.is_resting
    sim.reset(STAND_STATE)
    sim.set_targets(REST_STATE)                                   # new targets wake it
    assert not sim.is_resting


def test_centre_of_mass_follows_the_legs():
    plain = RobotSim.from_model(synthetic_model())
    massy = RobotSim.from_model(synthetic_model(with_link_masses=True))
    for s in (plain, massy):
        s.reset(STAND_STATE)
    com_stand = massy._com_cur.copy()
    massy.reset(REST_STATE)
    assert np.linalg.norm(massy._com_cur - com_stand) > 1.0       # legs out + up: COM moves
    plain.reset(REST_STATE)
    np.testing.assert_allclose(plain._com_cur, synthetic_model().com)
    # both variants still behave: stand -> rest ends flat and asleep
    for s in (plain, massy):
        s.reset(STAND_STATE)
        s.set_targets(REST_STATE)
        run(s, 3.0)
        assert s.is_resting and s.root[1, 3] < 5.0


# ---------------------------------------------------------------------------
# (h) performance
# ---------------------------------------------------------------------------

def _machine_slowdown() -> float:
    """How much slower than an idle dev machine this process currently runs
    (1.0 = idle), from a fixed numpy micro-workload of the solver's kind."""
    a = np.eye(6) * 7.0 + 0.1
    b = np.arange(6.0)
    x = np.linspace(0.0, 1.0, 144).reshape(48, 3)
    r = np.eye(3)
    best = float("inf")
    for _ in range(5):
        t0 = time.perf_counter()
        for _ in range(2000):
            np.linalg.solve(a, b)
            x @ r
        best = min(best, (time.perf_counter() - t0) / 2000)
    return best / 3.2e-6


def test_step_time_while_moving(model, capsys):
    sim = RobotSim.from_model(model)
    n_points = sum(len(p) for p in model.collision_points.values())
    best = {}
    for name in ("walk_forward", "pushup", "dance"):
        frames = BUILTIN_SEQUENCES[name].frames
        means = []
        for _ in range(5):                                       # best of 5: reject load bursts
            sim.reset(STAND_STATE)
            samples = []
            step = sim.step

            def timed(dt, _step=step, _samples=samples):
                t0 = time.perf_counter()
                _step(dt)
                _samples.append(time.perf_counter() - t0)

            sim.step = timed
            try:
                for _ in range(2):
                    play(sim, frames)
            finally:
                del sim.step
            means.append(float(np.mean(samples)))
        best[name] = min(means)
    slowdown = _machine_slowdown()
    with capsys.disabled():
        print(f"\n[perf] {n_points} collision points, mean step(1/60) while moving: "
              + ", ".join(f"{k} {v * 1e3:.3f} ms" for k, v in best.items())
              + f"  (machine load factor {slowdown:.2f})")
    if max(best.values()) >= 1.5e-3 and slowdown > 1.5:
        pytest.skip(f"machine too busy for a timing assertion (load factor {slowdown:.2f})")
    assert max(best.values()) < 1.5e-3
