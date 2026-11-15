"""Posable 3D robot viewport (OpenGL 3.3 core).

The widget shows the CAD-exact robot model (``app.robot_model``) on a studio
floor, lets the user pose it by grabbing the limbs directly, and previews
motion with the lightweight ground-contact simulation (``app.physics``).

Module layout
-------------
``gl_shaders``   GLSL sources (strings).
``gl_renderer``  all GL objects + the render passes (no Qt widget logic).
``gl_math``      camera / projection / ray helpers (pure numpy).
``gl_labels``    text chips rasterised with QPainter -> sprite textures.
``gl_hud``       the floating toolbar on top of the view.
``viewport3d``   (this file) the QOpenGLWidget: camera, picking, direct
                 manipulation, physics stepping, public API.

Interaction
-----------
* left-drag a leg (or the gizmo handle): rotate that joint; Ctrl snaps to 5
  degrees, Shift is slow/fine; Esc cancels and restores the pose
* Shift + left-drag a foot: two-joint IK (hip + knee follow the cursor)
* left-drag empty space or the body: orbit; Alt + left-drag: always orbit
* right / middle drag (or Shift + left-drag on empty space): pan
* wheel: zoom towards the cursor; double-click empty space or F: frame
* click a leg: select; Tab / Shift+Tab: cycle joints; Left / Right or [ ]:
  nudge 1 degree (Shift: 10); right-click a leg: context menu

Physics preview (``app.physics.RobotSim``): with Simulate on, the body's
root transform comes from the sim (``set_angles`` -> immediate, quasi-static;
``set_servo_targets`` -> servo-slewed) and a 60 Hz timer steps it only while
it is awake. Follow keeps the robot on an 18 mm "leash" around where the user
framed it. Display options persist in QSettings under ``viewport/``:
grid, shadows, labels, internals, cover, shell_color, physics, follow.

Camera state lives in the plain attributes ``_yaw``, ``_pitch`` (degrees),
``_distance`` and ``_target`` with
``eye = target + distance * (cos(pitch)cos(yaw), sin(pitch), cos(pitch)sin(yaw))``.
Writing them directly (then calling ``update()``) takes effect immediately.
"""

from __future__ import annotations

import math
import sys
import time
import traceback

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QLabel, QMenu, QVBoxLayout, QWidget

from . import theme
from .gl_math import (
    look_at, normalize, orbit_eye, perspective, project_points, ray_plane, rotate_about_axis,
    screen_ray, shortest_angle_delta, signed_angle_about, wrap_degrees,
)
from .kinematics import BODY_LINK, link_transforms, rotation_about_line
from .servo_model import DEFAULT_ANGLE, SERVO_COLORS, SERVO_ORDER, SERVO_RANGES

__all__ = ["Viewport3D", "apply_desktop_gl_format"]

FOV_Y = 36.0
DEFAULT_YAW = 145.0
DEFAULT_PITCH = 22.0
VIEWS = {
    "three_quarter": (DEFAULT_YAW, DEFAULT_PITCH),
    "front": (180.0, 8.0),
    "back": (0.0, 8.0),
    "left": (90.0, 8.0),
    "right": (-90.0, 8.0),
    "top": (180.0, 89.0),
}
_VIEW_ALIASES = {
    "3/4": "three_quarter", "3-4": "three_quarter", "iso": "three_quarter", "home": "three_quarter",
    "default": "three_quarter", "perspective": "three_quarter", "threequarter": "three_quarter",
    "three-quarter": "three_quarter",
}
HINT_LONG = "Drag a leg to pose  ·  Drag empty space to orbit  ·  Right-drag to pan  ·  Scroll to zoom"
HINT_SHORT = "Drag a leg to pose  ·  Drag to orbit  ·  Scroll to zoom"
FOLLOW_LEASH = 18.0
DRAG_THRESHOLD_PX = 3.0
HANDLE_PICK_PX = 12.0
PICK_TOLERANCE_PX = 4
SETTINGS_ORG, SETTINGS_APP = "SesameRobot", "SesameStudio"


def apply_desktop_gl_format() -> None:
    """Request an OpenGL 3.3 core context (24-bit depth, 8-bit stencil, 4x
    MSAA) for every QOpenGLWidget. Must run before QApplication is created."""
    fmt = QSurfaceFormat()
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setDepthBufferSize(24)
    fmt.setStencilBufferSize(8)
    fmt.setSamples(4)
    fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(fmt)


def _to_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return default


class _MouseState:
    __slots__ = ("button", "press", "last", "moved", "mode", "joint", "grab_w", "on_handle", "ik",
                 "context_joint")

    def __init__(self, button, x, y):
        self.button = button
        self.press = (x, y)
        self.last = (x, y)
        self.moved = False
        self.mode = None            # "orbit" | "pan" | "joint"
        self.joint = None
        self.grab_w = None
        self.on_handle = False
        self.ik = False
        self.context_joint = None


class _JointDrag:
    __slots__ = ("joint", "start_angles", "grab_local", "raw", "offset", "prev_cursor", "mode",
                 "last_xy", "tangent_dir", "ik", "hip", "raw_hip", "pixel_offset")

    def __init__(self, joint, start_angles):
        self.joint = joint
        self.start_angles = start_angles
        self.grab_local = np.zeros(3)
        self.raw = 0.0
        self.offset = 0.0
        self.prev_cursor = None
        self.mode = "plane"
        self.last_xy = (0.0, 0.0)
        self.tangent_dir = None
        self.ik = False
        self.hip = None
        self.raw_hip = 0.0
        self.pixel_offset = (0.0, 0.0)


