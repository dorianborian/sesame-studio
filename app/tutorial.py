"""Interactive onboarding tour ("learn by doing") and the help center.

Public API (used by ``app.main_window``)
---------------------------------------
``start_tour(main_window) -> TourOverlay``
    Start the hands-on tour over the editor. Calling it again while a tour is
    running restarts it cleanly (the old overlay is torn down first).
``should_show_first_run_tutorial() -> bool`` / ``mark_tutorial_seen()``
    First-run bookkeeping (``QSettings`` key ``tutorial/seen_v3``).
``HelpDialog(parent, open_article=None)``
    The help center (lives in ``app.tutorial_help``; re-exported here).

How the tour works
------------------
``TourOverlay`` is a child widget covering the whole main window. It paints a
dim scrim with rounded cut-outs ("spots") around the widgets the current step
is about, plus a callout card with the instructions. The cut-outs are also cut
out of the widget's *mask*, so the mouse goes straight through them to the
real widgets: the user performs each step on the live UI. Everything outside
the spots is swallowed by the overlay so the tour cannot be derailed.

Each step can carry one or more tasks. A task "arms" a detector (signal
connections, an event tap, a per-tick poll) through a ``TaskContext`` and
calls ``ctx.complete()`` when the user did the thing; when all tasks of a step
are done the step shows a short success beat and advances by itself. *Next*
is always available so nobody gets stuck.

Keyboard: Esc skips, Right / Enter go on, Left goes back. Right / Enter /
Left are only taken when they bubble up to the main window unhandled, so the
spotlighted widget keeps every key it uses (Space for Play, arrows in the
timeline ...). Esc is a window shortcut so it always works -- except while a
mouse button is down, when it is passed on so it can still cancel a drag.

The tour never edits the project by itself: it only shows panels, pauses
playback and switches back to one of the user's own sequences when a step
needs an editable one.
"""

from __future__ import annotations

import math
import traceback
from dataclasses import dataclass
from typing import Callable

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence, QPainter, QPainterPath, QPen, QRegion, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from . import theme
from .icons import pixmap as icon_pixmap
from .widgets import KeyHint

__all__ = [
    "start_tour", "should_show_first_run_tutorial", "mark_tutorial_seen",
    "HelpDialog", "TourOverlay", "TourStep", "TourTask", "Spot", "build_steps",
    "place_callout", "SETTINGS_KEY_TUTORIAL_SEEN",
]

SETTINGS_ORG, SETTINGS_APP = "SesameRobot", "SesameStudio"
# v3: the tour was rebuilt for the new 3D view / timeline / face editor, so
# people who dismissed an older one get to see this one once.
SETTINGS_KEY_TUTORIAL_SEEN = "tutorial/seen_v3"

TICK_MS = 50              # geometry tracking, polled detectors, pulse animation
CARD_WIDTH = 336
CARD_RADIUS = 12
CARD_GAP = 14             # callout edge <-> the thing it points at (the arrow lives here)
ARROW_LEN = 9
ARROW_HALF = 9
EDGE_MARGIN = 12          # callout never gets closer than this to the window edge
SPOT_RADIUS = 10
PANEL_PAD = -3            # big panels: ring sits just inside their edge
CONTROL_PAD = 6           # buttons and other small controls: ring sits around them
_TEXT_INPUTS = (QLineEdit, QAbstractSpinBox, QComboBox, QTextEdit, QPlainTextEdit)


# ---------------------------------------------------------------------------
# first-run flag
# ---------------------------------------------------------------------------
def _settings() -> QSettings:
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


def should_show_first_run_tutorial() -> bool:
    return not _settings().value(SETTINGS_KEY_TUTORIAL_SEEN, False, type=bool)


def mark_tutorial_seen(seen: bool = True) -> None:
    _settings().setValue(SETTINGS_KEY_TUTORIAL_SEEN, bool(seen))


# ---------------------------------------------------------------------------
# step model
# ---------------------------------------------------------------------------
class Spot:
    """A lit, click-through region: the united rect of one or more widgets,
    grown (or shrunk, negative) by ``pad`` pixels."""

    __slots__ = ("widgets", "pad")

    def __init__(self, *widgets: QWidget, pad: int = PANEL_PAD):
        self.widgets = widgets
        self.pad = pad

    def rect(self, window: QWidget) -> QRect | None:
        out = QRect()
        for w in self.widgets:
            r = widget_rect(window, w)
            if r is not None:
                out = out.united(r) if not out.isNull() else r
        if out.isNull():
            return None
        out = out.adjusted(-self.pad, -self.pad, self.pad, self.pad)
        out = out.intersected(window.rect().adjusted(3, 3, -3, -3))
        return out if out.width() > 8 and out.height() > 8 else None


@dataclass(frozen=True)
class TourTask:
    """One thing the user is asked to do. ``arm(ctx)`` wires up detection and
    calls ``ctx.complete()`` when it happened."""
    text: str
    arm: Callable[["TaskContext"], None]


@dataclass(frozen=True)
class TourStep:
    key: str
    title: str
    body: str | Callable[["TourOverlay"], str]
    icon: str = "sparkle"
    spots: Callable[[QWidget], list[Spot]] | None = None     # main window -> lit regions
    focus: Callable[[QWidget], QWidget] | None = None         # main window -> the control to ring
    prepare: Callable[["TourOverlay"], None] | None = None    # show docks, pause playback ...
    keys: tuple[tuple[str, str], ...] = ()                    # (key chips, what they do)
    tasks: tuple[TourTask, ...] = ()
    placement: tuple[str, ...] = ()                           # see place_callout()
    beat_ms: int = 1100                                       # success pause before auto-advancing
    next_label: str | None = None

    def spot_rects(self, window: QWidget) -> list[QRect]:
        if self.spots is None:
            return []
        try:
            spots = self.spots(window)
        except Exception:
            return []
        return [r for r in (s.rect(window) for s in spots) if r is not None]

    def focus_rect(self, window: QWidget) -> QRect | None:
        if self.focus is None:
            return None
        try:
            r = widget_rect(window, self.focus(window))
        except Exception:
            return None
        return r.adjusted(-3, -3, 3, 3) if r is not None else None

    def body_text(self, tour: "TourOverlay") -> str:
        if callable(self.body):
            try:
                return self.body(tour)
            except Exception:
                return ""
        return self.body


