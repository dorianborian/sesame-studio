"""Interaction tests for the 3D viewport (need a real OpenGL 3.3 context;
skipped gracefully when there is none, e.g. on a headless CI box).

A window is shown briefly while they run.
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PySide6")
pytest.importorskip("OpenGL")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.builtin_sequences import STAND_STATE  # noqa: E402
from app.gl_math import ray_plane, signed_angle_about  # noqa: E402
from app.servo_model import SERVO_ORDER  # noqa: E402
from app.viewport3d import Viewport3D, apply_desktop_gl_format  # noqa: E402


@pytest.fixture(scope="module")
def app():
    apply_desktop_gl_format()
    return QApplication.instance() or QApplication(sys.argv)


class _Log:
    def __init__(self, vp):
        self.edits, self.started, self.finished, self.selected, self.hovered = [], [], [], [], []
        vp.anglesEdited.connect(lambda a: self.edits.append(dict(a)))
        vp.dragStarted.connect(self.started.append)
        vp.dragFinished.connect(self.finished.append)
        vp.jointSelected.connect(self.selected.append)
        vp.jointHovered.connect(self.hovered.append)


@pytest.fixture
def vp(app):
    w = Viewport3D(persist_settings=False)
    w.resize(900, 640)
    w.show()
    QTest.qWaitForWindowExposed(w)
    QTest.qWait(150)
    if w._renderer is None or w._gl_failed or w._model is None:
        w.close()
        pytest.skip("no usable OpenGL 3.3 context / robot model")
    w.set_physics_enabled(False)
    w.set_angles(STAND_STATE)
    w._hint_opacity = 0.0
    QTest.qWait(50)
    w.log = _Log(w)
    yield w
    w.close()
    QTest.qWait(20)


def _mouse(vp, etype, pos: QPoint, button=Qt.NoButton, buttons=Qt.NoButton, mods=Qt.NoModifier) -> None:
    """Deliver a synthetic mouse event straight to the widget. (QTest.mouseMove
    warps the REAL cursor and waits for the OS to report it, which is slow,
    flaky when the window is covered, and rude to whoever is using the PC.)"""
    ev = QMouseEvent(etype, QPointF(pos), QPointF(vp.mapToGlobal(pos)), button, buttons, mods)
    QApplication.sendEvent(vp, ev)


def _move(vp, pos: QPoint, buttons=Qt.NoButton, mods=Qt.NoModifier) -> None:
    _mouse(vp, QEvent.MouseMove, pos, Qt.NoButton, buttons, mods)


def _press(vp, pos: QPoint, mods=Qt.NoModifier, button=Qt.LeftButton) -> None:
    _mouse(vp, QEvent.MouseButtonPress, pos, button, button, mods)


def _release(vp, pos: QPoint, mods=Qt.NoModifier, button=Qt.LeftButton) -> None:
    _mouse(vp, QEvent.MouseButtonRelease, pos, button, Qt.NoButton, mods)


def _click(vp, pos: QPoint) -> None:
    _move(vp, pos)
    _press(vp, pos)
    _release(vp, pos)


def _pt(vp, joint) -> QPoint:
    p = vp.joint_screen_point(joint)
    assert p is not None, f"{joint} is not visible"
    return QPoint(int(p[0]), int(p[1]))


def _empty_point(vp) -> QPoint:
    """A pixel with nothing under it (bottom-right of the floor)."""
    for x, y in ((vp.width() - 40, vp.height() - 70), (vp.width() - 40, 120), (40, vp.height() - 70)):
        if vp._pick(x, y)[0] is None:
            return QPoint(x, y)
    pytest.skip("no empty space found")


def _drag(vp, start: QPoint, dx: float, dy: float, steps: int = 24, release: bool = True, mods=Qt.NoModifier):
    _move(vp, start)
    _press(vp, start, mods)
    q = start
    for i in range(1, steps + 1):
        q = QPoint(start.x() + round(dx * i / steps), start.y() + round(dy * i / steps))
        _move(vp, q, Qt.LeftButton, mods)
        yield q
    if release:
        _release(vp, q, mods)


# ---------------------------------------------------------------------------

def test_public_api_surface(vp):
    for name in ("anglesEdited", "dragStarted", "dragFinished", "jointSelected", "jointHovered", "physicsToggled"):
        assert hasattr(vp, name)
    for name in ("set_angles", "set_servo_targets", "set_face_preview", "set_selected_joint", "set_hovered_joint",
                 "set_physics_enabled", "reset_physics", "set_playing", "set_mirror_posing", "reset_view",
                 "set_view", "frame_robot", "set_cover", "set_show_internals"):
        assert callable(getattr(vp, name))
    assert isinstance(vp.physics_enabled, bool)
    assert set(vp.angles) == set(SERVO_ORDER)
    assert vp.rig is None


def test_set_angles_is_silent_and_partial(vp):
    vp.set_angles({"R1": 100.5})
    assert vp.angles["R1"] == pytest.approx(100.5)
    assert vp.angles["L1"] == STAND_STATE["L1"]
    vp.set_servo_targets({"R1": 90})          # physics off: same as set_angles
    assert vp.angles["R1"] == 90
    assert vp.log.edits == []


def test_face_preview_formats(vp):
    vp.set_face_preview(None)
    assert vp._face_pixels is None
    rows = [[(x + y) % 2 == 0 for x in range(128)] for y in range(64)]
    vp.set_face_preview(rows)
    assert vp._face_pixels.shape == (64, 128) and vp._face_pixels[0, 0] == 255 and vp._face_pixels[0, 1] == 0
    vp.set_face_preview(np.ones((64, 128), dtype=bool))
    assert int(vp._face_pixels.min()) == 255
    packed = bytes([0x80] + [0] * 1023)        # only pixel (0, 0) lit, MSB first
    vp.set_face_preview(packed)
    assert vp._face_pixels[0, 0] == 255 and int(vp._face_pixels.sum()) == 255
    vp.set_face_preview(bytearray(packed))
    assert vp._face_pixels[0, 0] == 255
    QTest.qWait(40)                             # uploads without error
    assert vp._gl_failed is None


def test_pick_maps_pixels_to_links(vp):
    for joint in ("L1", "L3", "R3"):
        p = _pt(vp, joint)
        link, world = vp._pick(p.x(), p.y())
        assert link == joint
        assert world is not None and np.all(np.isfinite(world))
        # the world hit re-projects onto the same pixel
        xy, w = vp._project([world])
        assert abs(xy[0, 0] - (p.x() + 0.5)) < 1.5 and abs(xy[0, 1] - (p.y() + 0.5)) < 1.5
    e = _empty_point(vp)
    assert vp._pick(e.x(), e.y())[0] is None


def test_hover_highlights_and_emits(vp):
    p = _pt(vp, "L1")
    _move(vp, p)
    assert vp.hovered_joint == "L1"
    assert vp.log.hovered and vp.log.hovered[-1] == "L1"
    vp.set_hovered_joint("R3")                  # programmatic: no signal
    assert vp.hovered_joint == "R3" and vp.log.hovered[-1] == "L1"


def test_click_selects_and_empty_click_deselects(vp):
    p = _pt(vp, "L3")
    _click(vp, p)
    assert vp.selected_joint == "L3"
    assert vp.log.selected == ["L3"]
    assert vp.log.edits == [] and vp.log.started == []
    vp.set_selected_joint("R1")                 # programmatic: no signal
    assert vp.selected_joint == "R1" and vp.log.selected == ["L3"]
    _click(vp, _empty_point(vp))
    assert vp.selected_joint is None
    assert vp.log.selected == ["L3", None]


def test_drag_femur_changes_only_that_hip_monotonically(vp):
    start = _pt(vp, "L1")
    before = dict(vp.angles)
    seen = []
    for _ in _drag(vp, start, 130, 25):
        seen.append(vp.angles["L1"])
    assert vp.log.started == ["L1"] and vp.log.finished == ["L1"]
    assert vp.selected_joint == "L1"
    assert len(vp.log.edits) >= 5
    for snap in vp.log.edits:
        assert set(snap) == set(SERVO_ORDER)
        assert all(isinstance(v, int) for v in snap.values())
        for name in SERVO_ORDER:
            if name != "L1":
                assert snap[name] == int(round(before[name])), name
    diffs = np.diff(seen)
    assert np.all(diffs >= 0) or np.all(diffs <= 0), seen
    assert abs(seen[-1] - before["L1"]) > 8
    assert max(abs(d) for d in diffs) <= 6, "drag should be smooth, not jumpy"
    # the very first step past the drag threshold must not jump
    assert abs(vp.log.edits[0]["L1"] - before["L1"]) <= 3


def test_drag_tracks_the_cursor_in_the_rotation_plane(vp):
    vp._yaw, vp._pitch = 150.0, 55.0           # look down: hip rotation plane faces the camera
    vp.update()
    QTest.qWait(60)
    start = _pt(vp, "L1")
    worst = 0.0
    checked = 0
    for q in _drag(vp, start, 90, -60, steps=30):
        d = vp._drag
        if d is None or d.mode != "plane":
            continue
        a = vp.angles["L1"]
        if a <= 0 or a >= 180:
            continue
        pivot, axis = vp._joint_frame("L1")
        link = vp._world_links()["L1"]
        grab = link[:3, :3] @ d.grab_local + link[:3, 3]
        eye, direction = vp._ray(q.x(), q.y())
        hit = ray_plane(eye, direction, grab, axis)
        centre = pivot + axis * float(np.dot(grab - pivot, axis))
        worst = max(worst, abs(signed_angle_about(axis, grab - centre, hit - centre)))
        checked += 1
    assert checked >= 10
    assert worst <= 0.75, f"limb lags the cursor by {worst:.2f} deg"   # whole-degree rounding only


def test_drag_clamps_at_the_servo_limits(vp):
    start = _pt(vp, "R3")                      # R3 stands at 180: drag it far both ways
    values = [vp.angles["R3"] for _ in _drag(vp, start, -260, -160, steps=30)]
    assert min(values) >= 0 and max(values) <= 180
    values += [vp.angles["R3"] for _ in _drag(vp, _pt(vp, "R3"), 300, 260, steps=30)]
    assert min(values) >= 0 and max(values) <= 180
    assert all(0 <= s["R3"] <= 180 for s in vp.log.edits)


def test_ctrl_snaps_to_five_degrees(vp):
    start = _pt(vp, "L1")
    for _ in _drag(vp, start, 120, 20, mods=Qt.ControlModifier):
        pass
    assert vp.log.edits, "drag produced no edits"
    assert all(s["L1"] % 5 == 0 for s in vp.log.edits)
    assert len({s["L1"] for s in vp.log.edits}) >= 2


def test_escape_restores_the_pre_drag_pose(vp):
    start = _pt(vp, "L1")
    before = {k: int(round(v)) for k, v in vp.angles.items()}
    gen = _drag(vp, start, 120, 20, release=False)
    last = start
    for last in gen:
        pass
    assert vp.angles["L1"] != before["L1"]
    QTest.keyClick(vp, Qt.Key_Escape)
    assert {k: int(round(v)) for k, v in vp.angles.items()} == before
    assert vp.log.edits[-1] == before
    assert vp.log.finished == ["L1"]
    _move(vp, QPoint(last.x() + 30, last.y()), Qt.LeftButton)  # rest of the gesture is ignored
    _release(vp, last)
    assert {k: int(round(v)) for k, v in vp.angles.items()} == before
    assert vp.log.finished == ["L1"]


def test_dragging_empty_space_orbits_and_never_edits(vp):
    start = _empty_point(vp)
    yaw0, pitch0 = vp._yaw, vp._pitch
    for _ in _drag(vp, start, -120, -30):
        pass
    QTest.qWait(250)                           # camera easing
    assert abs(vp._yaw - yaw0) > 10
    assert vp._pitch != pitch0
    assert vp.log.edits == [] and vp.log.started == []
    assert -15.0 <= vp._pitch <= 89.5


def test_alt_drag_orbits_even_over_a_limb(vp):
    start = _pt(vp, "L1")
    yaw0 = vp._yaw
    for _ in _drag(vp, start, 100, 0, mods=Qt.AltModifier):
        pass
    QTest.qWait(250)
    assert abs(vp._yaw - yaw0) > 10
    assert vp.log.edits == []


def test_wheel_zoom_and_direct_camera_writes(vp):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QWheelEvent
    d0 = vp._distance
    pos = QPointF(vp.width() / 2, vp.height() / 2)
    ev = QWheelEvent(pos, vp.mapToGlobal(pos), QPoint(0, 0), QPoint(0, 240), Qt.NoButton, Qt.NoModifier,
                     Qt.NoScrollPhase, False)
    QApplication.sendEvent(vp, ev)
    QTest.qWait(250)
    assert vp._distance < d0
    # the trailer script writes the camera attributes directly: they must stick
    vp._yaw, vp._pitch, vp._distance = 10.0, 30.0, 222.0
    vp._target = np.array([5.0, 20.0, -3.0], dtype=np.float32)
    vp.update()
    QTest.qWait(200)
    assert (vp._yaw, vp._pitch, vp._distance) == (10.0, 30.0, 222.0)
    assert np.allclose(vp._target, [5.0, 20.0, -3.0])


def test_view_presets_animate_to_their_angles(vp):
    vp.set_view("front")
    QTest.qWait(900)
    assert abs(((vp._yaw - 180.0 + 180.0) % 360.0) - 180.0) < 0.5
    vp.set_view("3/4")
    QTest.qWait(900)
    assert abs(vp._yaw - 145.0) < 0.5 and abs(vp._pitch - 22.0) < 0.5


def test_keyboard_nudge_tab_and_mirror(vp):
    vp.setFocus()
    QTest.keyClick(vp, Qt.Key_Tab)
    assert vp.selected_joint == SERVO_ORDER[0]
    QTest.keyClick(vp, Qt.Key_Tab)
    assert vp.selected_joint == SERVO_ORDER[1]
    QTest.keyClick(vp, Qt.Key_Backtab, Qt.ShiftModifier)
    assert vp.selected_joint == SERVO_ORDER[0]          # R1
    a0 = int(vp.angles["R1"])
    QTest.keyClick(vp, Qt.Key_Right)
    assert vp.angles["R1"] == a0 + 1
    QTest.keyClick(vp, Qt.Key_Left, Qt.ShiftModifier)
    assert vp.angles["R1"] == a0 - 9
    assert vp.log.started == ["R1", "R1"] and vp.log.finished == ["R1", "R1"]
    vp.set_mirror_posing(True)
    QTest.keyClick(vp, Qt.Key_BracketRight)
    assert vp.angles["R1"] == a0 - 8
    assert vp.angles["L1"] == 180 - (a0 - 8)
    vp.set_mirror_posing(False)
    QTest.keyClick(vp, Qt.Key_Escape)
    assert vp.selected_joint is None


def test_physics_preview_slews_servo_targets(vp):
    if not vp.physics_available:
        pytest.skip("physics module unavailable")
    toggles = []
    vp.physicsToggled.connect(toggles.append)
    vp.set_physics_enabled(True)
    assert vp.physics_enabled and toggles == [True]
    QTest.qWait(100)
    root_y = float(vp._root()[1, 3])
    assert root_y > 5.0                                   # standing on the floor, not sunk into it
    vp.set_servo_targets({"R1": 90})
    first = vp.angles["R1"]
    QTest.qWait(60)
    mid = vp.angles["R1"]
    QTest.qWait(900)
    assert first == pytest.approx(STAND_STATE["R1"], abs=12)   # did not teleport
    assert vp.angles["R1"] == pytest.approx(90, abs=0.5)
    assert min(first, 90) - 1 <= mid <= max(first, 90) + 1
    assert vp.log.edits == []
    vp.set_physics_enabled(False)
    assert toggles == [True, False]


def test_display_options_do_not_break_rendering(vp):
    for variant in vp.cover_variants:
        vp.set_cover(variant)
        QTest.qWait(20)
    vp.set_show_internals(True)
    vp.set_shell_color("#3f8cff")
    vp.set_show_grid(False)
    vp.set_show_shadows(False)
    vp.set_show_labels(False)
    vp.set_selected_joint("L3")
    vp.set_playing(True)
    QTest.qWait(60)
    vp.set_playing(False)
    img = vp.grabFramebuffer()
    assert not img.isNull() and img.width() > 0
    assert vp._gl_failed is None
    # something other than the clear colour was drawn
    c = img.pixelColor(img.width() // 2, img.height() // 2)
    assert (c.red(), c.green(), c.blue()) != (0, 0, 0)


def _exercise_api(w):
    w.set_angles(STAND_STATE)
    w.set_servo_targets({"R1": 90})
    w.set_face_preview(np.ones((64, 128), dtype=bool))
    w.set_selected_joint("L1")
    w.set_hovered_joint("L3")
    w.set_physics_enabled(True)
    w.reset_physics()
    w.set_playing(True)
    w.set_playing(False)
    w.set_mirror_posing(True)
    w.set_view("front")
    w.reset_view()
    w.frame_robot()
    w.set_cover("enclosed")
    w.set_show_internals(True)
    assert set(w.angles) == set(SERVO_ORDER)
    p = QPoint(w.width() // 2, w.height() // 2)
    _move(w, p)
    for _ in _drag(w, p, 40, 10, steps=5):
        pass
    QTest.keyClick(w, Qt.Key_Tab)
    QTest.keyClick(w, Qt.Key_F)
    QTest.qWait(60)


def test_missing_model_shows_a_message_instead_of_crashing(app, monkeypatch):
    from app import robot_model

    def boom(*a, **k):
        raise FileNotFoundError("robot model not found (test)")

    monkeypatch.setattr(robot_model.RobotModel, "load", classmethod(boom))
    w = Viewport3D(persist_settings=False)
    try:
        w.resize(500, 400)
        w.show()
        QTest.qWaitForWindowExposed(w)
        QTest.qWait(80)
        assert w._error_label.isVisible()
        assert "model" in w._error_label.text().lower()
        _exercise_api(w)
        assert not w.physics_enabled
    finally:
        w.close()


def test_gl_failure_shows_a_message_instead_of_crashing(app, monkeypatch):
    from app import gl_renderer

    def boom(self, model):
        raise gl_renderer.GLError("robot: fragment shader failed to compile (test)")

    monkeypatch.setattr(gl_renderer.Renderer, "initialize", boom)
    w = Viewport3D(persist_settings=False)
    try:
        w.resize(500, 400)
        w.show()
        QTest.qWaitForWindowExposed(w)
        QTest.qWait(80)
        if not w._gl_failed and QApplication.platformName() == "offscreen":
            # No GL context is ever created on the offscreen platform (forced
            # when the whole suite runs in one process), so initializeGL --
            # and therefore the failure path under test -- never runs.
            pytest.skip("offscreen platform: OpenGL is never initialised")
        assert w._gl_failed
        assert w._error_label.isVisible()
        assert "OpenGL" in w._error_label.text()
        _exercise_api(w)
        assert not w.grabFramebuffer().isNull()
    finally:
        w.close()
