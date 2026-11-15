"""Interactive tour + help center tests.

Covers: step list integrity, targets resolving on a real MainWindow, callout
placement, auto-advance on (simulated) user actions, Back / Next / Skip,
keyboard handling, mouse pass-through of the spotlight, restart / abort /
close behaviour, cleanup (no filters, timers or connections left, overlay
deleted), the first-run flag, and the HelpDialog (articles, search, links).

Nothing here needs a working OpenGL context: with the offscreen platform the
3D view may fall back to its error label, the tour only talks to its signals
and geometry.

Every QSettings the app creates is redirected to a per-test INI file (see
``isolated_settings``), so the real "tutorial seen" flag, recent files and
window layout are never touched.

Run:  .venv\\Scripts\\python.exe -m pytest tests/test_tutorial.py -q
"""

import os
import re
import sys
from pathlib import Path

import pytest

STUDIO_DIR = Path(__file__).resolve().parent.parent
if str(STUDIO_DIR) not in sys.path:
    sys.path.insert(0, str(STUDIO_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6
from PySide6.QtCore import QEvent, QPoint, QRect, QSettings, QSize, Qt, QUrl
from PySide6.QtGui import QAction, QImage, QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

import app.main_window as main_window_mod
import app.tutorial as tutorial
import app.tutorial_help as tutorial_help
import app.viewport3d as viewport3d_mod
from app.face_editor import TOOL_DEFS
from app.tutorial import (
    HelpDialog, TourOverlay, build_steps, mark_tutorial_seen, place_callout,
    should_show_first_run_tutorial, start_tour,
)

MAIN_WINDOW_ARTICLES = [
    "Getting Started", "Posing in 3D", "Timeline Guide", "Face Painter Guide",
    "Export Guide", "Keyboard Shortcuts", "About",
]


def pump(times: int = 3) -> None:
    for _ in range(times):
        app.processEvents()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """QSettings(org, app) ignores QSettings.setDefaultFormat and would hit the
    real registry / plist, so swap the class the app modules instantiate."""
    ini = str(tmp_path / "settings.ini")

    def factory(*_args, **_kwargs):
        return QSettings(ini, QSettings.Format.IniFormat)

    for mod in (main_window_mod, viewport3d_mod, tutorial):
        monkeypatch.setattr(mod, "QSettings", factory)
    assert Path(tutorial._settings().fileName()) == Path(ini)
    return ini


@pytest.fixture
def window(isolated_settings):
    mark_tutorial_seen(True)                 # keep new_project() from auto-starting the tour
    win = main_window_mod.MainWindow()
    assert win.settings.format() == QSettings.Format.IniFormat
    win.resize(1500, 940)
    win.show()
    win.new_project()
    mark_tutorial_seen(False)
    pump()
    yield win
    tour = getattr(win, "_tour", None)
    if tour is not None and shiboken6.isValid(tour):
        tour.abort()
    for old in win.findChildren(TourOverlay):
        old.abort()
    win.face_library.close_editor(force=True)
    if win.project is not None:
        win.project.dirty = False            # no "save changes?" prompt on close
    if win.header_file is not None:
        win.header_file.dirty = False
    win.close()
    win.deleteLater()
    pump()


@pytest.fixture
def tour(window):
    t = start_tour(window)
    window._tour = t
    pump()
    return t


def step_index(t: TourOverlay, key: str) -> int:
    return [s.key for s in t.steps].index(key)


def goto(t: TourOverlay, key: str) -> None:
    t.go_to(step_index(t, key))
    pump()


def pose_snapshot(window, **changes) -> dict:
    angles = window.inspector.current_angles()
    angles.update(changes)
    return angles


# ---------------------------------------------------------------------------
# step list
# ---------------------------------------------------------------------------
def test_step_list_integrity():
    steps = build_steps()
    assert 7 <= len(steps) <= 9
    keys = [s.key for s in steps]
    assert len(set(keys)) == len(keys)
    assert keys[0] == "welcome" and keys[-1] == "export"
    for s in steps:
        assert s.title.strip()
        body = s.body if isinstance(s.body, str) else "dynamic"
        assert body.strip()
        assert s.beat_ms >= 500
        for sequence, label in s.keys:
            assert sequence and label
        for task in s.tasks:
            assert task.text.strip() and callable(task.arm)
        for spec in s.placement:
            assert spec == "center" or re.fullmatch(
                r"(spot|focus|edge):(right|left|above|below)-(start|center|end)"
                r"|inside:(top|bottom)-(left|right|center)", spec), spec
    assert not steps[0].tasks and steps[0].spots is None      # welcome: a centred card
    assert not steps[-1].tasks and steps[-1].next_label == "Done"
    assert sum(1 for s in steps if s.tasks) >= 6               # it is a learn-by-doing tour


def test_copy_mentions_real_ui_labels(window):
    """Names used in the tour copy must exist in the UI they describe."""
    tl = window.timeline
    assert tl.add_frame_btn.text().strip("+ ") == "Add Frame"
    assert tl.play_btn.text() == "Play"
    assert tl.duplicate_btn.text() == "Duplicate to edit"
    assert window.inspector.preset_btn.text() == "Presets"
    assert window.inspector.mirror_btn.text() == "Mirror"
    assert window.face_library.assign_btn.text() == "Assign to Frame"
    assert window.face_library.new_btn.text() == "New"
    assert window.export_btn.text() == "Export"
    assert window.inspector_dock.windowTitle() == "Pose"
    assert window.face_dock.windowTitle() == "Faces"
    for name in ("wave", "dance", "walk_forward"):
        assert name in tl.builtin_sequences
    assert window.act_tour.shortcut().toString() == "F1"
    assert window.act_shortcuts.shortcut().toString() == "Ctrl+/"
    assert window.act_play.shortcut().toString() == "Space"
    assert window.act_undo.shortcut().toString() == "Ctrl+Z"


def test_targets_resolve_on_main_window(window, tour):
    bounds = window.rect()
    for i, step in enumerate(tour.steps):
        tour.go_to(i)
        pump()
        assert tour.index == i
        spots = tour.spot_rects()
        if step.spots is not None:
            assert spots, f"step {step.key}: nothing to spotlight"
            for r in spots:
                assert bounds.contains(r), (step.key, r)
                assert r.width() > 20 and r.height() > 20
        else:
            assert spots == []
        card = tour.card_rect()
        assert bounds.contains(card), (step.key, card)
        assert card.width() == tutorial.CARD_WIDTH
        focus = step.focus_rect(window)
        if step.focus is not None:
            assert focus is not None, f"step {step.key}: focus control not on screen"
            assert not card.intersects(focus), f"step {step.key}: card hides its control"
        assert len(tour.card.task_rows) == len(step.tasks)
        assert tour.card.title_label.text() == step.title


def test_spotlights_at_minimum_window_size(window, tour):
    window.resize(window.minimumSize())
    pump()
    for i, step in enumerate(tour.steps):
        tour.go_to(i)
        pump()
        assert window.rect().contains(tour.card_rect()), step.key
        if step.spots is not None:
            assert tour.spot_rects(), step.key


def test_docks_are_shown_for_their_step(window, tour):
    window.face_dock.hide()
    window.inspector_dock.hide()
    window.timeline_dock.hide()
    pump()
    goto(tour, "tune")
    assert window.inspector_dock.isVisible()
    goto(tour, "frames")
    assert window.timeline_dock.isVisible()
    goto(tour, "faces")
    assert window.face_dock.isVisible()
    assert tour.spot_rects()


# ---------------------------------------------------------------------------
# callout placement (pure geometry)
# ---------------------------------------------------------------------------
def test_place_callout_center_without_target():
    bounds = QRect(12, 12, 976, 616)
    rect, arrow = place_callout(bounds, QSize(336, 240), [], None, ("center",))
    assert arrow is None
    assert abs(rect.center().x() - bounds.center().x()) <= 1
    assert abs(rect.center().y() - bounds.center().y()) <= 1


def test_place_callout_beside_spot_points_at_it():
    bounds = QRect(12, 12, 1476, 916)
    spot = QRect(3, 79, 1130, 520)
    rect, arrow = place_callout(bounds, QSize(336, 260), [spot], None, ("spot:right-start",))
    assert bounds.contains(rect) and not rect.intersects(spot)
    assert rect.left() > spot.right()
    assert arrow is not None and arrow[0] == "left"
    assert spot.right() < arrow[1].x() < rect.left()


def test_place_callout_falls_back_when_no_room():
    bounds = QRect(12, 12, 976, 616)
    spot = QRect(3, 79, 720, 260)                       # only 250 px to its right
    rect, arrow = place_callout(bounds, QSize(336, 240), [spot], None,
                                ("spot:right-start", "spot:below-end"))
    assert bounds.contains(rect) and not rect.intersects(spot)
    assert rect.top() > spot.bottom() and arrow[0] == "top"


def test_place_callout_never_covers_focus_or_leaves_window():
    bounds = QRect(12, 12, 976, 616)
    spot = QRect(3, 79, 990, 540)
    focus = QRect(560, 390, 110, 34)
    for prefs in ((), ("focus:right-start",), ("focus:above-end",), ("edge:right-start",),
                  ("inside:bottom-right",), ("spot:right-start", "edge:right-start")):
        rect, _arrow = place_callout(bounds, QSize(336, 250), [spot], focus, prefs)
        assert bounds.contains(rect), prefs
        assert not rect.intersects(focus), prefs


# ---------------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------------
def test_start_shows_overlay_over_whole_window(window, tour):
    assert tour.is_active() and tour.isVisible()
    assert tour.parent() is window
    assert tour.geometry() == window.rect()
    assert tour.index == 0
    assert tour.card.next_btn.text() == "Start tour"
    assert tour.card.back_btn.isHidden()
    assert not tour.card.skip_btn.isHidden()


def test_back_next(window, tour):
    seen = []
    tour.stepChanged.connect(seen.append)
    tour.back()
    assert tour.index == 0
    tour.next()
    tour.next()
    assert tour.index == 2
    tour.back()
    assert tour.index == 1
    assert seen == [1, 2, 1]
    assert not tour.card.back_btn.isHidden()
    QTest.mouseClick(tour.card.next_btn, Qt.MouseButton.LeftButton)
    assert tour.index == 2
    QTest.mouseClick(tour.card.back_btn, Qt.MouseButton.LeftButton)
    assert tour.index == 1


def test_next_is_always_available_and_tasks_reset(window, tour):
    goto(tour, "pose")
    assert tour.tasks_done() == [False]
    tour.next()                                         # without doing the task
    assert tour.current_step().key == "tune"
    tour.back()
    window.viewport.anglesEdited.emit(pose_snapshot(window, R1=100))
    assert tour.step_is_complete()
    tour.next()
    tour.back()                                         # coming back starts the step afresh
    assert tour.tasks_done() == [False] and not tour.step_is_complete()


def test_skip_marks_seen_and_cleans_up(window, tour):
    assert should_show_first_run_tutorial()
    results = []
    tour.finished.connect(results.append)
    tour.next()
    QTest.mouseClick(tour.card.skip_btn, Qt.MouseButton.LeftButton)
    assert results == [False]
    assert not tour.is_active() and not tour.isVisible()
    assert not should_show_first_run_tutorial()
    assert window._tour is None
    pump()
    assert not shiboken6.isValid(tour)


def test_finishing_flips_first_run_flag_and_cleans_up(window, tour):
    assert should_show_first_run_tutorial()
    results = []
    tour.finished.connect(results.append)
    for _ in range(len(tour.steps) - 1):
        tour.next()
    assert tour.index == len(tour.steps) - 1
    assert tour.card.next_btn.text() == "Done"
    assert tour.card.skip_btn.isHidden()
    assert should_show_first_run_tutorial()             # not until the tour is finished
    tour.next()
    assert results == [True]
    assert not should_show_first_run_tutorial()
    # nothing left behind
    assert not tour.is_active()
    assert tour._filtered == [] and tour._connections == [] and tour._ctx is None
    assert tour._escape is None
    assert not tour._timer.isActive() and not tour._advance_timer.isActive()
    assert window._tour is None
    pump()
    from PySide6.QtGui import QShortcut
    assert [s for s in window.findChildren(QShortcut)
            if s.key().toString() == "Esc"] == []           # the tour's Esc shortcut is gone
    assert not shiboken6.isValid(tour)
    assert window.findChildren(TourOverlay) == []
    # the app still works and none of its signals reach the dead tour
    window.viewport.anglesEdited.emit(pose_snapshot(window, R1=95))
    window.timeline.add_frame()
    QTest.keyClick(window.viewport, Qt.Key.Key_Escape)
    pump()


def test_restart_does_not_stack(window, tour):
    tour.next()
    second = start_tour(window)
    window._tour = second
    assert not tour.is_active()
    assert second.is_active() and second.index == 0
    pump()
    assert not shiboken6.isValid(tour)
    alive = [t for t in window.findChildren(TourOverlay) if t.is_active()]
    assert alive == [second]
    assert should_show_first_run_tutorial()             # a restart is not "seen"
    window.start_tutorial()                             # the main window's own entry point
    pump()
    assert not second.is_active()
    assert window._tour is not second and window._tour.is_active()


def test_first_run_starts_tour_from_new_project(window):
    assert should_show_first_run_tutorial()
    window.project.dirty = False
    window.new_project()
    QTest.qWait(450)                                    # new_project() defers the start by 250 ms
    assert window._tour is not None and window._tour.is_active()
    window._tour.skip()
    window.project.dirty = False
    window.new_project()
    QTest.qWait(450)
    assert window._tour is None                         # seen: not started again


# ---------------------------------------------------------------------------
# learn by doing: detection + auto-advance
# ---------------------------------------------------------------------------
def test_look_step_detects_camera_change(window, tour):
    goto(tour, "look")
    vp = window.viewport
    centre = vp.rect().center()
    tour.poll()
    assert tour.tasks_done() == [False]
    vp._yaw += 25.0                                     # camera moved, but not by the user
    tour.poll()
    assert tour.tasks_done() == [False]
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
    vp._yaw += 25.0                                     # what an orbit drag does
    tour.poll()
    assert tour.tasks_done() == [False]                 # still dragging
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
    tour.poll()
    assert tour.tasks_done() == [True] and tour.step_is_complete()


def test_look_step_ignores_click_without_camera_change(window, tour):
    goto(tour, "look")
    vp = window.viewport
    QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp.rect().center())
    tour.poll()
    assert tour.tasks_done() == [False]


def test_auto_advance_after_success_beat(window, tour):
    goto(tour, "pose")
    i = tour.index
    window.viewport.anglesEdited.emit(pose_snapshot(window, R1=100))
    assert tour.step_is_complete()
    assert tour.index == i                              # the success beat comes first
    assert tour.card.task_rows[0].state() == "done"
    assert tour._advance_timer.isActive()
    QTest.qWait(tour.steps[i].beat_ms + 250)
    assert tour.index == i + 1


def test_manual_next_cancels_pending_auto_advance(window, tour):
    goto(tour, "pose")
    window.viewport.anglesEdited.emit(pose_snapshot(window, R1=100))
    tour.next()
    i = tour.index
    assert not tour._advance_timer.isActive()
    QTest.qWait(1300)
    assert tour.index == i                              # no double step


def test_whole_tour_by_doing(window, tour):
    tl, vp, insp, faces = window.timeline, window.viewport, window.inspector, window.face_library
    assert tour.current_step().key == "welcome"
    tour.next()

    def expect(key):
        assert tour.current_step().key == key
        assert not tour.step_is_complete()

    def advance():
        assert tour.step_is_complete()
        assert tour._advance_timer.isActive()
        assert tour._ctx is None                        # detectors released at once
        tour._advance_timer.stop()
        tour._auto_advance()
        pump()

    expect("look")
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp.rect().center())
    vp._pitch += 12.0
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp.rect().center())
    tour.poll()
    advance()

    expect("pose")
    vp.dragStarted.emit("R1")
    vp.anglesEdited.emit(pose_snapshot(window, R1=110))
    vp.dragFinished.emit("R1")
    advance()

    expect("tune")
    assert window.inspector_dock.isVisible()
    row = insp.rows["L1"]
    row.slider.setValue(row.slider.value() + 7)
    advance()

    expect("frames")
    frames = len(tl.current_sequence().frames)
    vp.anglesEdited.emit(pose_snapshot(window, R2=60))  # posing first does not count
    assert tour.tasks_done() == [False, False]
    QTest.mouseClick(tl.add_frame_btn, Qt.MouseButton.LeftButton)
    assert len(tl.current_sequence().frames) == frames + 1
    assert tour.tasks_done() == [True, False]
    assert tour.card.task_rows[1].state() == "active"
    vp.anglesEdited.emit(pose_snapshot(window, R2=70))
    assert tour.tasks_done() == [True, True]
    advance()

    expect("play")
    QTest.mouseClick(tl.play_btn, Qt.MouseButton.LeftButton)
    assert tl.is_playing()
    advance()

    expect("builtin")
    tl.set_current_sequence(sorted(tl.sequences)[0])    # one of your own: not what was asked
    assert tour.tasks_done() == [False, False]
    assert tl.set_current_sequence("wave")
    assert tour.tasks_done() == [True, False]
    tl.play()
    assert tour.tasks_done() == [True, True]
    advance()

    expect("faces")
    assert not tl.is_playing()
    assert not tl.current_is_builtin()                  # back on an editable sequence
    assert window.face_dock.isVisible()
    assert faces.select_face("happy")
    advance()

    expect_last = tour.current_step()
    assert expect_last.key == "export" and not expect_last.tasks
    assert not tour._advance_timer.isActive()
    tour.next()
    assert not tour.is_active()
    assert not should_show_first_run_tutorial()