def widget_rect(window: QWidget, widget) -> QRect | None:
    """``widget``'s rectangle in ``window`` coordinates, or None when it is
    not on screen (hidden, tabbed away, deleted, or in another window)."""
    if widget is None or not shiboken6.isValid(widget) or not widget.isVisible():
        return None
    if widget.window() is not window:
        return None
    rect = QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())
    return rect if rect.width() > 1 and rect.height() > 1 else None


# ---------------------------------------------------------------------------
# task detection plumbing
# ---------------------------------------------------------------------------
class _EventTap(QObject):
    """Event filter that only observes (never swallows)."""

    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, obj, event) -> bool:
        try:
            self._callback(obj, event)
        except Exception:
            traceback.print_exc()
        return False


class TaskContext:
    """What a task's ``arm`` function gets: the main window plus helpers that
    register detectors. Everything registered here is undone by ``release()``
    (called on completion and whenever the step is left)."""

    def __init__(self, tour: "TourOverlay", index: int):
        self.tour = tour
        self.mw = tour.mw
        self.index = index
        self.done = False
        self._connections: list[tuple[object, object]] = []
        self._taps: list[tuple[QObject, _EventTap]] = []
        self._ticks: list[Callable[[], None]] = []

    def connect(self, signal, slot) -> None:
        signal.connect(slot)
        self._connections.append((signal, slot))

    def watch(self, widget: QObject, callback) -> None:
        """Observe every event delivered to ``widget``: ``callback(obj, event)``."""
        tap = _EventTap(callback, self.tour)
        widget.installEventFilter(tap)
        self._taps.append((widget, tap))

    def on_tick(self, callback) -> None:
        """Call ``callback()`` on every tour tick (about 20 times a second)."""
        self._ticks.append(callback)

    def tick(self) -> None:
        for fn in list(self._ticks):
            if self.done:
                break
            try:
                fn()
            except Exception:
                traceback.print_exc()

    def complete(self) -> None:
        if self.done:
            return
        self.done = True
        self.release()
        self.tour._task_completed(self)

    def release(self) -> None:
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._connections.clear()
        for widget, tap in self._taps:
            if shiboken6.isValid(widget) and shiboken6.isValid(tap):
                widget.removeEventFilter(tap)
            if shiboken6.isValid(tap):
                tap.deleteLater()
        self._taps.clear()
        self._ticks.clear()

    def is_idle(self) -> bool:
        return not (self._connections or self._taps or self._ticks)


def _mouse_up() -> bool:
    return QApplication.mouseButtons() == Qt.MouseButton.NoButton


def _arm_camera_move(ctx: TaskContext) -> None:
    """Done when the user orbited, zoomed or panned the 3D camera. The camera
    has no change signal, so its state is polled -- but only after the user
    touched the view, so automatic re-framing never counts."""
    vp = ctx.mw.viewport
    state = {"base": None, "touched": False}

    def snapshot():
        try:
            t = vp._target
            return (float(vp._yaw), float(vp._pitch), float(vp._distance),
                    float(t[0]), float(t[1]), float(t[2]))
        except Exception:
            return None

    def on_event(_obj, event) -> None:
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress or (kind == QEvent.Type.Wheel and not state["touched"]):
            state["touched"] = True                 # measure from where this gesture starts
            state["base"] = snapshot()
        elif kind == QEvent.Type.Resize:
            state["touched"] = False                # the view re-frames itself when resized

    def poll() -> None:
        if not state["touched"] or not _mouse_up():
            return
        base, cur = state["base"], snapshot()
        if base is None or cur is None:
            ctx.complete()          # camera internals unavailable: the interaction itself counts
            return
        if (abs(cur[0] - base[0]) > 3.0 or abs(cur[1] - base[1]) > 3.0
                or abs(cur[2] - base[2]) > 0.03 * max(1.0, base[2])
                or math.dist(cur[3:], base[3:]) > 3.0):
            ctx.complete()

    ctx.watch(vp, on_event)
    ctx.on_tick(poll)


def _arm_pose_edit(*signal_getters: Callable[[QWidget], object]):
    """Done when one of the given pose signals fired and the mouse is up
    again (so a drag finishes before the tour moves on)."""
    def arm(ctx: TaskContext) -> None:
        state = {"edited": False}

        def mark(*_args) -> None:
            state["edited"] = True
            if _mouse_up():
                ctx.complete()

        def poll() -> None:
            if state["edited"] and _mouse_up():
                ctx.complete()

        for getter in signal_getters:
            ctx.connect(getter(ctx.mw), mark)
        ctx.on_tick(poll)
    return arm


def _arm_frame_added(ctx: TaskContext) -> None:
    tl = ctx.mw.timeline

    def count() -> int:
        seq = tl.current_sequence()
        return len(seq.frames) if seq else 0

    state = {"seq": tl.current_seq_name, "n": count()}

    def rebase(*_args) -> None:
        state["seq"], state["n"] = tl.current_seq_name, count()

    def check() -> None:
        if tl.current_seq_name != state["seq"]:
            rebase()
            return
        n = count()
        if n > state["n"]:
            ctx.complete()
        else:
            state["n"] = n

    ctx.connect(tl.currentSequenceChanged, rebase)
    ctx.connect(tl.sequencesChanged, check)


def _arm_playback(ctx: TaskContext) -> None:
    def on_state(playing: bool) -> None:
        if playing:
            ctx.complete()
    ctx.connect(ctx.mw.timeline.playbackStateChanged, on_state)


def _arm_builtin_picked(ctx: TaskContext) -> None:
    tl = ctx.mw.timeline

    def on_changed(_name: str = "") -> None:
        if tl.current_is_builtin():
            ctx.complete()
    ctx.connect(tl.currentSequenceChanged, on_changed)


def _arm_face_picked(ctx: TaskContext) -> None:
    faces = ctx.mw.face_library

    def on_face(_name: str = "") -> None:
        ctx.complete()
    ctx.connect(faces.facePreviewRequested, on_face)
    ctx.connect(faces.faceAssigned, on_face)


# ---------------------------------------------------------------------------
# step preparation (never touches project data)
# ---------------------------------------------------------------------------
def _pause(tour: "TourOverlay") -> None:
    tl = tour.mw.timeline
    if tl.is_playing():
        tl.pause()


