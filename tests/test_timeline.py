"""Timeline editor tests: time/pose math, every edit + its undo/redo, signal
hygiene (no selection re-emission / loops), read-only built-ins, clipboard,
playback state and mouse/keyboard interaction on the canvas.

Run:  .venv\\Scripts\\python.exe -m pytest tests/test_timeline.py -q
"""

import os
import sys
from pathlib import Path

import pytest

STUDIO_DIR = Path(__file__).resolve().parent.parent
if str(STUDIO_DIR) not in sys.path:
    sys.path.insert(0, str(STUDIO_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from app.project import Frame, Sequence
from app.servo_model import SERVO_ORDER
from app.timeline import TimelineWidget, lerp_angles
from app.timeline_canvas import BLOCK_H, BLOCK_Y, RULER_H
from app.timeline_model import (
    TimeMap, fit_ppm, content_width, frames_from_text, frames_to_text,
    min_block_w, pose_at, tick_step, effective_faces,
)


def pose(value: int, **overrides) -> dict:
    d = {s: value for s in SERVO_ORDER}
    d.update(overrides)
    return d


def make_frames():
    return [
        Frame(200, pose(90), "happy", "loop"),
        Frame(300, pose(0), None, "loop"),
        Frame(100, pose(180), "sad", "once"),
        Frame(400, pose(90, R1=10), None, "loop"),
    ]


class Recorder:
    def __init__(self, tl: TimelineWidget):
        self.events = []
        tl.frameSelected.connect(lambda i: self.events.append(("selected", i)))
        tl.poseAtIndex.connect(lambda a, f, m: self.events.append(("pose", dict(a), f, m)))
        tl.sequencesChanged.connect(lambda: self.events.append(("changed",)))
        tl.editBlocked.connect(lambda msg: self.events.append(("blocked", msg)))
        tl.playbackStateChanged.connect(lambda on: self.events.append(("playing", on)))
        tl.playbackStarted.connect(lambda ms: self.events.append(("started", ms)))
        tl.frameTargetChanged.connect(lambda a: self.events.append(("target", dict(a))))
        tl.currentSequenceChanged.connect(lambda n: self.events.append(("sequence", n)))

    def names(self):
        return [e[0] for e in self.events]

    def of(self, name):
        return [e for e in self.events if e[0] == name]

    def clear(self):
        self.events.clear()


class FakeClock:
    """Stands in for the QElapsedTimers so tests control time."""

    def __init__(self):
        self.ms = 0.0

    def start(self):
        pass

    def restart(self):
        return 0

    def isValid(self):
        return True

    def elapsed(self):
        return int(self.ms)

    def nsecsElapsed(self):
        return int(self.ms * 1e6)


@pytest.fixture
def tl():
    w = TimelineWidget()
    w.resize(1200, 340)
    w.load_project_sequences({"seq": Sequence("seq", make_frames())})
    QGuiApplication.clipboard().clear()
    yield w
    w.pause()
    w.close()
    w.deleteLater()


@pytest.fixture
def shown(tl):
    tl.show()
    app.processEvents()
    return tl


def frames_of(tl):
    return tl.sequences[tl.current_seq_name].frames


# ---------------------------------------------------------------------------
# pure math
# ---------------------------------------------------------------------------

def test_lerp_angles_is_exported_and_clamps():
    a, b = {"R1": 0, "R2": 100}, {"R1": 100, "R2": 0}
    assert lerp_angles(a, b, 0.25) == {"R1": 25, "R2": 75}
    assert lerp_angles(a, b, -3) == a
    assert lerp_angles(a, b, 7) == b


def test_time_map_intervals_and_round_trip():
    frames = make_frames()
    tm = TimeMap()
    tm.rebuild(frames, 0.5)
    assert tm.total_ms == 1000
    assert tm.starts == [0, 200, 500, 600]
    assert tm.ends == [200, 500, 600, 1000]
    # (start, end] ownership: a key (block end) belongs to its own frame
    assert tm.index_at_time(0) == 0
    assert tm.index_at_time(200) == 0
    assert tm.index_at_time(200.01) == 1
    assert tm.index_at_time(500) == 1
    assert tm.index_at_time(1000) == 3
    assert tm.index_at_time(5000) == 3
    for ms in (0, 1, 199.5, 200, 350, 500, 599, 600, 999, 1000, 1500):
        assert tm.x_to_time(tm.time_to_x(ms)) == pytest.approx(ms, abs=1e-6)
    assert tm.time_to_x(200) == pytest.approx(100.0)
    assert tm.index_at_x(99.9) == 0 and tm.index_at_x(100.0) == 1
    assert tm.index_at_x(-1) is None and tm.index_at_x(tm.total_w) is None
    assert tm.nearest_key(101) == 0 and tm.nearest_key(240) == 1 and tm.nearest_key(1e6) == 3


def test_short_frames_get_a_minimum_width_and_stay_consistent():
    frames = [Frame(300, pose(90)), Frame(5, pose(0)), Frame(0, pose(10)), Frame(300, pose(90))]
    tm = TimeMap()
    tm.rebuild(frames, 0.2)
    mw = min_block_w(0.2)
    assert tm.xs[2] - tm.xs[1] == pytest.approx(mw)      # 5 ms would be 1 px
    assert tm.xs[3] - tm.xs[2] == pytest.approx(mw)      # 0 ms counts as 1 ms
    assert tm.total_ms == 300 + 5 + 1 + 300
    # the playhead map follows the stretched blocks exactly
    assert tm.time_to_x(305) == pytest.approx(tm.xs[2])
    assert tm.x_to_time((tm.xs[1] + tm.xs[2]) / 2) == pytest.approx(302.5)


def test_pose_at_time_interpolates_into_each_frame():
    frames = make_frames()
    tm = TimeMap()
    tm.rebuild(frames, 0.1)
    angles, i = pose_at(frames, tm, 0)
    assert i == 0 and angles == pose(90)
    angles, i = pose_at(frames, tm, 150)          # first frame holds its pose
    assert i == 0 and angles == pose(90)
    angles, i = pose_at(frames, tm, 350)          # halfway 90 -> 0
    assert i == 1 and angles["R1"] == pytest.approx(45)
    angles, i = pose_at(frames, tm, 500)          # exactly on frame 2's key
    assert i == 1 and angles == pose(0)
    angles, i = pose_at(frames, tm, 525)          # 25% of 0 -> 180
    assert i == 2 and angles["L4"] == pytest.approx(45)
    angles, i = pose_at(frames, tm, 99999)
    assert i == 3 and angles == frames[3].angles
    assert pose_at([], TimeMap(), 10) == ({}, -1)


def test_ruler_and_fit_helpers():
    assert tick_step(1 / 0.12) == 1000      # default zoom: 1 s labels
    assert tick_step(0.25) == 50
    assert tick_step(1e9) == 600000
    durs = [300] * 12
    ppm = fit_ppm(durs, 900)
    assert content_width(durs, ppm) <= 900 + 1e-6
    assert content_width(durs, ppm * 1.02) > 900
    many = [100] * 400
    assert content_width(many, fit_ppm(many, 1300)) <= 1300 + 1e-6
    assert fit_ppm([200], 2000) <= 0.6     # a lone frame isn't blown up to fill the view


def test_effective_faces_carry_forward():
    eff = effective_faces(make_frames())
    assert [e[0] for e in eff] == ["happy", "happy", "sad", "sad"]
    assert eff[3][1] == "once"


# ---------------------------------------------------------------------------
# selection + signal hygiene
# ---------------------------------------------------------------------------

def test_load_selects_first_frame_with_playhead_at_start(tl):
    assert tl.current_seq_name == "seq"
    assert tl.selected_index == 0 and tl.selected_indices() == [0]
    assert tl.current_time_ms() == 0
    assert tl.total_time_ms() == 1000
    assert not tl.current_is_builtin()
    assert tl.undo_stack.count() == 0


def test_select_frame_parks_playhead_on_key_and_announces(tl):
    rec = Recorder(tl)
    tl.select_frame(2)
    assert tl.selected_index == 2
    assert tl.current_time_ms() == 600          # END of block 3: its pose is reached there
    assert rec.names() == ["selected", "pose", "target"]
    assert rec.events[0] == ("selected", 2)
    assert rec.events[1][1] == pose(180) and rec.events[1][2] == "sad"
    assert rec.events[2][1] == pose(180)
    tl.select_frame(99)                         # out of range: ignored
    assert tl.selected_index == 2


def test_angle_edit_is_silent_and_local(tl):
    tl.select_frame(1)
    rec = Recorder(tl)
    tl.update_selected_frame_angles(pose(33))
    assert frames_of(tl)[1].angles == pose(33)
    assert rec.names() == ["changed"]           # no frameSelected / poseAtIndex -> no loop
    rec.clear()
    tl.update_selected_frame_angles(pose(33))   # no-op edit: nothing at all
    assert rec.events == [] and tl.undo_stack.count() == 1


def test_angle_edit_does_not_rebuild_layout(tl):
    tl.select_frame(1)
    tm_before = (tl.canvas.tm.xs, tl.canvas.tm.ends)
    calls = []
    orig = tl.canvas.relayout
    tl.canvas.relayout = lambda *a, **k: (calls.append(1), orig(*a, **k))
    for v in range(60):
        tl.update_selected_frame_angles(pose(v))
    assert calls == []
    assert (tl.canvas.tm.xs, tl.canvas.tm.ends) == tm_before   # same list objects
    assert tl.canvas._masks[1] == 0xFF and tl.canvas._masks[2] == 0xFF


def test_slider_drag_merges_into_one_undo_step(tl):
    tl.select_frame(1)
    for v in range(1, 61):
        tl.update_selected_frame_angles(pose(v))
    assert tl.undo_stack.count() == 1
    assert frames_of(tl)[1].angles == pose(60)
    rec = Recorder(tl)
    tl.undo_stack.undo()
    assert frames_of(tl)[1].angles == pose(0)
    assert ("selected", 1) in rec.events and ("changed",) in rec.events
    tl.undo_stack.redo()
    assert frames_of(tl)[1].angles == pose(60)
    assert not tl.undo_stack.canRedo()


def test_edits_far_apart_in_time_are_separate_steps(tl):
    clock = FakeClock()
    tl._edit_clock = clock
    tl.select_frame(1)
    tl.update_selected_frame_angles(pose(10))
    clock.ms = 100
    tl.update_selected_frame_angles(pose(20))       # within the 400 ms window: merged
    clock.ms = 900
    tl.update_selected_frame_angles(pose(30))       # new gesture
    assert tl.undo_stack.count() == 2
    tl.undo_stack.undo()
    assert frames_of(tl)[1].angles == pose(20)
    tl.undo_stack.undo()
    assert frames_of(tl)[1].angles == pose(0)


def test_begin_end_edit_brackets_one_step_each(tl):
    clock = FakeClock()
    tl._edit_clock = clock
    tl.select_frame(1)
    tl.begin_edit("Pose Limb")
    for v in (5, 6, 7):
        clock.ms += 1000                              # slow drag still merges inside a bracket
        tl.update_selected_frame_angles(pose(v))
    tl.end_edit()
    tl.begin_edit("Pose Limb")
    tl.update_selected_frame_angles(pose(50))         # immediately after, but a new bracket
    tl.end_edit()
    assert tl.undo_stack.count() == 2
    assert tl.undo_stack.undoText() == "Pose Limb"
    tl.undo_stack.undo()
    assert frames_of(tl)[1].angles == pose(7)


def test_edits_of_different_frames_never_merge(tl):
    tl.select_frame(1)
    tl.update_selected_frame_angles(pose(11))
    tl.select_frame(2)
    tl.update_selected_frame_angles(pose(22))
    assert tl.undo_stack.count() == 2


def test_field_edit_retimes_and_keeps_playhead_on_key(tl):
    tl.select_frame(1)
    rec = Recorder(tl)
    tl.update_selected_frame_fields(450, "wave", "boomerang")
    f = frames_of(tl)[1]
    assert (f.delay_ms, f.face, f.face_mode) == (450, "wave", "boomerang")
    assert tl.total_time_ms() == 1150
    assert tl.current_time_ms() == 650               # still on frame 2's key
    assert rec.names() == ["changed"]
    assert tl.canvas._eff[1] == ("wave", "boomerang")
    tl.undo_stack.undo()
    assert (f.delay_ms, f.face, f.face_mode) == (300, None, "loop")
    assert tl.total_time_ms() == 1000 and tl.current_time_ms() == 500
    assert tl.canvas._eff[1][0] == "happy"


def test_spinbox_style_delay_edits_merge(tl):
    tl.select_frame(1)
    for d in range(301, 340):
        tl.update_selected_frame_fields(d, None, "loop")
    assert tl.undo_stack.count() == 1
    tl.undo_stack.undo()
    assert frames_of(tl)[1].delay_ms == 300


# ---------------------------------------------------------------------------
# structural edits
# ---------------------------------------------------------------------------

def test_add_frame_inserts_copy_after_selection(tl):
    tl.select_frame(1)
    rec = Recorder(tl)
    tl.add_frame()
    frames = frames_of(tl)
    assert len(frames) == 5
    assert frames[2].angles == frames[1].angles and frames[2] is not frames[1]
    assert frames[2].angles is not frames[1].angles
    assert tl.selected_index == 2 and ("selected", 2) in rec.events
    assert tl.current_time_ms() == 800               # new frame's key
    tl.undo_stack.undo()
    assert len(frames_of(tl)) == 4 and tl.selected_index == 1
    tl.undo_stack.redo()
    assert len(frames_of(tl)) == 5 and tl.selected_index == 2


def test_add_frame_alias_and_append_when_nothing_selected(tl):
    tl._apply_selection([], None)
    assert tl.selected_index is None
    tl._add_frame_from_current()
    frames = frames_of(tl)
    assert len(frames) == 5 and frames[4].angles == frames[3].angles
    assert tl.selected_index == 4


def test_insert_blank_and_duplicate(tl):
    tl.insert_blank(0)
    assert frames_of(tl)[0].angles == pose(90) and frames_of(tl)[0].delay_ms == 200
    assert tl.selected_index == 0 and len(frames_of(tl)) == 5
    tl.undo_stack.undo()
    tl._apply_selection([0, 2], 2)
    tl.duplicate_selected()
    frames = frames_of(tl)
    assert len(frames) == 6
    assert frames[3].angles == pose(90) and frames[4].angles == pose(180)
    assert tl.selected_indices() == [3, 4]
    tl.undo_stack.undo()
    assert len(frames_of(tl)) == 4 and tl.selected_indices() == [0, 2]
    tl.duplicate_frame(3)
    assert len(frames_of(tl)) == 5 and tl.selected_index == 4


def test_delete_selected_and_undo_restores_positions(tl):
    originals = list(frames_of(tl))
    tl._apply_selection([0, 2], 2)
    tl.delete_selected()
    assert frames_of(tl) == [originals[1], originals[3]]
    assert tl.selected_indices() == [0]
    tl.undo_stack.undo()
    assert [id(f) for f in frames_of(tl)] == [id(f) for f in originals]
    assert tl.selected_indices() == [0, 2]


def test_cannot_delete_every_frame(tl):
    rec = Recorder(tl)
    tl.select_all()
    assert tl.selected_indices() == [0, 1, 2, 3]
    tl.delete_selected()
    assert len(frames_of(tl)) == 4
    assert len(rec.of("blocked")) == 1
    assert tl.undo_stack.count() == 0


def test_move_frames_and_reorder(tl):
    a, b, c, d = frames_of(tl)
    tl.select_frame(0)
    tl.move_frames([0], 3)                       # drop frame 1 before frame 4
    assert frames_of(tl) == [b, c, a, d] and tl.selected_index == 2
    tl.undo_stack.undo()
    assert frames_of(tl) == [a, b, c, d] and tl.selected_index == 0
    tl._apply_selection([1, 3], 3)
    tl.move_frames([1, 3], 0)                    # group keeps its order
    assert frames_of(tl) == [b, d, a, c] and tl.selected_indices() == [0, 1]
    assert tl.selected_index == 1
    tl.undo_stack.undo()
    count = tl.undo_stack.count()
    tl.move_frames([1], 1)                       # dropping where it already is: no step
    tl.move_frames([1], 2)
    assert tl.undo_stack.index() == 0 and tl.undo_stack.count() == count
    tl.reorder_frame(0, 2)                       # old API: final index
    assert frames_of(tl) == [b, c, a, d]
    tl.reorder_frame(2, 0)
    assert frames_of(tl) == [a, b, c, d]


def test_retime_frame_and_drag_protocol(tl):
    tl.retime_frame(1, 120)
    assert frames_of(tl)[1].delay_ms == 120 and tl.total_time_ms() == 820
    tl.undo_stack.undo()
    assert frames_of(tl)[1].delay_ms == 300

    rec = Recorder(tl)
    orig = tl._retime_begin(2)
    assert orig == {2: 100} and tl.selected_index == 2
    rec.clear()
    for d in (110, 150, 240):
        tl._retime_preview({2: d})
    assert rec.of("changed") == [] and tl.undo_stack.index() == 0   # nothing committed mid-drag
    assert tl.current_time_ms() == 740                              # playhead rides the key
    tl._retime_finish(orig, commit=True)
    assert tl.undo_stack.index() == 1 and len(rec.of("changed")) == 1
    assert rec.of("selected") == [("selected", 2)]                  # inspector reload, once
    tl.undo_stack.undo()
    assert frames_of(tl)[2].delay_ms == 100

    orig = tl._retime_begin(2)
    tl._retime_preview({2: 999})
    tl._retime_finish(orig, commit=False)                           # Esc
    assert frames_of(tl)[2].delay_ms == 100 and tl.total_time_ms() == 1000


def test_retime_applies_to_whole_selection(tl):
    tl._apply_selection([0, 1], 1)
    orig = tl._retime_begin(1)
    assert orig == {0: 200, 1: 300}
    tl._retime_preview({0: 250, 1: 350})
    tl._retime_finish(orig, commit=True)
    assert [f.delay_ms for f in frames_of(tl)] == [250, 350, 100, 400]
    tl.undo_stack.undo()
    assert [f.delay_ms for f in frames_of(tl)] == [200, 300, 100, 400]


# ---------------------------------------------------------------------------
# clipboard
# ---------------------------------------------------------------------------

def test_clipboard_text_round_trip():
    frames = make_frames()
    text = frames_to_text(frames)
    back = frames_from_text(text)
    assert [f.to_dict() for f in back] == [f.to_dict() for f in frames]
    assert frames_from_text("hello") is None
    assert frames_from_text('{"type": "sesame-studio/frames", "frames": "x"}') is None
    assert frames_from_text("") is None


def test_copy_paste_after_selection(tl):
    tl._apply_selection([1, 2], 2)
    tl.copy()
    assert "sesame-studio/frames" in QGuiApplication.clipboard().text()
    tl.select_frame(3)
    tl.paste()
    frames = frames_of(tl)
    assert len(frames) == 6
    assert frames[4].angles == pose(0) and frames[5].angles == pose(180)
    assert frames[5].face == "sad"
    assert tl.selected_indices() == [4, 5]
    frames[4].angles["R1"] = 77
    assert frames[1].angles["R1"] == 0               # pasted frames are independent copies
    tl.undo_stack.undo()
    assert len(frames_of(tl)) == 4 and tl.selected_indices() == [3]


def test_cut_then_paste_moves_frames(tl):
    tl.select_frame(0)
    tl.cut()
    assert len(frames_of(tl)) == 3 and frames_of(tl)[0].angles == pose(0)
    tl.select_frame(2)
    tl.paste()
    assert len(frames_of(tl)) == 4 and frames_of(tl)[3].face == "happy"
    tl.undo_stack.undo()
    tl.undo_stack.undo()
    assert [f.delay_ms for f in frames_of(tl)] == [200, 300, 100, 400]


def test_paste_falls_back_to_internal_clipboard(tl):
    tl.select_frame(1)
    tl.copy()
    QGuiApplication.clipboard().setText("something else entirely")
    assert tl.can_paste()
    tl.paste()
    assert len(frames_of(tl)) == 5 and frames_of(tl)[2].angles == pose(0)


# ---------------------------------------------------------------------------
# built-in (read-only) sequences + sequence management
# ---------------------------------------------------------------------------

def test_builtin_is_read_only_and_reports_once(tl):
    rec = Recorder(tl)
    assert tl.set_current_sequence("dance")
    assert tl.current_is_builtin()
    assert ("sequence", "dance") in rec.events
    assert not tl.readonly_banner.isHidden()
    assert not tl.add_frame_btn.isEnabled() and not tl.rename_seq_btn.isEnabled()
    builtin = tl.current_sequence()
    snapshot = [f.to_dict() for f in builtin.frames]
    rec.clear()
    tl.select_frame(2)
    for v in range(40):                               # a slider drag on a read-only frame
        tl.update_selected_frame_angles(pose(v))
    tl.update_selected_frame_fields(999, "x", "loop")
    tl.add_frame()
    tl.delete_selected()
    tl.duplicate_selected()
    tl.insert_blank(0)
    tl.move_frames([0], 3)
    tl.retime_frame(0, 50)
    tl.paste()
    tl.cut()
    assert tl._retime_begin(1) is None
    assert not tl.rename_sequence("nope") and not tl.delete_sequence()
    assert [f.to_dict() for f in builtin.frames] == snapshot
    assert len(rec.of("blocked")) == 1                # throttled: one toast, not dozens
    assert rec.of("changed") == [] and tl.undo_stack.count() == 0
    tl.copy()                                         # copying from a built-in is fine
    assert tl.can_paste()


def test_duplicate_builtin_makes_editable_copy(tl):
    tl.set_current_sequence("wave")
    rec = Recorder(tl)
    name = tl.duplicate_sequence()
    assert name == "wave_custom" and tl.current_seq_name == "wave_custom"
    assert not tl.current_is_builtin() and tl.readonly_banner.isHidden()
    assert ("changed",) in rec.events and ("sequence", "wave_custom") in rec.events
    copy = tl.sequences["wave_custom"]
    builtin = tl.builtin_sequences["wave"]
    assert [f.to_dict() for f in copy.frames] == [f.to_dict() for f in builtin.frames]
    tl.select_frame(1)
    tl.update_selected_frame_angles(pose(1))
    assert builtin.frames[1].angles != pose(1)        # the firmware template is untouched
    tl.undo_stack.undo()
    tl.undo_stack.undo()                              # undo the duplication itself
    assert "wave_custom" not in tl.sequences and tl.current_seq_name == "wave"
    assert tl.duplicate_sequence() == "wave_custom"
    tl.set_current_sequence("wave")
    assert tl.duplicate_sequence() == "wave_custom_2"


def test_sequence_new_rename_delete_duplicate_undo(tl):
    store = tl.sequences
    assert tl.new_sequence("alpha") == "alpha"
    assert tl.current_seq_name == "alpha" and len(store["alpha"].frames) == 1
    assert tl.new_sequence("alpha") is None and tl.new_sequence("dance") is None   # taken
    assert tl.rename_sequence("beta")
    assert "alpha" not in store and store["beta"].name == "beta"
    assert tl.current_seq_name == "beta" and tl.sequences is store
    assert tl.duplicate_sequence() == "beta_copy"
    assert tl.delete_sequence()
    assert "beta_copy" not in store and tl.current_seq_name in store
    order_before_delete = ["seq", "beta", "beta_copy"]
    tl.undo_stack.undo()                                # un-delete
    assert list(store) == order_before_delete and tl.current_seq_name == "beta_copy"
    tl.undo_stack.undo()                                # un-duplicate
    tl.undo_stack.undo()                                # un-rename
    assert list(store) == ["seq", "alpha"] and store["alpha"].name == "alpha"
    assert tl.current_seq_name == "alpha"
    tl.undo_stack.undo()                                # un-create
    assert list(store) == ["seq"] and tl.current_seq_name == "seq"
    for _ in range(4):
        tl.undo_stack.redo()
    assert sorted(store) == ["beta", "seq"]
    rec = Recorder(tl)
    tl.set_current_sequence("seq")
    assert tl.delete_sequence()
    assert not tl.delete_sequence()                     # the last custom sequence stays
    assert len(rec.of("blocked")) == 1 and list(store) == ["beta"]


def test_undo_of_edit_in_other_sequence_switches_to_it(tl):
    tl.select_frame(1)
    tl.update_selected_frame_angles(pose(5))
    tl.new_sequence("other")
    tl.set_current_sequence("dance")
    tl.undo_stack.undo()                                # un-create "other"
    tl.undo_stack.undo()                                # the pose edit lives in "seq"
    assert tl.current_seq_name == "seq" and tl.selected_index == 1
    assert frames_of(tl)[1].angles == pose(0)
    assert tl.seq_combo.currentData() == "seq"


def test_load_project_sequences_clears_history_and_keeps_dict(tl):
    tl.select_frame(1)
    tl.update_selected_frame_angles(pose(5))
    assert tl.undo_stack.count() == 1
    new = {"fresh": Sequence("fresh", [Frame(), Frame(50)])}
    tl.load_project_sequences(new)
    assert tl.undo_stack.count() == 0
    assert tl.sequences is new and tl.current_seq_name == "fresh"
    tl.add_frame()
    assert len(new["fresh"].frames) == 3                # mutated in place


def test_combo_groups_custom_and_builtin(tl):
    combo = tl.seq_combo
    texts = [combo.itemText(i) for i in range(combo.count())]
    assert texts[0] == "MY SEQUENCES" and texts[1] == "seq"
    assert any(t.startswith("BUILT-IN") for t in texts)
    assert combo.itemData(0) is None
    assert not (combo.model().item(0).flags() & Qt.ItemIsSelectable)
    assert combo.findData("dance") > combo.findData("seq") > 0
    idx = combo.findData("dance")                       # old scripts drive the combo directly
    combo.setCurrentIndex(idx)
    assert tl.current_seq_name == "dance"


# ---------------------------------------------------------------------------
# playback / scrubbing
# ---------------------------------------------------------------------------

def test_play_pause_stop_state_and_signals(tl):
    rec = Recorder(tl)
    assert not tl.is_playing()
    tl.play()
    assert tl.is_playing() and tl._play_timer.isActive()
    assert rec.of("playing") == [("playing", True)]
    assert rec.of("started") == [("started", 0.0)]
    assert rec.of("target")[0][1] == pose(90)
    tl.play()                                           # idempotent
    assert len(rec.of("playing")) == 1
    tl.pause()
    assert not tl.is_playing() and not tl._play_timer.isActive()
    assert rec.of("playing")[-1] == ("playing", False)
    tl.toggle_playback()
    assert tl.is_playing()
    tl.seek_ms(640)
    tl.stop()
    assert not tl.is_playing() and tl.current_time_ms() == 0


def test_playback_uses_elapsed_time_and_resumes_from_playhead(tl):
    clock = FakeClock()
    tl._play_clock = clock
    tl.set_loop(False)
    tl.seek_ms(250)
    rec = Recorder(tl)
    tl.play()
    assert rec.of("started") == [("started", 250.0)]     # resumes, doesn't restart
    clock.ms = 100
    tl._tick()
    assert tl.current_time_ms() == pytest.approx(350)
    assert rec.of("pose")[-1][1]["R1"] == pytest.approx(45)
    tl.set_speed(2.0)
    clock.ms = 100
    tl._tick()
    assert tl.current_time_ms() == pytest.approx(550)    # 100 ms real time at 2x
    targets = [e[1] for e in rec.of("target")]
    assert targets == [pose(0), pose(180)]               # frame 2 at start, then entered frame 3
    clock.ms = 240
    tl._tick()                                           # runs past the end with loop off
    assert tl.current_time_ms() == 1000 and not tl.is_playing()
    rec.clear()
    tl.play()                                            # at the end: starts over
    assert rec.of("started") == [("started", 0.0)]


def test_loop_wraps_and_long_stalls_are_clamped(tl):
    clock = FakeClock()
    tl._play_clock = clock
    assert tl.loop_enabled()
    tl.seek_ms(950)
    tl.play()
    clock.ms = 100
    tl._tick()
    assert tl.is_playing() and tl.current_time_ms() == pytest.approx(50)
    clock.ms = 60000                                     # event loop stalled for a minute
    tl._tick()
    assert tl.current_time_ms() == pytest.approx(300)    # advanced by the 250 ms cap only


def test_face_is_announced_only_when_it_changes(tl):
    clock = FakeClock()
    tl._play_clock = clock
    tl.set_loop(False)
    tl.stop()
    rec = Recorder(tl)
    tl.play()
    for _ in range(18):
        clock.ms = 50
        tl._tick()
    faces = [e[2] for e in rec.of("pose")]
    assert faces[0] == "happy"                           # forced at play start
    assert [f for f in faces if f is not None] == ["happy", "sad"]
    assert faces.count(None) == len(faces) - 2


def test_scrub_and_seek_emit_pose_and_targets_without_selecting(tl):
    rec = Recorder(tl)
    tl.seek_ms(350)
    assert rec.names() == ["pose", "target"]
    assert rec.events[0][1]["R2"] == pytest.approx(45)
    rec.clear()
    tl.seek_ms(360)
    assert rec.names() == ["pose"]                       # same frame: no new target
    tl.seek_ms(1e9)
    assert tl.current_time_ms() == 1000
    assert tl.selected_index == 0                        # selection untouched
    assert tl.playhead_frame_index() == 3
    tl._scrub_to_x(tl._time_to_x(500))                   # old helper names still work
    assert tl.current_time_ms() == pytest.approx(500)
    assert tl._total_time_ms == 1000


def test_step_and_home_end(tl):
    tl.seek_ms(350)                                      # mid frame 2
    tl.step(1)
    assert tl.selected_index == 1 and tl.current_time_ms() == 500
    tl.step(1)
    assert tl.selected_index == 2
    tl.step(-1)
    assert tl.selected_index == 1
    tl.step(1, extend=True)
    tl.step(1, extend=True)
    assert tl.selected_indices() == [1, 2, 3] and tl.selected_index == 3
    tl._select_first()
    assert tl.selected_indices() == [0]
    tl._select_last()
    assert tl.selected_index == 3 and tl.current_time_ms() == 1000


# ---------------------------------------------------------------------------
# view: zoom / fit / paint
# ---------------------------------------------------------------------------

def test_zoom_keeps_time_under_cursor_and_fit_fits(shown):
    tl = shown
    canvas = tl.canvas
    tl.zoom_fit()
    assert canvas.tm.total_w <= canvas.width()
    x = 200.0
    t = canvas.widget_x_to_time(x)
    canvas.zoom_by(2.5, x)
    assert canvas.scroll_x > 0
    assert canvas.widget_x_to_time(x) == pytest.approx(t, abs=0.5)
    canvas.zoom_by(1.2, x)
    assert canvas.widget_x_to_time(x) == pytest.approx(t, abs=0.5)
    assert tl.hbar.maximum() > 0 and tl.hbar.value() == round(canvas.scroll_x)
    ppm = canvas.ppm
    tl.select_frame(2)
    tl.update_selected_frame_fields(900, None, "loop")   # edits don't reset the zoom
    assert canvas.ppm == ppm
    tl.set_current_sequence("dance")
    tl.set_current_sequence("seq")                       # per-sequence zoom survives switching
    assert canvas.ppm == pytest.approx(ppm)
    tl.zoom_fit()
    assert canvas.scroll_x == 0


def test_paint_smoke_all_states(shown):
    tl = shown
    for name in ("seq", "dance", "walk_forward"):
        tl.set_current_sequence(name)
        tl._apply_selection([0, 1], 1)
        assert not tl.grab().isNull()
    tl.set_tracks_visible(False)
    assert not tl.tracks_visible() and not tl.grab().isNull()
    tl.set_tracks_visible(True)
    tl.load_project_sequences({})                        # only built-ins left
    assert tl.current_seq_name == "rest" and not tl.grab().isNull()
    tl.builtin_sequences = {}
    tl.load_project_sequences({})                        # nothing at all
    assert tl.current_sequence() is None and not tl.grab().isNull()
    tl.play()
    assert not tl.is_playing()
    tl.add_frame()
    tl.delete_selected()


# ---------------------------------------------------------------------------
# mouse + keyboard on the canvas
# ---------------------------------------------------------------------------

def block_center(tl, i):
    c = tl.canvas
    x = c._ox() + (c.tm.xs[i] + c.tm.xs[i + 1]) / 2
    return QPoint(int(x), BLOCK_Y + BLOCK_H // 2)


def block_edge(tl, i):
    c = tl.canvas
    return QPoint(int(c._ox() + c.tm.xs[i + 1]) - 2, BLOCK_Y + BLOCK_H // 2)


def drag(canvas, start: QPoint, end: QPoint, modifiers=Qt.NoModifier, release=True):
    QTest.mousePress(canvas, Qt.LeftButton, modifiers, start)
    steps = 8
    for k in range(1, steps + 1):
        p = QPoint(start.x() + (end.x() - start.x()) * k // steps,
                   start.y() + (end.y() - start.y()) * k // steps)
        QTest.mouseMove(canvas, p)
        app.processEvents()
    if release:
        QTest.mouseRelease(canvas, Qt.LeftButton, modifiers, end)


def test_click_ctrl_click_shift_click(shown):
    tl = shown
    c = tl.canvas
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, block_center(tl, 2))
    assert tl.selected_indices() == [2] and tl.current_time_ms() == 600
    QTest.mouseClick(c, Qt.LeftButton, Qt.ControlModifier, block_center(tl, 0))
    assert tl.selected_indices() == [0, 2] and tl.selected_index == 0
    QTest.mouseClick(c, Qt.LeftButton, Qt.ControlModifier, block_center(tl, 0))
    assert tl.selected_indices() == [2] and tl.selected_index == 2
    QTest.mouseClick(c, Qt.LeftButton, Qt.ShiftModifier, block_center(tl, 0))
    assert tl.selected_indices() == [0, 1, 2] and tl.selected_index == 0
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, block_center(tl, 1))   # collapses on release
    assert tl.selected_indices() == [1]
    assert tl.undo_stack.count() == 0


def test_drag_edge_retimes_with_snapping_as_one_undo_step(shown):
    tl = shown
    c = tl.canvas
    start = block_edge(tl, 1)
    assert c._hit(start.x(), start.y()) == ("edge", 1)
    dx = int(137 * c.ppm)                                # +137 ms worth of pixels
    rec = Recorder(tl)
    drag(c, start, QPoint(start.x() + dx, start.y()))
    new = frames_of(tl)[1].delay_ms
    assert new != 300 and new % 10 == 0                  # snapped to 10 ms
    assert abs(new - 437) <= 10
    assert tl.undo_stack.count() == 1 and len(rec.of("changed")) == 1
    tl.undo_stack.undo()
    assert frames_of(tl)[1].delay_ms == 300


def test_drag_block_reorders_and_escape_cancels(shown):
    tl = shown
    c = tl.canvas
    a, b, cc, d = frames_of(tl)
    start = block_center(tl, 0)
    end = QPoint(int(c._ox() + c.tm.xs[3]) - 3, start.y())       # just before frame 4's left edge
    drag(c, start, end)
    assert frames_of(tl) == [b, cc, a, d]
    assert tl.selected_index == 2 and tl.undo_stack.count() == 1
    tl.undo_stack.undo()
    assert frames_of(tl) == [a, b, cc, d]

    drag(c, block_center(tl, 0), QPoint(int(c._ox() + c.tm.xs[3]) - 3, start.y()), release=False)
    assert c.is_dragging() and c._drag.gap == 3
    QTest.keyClick(c, Qt.Key_Escape)
    assert not c.is_dragging()
    QTest.mouseRelease(c, Qt.LeftButton, Qt.NoModifier, end)
    assert frames_of(tl) == [a, b, cc, d] and tl.undo_stack.index() == 0

    start = block_edge(tl, 2)                                      # Esc during a retime drag
    drag(c, start, QPoint(start.x() + 80, start.y()), release=False)
    assert frames_of(tl)[2].delay_ms != 100
    QTest.keyClick(c, Qt.Key_Escape)
    assert frames_of(tl)[2].delay_ms == 100 and tl.undo_stack.index() == 0
    QTest.mouseRelease(c, Qt.LeftButton, Qt.NoModifier, start)


def test_ruler_scrub_snaps_to_keys_and_plus_button_adds(shown):
    tl = shown
    c = tl.canvas
    key_x = int(c._ox() + c.tm.xs[2])                              # frame 2's key (t = 500)
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, QPoint(key_x + 3, RULER_H // 2))
    assert tl.current_time_ms() == 500
    assert tl.selected_index == 0                                  # scrubbing doesn't select
    ax, ay, aw, ah = c._add_rect(c._ox())
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, QPoint(ax + aw // 2, ay + ah // 2))
    assert len(frames_of(tl)) == 5 and tl.selected_index == 1      # copy of the selected frame, after it


def test_readonly_drag_is_blocked_once(shown):
    tl = shown
    tl.set_current_sequence("dance")
    c = tl.canvas
    rec = Recorder(tl)
    edge = block_edge(tl, 1)
    assert c._hit(edge.x(), edge.y())[0] == "block"                # no retime handle on built-ins
    drag(c, block_center(tl, 1), block_center(tl, 5))
    assert len(rec.of("blocked")) == 1 and tl.undo_stack.count() == 0
    assert tl.selected_index == 1


def key(tl, k, mods=Qt.NoModifier):
    ev = QKeyEvent(QEvent.KeyPress, k, mods)
    return tl.handle_key(ev)


def test_keyboard_shortcuts(shown):
    tl = shown
    tl.select_frame(1)
    assert key(tl, Qt.Key_D, Qt.ControlModifier)
    assert len(frames_of(tl)) == 5 and tl.selected_index == 2
    assert key(tl, Qt.Key_Delete)
    assert len(frames_of(tl)) == 4
    assert key(tl, Qt.Key_Z, Qt.ControlModifier) and len(frames_of(tl)) == 5
    assert key(tl, Qt.Key_Y, Qt.ControlModifier) and len(frames_of(tl)) == 4
    assert key(tl, Qt.Key_Z, Qt.ControlModifier | Qt.ShiftModifier) is True
    assert key(tl, Qt.Key_A, Qt.ControlModifier) and len(tl.selected_indices()) == 4
    assert key(tl, Qt.Key_Escape) and len(tl.selected_indices()) == 1
    assert not key(tl, Qt.Key_Escape)                              # nothing to cancel: not ours
    assert key(tl, Qt.Key_C, Qt.ControlModifier) and key(tl, Qt.Key_V, Qt.ControlModifier)
    assert len(frames_of(tl)) == 5
    assert key(tl, Qt.Key_X, Qt.ControlModifier) and len(frames_of(tl)) == 4
    assert key(tl, Qt.Key_Home) and tl.selected_index == 0
    assert key(tl, Qt.Key_Right) and tl.selected_index == 1
    assert key(tl, Qt.Key_Right, Qt.ShiftModifier) and tl.selected_indices() == [1, 2]
    assert key(tl, Qt.Key_Left) and tl.selected_indices() == [1]
    assert key(tl, Qt.Key_End) and tl.selected_index == 3
    ppm = tl.canvas.ppm
    assert key(tl, Qt.Key_Plus) and tl.canvas.ppm > ppm
    assert key(tl, Qt.Key_Minus) and key(tl, Qt.Key_F)
    assert key(tl, Qt.Key_Space) and tl.is_playing()
    assert key(tl, Qt.Key_Space) and not tl.is_playing()
    assert key(tl, Qt.Key_L) and not tl.loop_enabled()
    assert key(tl, Qt.Key_T) and not tl.tracks_visible()
    assert key(tl, Qt.Key_T) and tl.tracks_visible()
    n = len(frames_of(tl))
    assert key(tl, Qt.Key_Insert) and len(frames_of(tl)) == n + 1
    assert not key(tl, Qt.Key_Q)
    assert not key(tl, Qt.Key_S, Qt.ControlModifier)               # left for the main window


def test_context_menus_build(tl):
    tl.select_frame(1)
    menu = tl.build_context_menu(1)
    labels = [a.text().split("\t")[0] for a in menu.actions() if not a.isSeparator()]
    assert labels == ["Add Frame After (copy)", "Duplicate", "Cut", "Copy", "Paste After",
                      "Insert Blank Before", "Insert Blank After", "Delete"]
    next(a for a in menu.actions() if a.text().startswith("Insert Blank Before")).trigger()
    assert len(frames_of(tl)) == 5 and tl.selected_index == 1
    empty = tl.build_context_menu(None)
    assert any(a.text().startswith("Zoom to Fit") for a in empty.actions())
    tl.set_current_sequence("dance")
    ro = tl.build_context_menu(0)
    enabled = {a.text().split("\t")[0]: a.isEnabled() for a in ro.actions() if not a.isSeparator()}
    assert enabled["Copy"] and not enabled["Delete"] and not enabled["Duplicate"]
    assert enabled["Duplicate Sequence to Edit"]


def test_strip_rerender_matches_full_render(shown):
    """Angle edits re-render only a strip of the cached picture; the result
    must be pixel-identical to rendering everything from scratch."""
    tl = shown
    frames = [Frame(60 + (i % 5) * 45, pose((i * 37) % 181, R1=(i * 11) % 181),
                    "walk" if i % 6 == 0 else None) for i in range(40)]
    tl.load_project_sequences({"long": Sequence("long", frames)})
    c = tl.canvas
    c.set_zoom(0.25)
    for index, scroll in ((3, 0), (12, 300), (0, 0), (39, 1e9), (20, 0)):
        c.set_scroll(scroll)
        tl.select_frame(index)
        c.repaint()
        assert c._dirty_span is None and not c._cache_dirty
        tl.update_selected_frame_angles(pose((index * 53) % 181, L3=7, R4=170))
        on_screen = c._visible_range()[0] <= index <= c._visible_range()[1]
        assert (c._dirty_span is not None) == on_screen and not c._cache_dirty
        c.repaint()
        partial = c._cache.toImage().copy()
        c.render_static()
        assert partial == c._cache.toImage(), f"strip render differs for frame {index}"


def test_empty_sequence_and_external_mutation_are_survivable(shown):
    tl = shown
    empty = Sequence("empty", [])
    tl.load_project_sequences({"empty": empty})
    assert tl.selected_index is None and tl.total_time_ms() == 0
    assert not tl.grab().isNull()
    tl.add_frame()                                       # first frame of an empty sequence
    assert len(empty.frames) == 1 and tl.selected_index == 0
    # code outside the timeline appends/removes frames directly...
    empty.frames.extend([Frame(100, pose(10)), Frame(100, pose(20))])
    assert not tl.grab().isNull()                        # ...painting self-heals
    assert tl.canvas.tm.n == 3
    del empty.frames[1:]
    tl._selection = {0, 2}
    tl.selected_index = 2
    tl.refresh()                                         # ...and refresh() re-validates
    assert tl.selected_indices() == [0] and tl.selected_index == 0
    assert tl.total_time_ms() == 200 and not tl.grab().isNull()


def test_reentrant_edits_from_change_notifications_are_ignored(tl):
    """A slot that edits the timeline in response to the timeline's own
    signals (a sloppy inspector) must not push commands inside push()/undo()."""
    tl.select_frame(1)
    tl.frameSelected.connect(lambda i: tl.update_selected_frame_angles(pose(123)))
    tl.sequencesChanged.connect(lambda: tl.update_selected_frame_fields(777, "x", "loop"))
    tl.add_frame()
    assert tl.undo_stack.count() == 1 and len(frames_of(tl)) == 5
    assert frames_of(tl)[2].angles == pose(0) and frames_of(tl)[2].delay_ms == 300
    tl.undo_stack.undo()
    assert len(frames_of(tl)) == 4 and tl.undo_stack.count() == 1
    assert [f.delay_ms for f in frames_of(tl)] == [200, 300, 100, 400]