def test_leaving_a_step_releases_its_detectors(window, tour):
    goto(tour, "look")
    ctx = tour._ctx
    assert ctx is not None and not ctx.is_idle()
    tour.next()
    assert ctx.is_idle()
    window.viewport._yaw += 40.0
    tour.poll()
    assert tour.current_step().key == "pose" and tour.tasks_done() == [False]
    old = tour._ctx
    tour.skip()
    assert old.is_idle()


def test_editing_steps_leave_builtin_sequences(window, tour):
    tl = window.timeline
    own = tl.current_seq_name
    assert tl.set_current_sequence("dance") and tl.current_is_builtin()
    goto(tour, "frames")
    assert tl.current_seq_name == own
    assert tl.add_frame_btn.isEnabled()


def test_tour_alone_never_edits_the_project(window, tour):
    tl = window.timeline

    def snapshot():
        return {name: [(f.delay_ms, dict(f.angles), f.face, f.face_mode) for f in seq.frames]
                for name, seq in tl.sequences.items()}

    before = snapshot()
    undo_count = tl.undo_stack.count()
    window.project.dirty = False
    for _ in range(len(tour.steps)):
        tour.next()
        pump()
    assert not tour.is_active()
    assert snapshot() == before
    assert tl.undo_stack.count() == undo_count
    assert window.project.dirty is False
    assert dict(window.face_library.faces) == {}