def _use_own_sequence(tour: "TourOverlay") -> None:
    """Posing and adding frames need an editable sequence: leave a read-only
    built-in for the sequence the user was on before (or their first one)."""
    tl = tour.mw.timeline
    if not tl.current_is_builtin():
        return
    names = sorted(tl.sequences)
    name = tour.home_sequence if tour.home_sequence in tl.sequences else (names[0] if names else None)
    if name:
        tl.set_current_sequence(name)


def _prepare_posing(tour: "TourOverlay") -> None:
    _pause(tour)                 # while playing, dragging in 3D orbits instead of posing
    _use_own_sequence(tour)


def _prepare_pose_panel(tour: "TourOverlay") -> None:
    _prepare_posing(tour)
    dock = tour.mw.inspector_dock
    dock.setVisible(True)
    dock.raise_()


def _prepare_timeline(tour: "TourOverlay") -> None:
    tour.mw.timeline_dock.setVisible(True)


def _prepare_keyframes(tour: "TourOverlay") -> None:
    _prepare_posing(tour)
    _prepare_timeline(tour)


def _prepare_play(tour: "TourOverlay") -> None:
    _pause(tour)                 # so pressing Play is a visible change
    _prepare_timeline(tour)


def _prepare_faces(tour: "TourOverlay") -> None:
    _pause(tour)
    _use_own_sequence(tour)      # Assign to Frame needs an editable sequence
    mw = tour.mw
    mw.face_dock.setVisible(True)
    mw.face_dock.raise_()
    # Make sure the robot's screen is in view; the camera is not project data.
    vp = mw.viewport
    try:
        off_front = abs((float(vp._yaw) - 180.0 + 180.0) % 360.0 - 180.0)
        if off_front > 70.0 or float(vp._pitch) > 65.0:
            vp.set_view("three_quarter")
    except Exception:
        pass


def _physics_on(tour: "TourOverlay") -> bool:
    try:
        return bool(tour.mw.viewport.physics_enabled)
    except Exception:
        return False


def _pose_body(tour: "TourOverlay") -> str:
    text = ("Grab any leg and drag it. A ring shows how far that joint can turn, "
            "with its angle next to it.")
    if _physics_on(tour):
        return text + " <b>Simulate</b> is on, so the robot stays planted on the floor while you pose."
    return text + " Turn on <b>Simulate</b> (top left) to see the robot stand on the floor as you pose."


def _play_body(tour: "TourOverlay") -> str:
    text = "Press <b>Play</b> to watch the robot move through your frames. "
    if _physics_on(tour):
        return text + ("<b>Simulate</b> is on, so the joints turn at real servo speed, "
                       "just as they would on the robot.")
    return text + "Turn on <b>Simulate</b> to see it at real servo speed, standing on the floor."


def build_steps() -> list[TourStep]:
    """The tour, in order. Every sentence here must stay true of the app."""
    view = lambda mw: [Spot(mw.viewport)]                                   # noqa: E731
    stage = lambda mw: [Spot(mw.viewport, mw.timeline_dock)]                # noqa: E731
    return [
        TourStep(
            key="welcome", icon="sparkle",
            title="Welcome to Sesame Studio",
            body=("Pose your robot in 3D, turn poses into animations, give it a face, "
                  "then export code for the firmware.<br><br>"
                  "This hands-on tour takes about two minutes."),
            placement=("center",),
            next_label="Start tour",
        ),
        TourStep(
            key="look", icon="rotate",
            title="Look around",
            body="This is your robot. Move the camera to see it from any side.",
            spots=view,
            keys=(("Drag", "empty space to orbit"), ("Scroll", "to zoom"), ("Right-drag", "to pan")),
            tasks=(TourTask("Orbit, zoom or pan the view", _arm_camera_move),),
            placement=("spot:right-start", "spot:below-end", "edge:right-start"),
        ),
        TourStep(
            key="pose", icon="cursor",
            title="Pose it by hand",
            body=_pose_body,
            spots=view,
            prepare=_prepare_posing,
            keys=(("Ctrl", "while dragging snaps to 5°"),),
            tasks=(TourTask("Drag a leg to a new pose",
                            _arm_pose_edit(lambda mw: mw.viewport.anglesEdited)),),
            placement=("spot:right-start", "spot:below-end", "edge:right-start"),
        ),
        TourStep(
            key="tune", icon="sliders",
            title="Fine-tune in the Pose panel",
            body=("Every joint has a slider and an exact angle. <b>Presets</b> jumps to Stand "
                  "or Rest, and <b>Mirror</b> copies one side of the robot onto the other."),
            spots=lambda mw: [Spot(mw.inspector_dock), Spot(mw.viewport)],
            prepare=_prepare_pose_panel,
            tasks=(TourTask("Move a slider or pick a preset",
                            _arm_pose_edit(lambda mw: mw.inspector.anglesChanged)),),
            placement=("spot:left-end", "spot:left-start"),
        ),
        TourStep(
            key="frames", icon="keyframe",
            title="Build a move from frames",
            body=("A frame is a pose plus the time the robot takes to reach it. "
                  "<b>Add Frame</b> copies the selected frame, ready for you to pose the next step."),
            spots=stage,
            focus=lambda mw: mw.timeline.add_frame_btn,
            prepare=_prepare_keyframes,
            keys=(("Ctrl+Z", "undoes any edit"),),
            tasks=(
                TourTask("Click Add Frame", _arm_frame_added),
                TourTask("Drag a leg to change the new pose",
                         _arm_pose_edit(lambda mw: mw.viewport.anglesEdited,
                                        lambda mw: mw.inspector.anglesChanged)),
            ),
            placement=("focus:right-start", "spot:right-start", "edge:right-start"),
        ),
        TourStep(
            key="play", icon="play",
            title="Play it back",
            body=_play_body,
            spots=stage,
            focus=lambda mw: mw.timeline.play_btn,
            prepare=_prepare_play,
            keys=(("Space", "plays and pauses"),),
            tasks=(TourTask("Press Play", _arm_playback),),
            placement=("spot:right-start", "edge:right-start"),
            beat_ms=2000,
        ),
        TourStep(
            key="builtin", icon="robot",
            title="Try a built-in move",
            body=("The sequence list also holds every move in the firmware, like wave, dance "
                  "and walk_forward. They are read-only: <b>Duplicate to edit</b> makes a copy "
                  "that is yours to change."),
            spots=stage,
            focus=lambda mw: mw.timeline.seq_combo,
            prepare=_prepare_timeline,
            tasks=(
                TourTask("Pick a built-in move from the list", _arm_builtin_picked),
                TourTask("Press Play to watch it", _arm_playback),
            ),
            placement=("spot:right-start", "edge:right-start"),
            beat_ms=3500,
        ),
        TourStep(
            key="faces", icon="face",
            title="Give it a face",
            body=("Faces are what the robot's screen shows. <b>Assign to Frame</b> puts the "
                  "selected face on the selected frame, and <b>New</b> opens the painter "
                  "to draw your own."),
            spots=lambda mw: [Spot(mw.face_dock), Spot(mw.viewport)],
            prepare=_prepare_faces,
            tasks=(TourTask("Click a face to preview it on the robot", _arm_face_picked),),
            placement=("spot:left-end", "spot:left-start"),
        ),
        TourStep(
            key="export", icon="export",
            title="Export, and where to find help",
            body=("<b>Export</b> turns the current sequence or a face into code to paste into "
                  "the firmware. The Export Guide in the <b>Help</b> menu shows where each "
                  "piece goes."),
            spots=lambda mw: [Spot(mw.export_btn, pad=CONTROL_PAD), Spot(mw.tour_btn, pad=CONTROL_PAD)],
            keys=(("F1", "replays this tour"), ("Ctrl+/", "lists every shortcut")),
            placement=("spot:below-start", "spot:right-start"),
            next_label="Done",
        ),
    ]