class Viewport3D(QOpenGLWidget):
    """Orbit-camera, directly posable robot view with a physics preview."""

    anglesEdited = Signal(dict)       # {servo: int}: full snapshot, continuously while the user poses in 3D
    dragStarted = Signal(str)
    dragFinished = Signal(str)
    jointSelected = Signal(object)    # servo name or None (user-initiated only)
    jointHovered = Signal(object)     # servo name or None (user-initiated only)
    physicsToggled = Signal(bool)
    viewOptionsChanged = Signal()     # any display option / toggle changed (for syncing menus)

    def __init__(self, parent=None, *, persist_settings: bool = True):
        super().__init__(parent)
        self.setObjectName("Viewport3D")
        self.setMinimumSize(240, 200)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        # legacy attributes kept so older callers keep importing / running
        self.rig = None
        self.calibration_mode = False

        self._persist = bool(persist_settings)
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP) if self._persist else None

        self._renderer = None
        self._gl_failed: str | None = None
        self._paint_ms = 0.0
        self._paint_count = 0

        # ---- model ---------------------------------------------------------
        self._model = None
        self._model_error: str | None = None
        try:
            from .robot_model import RobotModel
            self._model = RobotModel.load()
            self._prepare_model()
        except Exception as exc:  # missing / corrupt assets: show a message, don't crash
            self._model = None
            self._model_error = f"{type(exc).__name__}: {exc}"
            print(f"[viewport3d] could not load the robot model: {exc}", file=sys.stderr)

        # commanded pose (what set_angles / set_servo_targets / the user asked for)
        # (starts at 90 like the inspector sliders; the main window sets the real pose)
        self._angles: dict[str, float] = {s: float(DEFAULT_ANGLE) for s in SERVO_ORDER}
        if self._model is not None:
            for name in self._model.joints:
                self._angles.setdefault(name, float(DEFAULT_ANGLE))

        # ---- options -------------------------------------------------------
        self.show_grid = self._load_bool("grid", True)
        self.show_shadows = self._load_bool("shadows", True)
        self.show_labels = self._load_bool("labels", True)
        self.show_internals = self._load_bool("internals", False)
        self.follow = self._load_bool("follow", True)
        self.mirror_posing = False
        self.cover = self._model.default_cover if self._model is not None else None
        saved_cover = self._load_value("cover", None)
        if self._model is not None and saved_cover in self._model.cover_variants:
            self.cover = saved_cover
        self.shell_color: str | None = None
        saved_color = self._load_value("shell_color", "")
        if saved_color and QColor(str(saved_color)).isValid():
            self.shell_color = QColor(str(saved_color)).name()

        # ---- physics -------------------------------------------------------
        self._sim = None
        self._physics_note: str | None = None
        self._physics_on = False
        if self._model is not None:
            try:
                from .physics import RobotSim
                self._sim = RobotSim.from_model(self._model)
                self._sim.reset(self._angles)
            except Exception as exc:
                self._sim = None
                self._physics_note = "Physics preview unavailable"
                print(f"[viewport3d] physics unavailable: {exc}", file=sys.stderr)
        self._physics_on = self._sim is not None and self._load_bool("physics", True)

        # ---- camera --------------------------------------------------------
        self._yaw = DEFAULT_YAW
        self._pitch = DEFAULT_PITCH
        self._distance = 300.0
        self._target = np.array([0.0, 30.0, 0.0], dtype=np.float32)
        self._cam_goal = [self._yaw, self._pitch, self._distance, np.array(self._target, dtype=np.float64)]
        self._cam_tau = 0.045
        self._cam_written = None
        self._user_framed = False
        self._follow_ref = None

        # ---- interaction state --------------------------------------------
        self._selected: str | None = None
        self._hovered: str | None = None
        self._hover_handle = False
        self._mouse: _MouseState | None = None
        self._drag: _JointDrag | None = None
        self._playing = False
        self._links_cache = None
        self._scene_serial = 0
        self._pick_key = None
        self._pick_ids = None
        self._pick_depth = None
        self._pick_inv = None
        self._pick_ms = 0.0
        self._face_pixels = None
        self._face_dirty = True
        self._hint_opacity = 1.0
        self._hint_fading = False

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._last_tick = time.perf_counter()

        # ---- overlay widgets ----------------------------------------------
        self._error_label = QLabel(self)
        self._error_label.setWordWrap(True)
        self._error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_label.setStyleSheet(
            f"background-color: {theme.BG_SUNKEN}; color: {theme.FG_DIM}; padding: 24px;"
            f" font-family: '{theme.FONT_FAMILY}'; font-size: 13px;")
        self._error_label.hide()
        self._hud = None
        if self._model is not None:
            try:
                from .gl_hud import ViewportHud
                self._hud = ViewportHud(self)
                self._hud.show()
            except Exception as exc:
                self._hud = None
                print(f"[viewport3d] HUD unavailable: {exc}", file=sys.stderr)
        if self._model is None:
            self._show_error("The 3D robot model could not be loaded.\n\n"
                             f"{self._model_error}\n\nRe-generate it with tools/build_model.py.")

        self._mark_camera_written()
        if self._model is not None:
            self._snap_camera_to_frame()
            self._reanchor_follow()

    # ======================================================================
    # model preparation
    # ======================================================================
    def _prepare_model(self) -> None:
        model = self._model
        joints = model.joints
        self._joints = joints
        self._links = list(model.links)
        self._link_index = {n: i for i, n in enumerate(self._links)}
        self._kind = {n: ("hip" if j.parent is None else "knee") for n, j in joints.items()}
        self._children = {n: [c for c, j in joints.items() if j.parent == n] for n in joints}
        self._twin: dict[str, str] = {}
        for n in joints:
            other = ("L" if n[0] == "R" else "R") + n[1:] if n[:1] in ("R", "L") else None
            if other in joints:
                self._twin[n] = other
        self._joint_cycle = [s for s in SERVO_ORDER if s in joints] + [n for n in joints if n not in SERVO_ORDER]

        verts: dict[str, list] = {n: [] for n in self._links}
        for part in model.parts_for():
            verts[part.link].append(part.vertices)
        self._link_corners: dict[str, np.ndarray] = {}
        self._link_sphere: dict[str, tuple] = {}          # bounding sphere per link (bind pose)
        self._axis_extent: dict[str, tuple[float, float]] = {}
        self._tip_local: dict[str, np.ndarray] = {}
        for link in self._links:
            if not verts[link]:
                continue
            v = np.concatenate(verts[link]).astype(np.float64)
            lo, hi = v.min(axis=0), v.max(axis=0)
            self._link_corners[link] = np.array(
                [[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
            mid = (lo + hi) * 0.5
            self._link_sphere[link] = (mid, float(np.sqrt(((v - mid) ** 2).sum(axis=1).max())))
            j = joints.get(link)
            if j is None:
                continue
            pivot = np.asarray(j.pivot, dtype=np.float64)
            axis = normalize(j.axis)
            rel = v - pivot
            along = rel @ axis
            self._axis_extent[link] = (float(along.min()), float(along.max()))
            if self._children[link]:
                tip = np.asarray(joints[self._children[link][0]].pivot, dtype=np.float64)
            else:
                radial = rel - along[:, None] * axis
                dist = np.einsum("ij,ij->i", radial, radial)
                k = max(8, v.shape[0] // 100)
                far = np.argpartition(dist, -k)[-k:]
                tip = v[far].mean(axis=0)
            self._tip_local[link] = tip
        for n in joints:                       # joints whose link has no geometry
            self._axis_extent.setdefault(n, (-5.0, 5.0))
            self._tip_local.setdefault(n, np.asarray(joints[n].pivot, dtype=np.float64) + np.array([20.0, 0, 0]))

        # label anchor per limb: the surface point of its printed part nearest to that part's centroid
        self._label_local: dict[str, np.ndarray] = {}
        for part in model.parts_for():
            if (part.link in joints and part.group == "leg" and part.link not in self._label_local
                    and part.material != "pla_accent"):
                v = part.vertices.astype(np.float64)
                c = v.mean(axis=0)
                self._label_local[part.link] = v[int(np.argmin(((v - c) ** 2).sum(axis=1)))]
        for n in joints:
            self._label_local.setdefault(n, np.asarray(joints[n].pivot, dtype=np.float64))

        self._collision = {k: np.asarray(v, dtype=np.float64) for k, v in model.collision_points.items()}
        self._bounds_center = (np.asarray(model.bounds_min) + np.asarray(model.bounds_max)) * 0.5
        self._static_root = np.eye(4)
        self._static_root[1, 3] = -float(model.bounds_min[1])

    # ======================================================================
    # settings
    # ======================================================================
    def _load_value(self, key: str, default):
        if self._settings is None:
            return default
        return self._settings.value(f"viewport/{key}", default)

    def _load_bool(self, key: str, default: bool) -> bool:
        return _to_bool(self._load_value(key, None), default)

    def _save(self, key: str, value) -> None:
        if self._settings is not None:
            self._settings.setValue(f"viewport/{key}", value)

    def _options_changed(self) -> None:
        if self._hud is not None:
            self._hud.sync()
        self.viewOptionsChanged.emit()
        self.update()

    # ======================================================================
    # public API
    # ======================================================================
    @property
    def angles(self) -> dict[str, float]:
        """The joint angles currently displayed (servo degrees)."""
        if self._sim_active():
            out = dict(self._angles)
            out.update(self._sim.angles)
            return out
        return dict(self._angles)

    @property
    def physics_enabled(self) -> bool:
        return self._physics_on

    @property
    def physics_available(self) -> bool:
        return self._sim is not None

    @property
    def cover_variants(self) -> list[str]:
        return list(self._model.cover_variants) if self._model is not None else []

    @property
    def selected_joint(self) -> str | None:
        return self._selected

    @property
    def hovered_joint(self) -> str | None:
        return self._hovered

    def set_angles(self, angles: dict) -> None:
        """Show an externally supplied pose now (posing, scrubbing). Partial
        dicts and floats are fine. Never emits ``anglesEdited``."""
        if not self._merge_angles(angles) and not (self._sim_active() and self._sim.servos_moving):
            return
        if self._sim_active():
            self._sim_call(lambda: self._sim.set_angles_immediate(self._angles))
            self._ensure_timer()
        self._pose_changed()

    def set_servo_targets(self, angles: dict) -> None:
        """Playback targets: with physics on the simulated servos slew to them
        at a realistic speed; with physics off this equals ``set_angles``."""
        if not self._sim_active():
            self.set_angles(angles)
            return
        self._merge_angles(angles)
        self._sim_call(lambda: self._sim.set_targets(self._angles))
        self._ensure_timer()
        self.update()

    def set_face_preview(self, pixels) -> None:
        """None | 64 rows x 128 bools | ndarray (64, 128) | 1024 packed bytes
        (row-major, MSB first)."""
        self._face_pixels = self._coerce_face(pixels)
        self._face_dirty = True
        self.update()

    def set_selected_joint(self, name) -> None:
        """Programmatic selection (does not emit ``jointSelected``)."""
        name = name if (self._model is not None and name in self._joints) else None
        if name != self._selected:
            self._selected = name
            self.update()

    def set_hovered_joint(self, name) -> None:
        """Programmatic hover highlight (does not emit ``jointHovered``)."""
        name = name if (self._model is not None and name in self._joints) else None
        if name != self._hovered:
            self._hovered = name
            self._hover_handle = False
            self.update()

    def set_physics_enabled(self, on: bool) -> None:
        on = bool(on) and self._sim is not None
        if on == self._physics_on:
            if self._hud is not None:
                self._hud.sync()
            return
        self._physics_on = on
        if on:
            self._sim_call(lambda: self._sim.reset(self._angles))
            self._ensure_timer()
        self._reanchor_follow()
        self._save("physics", on)
        self._pose_changed()
        self._options_changed()
        self.physicsToggled.emit(on)

    def reset_physics(self) -> None:
        """Put the robot back at the origin, at rest, in its current pose."""
        if self._sim is None:
            return
        self._sim_call(lambda: self._sim.reset(self._angles))
        self._pose_changed()
        self._apply_follow(0.0)                      # re-centre exactly where the user framed it
        self._ensure_timer()

    def set_playing(self, playing: bool) -> None:
        """Hint from the timeline: hover feedback is suppressed while playing."""
        self._playing = bool(playing)
        if self._playing:
            self._set_hover(None, False, emit=False)
            self._ensure_timer()
        self.update()

    def set_mirror_posing(self, on: bool) -> None:
        on = bool(on)
        if on != self.mirror_posing:
            self.mirror_posing = on
            self._options_changed()

    def set_follow(self, on: bool) -> None:
        on = bool(on)
        if on != self.follow:
            self.follow = on
            self._reanchor_follow()
            self._save("follow", on)
            if on:
                self.frame_robot()
            self._options_changed()

    def set_show_grid(self, on: bool) -> None:
        self.show_grid = bool(on)
        self._save("grid", self.show_grid)
        self._options_changed()

    def set_show_shadows(self, on: bool) -> None:
        self.show_shadows = bool(on)
        self._save("shadows", self.show_shadows)
        self._options_changed()

    def set_show_labels(self, on: bool) -> None:
        self.show_labels = bool(on)
        self._save("labels", self.show_labels)
        self._options_changed()

    def set_show_internals(self, on: bool) -> None:
        self.show_internals = bool(on)
        self._save("internals", self.show_internals)
        self._scene_changed()
        self._options_changed()

    def set_cover(self, variant: str) -> None:
        if self._model is None or variant not in self._model.cover_variants:
            return
        self.cover = variant
        self._save("cover", variant)
        self._scene_changed()
        self._options_changed()

    def set_shell_color(self, color) -> None:
        """Tint the printed (tintable) parts; ``None`` restores the CAD colours."""
        if color is None or color == "":
            self.shell_color = None
        else:
            c = QColor(color)
            if not c.isValid():
                return
            self.shell_color = c.name()
        self._save("shell_color", self.shell_color or "")
        self._options_changed()

    def reset_view(self) -> None:
        """Default 3/4 view, robot framed."""
        self.set_view("three_quarter")

    def set_view(self, name: str) -> None:
        """Animated view preset: front, back, left, right, top, three_quarter (alias "3/4")."""
        key = str(name).strip().lower().replace(" ", "_")
        key = _VIEW_ALIASES.get(key, key)
        if key not in VIEWS:
            return
        yaw, pitch = VIEWS[key]
        self._absorb_external_camera()
        target, distance = self._frame_params(yaw, pitch)
        self._cam_goal = [self._yaw + shortest_angle_delta(self._yaw, yaw), pitch, distance, target]
        self._reanchor_follow()
        self._cam_tau = 0.11
        self._user_framed = True
        self._start_camera_animation()

    def frame_robot(self) -> None:
        """Centre and fit the robot without changing the view direction."""
        self._absorb_external_camera()
        target, distance = self._frame_params()
        self._cam_goal = [self._cam_goal[0], self._cam_goal[1], distance, target]
        self._reanchor_follow()
        self._cam_tau = 0.11
        self._user_framed = True
        self._start_camera_animation()

    # test / diagnostics helpers ------------------------------------------------
    def joint_screen_point(self, name: str):
        """A logical-pixel position that is on the visible surface of the given
        joint's limb (or None if it is hidden). Used by tests and tutorials."""
        if self._model is None or name not in self._joints or not self._ensure_pick():
            return None
        ys, xs = np.nonzero(self._pick_ids == self._link_index[name] + 1)
        if xs.size == 0:
            return None
        cx, cy = xs.mean(), ys.mean()
        i = int(np.argmin((xs - cx) ** 2 + (ys - cy) ** 2))
        return float(xs[i]) + 0.5, float(ys[i]) + 0.5

    def render_stats(self) -> dict:
        out = {"paint_ms": self._paint_ms, "pick_ms": self._pick_ms, "paints": self._paint_count}
        if self._renderer is not None:
            out.update(self._renderer.stats)
            out["samples"] = self._renderer.samples
        return out

    # ======================================================================
    # pose / simulation plumbing
    # ======================================================================
    def _merge_angles(self, angles) -> bool:
        changed = False
        if not angles:
            return False
        for name, value in dict(angles).items():
            if name not in self._angles:
                continue
            try:
                fv = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(fv):
                continue
            if fv != self._angles[name]:
                self._angles[name] = fv
                changed = True
        return changed

    def _sim_active(self) -> bool:
        return self._physics_on and self._sim is not None

    def _sim_call(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            traceback.print_exc()
            self._disable_physics(f"Physics preview stopped ({type(exc).__name__})")

    def _disable_physics(self, note: str) -> None:
        was_on = self._physics_on
        self._physics_on = False
        self._sim = None
        self._physics_note = note
        self._pose_changed()
        if self._hud is not None:
            self._hud.sync()
        if was_on:
            self.physicsToggled.emit(False)

    def _scene_changed(self) -> None:
        self._scene_serial += 1

    def _pose_changed(self) -> None:
        self._links_cache = None
        self._scene_serial += 1
        if self._sim_active():
            self._apply_follow()
        self.update()

    def _root(self) -> np.ndarray:
        if self._sim_active():
            return self._sim.root
        return self._static_root

    def _world_links(self) -> dict[str, np.ndarray]:
        """{link: 4x4 bind-pose body coords -> world} for what is displayed."""
        if self._links_cache is None:
            local = link_transforms(self._joints, self.angles)
            root = self._root()
            self._links_cache = {k: root @ m for k, m in local.items()}
        return self._links_cache

    def _joint_frame(self, name: str):
        """(pivot, unit axis) of a joint in world space, right now."""
        j = self._joints[name]
        parent = self._world_links()[j.parent or BODY_LINK]
        pivot = parent[:3, :3] @ np.asarray(j.pivot, dtype=np.float64) + parent[:3, 3]
        axis = normalize(parent[:3, :3] @ np.asarray(j.axis, dtype=np.float64))
        return pivot, axis

    def _robot_center(self) -> np.ndarray:
        root = self._root()
        return root[:3, :3] @ self._bounds_center + root[:3, 3]

    @staticmethod
    def _coerce_face(pixels):
        if pixels is None:
            return None
        try:
            if isinstance(pixels, (bytes, bytearray, memoryview)):
                raw = np.frombuffer(bytes(pixels), dtype=np.uint8)
                if raw.size == 128 * 64 // 8:
                    bits = np.unpackbits(raw, bitorder="big").reshape(64, 128)
                elif raw.size == 128 * 64:
                    bits = raw.reshape(64, 128) != 0
                else:
                    return None
            else:
                bits = np.asarray(pixels)
                if bits.shape != (64, 128):
                    return None
                bits = bits != 0
            return np.ascontiguousarray(np.where(bits, 255, 0).astype(np.uint8))
        except Exception:
            return None

    # ======================================================================
    # camera
    # ======================================================================
    def _camera_tuple(self):
        t = np.asarray(self._target, dtype=np.float64).reshape(-1)
        return (float(self._yaw), float(self._pitch), float(self._distance),
                (float(t[0]), float(t[1]), float(t[2])))

    def _mark_camera_written(self) -> None:
        self._cam_written = self._camera_tuple()

    def _absorb_external_camera(self) -> bool:
        """Detect direct writes to _yaw/_pitch/_distance/_target (the trailer
        script does that): they win at once; anything still animating jumps
        to its goal so nothing fights the caller."""
        cur = self._camera_tuple()
        w = self._cam_written
        if w is None or cur == w:
            return False
        g = self._cam_goal
        yaw = cur[0] if cur[0] != w[0] else g[0]
        pitch = cur[1] if cur[1] != w[1] else g[1]
        dist = cur[2] if cur[2] != w[2] else g[2]
        target = np.array(cur[3] if cur[3] != w[3] else g[3], dtype=np.float64)
        self._yaw, self._pitch, self._distance = yaw, pitch, dist
        self._target = target.astype(np.float32)
        self._cam_goal = [yaw, pitch, dist, target.copy()]
        self._user_framed = True
        self._mark_camera_written()
        return True

    def _camera(self):
        """(view, proj, view_proj, eye) for the current camera and widget size."""
        w, h = max(1, self.width()), max(1, self.height())
        target = np.asarray(self._target, dtype=np.float64)
        dist = max(float(self._distance), 1.0)
        eye = orbit_eye(target, float(self._yaw), max(-89.5, min(89.5, float(self._pitch))), dist)
        view = look_at(eye, target)
        near = max(1.0, dist * 0.02)
        far = dist * 8.0 + 4000.0
        proj = perspective(FOV_Y, w / h, near, far)
        return view, proj, proj @ view, eye

    def _frame_params(self, yaw: float | None = None, pitch: float | None = None, canonical: bool = False):
        """(target, distance) that centre and fit the robot for a view
        direction (default: where the camera is heading). Fits the corners of
        the per-link bounding boxes exactly under the perspective projection,
        leaving room for the toolbar and the hint. ``canonical`` frames the
        standing pose at the origin instead of the current pose."""
        if self._model is None:
            return np.array([0.0, 30.0, 0.0]), 300.0
        if canonical:
            local = link_transforms(self._joints, self._model.bind_angles)
            links = {k: self._static_root @ m for k, m in local.items()}
        else:
            links = self._world_links()
        pts = []
        for link, corners in self._link_corners.items():
            m = links[link]
            pts.append(corners @ m[:3, :3].T + m[:3, 3])
        pts = np.concatenate(pts)
        yaw = self._cam_goal[0] if yaw is None else yaw
        pitch = self._cam_goal[1] if pitch is None else pitch
        to_eye = normalize(orbit_eye(np.zeros(3), yaw, max(-89.0, min(89.0, pitch)), 1.0))
        right = np.cross(-to_eye, [0.0, 1.0, 0.0])
        if float(np.linalg.norm(right)) < 1e-6:
            right = np.array([0.0, 0.0, 1.0])
        right = normalize(right)
        up = np.cross(right, -to_eye)
        origin = (pts.min(axis=0) + pts.max(axis=0)) * 0.5
        rel = pts - origin
        a, b, c = rel @ right, rel @ up, rel @ to_eye
        a0 = (a.min() + a.max()) * 0.5
        b0 = (b.min() + b.max()) * 0.5
        aspect = max(1, self.width()) / max(1, self.height())
        tan_v = math.tan(math.radians(FOV_Y) * 0.5)
        tan_h = tan_v * aspect
        fill_h, fill_v = 0.86, 0.74                  # fraction of the half-view the robot may use
        distance = float(max((c + np.abs(a - a0) / (tan_h * fill_h)).max(),
                             (c + np.abs(b - b0) / (tan_v * fill_v)).max()))
        target = origin + right * a0 + up * b0
        return target, max(60.0, min(1400.0, distance))

    def _snap_camera_to_frame(self) -> None:
        target, distance = self._frame_params(canonical=True)
        self._distance = distance
        self._target = target.astype(np.float32)
        self._cam_goal = [self._yaw, self._pitch, distance, np.asarray(self._target, dtype=np.float64)]
        self._mark_camera_written()

    def _clamp_goal(self) -> None:
        g = self._cam_goal
        g[2] = max(45.0, min(1400.0, g[2]))
        center = self._robot_center() if self._model is not None else np.zeros(3)
        t = g[3]
        d = t - center
        horiz = math.hypot(d[0], d[2])
        if horiz > 320.0:
            d[0] *= 320.0 / horiz
            d[2] *= 320.0 / horiz
        t[0], t[2] = center[0] + d[0], center[2] + d[2]
        t[1] = max(-5.0, min(170.0, t[1]))
        # keep the eye above the floor
        min_pitch = max(-12.0, math.degrees(math.asin(max(-1.0, min(1.0, (4.0 - t[1]) / g[2])))))
        g[1] = max(min_pitch, min(89.0, g[1]))

    def _start_camera_animation(self) -> None:
        self._clamp_goal()
        if not self.isVisible():
            self._finish_camera_animation()
            self.update()
            return
        self._ensure_timer()

    def _finish_camera_animation(self) -> None:
        g = self._cam_goal
        self._yaw, self._pitch, self._distance = g[0], g[1], g[2]
        self._target = np.asarray(g[3], dtype=np.float32).copy()
        self._mark_camera_written()

    def _camera_step(self, dt: float) -> bool:
        """Ease the camera towards its goal. Returns True while still moving."""
        self._absorb_external_camera()
        g = self._cam_goal
        target = np.asarray(self._target, dtype=np.float64)
        d_yaw = g[0] - self._yaw
        d_pitch = g[1] - self._pitch
        d_dist = g[2] - self._distance
        d_target = g[3] - target
        if (abs(d_yaw) < 0.02 and abs(d_pitch) < 0.02 and abs(d_dist) < 0.02
                and float(np.abs(d_target).max()) < 0.02):
            if d_yaw or d_pitch or d_dist or float(np.abs(d_target).max()) > 0.0:
                self._finish_camera_animation()
                self.update()
            return False
        k = 1.0 - math.exp(-dt / max(self._cam_tau, 1e-3))
        self._yaw += d_yaw * k
        self._pitch += d_pitch * k
        self._distance += d_dist * k
        self._target = (target + d_target * k).astype(np.float32)
        self._mark_camera_written()
        self.update()
        return True

    def _reanchor_follow(self) -> None:
        """The robot's current position becomes the leash origin."""
        self._follow_ref = self._robot_center().copy() if self._model is not None else None

    def _apply_follow(self, leash: float = FOLLOW_LEASH) -> None:
        """Follow mode: keep the robot on a short leash. The camera target
        only moves once the robot has drifted FOLLOW_LEASH mm from where the
        user framed it, so small shifts while posing never move the view,
        but a walking robot is tracked smoothly."""
        if self._model is None:
            return
        center = self._robot_center()
        if not self.follow or self._follow_ref is None:
            self._follow_ref = center.copy()
            return
        if self._drag is not None:
            return                                   # never move the camera under a pose drag
        d = center - self._follow_ref
        d[1] = 0.0
        dist = math.hypot(d[0], d[2])
        if dist <= max(leash, 1e-6):
            return
        shift = d * (1.0 - leash / dist)
        self._follow_ref = self._follow_ref + shift
        self._absorb_external_camera()
        self._cam_goal[3] = self._cam_goal[3] + shift
        self._cam_tau = 0.14
        self._start_camera_animation()

    # ======================================================================
    # timer
    # ======================================================================
    def _ensure_timer(self) -> None:
        if not self._timer.isActive() and self.isVisible():
            self._last_tick = time.perf_counter()
            self._timer.start()

    def _tick(self) -> None:
        now = time.perf_counter()
        dt = max(0.001, min(0.05, now - self._last_tick))
        self._last_tick = now
        busy = False

        if self._sim_active() and not self._sim.is_resting:
            try:
                self._sim.step(min(dt, 1.0 / 30.0))
            except Exception as exc:
                traceback.print_exc()
                self._disable_physics(f"Physics preview stopped ({type(exc).__name__})")
            else:
                self._pose_changed()
                busy = True

        if self._camera_step(dt):
            busy = True

        if self._hint_fading and self._hint_opacity > 0.0:
            self._hint_opacity = max(0.0, self._hint_opacity - dt / 0.6)
            self.update()
            busy = True

        if self._playing:
            busy = True
        if not busy:
            self._timer.stop()

    # ======================================================================
    # GL lifecycle
    # ======================================================================
    def initializeGL(self) -> None:
        self._renderer = None
        self._pick_key = None
        if self._model is None:
            return
        try:
            from .gl_renderer import Renderer, hex_to_linear
            r = Renderer()
            r.initialize(self._model)
            accent = hex_to_linear(theme.ACCENT)
            r.set_theme_colors(
                bg_top=hex_to_linear(theme.BG_ELEVATED) * 1.15,
                bg_bottom=hex_to_linear(theme.BG_SUNKEN),
                bg_glow=hex_to_linear("#2a2c33") * 0.55,
                floor=np.array([0.088, 0.090, 0.100]),
                grid=hex_to_linear("#8a8d9a") * 0.55,
                accent=accent,
                hover=hex_to_linear("#eaf2ff"),
                select=hex_to_linear(theme.ACCENT_HOVER),
            )
            self._renderer = r
            self._face_dirty = True
            self._gl_failed = None
            self._error_label.hide()
        except Exception as exc:
            traceback.print_exc()
            self._renderer = None
            self._fail_gl(exc)

    def _fail_gl(self, exc: BaseException) -> None:
        self._gl_failed = f"{type(exc).__name__}: {exc}"
        print(f"[viewport3d] OpenGL error: {self._gl_failed}", file=sys.stderr)
        text = str(exc)
        if len(text) > 600:
            text = text[:600] + "…"
        self._show_error("The 3D view could not start.\n\n"
                         "Sesame Studio needs OpenGL 3.3 (try updating the graphics driver).\n\n" + text)

    def _show_error(self, text: str) -> None:
        self._error_label.setText(text)
        self._error_label.setGeometry(self.rect())
        self._error_label.show()
        self._error_label.raise_()
        if self._hud is not None:
            self._hud.hide()

    def resizeGL(self, w: int, h: int) -> None:
        pass   # render targets are (re)sized lazily in paintGL

    def paintGL(self) -> None:
        r = self._renderer
        if r is None or not r.ready or self._gl_failed:
            try:
                from OpenGL import GL
                GL.glClearColor(0.05, 0.05, 0.06, 1.0)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            except Exception:
                pass
            return
        t0 = time.perf_counter()
        try:
            self._absorb_external_camera()
            if self._face_dirty:
                r.upload_face(self._face_pixels)
                self._face_dirty = False
            frame = self._build_frame()
            r.render(frame, self.defaultFramebufferObject())
        except Exception as exc:
            traceback.print_exc()
            self._fail_gl(exc)
            return
        ms = (time.perf_counter() - t0) * 1000.0
        self._paint_count += 1
        self._paint_ms = ms if self._paint_count < 3 else self._paint_ms * 0.9 + ms * 0.1

    # ======================================================================
    # frame assembly
    # ======================================================================
    def _descendant_links(self, joint: str) -> tuple:
        out = [self._link_index[joint]]
        stack = list(self._children.get(joint, ()))
        while stack:
            c = stack.pop()
            out.append(self._link_index[c])
            stack.extend(self._children.get(c, ()))
        return tuple(out)

    def _gizmo_geometry(self, name: str, eye: np.ndarray):
        """World-space layout of the rotation gizmo of joint ``name``:
        (centre, unit limb direction, unit axis, radius)."""
        pivot, axis = self._joint_frame(name)
        lo, hi = self._axis_extent[name]
        side_up = float(np.dot(axis, eye - pivot)) >= (lo + hi) * 0.5
        centre = pivot + axis * ((hi + 1.5) if side_up else (lo - 1.5))
        link = self._world_links()[name]
        tip = link[:3, :3] @ self._tip_local[name] + link[:3, 3]
        radial = tip - pivot
        radial = radial - axis * float(np.dot(radial, axis))
        if float(np.linalg.norm(radial)) < 1e-4:
            radial = np.cross(axis, [0.0, 1.0, 0.0])
            if float(np.linalg.norm(radial)) < 1e-4:
                radial = np.cross(axis, [1.0, 0.0, 0.0])
        direction = normalize(radial)
        radius = max(15.0, min(36.0, 0.105 * float(np.linalg.norm(eye - centre))))
        return centre, direction, axis, radius

    def _build_frame(self):
        from .gl_renderer import Frame, hex_to_linear
        from .gl_labels import chip_image, chip_key

        r = self._renderer
        dpr = float(self.devicePixelRatioF())
        w, h = max(1, self.width()), max(1, self.height())
        view, proj, vp, eye = self._camera()
        links = self._world_links()

        f = Frame()
        f.fb_size = (max(1, int(round(w * dpr))), max(1, int(round(h * dpr))))
        f.proj = proj
        f.pick_size = (w, h)
        f.pick_key = self._pick_signature()
        f.view_proj = vp
        f.eye = eye
        for i, name in enumerate(self._links):
            f.link_mats[i] = links[name]
        f.cover = self.cover
        f.ghost = self.show_internals
        f.grid = self.show_grid
        f.shadows = self.show_shadows
        f.tint = hex_to_linear(self.shell_color) if self.shell_color else None
        f.outline_px = 2.4 * dpr
        center = self._robot_center()
        f.light_center = center
        f.floor_focus = (float(center[0]), float(center[2]))
        f.horizon = 0.5 + math.tan(math.radians(float(self._pitch))) / (2.0 * math.tan(math.radians(FOV_Y) * 0.5))

        # contact darkening under the body and the feet
        blobs = []
        body_h = max(0.0, float(center[1]))
        blobs.append((center[0], center[2], 58.0, 0.42 * math.exp(-body_h / 70.0)))
        for name, kind in self._kind.items():
            pts = self._collision.get(name)
            if kind != "knee" or pts is None or not len(pts):
                continue
            m = links[name]
            wp = pts @ m[:3, :3].T + m[:3, 3]
            low = wp[int(np.argmin(wp[:, 1]))]
            blobs.append((low[0], low[2], 13.0, 0.55 * math.exp(-max(0.0, float(low[1])) / 7.0)))
        f.blobs = np.array(blobs, dtype=np.float32)

        if self._sim_active():
            try:
                by_link: dict[str, list] = {}
                for pos, link in self._sim.contacts:
                    by_link.setdefault(link, []).append(pos)
                contacts = []
                for link, pts in by_link.items():
                    c = np.mean(pts, axis=0)
                    radius = 8.0 if link != BODY_LINK else 14.0
                    contacts.append((c[0], c[2], radius, 0.5 if link != BODY_LINK else 0.3))
                f.contacts = np.array(contacts, dtype=np.float32).reshape(-1, 4)
            except Exception:
                pass

        # highlight state
        interactive = not self._playing
        hover = self._hovered if interactive else None
        selected = self._selected
        dragging = self._drag is not None
        if hover and hover != selected:
            f.hover_links = self._descendant_links(hover)
            for li in f.hover_links:
                f.glow[li] = (0.80, 0.88, 1.0, 0.55)
        if selected:
            f.selected_links = self._descendant_links(selected)
            acc = hex_to_linear(theme.ACCENT_HOVER)
            strength = 1.0 if dragging else (0.9 if hover == selected else 0.7)
            for li in f.selected_links:
                f.glow[li] = (acc[0], acc[1], acc[2], strength)

        # sprites ---------------------------------------------------------------
        def add_chip(text, style, dot, cx, cy, opacity=1.0, anchor=None):
            key = chip_key(text, style, dot, dpr)
            tex, tw, th = r.sprite_texture(key, lambda: chip_image(text, style, dot, dpr))
            x = round(cx * dpr - tw / 2.0)
            y = round(cy * dpr - th / 2.0)
            x = max(2, min(f.fb_size[0] - tw - 2, x))
            y = max(2, min(f.fb_size[1] - th - 2, y))
            f.sprites.append((tex, float(x), float(y), float(tw), float(th), float(opacity), anchor))

        angles = self.angles
        centre_xy, centre_w = project_points(vp, [center], w, h)
        centre_xy = centre_xy[0]

        gizmo = None
        if selected:
            g_centre, g_dir, g_axis, g_radius = self._gizmo_geometry(selected, eye)
            j = self._joints[selected]
            lo, hi = SERVO_RANGES.get(selected, (0, 180))
            gizmo = {
                "center": g_centre, "u": g_dir * g_radius, "v": np.cross(g_axis, g_dir) * g_radius,
                "cur": float(angles[selected]), "range": (float(lo), float(hi)), "dir": float(j.sign),
                "hot": 1.0 if (dragging or self._hover_handle) else 0.0,
            }
            f.gizmo = gizmo

        label_joints = list(self._joints) if (self.show_labels or hover or selected) else []
        if label_joints:
            anchors = []
            for n in label_joints:
                m = links[n]
                anchors.append(m[:3, :3] @ self._label_local[n] + m[:3, 3])
            xy, wclip = project_points(vp, np.array(anchors), w, h)
            for i, name in enumerate(label_joints):
                if wclip[i] <= 0.0:
                    continue
                is_sel, is_hov = name == selected, name == hover
                if not (self.show_labels or is_sel or is_hov):
                    continue
                dot = SERVO_COLORS.get(name)
                ax, ay = float(xy[i, 0]), float(xy[i, 1])
                if is_hov and not is_sel:
                    text = f"{name} · {self._kind[name]} · {int(round(angles[name]))}°"
                    add_chip(text, "hover", dot, ax, ay - 30.0)
                elif is_sel and gizmo is not None:
                    # opposite the handle, so it never sits under the degree read-out
                    pts, wc = project_points(vp, [gizmo["center"], gizmo["center"] + gizmo["u"]], w, h)
                    out = pts[0] - pts[1]
                    n = float(np.linalg.norm(out))
                    out = out / n if n > 1e-3 else np.array([0.0, -1.0])
                    pos = pts[0] + out * 30.0
                    add_chip(f"{name} · {self._kind[name]}", "selected", dot, pos[0], pos[1])
                else:
                    add_chip(name, "label", dot, ax, ay, 0.5 if self._playing else 0.95,
                             anchor=(ax, ay, self._link_index[name]))

        if gizmo is not None:
            handle = gizmo["center"] + gizmo["u"]
            pts, wclip = project_points(vp, [gizmo["center"], handle], w, h)
            if wclip[1] > 0.0:
                out = pts[1] - pts[0]
                n = float(np.linalg.norm(out))
                out = out / n if n > 1e-3 else np.array([0.0, -1.0])
                pos = pts[1] + out * 26.0
                add_chip(f"{int(round(angles[selected]))}°", "readout", None, pos[0], pos[1])

        if self._physics_note:
            add_chip(self._physics_note, "note", None, w / 2.0, h - 58.0)
        if self._hint_opacity > 0.01 and h > 150:
            text = HINT_LONG if w >= 640 else HINT_SHORT
            if w >= 360:
                add_chip(text, "hint", None, w / 2.0, h - 24.0, self._hint_opacity)
        return f

    # ======================================================================
    # picking
    # ======================================================================
    def _pick_signature(self):
        return (self._camera_tuple(), max(1, self.width()), max(1, self.height()), self._scene_serial,
                self.cover, self.show_internals)

    def _ensure_pick(self) -> bool:
        r = self._renderer
        if r is None or not r.ready or self._gl_failed or self._model is None:
            return False
        self._absorb_external_camera()
        w, h = max(1, self.width()), max(1, self.height())
        key = self._pick_signature()
        if key == self._pick_key and self._pick_ids is not None:
            return True
        from .gl_renderer import Frame
        t0 = time.perf_counter()
        view, proj, vp, eye = self._camera()
        links = self._world_links()
        f = Frame()
        f.view_proj = vp
        for i, name in enumerate(self._links):
            f.link_mats[i] = links[name]
        f.cover = self.cover
        f.ghost = self.show_internals
        f.pick_size = (w, h)
        f.pick_key = key
        try:
            self.makeCurrent()
            try:
                self._pick_ids, self._pick_depth = r.read_pick(f)
            finally:
                self.doneCurrent()
        except Exception as exc:
            print(f"[viewport3d] picking failed: {exc}", file=sys.stderr)
            self._pick_ids = None
            return False
        try:
            self._pick_inv = np.linalg.inv(vp)
        except np.linalg.LinAlgError:
            self._pick_ids = None
            return False
        self._pick_key = key
        ms = (time.perf_counter() - t0) * 1000.0
        self._pick_ms = ms if self._pick_ms == 0.0 else self._pick_ms * 0.8 + ms * 0.2
        return True

    def _pick(self, x: float, y: float):
        """(link name | None, world position | None) under the logical pixel."""
        if not self._ensure_pick():
            return None, None
        ids = self._pick_ids
        h, w = ids.shape
        ix, iy = int(math.floor(x)), int(math.floor(y))
        if not (0 <= ix < w and 0 <= iy < h):
            return None, None
        lid = int(ids[iy, ix])
        if lid == 0:
            # nothing exactly here: forgive a few pixels so thin edges are easy to grab
            t = PICK_TOLERANCE_PX
            x0, x1 = max(0, ix - t), min(w, ix + t + 1)
            y0, y1 = max(0, iy - t), min(h, iy + t + 1)
            sub = ids[y0:y1, x0:x1]
            ys, xs = np.nonzero(sub > 1)
            if xs.size == 0:
                return None, None
            d2 = (xs + x0 - ix) ** 2 + (ys + y0 - iy) ** 2
            k = int(np.argmin(d2))
            if d2[k] > t * t:
                return None, None
            ix, iy = int(xs[k] + x0), int(ys[k] + y0)
            lid = int(ids[iy, ix])
        z = float(self._pick_depth[iy, ix])
        ndc = np.array([2.0 * (ix + 0.5) / w - 1.0, 1.0 - 2.0 * (iy + 0.5) / h, 2.0 * z - 1.0, 1.0])
        p = self._pick_inv @ ndc
        world = p[:3] / p[3] if abs(p[3]) > 1e-12 else None
        return self._links[lid - 1], world

    def _ray(self, x: float, y: float):
        view, proj, vp, eye = self._camera()
        origin, direction = screen_ray(np.linalg.inv(vp), x, y, max(1, self.width()), max(1, self.height()))
        return eye, direction

    def _project(self, points):
        view, proj, vp, eye = self._camera()
        return project_points(vp, points, max(1, self.width()), max(1, self.height()))

    def _handle_screen_pos(self):
        if not self._selected or self._model is None:
            return None
        view, proj, vp, eye = self._camera()
        centre, direction, axis, radius = self._gizmo_geometry(self._selected, eye)
        handle = centre + direction * radius
        xy, wclip = project_points(vp, [handle], max(1, self.width()), max(1, self.height()))
        if wclip[0] <= 0.0:
            return None
        return float(xy[0, 0]), float(xy[0, 1]), handle

    def _handle_hit(self, x: float, y: float):
        hp = self._handle_screen_pos()
        if hp is None:
            return None
        if math.hypot(hp[0] - x, hp[1] - y) <= HANDLE_PICK_PX:
            return hp[2]
        return None

    # ======================================================================
    # selection / hover
    # ======================================================================
    def _select(self, name, *, emit: bool) -> None:
        if name == self._selected:
            return
        self._selected = name
        self.update()
        if emit:
            self.jointSelected.emit(name)

    def _set_hover(self, name, on_handle: bool, *, emit: bool) -> None:
        changed = name != self._hovered
        if changed or on_handle != self._hover_handle:
            self._hovered = name
            self._hover_handle = on_handle
            self.update()
            if changed and emit:
                self.jointHovered.emit(name)
        self._apply_cursor()

    def _apply_cursor(self) -> None:
        if self._drag is not None:
            shape = Qt.CursorShape.ClosedHandCursor
        elif self._mouse is not None and self._mouse.mode == "pan" and self._mouse.moved:
            shape = Qt.CursorShape.SizeAllCursor
        elif self._mouse is None and (self._hovered or self._hover_handle) and not self._playing:
            shape = Qt.CursorShape.OpenHandCursor
        else:
            shape = Qt.CursorShape.ArrowCursor
        if self.cursor().shape() != shape:
            self.setCursor(shape)

    def _update_hover(self, x: float, y: float) -> None:
        if self._playing or self._model is None:
            self._set_hover(None, False, emit=True)
            return
        if self._handle_hit(x, y) is not None:
            self._set_hover(self._selected, True, emit=True)
            return
        link, _ = self._pick(x, y)
        joint = link if (link and link != BODY_LINK and link in self._joints) else None
        self._set_hover(joint, False, emit=True)

    # ======================================================================
    # user edits
    # ======================================================================
    def _snapshot(self) -> dict:
        return {s: int(round(self._angles[s])) for s in SERVO_ORDER if s in self._angles}

    def _user_set(self, updates: dict) -> bool:
        """Apply a user edit {joint: int angle}; mirrors onto the twin when
        mirror posing is on. Emits ``anglesEdited`` if anything changed."""
        final = {}
        for name, value in updates.items():
            lo, hi = SERVO_RANGES.get(name, (0, 180))
            final[name] = float(max(lo, min(hi, int(round(value)))))
        if self.mirror_posing:
            for name in list(final):
                twin = self._twin.get(name)
                if twin and twin not in updates:
                    lo, hi = SERVO_RANGES.get(twin, (0, 180))
                    final[twin] = float(max(lo, min(hi, 180.0 - final[name])))
        if not self._merge_angles(final):
            return False
        if self._sim_active():
            self._sim_call(lambda: self._sim.set_angles_immediate(self._angles))
            self._ensure_timer()
        self._pose_changed()
        self.anglesEdited.emit(self._snapshot())
        return True

    def _edit_once(self, joint: str, updates: dict) -> None:
        """A discrete edit (nudge, context menu) as one undoable gesture."""
        self.dragStarted.emit(joint)
        self._user_set(updates)
        self.dragFinished.emit(joint)

    def _nudge(self, delta: int) -> None:
        if not self._selected:
            return
        cur = int(round(self._angles[self._selected]))
        self._edit_once(self._selected, {self._selected: cur + delta})

    def _cycle_selection(self, step: int) -> None:
        order = self._joint_cycle
        if not order:
            return
        if self._selected in order:
            i = (order.index(self._selected) + step) % len(order)
        else:
            i = 0 if step > 0 else len(order) - 1
        self._select(order[i], emit=True)

    # ======================================================================
    # joint dragging
    # ======================================================================
    def _begin_joint_drag(self, m: _MouseState, x: float, y: float) -> None:
        name = m.joint
        j = self._joints[name]
        d = _JointDrag(name, dict(self._angles))
        links = self._world_links()
        grab = np.asarray(m.grab_w, dtype=np.float64)
        d.last_xy = (x, y)
        d.raw = float(self.angles[name])
        if m.ik and j.parent:
            d.ik = True
            d.hip = j.parent
            d.raw_hip = float(self.angles[j.parent])
            xy, wclip = self._project([grab])
            if wclip[0] > 0:
                d.pixel_offset = (float(xy[0, 0]) - x, float(xy[0, 1]) - y)
        else:
            # Re-base the grab point onto where the cursor is NOW (the press
            # position plus the few pixels of drag threshold), so the limb
            # does not jump when the drag engages.
            pivot, axis = self._joint_frame(name)
            eye, direction = self._ray(x, y)
            if abs(float(np.dot(direction, axis))) > 0.30:
                hit = ray_plane(eye, direction, grab, axis)
                if hit is not None and float(np.linalg.norm(hit - grab)) < 40.0:
                    grab = hit
            else:
                d.mode = "tangent"
        link_inv = np.linalg.inv(links[name])
        d.grab_local = link_inv[:3, :3] @ grab + link_inv[:3, 3]
        self._drag = d
        self._set_hover(name, m.on_handle, emit=True)
        self._apply_cursor()
        self.dragStarted.emit(name)

    def _update_joint_drag(self, x: float, y: float, mods) -> None:
        d = self._drag
        if d is None:
            return
        if d.ik:
            self._update_ik_drag(x, y)
            return
        name = d.joint
        j = self._joints[name]
        lo, hi = SERVO_RANGES.get(name, (0, 180))
        cur = float(self.angles[name])
        fine = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        pivot, axis = self._joint_frame(name)
        link = self._world_links()[name]
        grab = link[:3, :3] @ d.grab_local + link[:3, 3]            # where the grabbed point is now
        centre = pivot + axis * float(np.dot(grab - pivot, axis))
        eye, direction = self._ray(x, y)
        facing = abs(float(np.dot(direction, axis)))
        use_plane = facing > (0.22 if d.mode == "plane" else 0.30)
        hit = ray_plane(eye, direction, grab, axis) if use_plane else None
        radius = float(np.linalg.norm(grab - centre))
        if hit is None or radius < 1e-3 or float(np.linalg.norm(hit - centre)) < 1e-6:
            use_plane = False
        if use_plane:
            # grabbed (or cursor) right next to the hinge: pointing-at-the-cursor
            # would swing wildly there, so use the bounded slider instead
            pxy, pw = self._project([centre, grab, hit])
            near = 30.0 if d.mode == "plane" else 40.0
            if (pw.min() <= 0.0 or float(np.linalg.norm(pxy[1] - pxy[0])) < near * 0.8
                    or float(np.linalg.norm(pxy[2] - pxy[0])) < near * 0.5):
                use_plane = False

        if use_plane:
            rot = signed_angle_about(axis, grab - centre, hit - centre)
            mid = (lo + hi) * 0.5
            cursor = mid + wrap_degrees(cur + rot / j.sign - mid)   # servo angle the cursor points at
            if d.mode != "plane" or d.prev_cursor is None:
                d.offset = d.raw - cursor if d.mode != "plane" else 0.0
                d.raw = cursor + d.offset
            elif fine:
                d.raw = max(lo, min(hi, d.raw + 0.25 * wrap_degrees(cursor - d.prev_cursor)))
                d.offset = d.raw - cursor
            else:
                d.raw = cursor + d.offset
            d.prev_cursor = cursor
            d.mode = "plane"
            d.tangent_dir = None
        else:
            # rotation plane (nearly) edge-on: drag along the limb's on-screen tangent
            rel = grab - centre
            p_plus = centre + rotate_about_axis(rel, axis, 2.0)
            p_minus = centre + rotate_about_axis(rel, axis, -2.0)
            xy, wclip = self._project([p_plus, p_minus, grab, grab + axis * 10.0])
            tangent = (xy[0] - xy[1]) / 4.0                          # px per degree of rotation
            speed = float(np.linalg.norm(tangent))
            view, proj, vp, eye2 = self._camera()
            depth = max(1.0, float(wclip[2]))
            px_per_mm = max(1, self.height()) / (2.0 * depth * math.tan(math.radians(FOV_Y) * 0.5))
            floor = max(0.6, 0.35 * radius * px_per_mm * math.pi / 180.0)
            if d.tangent_dir is None:
                # The slider direction is fixed when tangent mode engages: moving
                # the mouse along it always increases the rotation, even past the
                # point where the limb turns around on screen (never twitchy).
                if speed > 0.25 * floor:
                    d.tangent_dir = tangent / speed
                else:
                    a2 = xy[3] - xy[2]
                    n = float(np.linalg.norm(a2))
                    d.tangent_dir = np.array([-a2[1], a2[0]]) / n if n > 1e-6 else np.array([1.0, 0.0])
            direction2d = d.tangent_dir
            speed = max(abs(float(np.dot(tangent, direction2d))), floor)
            dx, dy = x - d.last_xy[0], y - d.last_xy[1]
            rot = (dx * direction2d[0] + dy * direction2d[1]) / speed
            d.raw = max(lo, min(hi, d.raw + (0.25 if fine else 1.0) * rot / j.sign))
            d.mode = "tangent"
            d.prev_cursor = None
        d.last_xy = (x, y)

        value = max(lo, min(hi, d.raw))
        if mods & Qt.KeyboardModifier.ControlModifier:
            value = round(value / 5.0) * 5.0
        self._user_set({name: int(round(value))})

    def _update_ik_drag(self, x: float, y: float) -> None:
        """Two-joint IK: solve hip + knee so the grabbed point of the foot
        lands under the cursor (damped Gauss-Newton in screen space)."""
        d = self._drag
        knee, hip = self._joints[d.joint], self._joints[d.hip]
        view, proj, vp, eye = self._camera()
        w, h = max(1, self.width()), max(1, self.height())
        root = self._root()
        g = d.grab_local
        tx, ty = x + d.pixel_offset[0], y + d.pixel_offset[1]
        klo, khi = SERVO_RANGES.get(d.joint, (0, 180))
        hlo, hhi = SERVO_RANGES.get(d.hip, (0, 180))
        lo = np.array([hlo, klo], dtype=np.float64)
        hi = np.array([hhi, khi], dtype=np.float64)

        def error(a):
            m = (root @ rotation_about_line(hip.pivot, hip.axis, hip.rotation_deg(a[0]))
                 @ rotation_about_line(knee.pivot, knee.axis, knee.rotation_deg(a[1])))
            p = m[:3, :3] @ g + m[:3, 3]
            xy, wclip = project_points(vp, [p], w, h)
            return np.array([xy[0, 0] - tx, xy[0, 1] - ty])

        a = np.array([d.raw_hip, d.raw], dtype=np.float64)
        e = error(a)
        for _ in range(8):
            if float(np.linalg.norm(e)) < 0.4:
                break
            eps = 0.5
            jac = np.column_stack([(error(a + [eps, 0.0]) - e) / eps, (error(a + [0.0, eps]) - e) / eps])
            jtj = jac.T @ jac
            lam = 0.02 * float(np.trace(jtj)) + 1e-4
            step = np.linalg.solve(jtj + lam * np.eye(2), -jac.T @ e)
            n = float(np.linalg.norm(step))
            if n > 12.0:
                step *= 12.0 / n
            a_new = np.clip(a + step, lo, hi)
            e_new = error(a_new)
            if float(np.linalg.norm(e_new)) > float(np.linalg.norm(e)) + 1e-6:
                a_new = np.clip(a + step * 0.35, lo, hi)
                e_new = error(a_new)
                if float(np.linalg.norm(e_new)) > float(np.linalg.norm(e)):
                    break
            a, e = a_new, e_new
        d.raw_hip, d.raw = float(a[0]), float(a[1])
        d.last_xy = (x, y)
        self._user_set({d.hip: int(round(a[0])), d.joint: int(round(a[1]))})

    def _finish_joint_drag(self, cancel: bool = False) -> None:
        d = self._drag
        if d is None:
            return
        self._drag = None
        if cancel:
            restore = {k: v for k, v in d.start_angles.items() if self._angles.get(k) != v}
            if restore:
                self._merge_angles(restore)
                if self._sim_active():
                    self._sim_call(lambda: self._sim.set_angles_immediate(self._angles))
                    self._ensure_timer()
                self._pose_changed()
                self.anglesEdited.emit(self._snapshot())
        self._apply_cursor()
        self.update()
        self.dragFinished.emit(d.joint)

    # ======================================================================
    # events
    # ======================================================================
    def _use_hint(self) -> None:
        if not self._hint_fading and self._hint_opacity > 0.0:
            self._hint_fading = True
            self._ensure_timer()

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if self._mouse is not None and self._mouse.button == event.button():
            self._abort_gesture()                    # its release got lost (popup, focus change...)
        if self._mouse is not None or self._model is None:
            event.accept()
            return
        pos = event.position()
        x, y = pos.x(), pos.y()
        button = event.button()
        mods = event.modifiers()
        m = _MouseState(button, x, y)
        if button == Qt.MouseButton.LeftButton:
            if mods & Qt.KeyboardModifier.AltModifier or self._playing:
                m.mode = "orbit"
            else:
                handle = self._handle_hit(x, y)
                if handle is not None:
                    m.mode, m.joint, m.grab_w, m.on_handle = "joint", self._selected, handle, True
                else:
                    link, world = self._pick(x, y)
                    if link and link != BODY_LINK and link in self._joints and world is not None:
                        m.mode, m.joint, m.grab_w = "joint", link, world
                        m.ik = bool(mods & Qt.KeyboardModifier.ShiftModifier) and self._kind[link] == "knee"
                        self._select(link, emit=True)
                    elif mods & Qt.KeyboardModifier.ShiftModifier:
                        m.mode = "pan"
                    else:
                        m.mode = "orbit"
        elif button in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            m.mode = "pan"
            if button == Qt.MouseButton.RightButton and not self._playing:
                link, _ = self._pick(x, y)
                if link and link != BODY_LINK and link in self._joints:
                    m.context_joint = link
        else:
            super().mousePressEvent(event)
            return
        self._mouse = m
        event.accept()

    def _abort_gesture(self) -> None:
        """End the current mouse gesture without a release event."""
        if self._drag is not None:
            self._finish_joint_drag()
        self._mouse = None
        self._apply_cursor()

    def focusOutEvent(self, event) -> None:
        if self._mouse is not None and event.reason() not in (
                Qt.FocusReason.PopupFocusReason, Qt.FocusReason.MenuBarFocusReason):
            self._abort_gesture()
        super().focusOutEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        x, y = pos.x(), pos.y()
        m = self._mouse
        if m is None:
            if not event.buttons():
                self._update_hover(x, y)
            return
        if not m.moved:
            if math.hypot(x - m.press[0], y - m.press[1]) < DRAG_THRESHOLD_PX:
                return
            m.moved = True
            m.last = (x, y)
            self._use_hint()
            if m.mode == "joint":
                self._begin_joint_drag(m, x, y)
            else:
                self._absorb_external_camera()
                self._cam_tau = 0.045
                self._user_framed = True
            self._apply_cursor()
            return
        dx, dy = x - m.last[0], y - m.last[1]
        m.last = (x, y)
        if m.mode == "joint":
            self._update_joint_drag(x, y, event.modifiers() | QGuiApplication.keyboardModifiers())
        elif m.mode == "orbit":
            self._absorb_external_camera()
            g = self._cam_goal
            g[0] += dx * 0.38
            g[1] += dy * 0.38
            self._clamp_goal()
            self._ensure_timer()
        elif m.mode == "pan":
            self._absorb_external_camera()
            view, proj, vp, eye = self._camera()
            g = self._cam_goal
            scale = 2.0 * g[2] * math.tan(math.radians(FOV_Y) * 0.5) / max(1, self.height())
            right, up = view[0, :3], view[1, :3]
            g[3] = g[3] + (-dx * right + dy * up) * scale
            self._clamp_goal()
            self._ensure_timer()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        m = self._mouse
        if m is None or event.button() != m.button:
            return
        self._mouse = None
        pos = event.position()
        if self._drag is not None:
            self._finish_joint_drag()
        elif not m.moved:
            if m.button == Qt.MouseButton.LeftButton and m.mode != "joint" and not self._playing:
                self._select(None, emit=True)                 # click on empty space / body
            elif m.button == Qt.MouseButton.RightButton and m.context_joint:
                self._show_context_menu(m.context_joint, event.globalPosition().toPoint())
        self._apply_cursor()
        self._update_hover(pos.x(), pos.y())
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._model is not None:
            pos = event.position()
            link, _ = self._pick(pos.x(), pos.y())
            if link is None or link == BODY_LINK:
                self.frame_robot()
                event.accept()
                return
        self.mousePressEvent(event)

    def wheelEvent(self, event) -> None:
        if self._model is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.pixelDelta().y() * 4
        if delta == 0:
            return
        self._absorb_external_camera()
        self._use_hint()
        self._cam_tau = 0.045
        self._user_framed = True
        pos = event.position()
        x, y = pos.x(), pos.y()
        factor = 0.88 ** (delta / 120.0)
        g = self._cam_goal
        new_dist = max(45.0, min(1400.0, g[2] * factor))
        ratio = new_dist / g[2]
        # anchor: the surface under the cursor, else the floor, else the target plane
        link, anchor = self._pick(x, y)
        eye, direction = self._ray(x, y)
        if anchor is None:
            anchor = ray_plane(eye, direction, np.zeros(3), np.array([0.0, 1.0, 0.0]))
            if anchor is not None and float(np.linalg.norm(anchor - eye)) > 4.0 * float(self._distance):
                anchor = None
        if anchor is None:
            view = self._camera()[0]
            anchor = ray_plane(eye, direction, np.asarray(self._target, dtype=np.float64), -view[2, :3])
        if anchor is not None:
            g[3] = anchor + (g[3] - anchor) * ratio
        g[2] = new_dist
        self._clamp_goal()
        self._ensure_timer()
        event.accept()

    def leaveEvent(self, event) -> None:
        if self._mouse is None:
            self._set_hover(None, False, emit=True)
        super().leaveEvent(event)

    def event(self, event) -> bool:
        # Tab / Shift+Tab cycle the joints instead of moving keyboard focus
        if event.type() == QEvent.Type.KeyPress and self._model is not None:
            key = event.key()
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                back = key == Qt.Key.Key_Backtab or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self._cycle_selection(-1 if back else 1)
                event.accept()
                return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mods = event.modifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        plain = not (mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                             | Qt.KeyboardModifier.MetaModifier))
        if key == Qt.Key.Key_Escape:
            if self._drag is not None:
                self._finish_joint_drag(cancel=True)
                if self._mouse is not None:
                    self._mouse.mode = None          # swallow the rest of this gesture
                event.accept()
                return
            if self._selected is not None:
                self._select(None, emit=True)
                event.accept()
                return
        elif plain and key in (Qt.Key.Key_Left, Qt.Key.Key_BracketLeft) and self._selected:
            self._nudge(-10 if shift else -1)
            event.accept()
            return
        elif plain and key in (Qt.Key.Key_Right, Qt.Key.Key_BracketRight) and self._selected:
            self._nudge(10 if shift else 1)
            event.accept()
            return
        elif plain and not shift and key == Qt.Key.Key_F:
            self.frame_robot()
            event.accept()
            return
        elif plain and not shift and key == Qt.Key.Key_Home:
            self.reset_view()
            event.accept()
            return
        super().keyPressEvent(event)

    def _show_context_menu(self, joint: str, global_pos: QPoint) -> None:
        self._select(joint, emit=True)
        menu = QMenu(self)
        try:
            theme.polish_popup(menu)
        except Exception:
            pass
        cur = int(round(self._angles[joint]))
        title = menu.addAction(f"{joint}  ·  {self._kind[joint]}  ·  {cur}°")
        title.setEnabled(False)
        menu.addSeparator()
        stand = int(round(self._model.bind_angles.get(joint, 90.0)))
        twin = self._twin.get(joint)
        menu.addAction("Reset to 90°").triggered.connect(lambda: self._edit_once(joint, {joint: 90}))
        menu.addAction(f"Set to stand angle ({stand}°)").triggered.connect(
            lambda: self._edit_once(joint, {joint: stand}))
        a_mirror = menu.addAction(f"Mirror onto {twin}" if twin else "Mirror onto twin")
        a_mirror.setEnabled(bool(twin))
        if twin:
            a_mirror.triggered.connect(lambda: self._edit_once(twin, {twin: 180 - cur}))
        menu.exec(global_pos)
        menu.deleteLater()
        self._update_hover(*self._cursor_xy())

    def _cursor_xy(self):
        p = self.mapFromGlobal(QCursor.pos())
        return float(p.x()), float(p.y())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._hud is not None:
            self._hud.reflow(self.width())
        if self._error_label.isVisible():
            self._error_label.setGeometry(self.rect())
        if self._model is not None and not self._user_framed:
            self._absorb_external_camera()
            if not self._user_framed:
                self._snap_camera_to_frame()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._hud is not None:
            self._hud.reflow(self.width())
            self._hud.raise_()
        self._ensure_timer()                         # the tick stops itself when there is nothing to do

    def hideEvent(self, event) -> None:
        self._timer.stop()
        if self._mouse is not None:
            self._abort_gesture()
        super().hideEvent(event)