# ---------------------------------------------------------------------------
# keyboard
# ---------------------------------------------------------------------------
def test_keys_navigate_when_nothing_else_wants_them(window, tour):
    vp = window.viewport
    QTest.keyClick(vp, Qt.Key.Key_Right)
    assert tour.index == 1
    QTest.keyClick(vp, Qt.Key.Key_Return)
    assert tour.index == 2
    QTest.keyClick(vp, Qt.Key.Key_Left)
    assert tour.index == 1
    QTest.keyClick(vp, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
    assert tour.index == 1                              # modified keys are not ours
    QTest.keyClick(vp, Qt.Key.Key_Escape)
    assert not tour.is_active()
    assert not should_show_first_run_tutorial()


def test_spotlighted_widget_keeps_its_keys(window, tour):
    tl = window.timeline
    tl.add_frame()
    tl.add_frame()
    goto(tour, "play")
    i = tour.index
    tl.select_frame(0)
    QTest.keyClick(tl.canvas, Qt.Key.Key_Right)         # timeline: next frame
    assert tl.selected_index == 1 and tour.index == i
    QTest.keyClick(tl.canvas, Qt.Key.Key_Left)
    assert tl.selected_index == 0 and tour.index == i
    QTest.keyClick(tl.canvas, Qt.Key.Key_Space)         # timeline: play -> completes the task
    assert tl.is_playing() and tour.index == i and tour.step_is_complete()
    tl.pause()


def test_escape_always_leaves_even_when_the_view_would_eat_it(window, tour):
    goto(tour, "pose")
    vp = window.viewport
    if getattr(vp, "_model", None) is None:
        pytest.skip("robot model not available")
    window.activateWindow()
    vp.setFocus()
    pump()
    if not window.isActiveWindow():
        pytest.skip("window activation is not available on this platform")
    vp._select("R1", emit=True)
    QTest.keyClick(vp, Qt.Key.Key_Right)                # nudges the joint, not the tour
    assert tour.current_step().key == "pose"
    QTest.keyClick(vp, Qt.Key.Key_Escape)               # Esc: out, although a joint is selected
    assert not tour.is_active()
    assert tour._escape is None


def test_escape_during_a_drag_goes_to_the_dragged_widget(window, tour):
    goto(tour, "pose")
    vp = window.viewport
    window.activateWindow()
    vp.setFocus()
    pump()
    if not window.isActiveWindow():
        pytest.skip("window activation is not available on this platform")
    centre = vp.rect().center()
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
    QTest.keyClick(vp, Qt.Key.Key_Escape)               # would cancel the drag
    assert tour.is_active()
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
    QTest.keyClick(vp, Qt.Key.Key_Escape)
    assert not tour.is_active()


def test_space_shortcut_still_plays(window, tour):
    goto(tour, "play")
    window.activateWindow()
    window.viewport.setFocus()
    pump()
    if not window.isActiveWindow():
        pytest.skip("window activation is not available on this platform")
    QTest.keyClick(window.viewport, Qt.Key.Key_Space)   # the window-wide Play / Pause action
    assert window.timeline.is_playing()
    assert tour.step_is_complete() and tour.current_step().key == "play"
    window.timeline.pause()


def test_enter_in_a_field_does_not_advance(window, tour):
    goto(tour, "tune")
    spin = window.inspector.rows["R1"].spin
    window.activateWindow()
    spin.setFocus()
    pump()
    if QApplication.focusWidget() is not spin:
        pytest.skip("keyboard focus is not available on this platform")
    i = tour.index
    QTest.keyClick(spin, Qt.Key.Key_Return)
    QTest.keyClick(spin, Qt.Key.Key_Left)
    assert tour.index == i and tour.is_active()
    QTest.keyClick(spin, Qt.Key.Key_Escape)             # Esc always leaves
    assert not tour.is_active()


def test_card_buttons_never_take_focus(tour):
    for btn in (tour.card.next_btn, tour.card.back_btn, tour.card.skip_btn):
        assert btn.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert tour.focusPolicy() == Qt.FocusPolicy.NoFocus


# ---------------------------------------------------------------------------
# mouse: through the spotlight, swallowed elsewhere
# ---------------------------------------------------------------------------
def is_inside(widget, ancestor) -> bool:
    while widget is not None:
        if widget is ancestor:
            return True
        widget = widget.parentWidget()
    return False


def test_hit_testing_through_the_spotlight(window, tour):
    goto(tour, "look")
    vp_rect = QRect(window.viewport.mapTo(window, QPoint(0, 0)), window.viewport.size())
    inside = window.childAt(vp_rect.center())
    assert is_inside(inside, window.viewport), inside
    for widget in (window.timeline.add_frame_btn, window.inspector.preset_btn, window.export_btn):
        pt = widget.mapTo(window, widget.rect().center())
        if tour.card_rect().adjusted(-16, -16, 16, 20).contains(pt):
            continue
        assert window.childAt(pt) is tour, widget
    hit = window.childAt(tour.card.next_btn.mapTo(window, tour.card.next_btn.rect().center()))
    assert hit is tour.card.next_btn


def test_clicks_reach_the_target_but_not_the_rest(window, tour):
    handle = window.windowHandle()
    tl = window.timeline
    frames = len(tl.current_sequence().frames)

    goto(tour, "look")                                  # the timeline is dimmed here
    pt = tl.add_frame_btn.mapTo(window, tl.add_frame_btn.rect().center())
    assert not tour.card_rect().contains(pt)
    QTest.mouseClick(handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pt)
    pump()
    assert len(tl.current_sequence().frames) == frames  # swallowed
    assert tour.is_active() and tour.current_step().key == "look"

    goto(tour, "frames")                                # now it is the spotlighted control
    pt = tl.add_frame_btn.mapTo(window, tl.add_frame_btn.rect().center())
    QTest.mouseClick(handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pt)
    pump()
    assert len(tl.current_sequence().frames) == frames + 1
    assert tour.tasks_done() == [True, False]

    goto(tour, "play")
    pt = tl.play_btn.mapTo(window, tl.play_btn.rect().center())
    QTest.mouseClick(handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pt)
    pump()
    assert tl.is_playing() and tour.step_is_complete()
    tl.pause()

    # the callout's own buttons work through the real event path too
    pt = tour.card.back_btn.mapTo(window, tour.card.back_btn.rect().center())
    QTest.mouseClick(handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pt)
    pump()
    assert tour.current_step().key == "frames"


def test_spot_mask_matches_spot_rects(window, tour):
    goto(tour, "tune")
    mask = tour.mask()
    for r in tour.spot_rects():
        assert not mask.contains(r.center())            # a hole: clicks fall through
        assert mask.contains(QPoint(r.left() - 6, r.center().y())) or r.left() < 8
    assert mask.contains(tour.card_rect().center())
    assert mask.contains(QPoint(5, 5))                  # menu bar corner is covered


# ---------------------------------------------------------------------------
# window events
# ---------------------------------------------------------------------------
def test_follows_window_resize(window, tour):
    goto(tour, "tune")
    before = tour.spot_rects()
    window.resize(1180, 760)
    pump()
    tour.poll()
    assert tour.geometry() == window.rect()
    assert window.rect().contains(tour.card_rect())
    after = tour.spot_rects()
    assert after and after != before
    dock = window.inspector_dock
    dock_rect = QRect(dock.mapTo(window, QPoint(0, 0)), dock.size())
    assert dock_rect.adjusted(-1, -1, 1, 1).contains(after[0])


def test_stays_above_docks_that_raise_themselves(window, tour):
    goto(tour, "look")
    window.inspector_dock.raise_()                      # e.g. selecting a joint in 3D does this
    pump()
    tour.poll()
    widgets = [c for c in window.children() if c.isWidgetType() and c.isVisible() and not c.isWindow()]
    assert widgets[-1] is tour


def test_paused_while_minimized(window, tour):
    goto(tour, "pose")
    window.showMinimized()
    pump()
    if not window.isMinimized():
        pytest.skip("platform cannot minimize")
    phase = tour._phase
    tour.poll()
    assert tour._phase == phase and tour.is_active()
    window.showNormal()
    pump()
    tour.poll()
    assert tour.is_active() and tour.current_step().key == "pose"


def test_going_back_to_welcome_page_aborts(window, tour):
    tour.next()
    window.project.dirty = False
    window._goto_welcome()
    pump()
    assert not tour.is_active()
    assert should_show_first_run_tutorial()             # aborted, not "seen"
    assert not shiboken6.isValid(tour)


def test_closing_the_window_tears_down(window, tour):
    tour.next()
    window.project.dirty = False
    window.close()
    assert not tour.is_active()
    assert tour._filtered == [] and not tour._timer.isActive()
    assert not should_show_first_run_tutorial()         # they did see it
    pump()
    assert not shiboken6.isValid(tour)


def test_overlay_paints(window, tour):
    for key in ("welcome", "frames", "export"):
        goto(tour, key)
        tour.poll()
        image = tour.grab().toImage()
        assert not image.isNull() and image.width() >= window.width()


# ---------------------------------------------------------------------------
# help center
# ---------------------------------------------------------------------------
def test_help_has_every_article_the_main_window_uses():
    titles = tutorial_help.article_titles()
    assert titles == MAIN_WINDOW_ARTICLES
    source = (STUDIO_DIR / "app" / "main_window.py").read_text(encoding="utf-8")
    used = set(re.findall(r'show_help\("([^"]+)"\)', source))
    block = re.search(r'for title in \(([^)]*)\):\s*\n\s*help_menu', source)
    if block:
        used |= set(re.findall(r'"([^"]+)"', block.group(1)))
    assert used, "could not find the help titles in main_window.py"
    for title in used:
        assert title in titles, f"main window opens a missing help article: {title}"


def test_help_opens_each_article_by_title():
    for title in MAIN_WINDOW_ARTICLES:
        dlg = HelpDialog(None, open_article=title)
        assert dlg.current_title() == title
        assert dlg.list_widget.currentItem().text() == title
        text = dlg.browser.toPlainText()
        assert len(text) > 80
        assert "[[" not in text and "]]" not in text and "{version}" not in text
        dlg.deleteLater()
    dlg = HelpDialog(None, open_article="No Such Article")
    assert dlg.current_title() == "Getting Started"
    assert dlg.open_article("timeline guide") and dlg.current_title() == "Timeline Guide"
    assert not dlg.open_article("nope") and dlg.current_title() == "Timeline Guide"
    assert HelpDialog(None).current_title() == "Getting Started"
    pump()


def test_help_articles_are_current():
    everything = " ".join(a.plain_text() for a in tutorial_help.HELP_ARTICLES)
    assert "joint calibration" not in everything
    from app import __version__
    about = tutorial_help.find_article("About")
    assert __version__ in tutorial_help.render_article(about)
    export = tutorial_help.find_article("Export Guide").plain_text()
    for needle in ("movement-sequences.h", "sesame-firmware-main.ino", "face_list", "epd_bitmap_",
                   "facefpsentries[]", "face-bitmaps.h", "readme.md"):
        assert needle in export, needle


def test_shortcut_reference_matches_the_code(window):
    article = tutorial_help.find_article("Keyboard Shortcuts")
    text = article.plain_text()
    for act in window.findChildren(QAction):
        for seq in act.shortcuts():
            label = seq.toString().replace("Return", "Enter")
            if label:
                assert label.lower() in text, f"{act.text()}: {label} missing from the shortcut reference"
    for _tool, _icon, _label, key in TOOL_DEFS:
        assert f"[[{key}]]" in article.source, f"face tool key {key} missing"
    for key in ("Insert", "Home", "End", "Tab", "Shift+Tab", "Ctrl+0", "Ctrl+I", "Ctrl+Del"):
        assert f"[[{key}]]" in article.source, key


def test_help_search_filters_articles():
    dlg = HelpDialog(None)
    assert dlg.visible_titles() == MAIN_WINDOW_ARTICLES
    dlg.search_edit.setText("onion")
    assert dlg.visible_titles() == ["Face Painter Guide", "Keyboard Shortcuts"]
    assert dlg.current_title() == "Face Painter Guide"          # selection follows the filter
    assert dlg.browser.textCursor().selectedText().lower() == "onion"
    dlg.search_edit.setText("ctrl+shift+o")                     # key chips are searchable
    assert "Keyboard Shortcuts" in dlg.visible_titles()
    dlg.search_edit.setText("zzzz-no-such-thing")
    assert dlg.visible_titles() == [] and not dlg.empty_label.isHidden()
    dlg.search_edit.clear()
    assert dlg.visible_titles() == MAIN_WINDOW_ARTICLES and dlg.empty_label.isHidden()
    dlg.search_edit.setText("zzzz")
    assert dlg.open_article("About") and dlg.current_title() == "About"   # clears a hiding filter
    assert dlg.search_edit.text() == ""
    dlg.deleteLater()
    pump()


def test_help_links_and_key_chips():
    dlg = HelpDialog(None, "Getting Started")
    dlg._on_link(QUrl("help:Export Guide"))
    assert dlg.current_title() == "Export Guide"
    image = dlg.browser.loadResource(QTextDocument.ResourceType.ImageResource, QUrl("key:Ctrl"))
    assert isinstance(image, QImage) and not image.isNull() and image.height() >= tutorial_help.KEY_H
    wide = dlg.browser.loadResource(QTextDocument.ResourceType.ImageResource, QUrl("key:Right-drag"))
    assert wide.width() > image.width()
    assert tutorial_help.split_keys("Ctrl+Shift+Z") == ["Ctrl", "Shift", "Z"]
    assert tutorial_help.split_keys("Ctrl++") == ["Ctrl", "+"]
    assert tutorial_help.split_keys("[") == ["["]
    assert dlg.tour_btn.isHidden()                              # nobody to run the tour without a main window
    dlg.deleteLater()
    pump()


def test_help_can_start_the_tour(window):
    calls = []
    window.start_tutorial = lambda: calls.append("tour")
    dlg = HelpDialog(window, "About")
    assert not dlg.tour_btn.isHidden()
    dlg.show()
    QTest.mouseClick(dlg.tour_btn, Qt.MouseButton.LeftButton)
    pump()
    assert calls == ["tour"]
    assert not dlg.isVisible()
    dlg.deleteLater()
    pump()