# ---------------------------------------------------------------------------
# callout placement (pure geometry)
# ---------------------------------------------------------------------------
_AUTO_PLACEMENTS = (
    "spot:right-start", "spot:left-start", "spot:below-start", "spot:above-start",
    "focus:above-center", "focus:below-center", "focus:right-center", "focus:left-center",
    "edge:right-start", "edge:left-start",
    "inside:bottom-right", "inside:top-right", "inside:bottom-left",
)


def _clamp(value: int, lo: int, hi: int) -> int:
    return lo if hi < lo else max(lo, min(hi, value))


def _overlap(a: QRect, b: QRect) -> int:
    r = a.intersected(b)
    return max(0, r.width()) * max(0, r.height())


def place_callout(bounds: QRect, size: QSize, spots: list[QRect], focus: QRect | None,
                  prefs: tuple[str, ...] = ()) -> tuple[QRect, tuple[str, QPoint] | None]:
    """Where the callout card goes.

    ``prefs`` is an ordered wish list; the first entry that fits inside
    ``bounds`` without covering ``focus`` wins:

    * ``"center"``: middle of the window, no arrow
    * ``"spot:<side>-<align>"``: beside the lit region (the one containing
      the focus control, else the first)
    * ``"focus:<side>-<align>"``: beside the focus control itself
    * ``"edge:<right|left>-<align>"``: flush with that window edge, level
      with the lit region (it may overlap the region's rim; no arrow then)
    * ``"inside:<top|bottom>-<left|right|center>"``: in a corner of the lit
      region, no arrow

    ``side`` is ``right | left | above | below`` (where the card sits),
    ``align`` is ``start | center | end`` along that side. If nothing on the
    list fits, the candidate that hides the least of the lit regions is used,
    and as a last resort the card is centred.

    Returns the card rectangle and ``(edge, tip)`` for the pointer arrow, or
    ``None`` when there is none. ``edge`` names the card edge the arrow sits
    on: ``left | right | top | bottom``.
    """
    w, h = size.width(), size.height()
    primary = spots[0] if spots else None
    home = primary
    if focus is not None:
        home = next((s for s in spots if s.contains(focus.center())), primary)

    def centered():
        x = bounds.left() + (bounds.width() - w) // 2
        y = bounds.top() + (bounds.height() - h) // 2
        return QRect(max(bounds.left(), x), max(bounds.top(), y), w, h), None

    def fits(rect: QRect) -> bool:
        if not bounds.contains(rect):
            return False
        return focus is None or not rect.intersects(focus.adjusted(-4, -4, 4, 4))

    def build(spec: str):
        if spec == "center":
            return centered()
        kind, _, where = spec.partition(":")
        first, _, second = where.partition("-")
        if kind == "inside":
            if primary is None:
                return None
            pad = 16
            if second == "left":
                x = primary.left() + pad
            elif second == "right":
                x = primary.right() + 1 - pad - w
            else:
                x = primary.center().x() - w // 2
            y = primary.top() + pad if first == "top" else primary.bottom() + 1 - pad - h
            return QRect(x, y, w, h), None
        ref = focus if kind == "focus" and focus is not None else home
        if ref is None:
            return None
        aim = focus if focus is not None else ref
        side, align = first, second or "center"
        if kind == "edge":
            x = bounds.right() + 1 - w if side == "right" else bounds.left()
            if align == "start":
                y = ref.top()
            elif align == "end":
                y = ref.bottom() + 1 - h
            else:
                y = ref.center().y() - h // 2
            rect = QRect(x, _clamp(y, bounds.top(), bounds.bottom() + 1 - h), w, h)
            if rect.intersects(ref):
                if align == "start":                      # tuck it just inside the ring
                    rect.moveTop(_clamp(ref.top() + 10, bounds.top(), bounds.bottom() + 1 - h))
                return rect, None
            tip_y = _clamp(aim.center().y(), rect.top() + 24, rect.bottom() - 24)
            if side == "right":
                return rect, ("left", QPoint(rect.left() - ARROW_LEN, tip_y))
            return rect, ("right", QPoint(rect.right() + 1 + ARROW_LEN, tip_y))
        if side in ("right", "left"):
            x = ref.right() + 1 + CARD_GAP if side == "right" else ref.left() - CARD_GAP - w
            if align == "start":
                y = ref.top()
            elif align == "end":
                y = ref.bottom() + 1 - h
            else:
                y = ref.center().y() - h // 2
            y = _clamp(y, bounds.top(), bounds.bottom() + 1 - h)
            rect = QRect(x, y, w, h)
            tip_y = _clamp(aim.center().y(), rect.top() + 24, rect.bottom() - 24)
            if side == "right":
                return rect, ("left", QPoint(rect.left() - ARROW_LEN, tip_y))
            return rect, ("right", QPoint(rect.right() + 1 + ARROW_LEN, tip_y))
        if side in ("above", "below"):
            y = ref.bottom() + 1 + CARD_GAP if side == "below" else ref.top() - CARD_GAP - h
            if align == "start":
                x = ref.left()
            elif align == "end":
                x = ref.right() + 1 - w
            else:
                x = ref.center().x() - w // 2
            x = _clamp(x, bounds.left(), bounds.right() + 1 - w)
            rect = QRect(x, y, w, h)
            tip_x = _clamp(aim.center().x(), rect.left() + 24, rect.right() - 24)
            if side == "below":
                return rect, ("top", QPoint(tip_x, rect.top() - ARROW_LEN))
            return rect, ("bottom", QPoint(tip_x, rect.bottom() + 1 + ARROW_LEN))
        return None

    if not spots and focus is None:
        return centered()
    for spec in prefs:
        made = build(spec)
        if made is not None and fits(made[0]):
            return made
    best = None
    for order, spec in enumerate(_AUTO_PLACEMENTS):
        made = build(spec)
        if made is None or not fits(made[0]):
            continue
        cost = sum(_overlap(made[0], s) for s in spots) * 100 + order
        if best is None or cost < best[0]:
            best = (cost, made)
    return best[1] if best is not None else centered()


# ---------------------------------------------------------------------------
# callout card widgets
# ---------------------------------------------------------------------------
class _TaskGlyph(QWidget):
    """Status mark of a task row: hollow ring (not yet), pulsing dot
    (waiting for the user), check mark (done)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self.state = "pending"
        self.phase = 0.0

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        if self.state == "done":
            p.drawPixmap(1, 1, icon_pixmap("check-circle", theme.SUCCESS, 16))
        elif self.state == "active":
            pulse = 0.5 - 0.5 * math.cos(self.phase * math.tau / 1.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor(theme.ACCENT, 0.12 + 0.22 * pulse))
            radius = 5.0 + 3.5 * pulse
            p.drawEllipse(c, radius, radius)
            p.setBrush(theme.qcolor(theme.ACCENT))
            p.drawEllipse(c, 3.5, 3.5)
        else:
            p.setPen(QPen(theme.qcolor(theme.FG_MUTED), 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, 4.5, 4.5)


class _TaskRow(QWidget):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.glyph = _TaskGlyph(self)
        self.label = QLabel(text, self)
        self.label.setFont(theme.ui_font(13, 500))
        self.label.setWordWrap(True)
        row.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.label, 1)
        self.set_state("pending")

    def state(self) -> str:
        return self.glyph.state

    def set_state(self, state: str) -> None:
        self.glyph.state = state
        color = {"active": theme.FG, "done": theme.SUCCESS}.get(state, theme.FG_MUTED)
        self.label.setStyleSheet(f"color: {color};")
        self.glyph.update()

    def set_phase(self, phase: float) -> None:
        if self.glyph.state == "active":
            self.glyph.phase = phase
            self.glyph.update()


class _Dots(QWidget):
    """Progress dots; the current step is a short accent pill."""

    DOT, GAP, PILL = 6, 5, 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self._count = 0
        self._current = 0

    def set_progress(self, count: int, current: int) -> None:
        self._count, self._current = count, current
        width = count * self.DOT + max(0, count - 1) * self.GAP + (self.PILL - self.DOT)
        self.setFixedSize(max(1, width), 12)
        self.setAccessibleName(f"Step {current + 1} of {count}")
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        x = 0.0
        y = (self.height() - self.DOT) / 2.0
        for i in range(self._count):
            w = self.PILL if i == self._current else self.DOT
            if i == self._current:
                color = theme.ACCENT
            elif i < self._current:
                color = theme.FG_MUTED
            else:
                color = theme.BORDER_STRONG
            p.setBrush(theme.qcolor(color))
            p.drawRoundedRect(QRectF(x, y, w, self.DOT), self.DOT / 2.0, self.DOT / 2.0)
            x += w + self.GAP


class _Callout(QWidget):
    """The instruction card. Transparent: the overlay paints its bubble
    (background, border, shadow, pointer) so card and arrow are one shape."""

    nextClicked = Signal()
    backClicked = Signal()
    skipClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TourCard")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(
            f"#TourTitle {{ color: {theme.FG}; }}"
            f"#TourBody {{ color: {theme.FG_DIM}; }}"
            f"#TourKeyLabel {{ color: {theme.FG_DIM}; }}"
        )
        self.task_rows: list[_TaskRow] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(10)
        self.icon_label = QLabel(self)
        self.icon_label.setProperty("role", "iconChip")
        self.icon_label.setFixedSize(28, 28)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label = QLabel(self)
        self.title_label.setObjectName("TourTitle")
        self.title_label.setFont(theme.ui_font(15, 600))
        self.title_label.setWordWrap(True)
        head.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.title_label, 1)
        root.addLayout(head)
        root.addSpacing(10)

        self.body_label = QLabel(self)
        self.body_label.setObjectName("TourBody")
        self.body_label.setFont(theme.ui_font(13))
        self.body_label.setWordWrap(True)
        self.body_label.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(self.body_label)

        self.keys_host = QWidget(self)
        self.keys_layout = QVBoxLayout(self.keys_host)
        self.keys_layout.setContentsMargins(0, 10, 0, 0)
        self.keys_layout.setSpacing(5)
        root.addWidget(self.keys_host)

        self.tasks_host = QWidget(self)
        self.tasks_layout = QVBoxLayout(self.tasks_host)
        self.tasks_layout.setContentsMargins(0, 12, 0, 0)
        self.tasks_layout.setSpacing(6)
        root.addWidget(self.tasks_host)
        root.addSpacing(14)

        foot = QHBoxLayout()
        foot.setSpacing(4)
        self.dots = _Dots(self)
        foot.addWidget(self.dots, 0, Qt.AlignmentFlag.AlignVCenter)
        foot.addStretch(1)
        self.skip_btn = self._button("Skip", "flat", "Leave the tour (Esc)")
        self.back_btn = self._button("Back", "flat", "Previous step (Left)")
        self.next_btn = self._button("Next", "primary", "Next step (Right or Enter)")
        self.skip_btn.clicked.connect(self.skipClicked)
        self.back_btn.clicked.connect(self.backClicked)
        self.next_btn.clicked.connect(self.nextClicked)
        for b in (self.skip_btn, self.back_btn, self.next_btn):
            foot.addWidget(b)
        root.addLayout(foot)

    def _button(self, text: str, variant: str | None, tip: str) -> QPushButton:
        btn = QPushButton(text, self)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)      # Space / Enter must never "click" the card
        btn.setAutoDefault(False)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(tip)
        if variant:
            btn.setProperty("variant", variant)
        return btn

    @staticmethod
    def _clear(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def show_step(self, step: TourStep, index: int, total: int, body: str) -> None:
        last = index == total - 1
        self.icon_label.setPixmap(icon_pixmap(step.icon, theme.ACCENT, 16))
        self.title_label.setText(step.title)
        self.body_label.setText(f'<div style="line-height:142%;">{body}</div>')

        # Widgets added to a visible parent are only shown by a queued call;
        # show them now so the card can be measured straight away.
        self._clear(self.keys_layout)
        for sequence, label in step.keys:
            line = QWidget(self.keys_host)
            row = QHBoxLayout(line)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(7)
            row.addWidget(KeyHint(sequence, line))
            text = QLabel(label, line)
            text.setObjectName("TourKeyLabel")
            text.setFont(theme.ui_font(12))
            row.addWidget(text, 1)
            self.keys_layout.addWidget(line)
            line.show()
        self.keys_host.setVisible(bool(step.keys))

        self._clear(self.tasks_layout)
        self.task_rows = []
        for task in step.tasks:
            row = _TaskRow(task.text, self.tasks_host)
            self.tasks_layout.addWidget(row)
            row.show()
            self.task_rows.append(row)
        self.tasks_host.setVisible(bool(step.tasks))

        self.dots.set_progress(total, index)
        self.back_btn.setVisible(index > 0)
        self.skip_btn.setVisible(not last)
        self.next_btn.setText(step.next_label or ("Done" if last else "Next"))
        self.next_btn.setToolTip("Finish the tour (Enter)" if last else "Next step (Right or Enter)")
        self.set_waiting(bool(step.tasks))

        self.setFixedWidth(CARD_WIDTH)
        self.layout().invalidate()
        self.layout().activate()
        self.setFixedHeight(max(self.layout().totalHeightForWidth(CARD_WIDTH),
                                self.layout().totalMinimumSize().height()))

    def set_waiting(self, waiting: bool) -> None:
        """While a task is pending the action is the star, so Next is quiet."""
        theme.set_variant(self.next_btn, None if waiting else "primary")

    def set_task_state(self, index: int, state: str) -> None:
        if 0 <= index < len(self.task_rows):
            self.task_rows[index].set_state(state)

    def set_phase(self, phase: float) -> None:
        for row in self.task_rows:
            row.set_phase(phase)


# ---------------------------------------------------------------------------
# the overlay / controller
# ---------------------------------------------------------------------------
class TourOverlay(QWidget):
    """Spotlight overlay + tour controller. Create through ``start_tour``."""

    finished = Signal(bool)      # True: reached Done; False: skipped or aborted
    stepChanged = Signal(int)

    def __init__(self, main_window: QWidget, steps: list[TourStep] | None = None):
        super().__init__(main_window)
        self.mw = main_window
        self.steps: list[TourStep] = list(steps) if steps is not None else build_steps()
        self.index = -1
        self.home_sequence: str | None = None    # the user's own sequence to return to
        self._active = False
        self._ctx: TaskContext | None = None
        self._task_done: list[bool] = []
        self._step_done = False
        self._spots: list[QRect] = []
        self._focus: QRect | None = None
        self._arrow: tuple[str, QPoint] | None = None
        self._layout_key = None
        self._phase = 0.0
        self._nudge = 0                           # ticks left of the "over here" ring flash
        self._filtered: list[QObject] = []
        self._connections: list[tuple[object, object]] = []
        self._escape: QShortcut | None = None

        self.setObjectName("TourOverlay")
        self.setAccessibleName("Interactive tour")
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.hide()

        self.card = _Callout(self)
        self.card.nextClicked.connect(self.next)
        self.card.backClicked.connect(self.back)
        self.card.skipClicked.connect(self.skip)

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.poll)
        self._advance_timer = QTimer(self)
        self._advance_timer.setSingleShot(True)
        self._advance_timer.timeout.connect(self._auto_advance)

    # -- state ------------------------------------------------------------
    def is_active(self) -> bool:
        return self._active

    def current_step(self) -> TourStep | None:
        return self.steps[self.index] if 0 <= self.index < len(self.steps) else None

    def step_is_complete(self) -> bool:
        return self._step_done

    def tasks_done(self) -> list[bool]:
        return list(self._task_done)

    def spot_rects(self) -> list[QRect]:
        """The lit, click-through rectangles of the current step (window coords)."""
        return [QRect(r) for r in self._spots]

    def card_rect(self) -> QRect:
        return self.card.geometry()

    # -- lifecycle ----------------------------------------------------------
    def start(self, index: int = 0) -> None:
        if self._active or not self.steps:
            return
        self._active = True
        mw = self.mw
        self._install_filter(mw)
        for name in ("inspector_dock", "face_dock", "timeline_dock"):
            dock = getattr(mw, name, None)
            if dock is not None:
                self._install_filter(dock)        # a raised dock must not climb over the scrim
        stack = getattr(mw, "stack", None)
        if stack is not None:
            self._connect(stack.currentChanged, self._on_page_changed)
        # Esc as a window shortcut: it wins over widgets that would otherwise
        # eat the key (a selected joint, a focused field), so Esc always works.
        self._escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), mw)
        self._escape.setContext(Qt.ShortcutContext.WindowShortcut)
        self._escape.setAutoRepeat(False)
        self._connect(self._escape.activated, self._on_escape)
        self.setGeometry(mw.rect())
        self.show()
        self.raise_()
        self._timer.start()
        self.go_to(index)

    def next(self) -> None:
        if not self._active:
            return
        if self.index >= len(self.steps) - 1:
            self.finish()
        else:
            self.go_to(self.index + 1)

    def back(self) -> None:
        if self._active and self.index > 0:
            self.go_to(self.index - 1)

    def skip(self) -> None:
        """The user left the tour early: it still counts as seen."""
        self._teardown(completed=False, seen=True)

    def finish(self) -> None:
        self._teardown(completed=True, seen=True)

    def abort(self) -> None:
        """Stop without recording anything (restart, editor went away)."""
        self._teardown(completed=False, seen=False)

    def go_to(self, index: int) -> None:
        if not self._active:
            return
        index = max(0, min(len(self.steps) - 1, int(index)))
        self._advance_timer.stop()
        self._release_task()
        self.index = index
        step = self.steps[index]
        self._remember_home_sequence()
        if step.prepare is not None:
            try:
                step.prepare(self)
            except Exception:
                traceback.print_exc()
        self._task_done = [False] * len(step.tasks)
        self._step_done = False
        self._nudge = 0
        self.card.show_step(step, index, len(self.steps), step.body_text(self))
        self._relayout(force=True)
        self._stay_on_top()
        if step.tasks:
            self._arm_task(0)
        self.stepChanged.emit(index)

    def _teardown(self, completed: bool, seen: bool) -> None:
        if not self._active:
            return
        self._active = False
        self._timer.stop()
        self._advance_timer.stop()
        self._release_task()
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._connections.clear()
        for obj in self._filtered:
            if shiboken6.isValid(obj):
                obj.removeEventFilter(self)
        self._filtered.clear()
        if self._escape is not None:
            if shiboken6.isValid(self._escape):
                self._escape.setEnabled(False)
                self._escape.deleteLater()
            self._escape = None
        if seen:
            mark_tutorial_seen()
        self.hide()
        self.clearMask()
        if getattr(self.mw, "_tour", None) is self:
            self.mw._tour = None                  # let the window drop its reference
        self.finished.emit(completed)
        self.deleteLater()

    def _install_filter(self, obj: QObject) -> None:
        obj.installEventFilter(self)
        self._filtered.append(obj)

    def _connect(self, signal, slot) -> None:
        signal.connect(slot)
        self._connections.append((signal, slot))

    def _on_escape(self) -> None:
        if not self._active:
            return
        if not _mouse_up():
            # mid-drag: Esc belongs to the widget being dragged (cancel the drag)
            target = QApplication.focusWidget()
            if target is not None and target.window() is self.mw and self._escape is not None:
                self._escape.setEnabled(False)    # or the forwarded key would land here again
                try:
                    for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                        QApplication.sendEvent(target, QKeyEvent(kind, Qt.Key.Key_Escape,
                                                                 Qt.KeyboardModifier.NoModifier))
                finally:
                    if self._escape is not None and shiboken6.isValid(self._escape):
                        self._escape.setEnabled(True)
            return
        self.skip()

    def _on_page_changed(self, *_args) -> None:
        mw = self.mw
        if getattr(mw, "stack", None) is not None and mw.stack.currentWidget() is not getattr(mw, "viewport", None):
            self.abort()                          # back on the welcome page: nothing left to point at

    def _remember_home_sequence(self) -> None:
        tl = getattr(self.mw, "timeline", None)
        if tl is not None and tl.current_seq_name in tl.sequences:
            self.home_sequence = tl.current_seq_name

    # -- tasks ----------------------------------------------------------------
    def _arm_task(self, k: int) -> None:
        step = self.steps[self.index]
        self.card.set_task_state(k, "active")
        ctx = TaskContext(self, k)
        self._ctx = ctx
        try:
            step.tasks[k].arm(ctx)
        except Exception:
            traceback.print_exc()

    def _release_task(self) -> None:
        if self._ctx is not None:
            self._ctx.release()
            self._ctx = None

    def _task_completed(self, ctx: TaskContext) -> None:
        if ctx is not self._ctx or not self._active:
            return
        self._ctx = None
        step = self.steps[self.index]
        k = ctx.index
        self._task_done[k] = True
        self.card.set_task_state(k, "done")
        if k + 1 < len(step.tasks):
            self._arm_task(k + 1)
            return
        self._step_done = True
        self.card.set_waiting(False)
        self.update()
        if self.index < len(self.steps) - 1:
            self._advance_timer.start(max(0, int(step.beat_ms)))

    def _auto_advance(self) -> None:
        if self._active and self._step_done and self.index < len(self.steps) - 1:
            self.go_to(self.index + 1)

    # -- ticking ---------------------------------------------------------------
    def poll(self) -> None:
        """One tick: polled detectors, geometry tracking, pulse animation."""
        if not self._active:
            return
        mw = self.mw
        if not mw.isVisible() or mw.isMinimized():
            return                                # paused while the window is away
        self._phase += TICK_MS / 1000.0
        if self._ctx is not None:
            self._ctx.tick()
            if not self._active:
                return
        if self.geometry() != mw.rect():
            self.setGeometry(mw.rect())
        self._stay_on_top()
        self._relayout()
        if self._nudge > 0:
            self._nudge -= 1
            self.update()
        elif self._focus is not None and not self._step_done:
            self.update(self._focus.adjusted(-8, -8, 8, 8))
        self.card.set_phase(self._phase)

    def _stay_on_top(self) -> None:
        """Docks call raise_() on themselves (tab switches, joint selection);
        keep the scrim above them, and toasts above the scrim."""
        siblings = [c for c in self.mw.children()
                    if isinstance(c, QWidget) and not c.isWindow() and c.isVisible()]
        toasts = [c for c in siblings if type(c).__name__ == "Toast"]
        others = [c for c in siblings if c not in toasts]
        if others and others[-1] is not self:
            self.raise_()
            for toast in toasts:
                toast.raise_()

    # -- geometry ----------------------------------------------------------------
    def _relayout(self, force: bool = False) -> None:
        step = self.current_step()
        if step is None:
            return
        spots = step.spot_rects(self.mw)
        focus = step.focus_rect(self.mw)
        key = (self.size(), tuple(spots), focus, self.card.size())
        if not force and key == self._layout_key:
            return
        self._layout_key = key
        self._spots, self._focus = spots, focus
        bounds = self.rect().adjusted(EDGE_MARGIN, EDGE_MARGIN, -EDGE_MARGIN, -EDGE_MARGIN)
        rect, self._arrow = place_callout(bounds, self.card.size(), spots, focus, step.placement)
        self.card.setGeometry(rect)
        self.card.show()
        self._update_mask()
        self.update()

    @staticmethod
    def _rounded_region(rect: QRect, radius: float) -> QRegion:
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), radius, radius)
        return QRegion(path.toFillPolygon().toPolygon())

    def _update_mask(self) -> None:
        """Everything is the overlay's (and so swallows the mouse) except the
        inside of the spots. The card, its arrow and the focus ring are added
        back in case they sit on top of a spot."""
        region = QRegion(self.rect())
        for r in self._spots:
            region -= self._rounded_region(r.adjusted(2, 2, -2, -2), SPOT_RADIUS - 2)
        if self._focus is not None:
            f = self._focus
            region += QRegion(f.adjusted(-5, -5, 5, 5)) - QRegion(f)
        region += QRegion(self.card.geometry().adjusted(-16, -16, 16, 20))
        self.setMask(region)

    # -- painting ------------------------------------------------------------------
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        scrim = QPainterPath()
        scrim.addRect(QRectF(self.rect()))
        holes = QPainterPath()
        for r in self._spots:
            hole = QPainterPath()
            hole.addRoundedRect(QRectF(r), SPOT_RADIUS, SPOT_RADIUS)
            holes = holes.united(hole)
        p.fillPath(scrim.subtracted(holes), theme.qcolor(theme.BG_SUNKEN, 0.70))

        ring = theme.SUCCESS if self._step_done else theme.ACCENT
        flash = self._nudge / 8.0
        p.setBrush(Qt.BrushStyle.NoBrush)
        for r in self._spots:
            rf = QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5)
            p.setPen(QPen(theme.qcolor(ring, 0.16 + 0.30 * flash), 5.0 + 3.0 * flash))
            p.drawRoundedRect(rf, SPOT_RADIUS, SPOT_RADIUS)
            p.setPen(QPen(theme.qcolor(ring, 0.95), 1.5))
            p.drawRoundedRect(rf, SPOT_RADIUS, SPOT_RADIUS)

        if self._focus is not None and not self._step_done:
            pulse = 0.5 - 0.5 * math.cos(self._phase * math.tau / 1.5)
            grow = 1.0 + 2.0 * pulse
            rf = QRectF(self._focus).adjusted(-grow, -grow, grow, grow)
            p.setPen(QPen(theme.qcolor(theme.ACCENT, 0.95 - 0.55 * pulse), 2.0))
            p.drawRoundedRect(rf, theme.RADIUS + 2, theme.RADIUS + 2)

        self._paint_bubble(p)

    def _paint_bubble(self, p: QPainter) -> None:
        card = QRectF(self.card.geometry())
        if card.isEmpty():
            return
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(6, 0, -1):                 # soft drop shadow
            p.setBrush(theme.qcolor(theme.BG_SUNKEN, 0.10))
            grow = i * 2.0
            p.drawRoundedRect(card.adjusted(-grow, -grow + 4, grow, grow + 4),
                              CARD_RADIUS + grow, CARD_RADIUS + grow)
        bubble = QPainterPath()
        bubble.addRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), CARD_RADIUS, CARD_RADIUS)
        if self._arrow is not None:
            edge, tip = self._arrow
            t = QPointF(tip)
            tri = QPainterPath()
            if edge == "left":
                tri.moveTo(t)
                tri.lineTo(card.left() + 1.5, t.y() - ARROW_HALF)
                tri.lineTo(card.left() + 1.5, t.y() + ARROW_HALF)
            elif edge == "right":
                tri.moveTo(t)
                tri.lineTo(card.right() - 1.5, t.y() - ARROW_HALF)
                tri.lineTo(card.right() - 1.5, t.y() + ARROW_HALF)
            elif edge == "top":
                tri.moveTo(t)
                tri.lineTo(t.x() - ARROW_HALF, card.top() + 1.5)
                tri.lineTo(t.x() + ARROW_HALF, card.top() + 1.5)
            else:
                tri.moveTo(t)
                tri.lineTo(t.x() - ARROW_HALF, card.bottom() - 1.5)
                tri.lineTo(t.x() + ARROW_HALF, card.bottom() - 1.5)
            tri.closeSubpath()
            bubble = bubble.united(tri)
        p.setBrush(theme.qcolor(theme.POPUP_BG))
        p.setPen(QPen(theme.qcolor(theme.BORDER_STRONG), 1.0))
        p.drawPath(bubble)

    # -- input ------------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        self._nudge = 8                           # "the action is over here"
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        event.accept()

    def wheelEvent(self, event) -> None:
        event.accept()

    def contextMenuEvent(self, event) -> None:
        event.accept()

    def eventFilter(self, obj, event) -> bool:
        if not self._active:
            return False
        etype = event.type()
        if obj is self.mw:
            if etype == QEvent.Type.Resize:
                self.setGeometry(QRect(QPoint(0, 0), event.size()))
                QTimer.singleShot(0, self._deferred_relayout)   # after the docks re-laid out
            elif etype == QEvent.Type.KeyPress:
                return self._handle_key(event)
            elif etype == QEvent.Type.Hide and not event.spontaneous():
                self._teardown(completed=False, seen=True)      # window closed mid-tour
        elif etype == QEvent.Type.ZOrderChange:
            QTimer.singleShot(0, self._deferred_raise)
        return False

    def _deferred_relayout(self) -> None:
        if self._active and shiboken6.isValid(self):
            self._relayout(force=True)

    def _deferred_raise(self) -> None:
        if self._active and shiboken6.isValid(self):
            self._stay_on_top()

    def _handle_key(self, event) -> bool:
        """Tour keys. Only reached for key presses nothing else wanted (they
        bubbled up to the main window), so the spotlighted widget's own keys
        keep working."""
        if event.isAutoRepeat():
            return False
        mods = event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                                    | Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ShiftModifier)
        if mods:
            return False
        key = event.key()
        if key == Qt.Key.Key_Escape:              # normally the shortcut gets it first
            if not _mouse_up():
                return False
            self.skip()
            return True
        if isinstance(QApplication.focusWidget(), _TEXT_INPUTS):
            return False                          # Enter / arrows belong to the field being edited
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.next()
            return True
        if key == Qt.Key.Key_Left:
            self.back()
            return True
        return False


def start_tour(main_window: QWidget, steps: list[TourStep] | None = None) -> TourOverlay:
    """Start the interactive tour over ``main_window`` and return its overlay
    (the caller keeps the reference). A tour that is already running on that
    window is stopped first, so this restarts rather than stacks."""
    for old in main_window.findChildren(TourOverlay, options=Qt.FindChildOption.FindDirectChildrenOnly):
        old.abort()
    tour = TourOverlay(main_window, steps)
    tour.start()
    return tour


from .tutorial_help import HELP_ARTICLES, HelpDialog  # noqa: E402,F401  (public re-export)
