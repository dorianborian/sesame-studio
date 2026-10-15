"""Posable 3D robot viewport.

Renders the real printed geometry (hardware/printing/stl/*.stl) using the
fixed-function OpenGL matrix stack: each limb is a glPushMatrix/glPopMatrix
block that rotates around its calibrated pivot, and two-segment legs
(R3 mounted on R1, etc.) nest their child block inside the parent's so the
child inherits the parent's rotation for free -- a standard forward-
kinematics trick that needs no manual matrix math.

Joint pivots/axes are *estimates* derived from STL bounding boxes (see
assets/joints_config.json) and are meant to be refined once against the
real hardware using JointCalibrationPanel below. Three things are
calibratable per part:

- **Pivot** (joints only): the rotation center used for live posing. Moving
  it changes where the joint hinges from, and moves any child joint mounted
  on it.
- **Position offset** (any part): a static visual-only nudge applied to
  that part's mesh alone (frame/cover/bottom, or a joint's own mesh).
- **Rotation offset** (any part): a static visual-only spin of that part's
  mesh about its own center, in degrees.

Neither offset affects live posing or child joints -- they only exist to
fix parts that render slightly out of place/misrotated because their STL
was exported with a different origin/orientation than its neighbors.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from OpenGL import GL, GLU
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QDoubleSpinBox, QPushButton, QRadioButton, QButtonGroup, QGroupBox,
    QCheckBox,
)

from .stl_loader import load_stl, Mesh
from .servo_model import SERVO_ORDER, SERVO_COLORS, SERVO_RANGES, FACE_WIDTH, FACE_HEIGHT
from .theme import ACCENT
from .paths import STL_DIR, BASE_DIR, get_joints_config_load_path, get_joints_config_save_path


def _find_stl_root() -> Path:
    candidates = [
        STL_DIR,
        BASE_DIR.parent / "sesame-robot" / "hardware" / "printing" / "stl",
        BASE_DIR.parent.parent / "hardware" / "printing" / "stl",
    ]
    for c in candidates:
        if c.is_dir() and any(c.glob("*.stl")):
            return c
    return candidates[0]


STL_ROOT = _find_stl_root()

AXIS_VECTORS = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}
AXIS_COLORS = {"X": (1.0, 0.2, 0.2), "Y": (0.2, 1.0, 0.2), "Z": (0.3, 0.5, 1.0)}
GIZMO_ARM_LEN = 18.0
GIZMO_PICK_PX = 12.0
ROTATE_DEG_PER_PIXEL = 0.6

BODY_PART_KEYS = ("frame", "bottom", "cover")
BODY_PART_LABELS = {
    "frame": "Frame (Body)",
    "bottom": "Bottom Cover (Body)",
    "cover": "Top Cover (Body)",
}

FACE_PLANE_KEY = "face_plane"
FACE_PLANE_LABEL = "Face Display (Screen)"
NO_PIVOT_KEYS = BODY_PART_KEYS + (FACE_PLANE_KEY,)
DEFAULT_FACE_WIDTH_IN = 0.96
MM_PER_IN = 25.4

_HEX_CACHE: dict[str, tuple[float, float, float]] = {}


def _default_offset() -> dict:
    return {"position": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0]}


def _normalize_offset(raw) -> dict:
    """Accepts either the current {"position":[...], "rotation":[...]}
    shape or the older flat [x, y, z] (position-only) shape, and returns
    the normalized dict form."""
    if isinstance(raw, dict):
        pos = list(raw.get("position", [0.0, 0.0, 0.0]))
        rot = list(raw.get("rotation", [0.0, 0.0, 0.0]))
        return {"position": pos, "rotation": rot}
    if isinstance(raw, (list, tuple)):
        return {"position": list(raw), "rotation": [0.0, 0.0, 0.0]}
    return _default_offset()


def _normalize_face_plane(raw, frame_mesh: Mesh) -> dict:
    """The OLED face-preview plane's placement. Defaults to just beyond the
    frame's max-X face, vertically/laterally centered -- a best-effort guess
    (nobody encoded which end is actually the front), refine it in-app via
    the calibration panel like any other part."""
    if isinstance(raw, dict) and "position" in raw:
        return {
            "position": list(raw.get("position", [0.0, 0.0, 0.0])),
            "rotation": list(raw.get("rotation", [0.0, 0.0, 0.0])),
            "width_in": float(raw.get("width_in", DEFAULT_FACE_WIDTH_IN)),
        }
    cy, cz = float(frame_mesh.center[1]), float(frame_mesh.center[2])
    front_x = float(frame_mesh.bounds_max[0]) + 2.0
    return {
        "position": [front_x, cy, cz],
        "rotation": [0.0, 0.0, 0.0],
        "width_in": DEFAULT_FACE_WIDTH_IN,
    }


def _point_segment_distance(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    abx, aby = bx - ax, by - ay
    length_sq = abx * abx + aby * aby
    if length_sq < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / length_sq))
    proj_x, proj_y = ax + t * abx, ay + t * aby
    return math.hypot(px - proj_x, py - proj_y)


def _hex_to_rgb(h: str) -> tuple[float, float, float]:
    if h not in _HEX_CACHE:
        h = h.lstrip("#")
        _HEX_CACHE[h] = (
            int(h[0:2], 16) / 255.0,
            int(h[2:4], 16) / 255.0,
            int(h[4:6], 16) / 255.0,
        )
    return _HEX_CACHE[h]


def apply_desktop_gl_format() -> None:
    """Requests a compatibility-profile context so the fixed-function
    pipeline (glRotatef/glVertexPointer/glLight) used below is available.
    Must be called before any QOpenGLWidget is constructed."""
    fmt = QSurfaceFormat()
    fmt.setDepthBufferSize(24)
    fmt.setSamples(4)
    fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
    fmt.setVersion(2, 1)
    QSurfaceFormat.setDefaultFormat(fmt)


class JointRig:
    """Loads assets/joints_config.json and the STL meshes it references."""

    def __init__(self):
        config_path = get_joints_config_load_path()
        with open(config_path, "r", encoding="utf-8") as fh:
            self.config = json.load(fh)
        self._mesh_cache: dict[str, Mesh] = {}

        body_cfg = self.config["body"]
        for key in BODY_PART_KEYS:
            offset_key = f"{key}_offset"
            body_cfg[offset_key] = _normalize_offset(body_cfg.get(offset_key))

        self.body_frame = self._load(body_cfg["frame_stl"])
        self.body_bottom = self._load(body_cfg["bottom_stl"])
        try:
            self.body_cover = self._load(body_cfg["cover_stl"])
        except FileNotFoundError:
            self.body_cover = None

        self.config[FACE_PLANE_KEY] = _normalize_face_plane(
            self.config.get(FACE_PLANE_KEY), self.body_frame
        )

        for cfg in self.config["joints"].values():
            cfg["offset"] = _normalize_offset(cfg.get("offset"))
            cfg.setdefault("invert", False)

        self.joint_meshes: dict[str, Mesh] = {
            name: self._load(cfg["stl"]) for name, cfg in self.config["joints"].items()
        }
        self.roots = [n for n, c in self.config["joints"].items() if not c.get("parent")]
        self.children: dict[str, list[str]] = {n: [] for n in self.config["joints"]}
        for name, cfg in self.config["joints"].items():
            parent = cfg.get("parent")
            if parent:
                self.children[parent].append(name)

    def _load(self, rel_path: str) -> Mesh:
        if rel_path not in self._mesh_cache:
            path = STL_ROOT / rel_path
            self._mesh_cache[rel_path] = load_stl(str(path))
        return self._mesh_cache[rel_path]

    def frame_center(self) -> np.ndarray:
        return self.body_frame.center

    def bounding_radius(self) -> float:
        extent = self.body_frame.bounds_max - self.body_frame.bounds_min
        return float(np.linalg.norm(extent)) or 100.0

    def body_mesh(self, key: str) -> Mesh | None:
        return {"frame": self.body_frame, "bottom": self.body_bottom, "cover": self.body_cover}.get(key)

    def save(self) -> None:
        save_path = get_joints_config_save_path()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as fh:
            json.dump(self.config, fh, indent=2)


def _draw_mesh(mesh: Mesh) -> None:
    if len(mesh.vertices) == 0:
        return
    GL.glEnableClientState(GL.GL_VERTEX_ARRAY)
    GL.glEnableClientState(GL.GL_NORMAL_ARRAY)
    GL.glVertexPointer(3, GL.GL_FLOAT, 0, mesh.vertices)
    GL.glNormalPointer(GL.GL_FLOAT, 0, mesh.normals)
    GL.glDrawArrays(GL.GL_TRIANGLES, 0, len(mesh.vertices))
    GL.glDisableClientState(GL.GL_VERTEX_ARRAY)
    GL.glDisableClientState(GL.GL_NORMAL_ARRAY)


def _apply_offset_transform(offset: dict, center: np.ndarray) -> None:
    """Pushes position + rotation-about-mesh-center onto the current GL
    matrix. Caller is responsible for the matching glPopMatrix()."""
    pos = offset["position"]
    rot = offset["rotation"]
    cx, cy, cz = float(center[0]), float(center[1]), float(center[2])
    GL.glTranslatef(*pos)
    GL.glTranslatef(cx, cy, cz)
    GL.glRotatef(rot[2], 0, 0, 1)
    GL.glRotatef(rot[1], 0, 1, 0)
    GL.glRotatef(rot[0], 1, 0, 0)
    GL.glTranslatef(-cx, -cy, -cz)


class Viewport3D(QOpenGLWidget):
    """Orbit-camera posable robot view. Call set_angles() to pose it."""

    angleHovered = Signal(str, int)
    calibrationEdited = Signal()  # emitted after a live gizmo drag updates rig.config

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Viewport3D")
        self.setMinimumHeight(260)
        self.rig: JointRig | None = None
        self.angles: dict[str, int] = {s: 90 for s in SERVO_ORDER}
        self.show_labels = True
        self.show_grid = True
        self.calibration_mode = False
        self.calib_target: str | None = None      # joint name, or one of NO_PIVOT_KEYS
        self.calib_edit_mode: str = "pivot"        # "pivot" (joints only) or "offset"
        self.gizmo_mode: str = "move"               # "move" or "rotate" -- only used in "offset" mode

        self._yaw = -35.0
        self._pitch = 22.0
        self._distance = 260.0
        self._target = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        self._last_pos = None
        self._panning = False

        # Gizmo picking/dragging state -- matrices captured during paintGL at
        # the calibration target's world transform (see _draw_joint_chain /
        # _draw_body_part), used to project the gizmo anchor to screen space
        # for hit-testing and axis-constrained dragging.
        self._gizmo_modelview = None
        self._gizmo_projection = None
        self._gizmo_viewport = None
        self._hover_axis: str | None = None
        self._drag_axis: str | None = None
        self._drag_kind: str | None = None          # "pivot" | "position" | "rotation"
        self._drag_start_mouse: tuple[float, float] | None = None
        self._drag_start_value: list[float] | None = None
        self._drag_screen_dir: tuple[float, float] | None = None
        self._drag_world_per_px: float = 1.0

        # OLED face-preview texture, uploaded lazily on the GL thread.
        self._face_texture_id = None
        self._face_pixels = None            # 128x64 bool grid, row-major, or None (blank)
        self._face_texture_dirty = True

        self.setMouseTracking(True)

    # -- public API ---------------------------------------------------
    def set_angles(self, angles: dict[str, int]) -> None:
        self.angles.update(angles)
        self.update()

    def set_face_preview(self, pixels) -> None:
        """pixels: 128x64 bool grid (row-major, pixels[y][x]), or None to
        blank the on-model screen."""
        self._face_pixels = pixels
        self._face_texture_dirty = True
        self.update()

    def reset_view(self) -> None:
        self._yaw, self._pitch = -35.0, 22.0
        if self.rig:
            self._distance = self.rig.bounding_radius() * 1.8
            self._target = np.array(self.rig.frame_center(), dtype=np.float32)
        self.update()

    def set_calib_target(self, target: str | None, edit_mode: str = "pivot") -> None:
        self.calib_target = target
        self.calib_edit_mode = edit_mode
        self._hover_axis = None
        self.update()

    def set_gizmo_mode(self, mode: str) -> None:
        self.gizmo_mode = mode
        self._hover_axis = None
        self.update()

    # -- GL lifecycle ---------------------------------------------------
    def initializeGL(self) -> None:
        GL.glClearColor(0.02, 0.02, 0.02, 1.0)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_LIGHTING)
        GL.glEnable(GL.GL_LIGHT0)
        GL.glEnable(GL.GL_NORMALIZE)
        GL.glEnable(GL.GL_COLOR_MATERIAL)
        GL.glColorMaterial(GL.GL_FRONT_AND_BACK, GL.GL_AMBIENT_AND_DIFFUSE)
        GL.glLightfv(GL.GL_LIGHT0, GL.GL_POSITION, [200.0, 400.0, 300.0, 0.0])
        GL.glLightfv(GL.GL_LIGHT0, GL.GL_DIFFUSE, [0.9, 0.9, 0.9, 1.0])
        GL.glLightfv(GL.GL_LIGHT0, GL.GL_AMBIENT, [0.35, 0.35, 0.38, 1.0])
        self._face_texture_id = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._face_texture_id)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        self.rig = JointRig()
        self.reset_view()

    def resizeGL(self, w: int, h: int) -> None:
        GL.glViewport(0, 0, max(1, w), max(1, h))

    def paintGL(self) -> None:
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        if not self.rig:
            return
        w, h = max(1, self.width()), max(1, self.height())

        GL.glMatrixMode(GL.GL_PROJECTION)
        GL.glLoadIdentity()
        GLU.gluPerspective(45.0, w / h, 1.0, 5000.0)

        GL.glMatrixMode(GL.GL_MODELVIEW)
        GL.glLoadIdentity()
        eye = self._camera_position()
        GLU.gluLookAt(*eye, *self._target, 0, 1, 0)

        if self.show_grid:
            self._draw_grid()

        self._draw_body_part("frame", self.rig.body_frame, (0.75, 0.75, 0.78))
        self._draw_body_part("bottom", self.rig.body_bottom, (0.75, 0.75, 0.78))
        if self.rig.body_cover is not None:
            self._draw_body_part("cover", self.rig.body_cover, (0.55, 0.6, 0.66))

        for root in self.rig.roots:
            self._draw_joint_chain(root)

        self._draw_face_plane()

    # -- drawing helpers ---------------------------------------------------
    def _camera_position(self) -> tuple[float, float, float]:
        yaw = math.radians(self._yaw)
        pitch = math.radians(self._pitch)
        x = self._distance * math.cos(pitch) * math.cos(yaw)
        y = self._distance * math.sin(pitch)
        z = self._distance * math.cos(pitch) * math.sin(yaw)
        return (self._target[0] + x, self._target[1] + y, self._target[2] + z)

    def _draw_grid(self, size: float = 200.0, step: float = 20.0) -> None:
        GL.glDisable(GL.GL_LIGHTING)
        GL.glColor3f(0.16, 0.16, 0.18)
        y = self.rig.body_bottom.bounds_min[1] if self.rig.body_bottom.vertices.size else 0.0
        GL.glBegin(GL.GL_LINES)
        n = int(size / step)
        for i in range(-n, n + 1):
            GL.glVertex3f(i * step, y, -size)
            GL.glVertex3f(i * step, y, size)
            GL.glVertex3f(-size, y, i * step)
            GL.glVertex3f(size, y, i * step)
        GL.glEnd()
        GL.glEnable(GL.GL_LIGHTING)

    def _draw_body_part(self, key: str, mesh: Mesh | None, color: tuple[float, float, float]) -> None:
        if mesh is None:
            return
        offset = self.rig.config["body"][f"{key}_offset"]
        is_target = self.calibration_mode and self.calib_target == key
        if is_target:
            # World space (camera view only, no part transform yet) -- the
            # correct basis for a gizmo anchor expressed in world coords.
            self._capture_gizmo_matrices()

        GL.glColor3f(*(1.0, 0.85, 0.2) if is_target else color)
        GL.glPushMatrix()
        _apply_offset_transform(offset, mesh.center)
        _draw_mesh(mesh)
        GL.glPopMatrix()

        if is_target:
            self._draw_calibration_gizmo(mesh.center, offset)

    def _draw_joint_chain(self, name: str) -> None:
        cfg = self.rig.config["joints"][name]
        px, py, pz = cfg["pivot"]
        rest = cfg.get("rest_angle", 90)
        raw_angle = self.angles.get(name, rest)
        if cfg.get("invert", False):
            # Mirror around the servo's range so e.g. 180 renders like 0 and
            # vice versa -- corrects joints mounted/wired in the opposite
            # direction without touching the angle values themselves (the
            # exported firmware code still sees the literal angle).
            lo, hi = SERVO_RANGES.get(name, (0, 180))
            raw_angle = lo + hi - raw_angle
        angle = raw_angle - rest
        axis = AXIS_VECTORS.get(cfg.get("axis", "Y"), (0, 1, 0))
        offset = cfg["offset"]
        is_target = self.calibration_mode and self.calib_target == name

        GL.glPushMatrix()
        if is_target and self.calib_edit_mode == "pivot":
            # Capture the parent-accumulated transform *before* this joint's
            # own rotation is applied -- a point transformed by
            # T(p)*R*T(-p) is invariant under R, so this matrix is exactly
            # what's needed to project/unproject the pivot in world space.
            self._capture_gizmo_matrices()
        GL.glTranslatef(px, py, pz)
        GL.glRotatef(angle, *axis)
        GL.glTranslatef(-px, -py, -pz)

        if is_target and self.calib_edit_mode == "offset":
            # Now in this joint's fully-posed local space -- correct basis
            # for the offset gizmo anchor.
            self._capture_gizmo_matrices()

        mesh = self.rig.joint_meshes[name]
        GL.glPushMatrix()
        _apply_offset_transform(offset, mesh.center)
        color = _hex_to_rgb(SERVO_COLORS.get(name, ACCENT))
        GL.glColor3f(*((1.0, 0.85, 0.2) if is_target else color))
        _draw_mesh(mesh)
        GL.glPopMatrix()

        if is_target:
            if self.calib_edit_mode == "pivot":
                self._draw_calibration_gizmo(None, None, anchor_override=(px, py, pz))
            else:
                self._draw_calibration_gizmo(mesh.center, offset)

        for child in self.rig.children.get(name, []):
            self._draw_joint_chain(child)

        GL.glPopMatrix()

    def _ensure_face_texture(self) -> None:
        if not self._face_texture_dirty or self._face_texture_id is None:
            return
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._face_texture_id)
        if self._face_pixels is None:
            data = bytes(FACE_WIDTH * FACE_HEIGHT)
        else:
            # OpenGL textures are bottom-up; flip so the image reads upright.
            rows = bytearray()
            for y in range(FACE_HEIGHT - 1, -1, -1):
                row = self._face_pixels[y]
                rows.extend(255 if v else 0 for v in row)
            data = bytes(rows)
        GL.glTexImage2D(
            GL.GL_TEXTURE_2D, 0, GL.GL_LUMINANCE, FACE_WIDTH, FACE_HEIGHT, 0,
            GL.GL_LUMINANCE, GL.GL_UNSIGNED_BYTE, data,
        )
        self._face_texture_dirty = False

    def _draw_face_plane(self) -> None:
        if not self.rig:
            return
        cfg = self.rig.config[FACE_PLANE_KEY]
        pos = cfg["position"]
        rot = cfg["rotation"]
        width_mm = cfg.get("width_in", DEFAULT_FACE_WIDTH_IN) * MM_PER_IN
        height_mm = width_mm * (FACE_HEIGHT / FACE_WIDTH)
        hw, hh = width_mm / 2.0, height_mm / 2.0

        is_target = self.calibration_mode and self.calib_target == FACE_PLANE_KEY
        if is_target:
            self._capture_gizmo_matrices()

        self._ensure_face_texture()

        GL.glPushMatrix()
        GL.glTranslatef(*pos)
        GL.glRotatef(rot[2], 0, 0, 1)
        GL.glRotatef(rot[1], 0, 1, 0)
        GL.glRotatef(rot[0], 1, 0, 0)

        GL.glDisable(GL.GL_LIGHTING)  # self-illuminated, like a real OLED
        GL.glEnable(GL.GL_TEXTURE_2D)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._face_texture_id)
        GL.glColor3f(1.0, 1.0, 1.0)
        GL.glBegin(GL.GL_QUADS)
        GL.glTexCoord2f(0, 0); GL.glVertex3f(0, hh, -hw)
        GL.glTexCoord2f(1, 0); GL.glVertex3f(0, hh, hw)
        GL.glTexCoord2f(1, 1); GL.glVertex3f(0, -hh, hw)
        GL.glTexCoord2f(0, 1); GL.glVertex3f(0, -hh, -hw)
        GL.glEnd()
        GL.glDisable(GL.GL_TEXTURE_2D)

        # Thin bezel so the plane reads as a screen even with no face set.
        GL.glColor3f(0.3, 0.3, 0.3)
        GL.glLineWidth(1.5)
        GL.glBegin(GL.GL_LINE_LOOP)
        GL.glVertex3f(0, hh, -hw); GL.glVertex3f(0, hh, hw)
        GL.glVertex3f(0, -hh, hw); GL.glVertex3f(0, -hh, -hw)
        GL.glEnd()
        GL.glLineWidth(1.0)
        GL.glEnable(GL.GL_LIGHTING)
        GL.glPopMatrix()

        if is_target:
            self._draw_gizmo_arrows(float(pos[0]), float(pos[1]), float(pos[2]))

    def _draw_calibration_gizmo(self, center, offset, anchor_override: tuple[float, float, float] | None = None) -> None:
        if anchor_override is not None:
            x, y, z = anchor_override
        else:
            pos = offset["position"]
            x = float(center[0]) + pos[0]
            y = float(center[1]) + pos[1]
            z = float(center[2]) + pos[2]
        self._draw_gizmo_arrows(x, y, z)

    def _draw_gizmo_arrows(self, x: float, y: float, z: float, size: float = GIZMO_ARM_LEN) -> None:
        active = self._drag_axis or self._hover_axis
        rotating = self.calib_edit_mode == "offset" and self.gizmo_mode == "rotate"
        GL.glDisable(GL.GL_LIGHTING)
        GL.glDisable(GL.GL_DEPTH_TEST)  # stay visible/grabbable through the mesh
        GL.glLineWidth(4.0)
        GL.glBegin(GL.GL_LINES)
        for axis_name, vec in AXIS_VECTORS.items():
            color = (1.0, 1.0, 0.3) if axis_name == active else AXIS_COLORS[axis_name]
            GL.glColor3f(*color)
            GL.glVertex3f(x, y, z)
            GL.glVertex3f(x + vec[0] * size, y + vec[1] * size, z + vec[2] * size)
        GL.glEnd()
        if rotating:
            # Small ring around each arm's tip hints "this drags a rotation,
            # not a translation" without needing full arcball hit-testing.
            for axis_name, vec in AXIS_VECTORS.items():
                color = (1.0, 1.0, 0.3) if axis_name == active else AXIS_COLORS[axis_name]
                GL.glColor3f(*color)
                self._draw_ring(x + vec[0] * size, y + vec[1] * size, z + vec[2] * size, axis_name, size * 0.28)
        GL.glLineWidth(1.0)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_LIGHTING)

    def _draw_ring(self, x: float, y: float, z: float, axis_name: str, radius: float, segments: int = 20) -> None:
        GL.glBegin(GL.GL_LINE_LOOP)
        for i in range(segments):
            t = 2 * math.pi * i / segments
            c, s = math.cos(t) * radius, math.sin(t) * radius
            if axis_name == "X":
                GL.glVertex3f(x, y + c, z + s)
            elif axis_name == "Y":
                GL.glVertex3f(x + c, y, z + s)
            else:
                GL.glVertex3f(x + c, y + s, z)
        GL.glEnd()

    # -- gizmo picking / projection ---------------------------------------
    def _capture_gizmo_matrices(self) -> None:
        self._gizmo_modelview = GL.glGetDoublev(GL.GL_MODELVIEW_MATRIX)
        self._gizmo_projection = GL.glGetDoublev(GL.GL_PROJECTION_MATRIX)
        self._gizmo_viewport = GL.glGetIntegerv(GL.GL_VIEWPORT)

    def _project_point(self, x: float, y: float, z: float) -> tuple[float, float] | None:
        if self._gizmo_modelview is None or self._gizmo_viewport is None:
            return None
        try:
            wx, wy, _wz = GLU.gluProject(
                x, y, z, self._gizmo_modelview, self._gizmo_projection, self._gizmo_viewport
            )
        except Exception:
            return None
        # OpenGL window coords are bottom-origin; Qt widget coords are top-origin.
        return wx, self._gizmo_viewport[3] - wy

    def _gizmo_value_kind(self) -> str:
        if self.calib_edit_mode == "pivot":
            return "pivot"
        return "rotation" if self.gizmo_mode == "rotate" else "position"

    def _gizmo_visual_anchor(self) -> list[float] | None:
        """World-space point where the gizmo arrows are drawn/projected --
        stays at the part's (possibly offset) location regardless of
        whether position or rotation is currently being dragged."""
        if not self.rig or not self.calib_target:
            return None
        if self.calib_target == FACE_PLANE_KEY:
            return list(self.rig.config[FACE_PLANE_KEY]["position"])
        if self.calib_target in BODY_PART_KEYS:
            mesh = self.rig.body_mesh(self.calib_target)
            if mesh is None:
                return None
            pos = self.rig.config["body"][f"{self.calib_target}_offset"]["position"]
            return [float(mesh.center[0]) + pos[0], float(mesh.center[1]) + pos[1], float(mesh.center[2]) + pos[2]]
        cfg = self.rig.config["joints"].get(self.calib_target)
        if not cfg:
            return None
        if self.calib_edit_mode == "pivot":
            return list(cfg["pivot"])
        mesh = self.rig.joint_meshes[self.calib_target]
        pos = cfg["offset"]["position"]
        return [float(mesh.center[0]) + pos[0], float(mesh.center[1]) + pos[1], float(mesh.center[2]) + pos[2]]

    def _gizmo_get_value(self) -> list[float] | None:
        """The 3-vector actually being dragged: pivot (mm), offset position
        (mm), or offset rotation (degrees), depending on current mode."""
        if not self.rig or not self.calib_target:
            return None
        kind = self._gizmo_value_kind()
        if kind == "pivot":
            cfg = self.rig.config["joints"].get(self.calib_target)
            return list(cfg["pivot"]) if cfg else None
        if self.calib_target == FACE_PLANE_KEY:
            return list(self.rig.config[FACE_PLANE_KEY][kind])
        offset = (
            self.rig.config["body"][f"{self.calib_target}_offset"]
            if self.calib_target in BODY_PART_KEYS
            else self.rig.config["joints"][self.calib_target]["offset"]
        )
        return list(offset[kind])

    def _gizmo_set_value(self, value: list[float]) -> None:
        if not self.rig or not self.calib_target:
            return
        kind = self._gizmo_value_kind()
        if kind == "pivot":
            cfg = self.rig.config["joints"].get(self.calib_target)
            if cfg:
                cfg["pivot"] = value
            return
        if self.calib_target == FACE_PLANE_KEY:
            self.rig.config[FACE_PLANE_KEY][kind] = value
            return
        offset = (
            self.rig.config["body"][f"{self.calib_target}_offset"]
            if self.calib_target in BODY_PART_KEYS
            else self.rig.config["joints"][self.calib_target]["offset"]
        )
        offset[kind] = value

    def _hit_test_gizmo(self, mx: float, my: float) -> str | None:
        anchor = self._gizmo_visual_anchor()
        if anchor is None:
            return None
        origin = self._project_point(*anchor)
        if origin is None:
            return None
        best_axis, best_dist = None, GIZMO_PICK_PX
        for axis_name, vec in AXIS_VECTORS.items():
            tip = self._project_point(
                anchor[0] + vec[0] * GIZMO_ARM_LEN,
                anchor[1] + vec[1] * GIZMO_ARM_LEN,
                anchor[2] + vec[2] * GIZMO_ARM_LEN,
            )
            if tip is None:
                continue
            dist = _point_segment_distance((mx, my), origin, tip)
            if dist < best_dist:
                best_dist, best_axis = dist, axis_name
        return best_axis

    def _begin_drag(self, axis: str, mx: float, my: float) -> None:
        anchor = self._gizmo_visual_anchor()
        value = self._gizmo_get_value()
        if anchor is None or value is None:
            return
        origin = self._project_point(*anchor)
        vec = AXIS_VECTORS[axis]
        tip = self._project_point(
            anchor[0] + vec[0] * GIZMO_ARM_LEN,
            anchor[1] + vec[1] * GIZMO_ARM_LEN,
            anchor[2] + vec[2] * GIZMO_ARM_LEN,
        )
        if origin is None or tip is None:
            return
        dx, dy = tip[0] - origin[0], tip[1] - origin[1]
        length = math.hypot(dx, dy) or 1.0
        self._drag_axis = axis
        self._drag_kind = self._gizmo_value_kind()
        self._drag_start_mouse = (mx, my)
        self._drag_start_value = list(value)
        self._drag_screen_dir = (dx / length, dy / length)
        self._drag_world_per_px = GIZMO_ARM_LEN / length

    def _update_drag(self, mx: float, my: float) -> None:
        if not self._drag_axis or self._drag_screen_dir is None or self._drag_start_value is None:
            return
        dx = mx - self._drag_start_mouse[0]
        dy = my - self._drag_start_mouse[1]
        raw = dx * self._drag_screen_dir[0] + dy * self._drag_screen_dir[1]
        scalar = raw * ROTATE_DEG_PER_PIXEL if self._drag_kind == "rotation" else raw * self._drag_world_per_px
        vec = AXIS_VECTORS[self._drag_axis]
        new_value = [self._drag_start_value[i] + vec[i] * scalar for i in range(3)]
        self._gizmo_set_value(new_value)
        self.calibrationEdited.emit()
        self.update()

    # -- mouse / camera interaction ---------------------------------------
    def mousePressEvent(self, event) -> None:
        pos = event.position()
        if (self.calibration_mode and self.calib_target
                and event.button() == Qt.LeftButton):
            axis = self._hit_test_gizmo(pos.x(), pos.y())
            if axis:
                self._begin_drag(axis, pos.x(), pos.y())
                self.update()
                return
        self._last_pos = pos
        self._panning = event.buttons() & Qt.MiddleButton or (
            event.buttons() & Qt.LeftButton and event.modifiers() & Qt.ShiftModifier
        )

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        if self._drag_axis:
            self._update_drag(pos.x(), pos.y())
            return

        if (self.calibration_mode and self.calib_target
                and not (event.buttons() & (Qt.LeftButton | Qt.MiddleButton))):
            hover = self._hit_test_gizmo(pos.x(), pos.y())
            if hover != self._hover_axis:
                self._hover_axis = hover
                self.update()

        if self._last_pos is None:
            return
        dx = pos.x() - self._last_pos.x()
        dy = pos.y() - self._last_pos.y()
        self._last_pos = pos
        if self._panning:
            right = np.array([math.sin(math.radians(self._yaw - 90)), 0,
                               math.cos(math.radians(self._yaw - 90))])
            up = np.array([0.0, 1.0, 0.0])
            self._target += (-dx * right + dy * up) * (self._distance * 0.0025)
        elif event.buttons() & Qt.LeftButton:
            self._yaw += dx * 0.4
            self._pitch = max(-85.0, min(85.0, self._pitch + dy * 0.4))
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_axis:
            self._drag_axis = None
            self._drag_kind = None
            self._drag_start_mouse = None
            self._drag_start_value = None
            self._drag_screen_dir = None
            self.update()
            return
        self._last_pos = None
        self._panning = False

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        factor = 0.88 if delta > 0 else 1.12
        self._distance = max(30.0, min(3000.0, self._distance * factor))
        self.update()


class JointCalibrationPanel(QWidget):
    """Editor for the currently selected part's pivot (joints only) and/or
    default position+rotation offset (any part), used to refine the
    auto-estimated joints_config.json against real hardware. Backs both the
    numeric fields here and the drag gizmo drawn directly in the 3D view."""

    def __init__(self, viewport: Viewport3D, parent=None):
        super().__init__(parent)
        self.viewport = viewport
        layout = QVBoxLayout(self)

        note = QLabel(
            "Select a part, enable Calibration Mode, then drag the "
            "red/green/blue arrows directly in the 3D view (or type exact "
            "numbers below). 'Pivot' moves a joint's rotation center. "
            "'Position/Rotation Offset' nudges a part's mesh only, without "
            "affecting rotation or child joints -- use it to fix parts that "
            "render slightly out of place or misrotated."
        )
        note.setWordWrap(True)
        note.setProperty("role", "subheading")
        layout.addWidget(note)

        self.calib_check = QCheckBox("Calibration Mode")
        self.calib_check.toggled.connect(self._on_calib_toggled)
        layout.addWidget(self.calib_check)

        self.target_combo = QComboBox()
        for key in BODY_PART_KEYS:
            self.target_combo.addItem(BODY_PART_LABELS[key], key)
        self.target_combo.insertSeparator(self.target_combo.count())
        for servo in SERVO_ORDER:
            self.target_combo.addItem(servo, servo)
        self.target_combo.insertSeparator(self.target_combo.count())
        self.target_combo.addItem(FACE_PLANE_LABEL, FACE_PLANE_KEY)
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        layout.addWidget(self.target_combo)

        mode_box = QGroupBox("Editing")
        mode_layout = QHBoxLayout(mode_box)
        self.mode_group = QButtonGroup(self)
        self.pivot_radio = QRadioButton("Pivot / Rotation Center")
        self.offset_radio = QRadioButton("Position / Rotation Offset")
        self.offset_radio.setChecked(True)
        for btn in (self.pivot_radio, self.offset_radio):
            self.mode_group.addButton(btn)
            mode_layout.addWidget(btn)
            btn.toggled.connect(self._on_mode_changed)
        layout.addWidget(mode_box)

        pivot_box = QGroupBox("Pivot (mm)")
        pivot_layout = QHBoxLayout(pivot_box)
        self.pivot_spins: dict[str, QDoubleSpinBox] = {}
        for axis in "XYZ":
            spin = QDoubleSpinBox()
            spin.setRange(-500, 500)
            spin.setDecimals(2)
            spin.valueChanged.connect(self._on_pivot_changed)
            pivot_layout.addWidget(QLabel(axis))
            pivot_layout.addWidget(spin)
            self.pivot_spins[axis] = spin
        layout.addWidget(pivot_box)
        self.pivot_box = pivot_box

        axis_box = QGroupBox("Rotation Axis")
        axis_layout = QVBoxLayout(axis_box)
        axis_radio_row = QHBoxLayout()
        self.axis_group = QButtonGroup(self)
        for axis in "XYZ":
            btn = QRadioButton(axis)
            btn.toggled.connect(self._on_axis_changed)
            axis_radio_row.addWidget(btn)
            self.axis_group.addButton(btn)
            setattr(self, f"_axis_btn_{axis}", btn)
        axis_layout.addLayout(axis_radio_row)
        self.invert_check = QCheckBox("Invert (180 behaves like 0, and vice versa)")
        self.invert_check.toggled.connect(self._on_invert_changed)
        axis_layout.addWidget(self.invert_check)
        layout.addWidget(axis_box)
        self.axis_box = axis_box

        gizmo_box = QGroupBox("3D Gizmo Drags")
        gizmo_layout = QHBoxLayout(gizmo_box)
        self.gizmo_mode_group = QButtonGroup(self)
        self.gizmo_move_radio = QRadioButton("Move")
        self.gizmo_rotate_radio = QRadioButton("Rotate")
        self.gizmo_move_radio.setChecked(True)
        for btn in (self.gizmo_move_radio, self.gizmo_rotate_radio):
            self.gizmo_mode_group.addButton(btn)
            gizmo_layout.addWidget(btn)
            btn.toggled.connect(self._on_gizmo_mode_changed)
        layout.addWidget(gizmo_box)
        self.gizmo_box = gizmo_box

        offset_box = QGroupBox("Position Offset (mm)")
        offset_layout = QHBoxLayout(offset_box)
        self.offset_spins: dict[str, QDoubleSpinBox] = {}
        for axis in "XYZ":
            spin = QDoubleSpinBox()
            spin.setRange(-500, 500)
            spin.setDecimals(2)
            spin.valueChanged.connect(self._on_offset_changed)
            offset_layout.addWidget(QLabel(axis))
            offset_layout.addWidget(spin)
            self.offset_spins[axis] = spin
        layout.addWidget(offset_box)
        self.offset_box = offset_box

        rotation_box = QGroupBox("Rotation Offset (deg)")
        rotation_layout = QHBoxLayout(rotation_box)
        self.rotation_spins: dict[str, QDoubleSpinBox] = {}
        for axis in "XYZ":
            spin = QDoubleSpinBox()
            spin.setRange(-360, 360)
            spin.setDecimals(1)
            spin.valueChanged.connect(self._on_rotation_changed)
            rotation_layout.addWidget(QLabel(axis))
            rotation_layout.addWidget(spin)
            self.rotation_spins[axis] = spin
        layout.addWidget(rotation_box)
        self.rotation_box = rotation_box

        width_box = QGroupBox("Screen Size")
        width_layout = QHBoxLayout(width_box)
        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(0.2, 12.0)
        self.width_spin.setDecimals(2)
        self.width_spin.setSuffix(" in (across)")
        self.width_spin.valueChanged.connect(self._on_width_changed)
        width_layout.addWidget(self.width_spin)
        layout.addWidget(width_box)
        self.width_box = width_box

        btn_row = QHBoxLayout()
        reset_btn = QPushButton("Reset to Auto")
        reset_btn.setProperty("variant", "flat")
        reset_btn.clicked.connect(self._reset_current)
        save_btn = QPushButton("Save Calibration")
        save_btn.setProperty("variant", "success")
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(reset_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)
        layout.addStretch(1)

        self.viewport.calibrationEdited.connect(self._on_gizmo_edited)

        self._loading = False
        self._auto_backup: dict | None = None

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.viewport.rig and self._auto_backup is None:
            self._auto_backup = json.loads(json.dumps(self.viewport.rig.config))
            self._on_target_changed()

    # -- helpers -----------------------------------------------------
    def _current_target(self) -> str | None:
        return self.target_combo.currentData()

    def _is_body_target(self) -> bool:
        return self._current_target() in BODY_PART_KEYS

    def _is_face_target(self) -> bool:
        return self._current_target() == FACE_PLANE_KEY

    def _current_joint_cfg(self) -> dict | None:
        target = self._current_target()
        if not self.viewport.rig or self._is_body_target() or self._is_face_target() or not target:
            return None
        return self.viewport.rig.config["joints"].get(target)

    def _current_offset(self) -> dict | None:
        target = self._current_target()
        if not self.viewport.rig or not target:
            return None
        if self._is_face_target():
            # config["face_plane"] already has "position"/"rotation" keys in
            # the same shape as a part offset -- reuse it directly.
            return self.viewport.rig.config[FACE_PLANE_KEY]
        if self._is_body_target():
            return self.viewport.rig.config["body"][f"{target}_offset"]
        cfg = self._current_joint_cfg()
        return cfg["offset"] if cfg else None

    def _current_mode(self) -> str:
        return "pivot" if self.pivot_radio.isChecked() else "offset"

    def _current_gizmo_mode(self) -> str:
        return "rotate" if self.gizmo_rotate_radio.isChecked() else "move"

    def _update_group_visibility(self) -> None:
        no_pivot = self._current_target() in NO_PIVOT_KEYS
        self.pivot_radio.setEnabled(not no_pivot)
        if no_pivot and self.pivot_radio.isChecked():
            self._loading = True
            self.offset_radio.setChecked(True)
            self._loading = False
        mode = self._current_mode()
        self.pivot_box.setVisible(not no_pivot and mode == "pivot")
        self.axis_box.setVisible(not no_pivot and mode == "pivot")
        self.gizmo_box.setVisible(mode == "offset")
        self.offset_box.setVisible(mode == "offset")
        self.rotation_box.setVisible(mode == "offset")
        self.width_box.setVisible(self._is_face_target())

    # -- calibration mode / target selection ------------------------------
    def _on_calib_toggled(self, checked: bool) -> None:
        self.viewport.calibration_mode = checked
        if checked:
            self.viewport.set_calib_target(self._current_target(), self._current_mode())
            self.viewport.set_gizmo_mode(self._current_gizmo_mode())
        else:
            self.viewport.set_calib_target(None)

    def _on_target_changed(self) -> None:
        if not self.viewport.rig:
            return
        self._update_group_visibility()

        if not self._is_body_target() and not self._is_face_target():
            cfg = self._current_joint_cfg()
            if not cfg:
                return
            self._loading = True
            for axis, val in zip("XYZ", cfg["pivot"]):
                self.pivot_spins[axis].setValue(val)
            getattr(self, f"_axis_btn_{cfg.get('axis', 'Y')}").setChecked(True)
            self.invert_check.setChecked(cfg.get("invert", False))
            self._loading = False

        offset = self._current_offset()
        if offset:
            self._loading = True
            for axis, val in zip("XYZ", offset["position"]):
                self.offset_spins[axis].setValue(val)
            for axis, val in zip("XYZ", offset["rotation"]):
                self.rotation_spins[axis].setValue(val)
            if self._is_face_target():
                self.width_spin.setValue(offset.get("width_in", DEFAULT_FACE_WIDTH_IN))
            self._loading = False

        if self.calib_check.isChecked():
            self.viewport.set_calib_target(self._current_target(), self._current_mode())
            self.viewport.set_gizmo_mode(self._current_gizmo_mode())

    def _on_mode_changed(self) -> None:
        if self._loading:
            return
        self._update_group_visibility()
        if self.calib_check.isChecked():
            self.viewport.set_calib_target(self._current_target(), self._current_mode())

    def _on_gizmo_mode_changed(self) -> None:
        if self._loading:
            return
        if self.calib_check.isChecked():
            self.viewport.set_gizmo_mode(self._current_gizmo_mode())

    def _on_gizmo_edited(self) -> None:
        # Live-refresh whichever field the gizmo is currently driving.
        if self.viewport.calib_target != self._current_target():
            return
        kind = self.viewport._gizmo_value_kind()
        value = self.viewport._gizmo_get_value()
        if value is None:
            return
        self._loading = True
        spins = {"pivot": self.pivot_spins, "position": self.offset_spins, "rotation": self.rotation_spins}[kind]
        for axis, val in zip("XYZ", value):
            spins[axis].setValue(val)
        self._loading = False

    # -- field edits -----------------------------------------------------
    def _on_pivot_changed(self) -> None:
        cfg = self._current_joint_cfg()
        if not cfg or self._loading:
            return
        cfg["pivot"] = [self.pivot_spins[a].value() for a in "XYZ"]
        self.viewport.update()

    def _on_offset_changed(self) -> None:
        offset = self._current_offset()
        if not offset or self._loading:
            return
        offset["position"] = [self.offset_spins[a].value() for a in "XYZ"]
        self.viewport.update()

    def _on_rotation_changed(self) -> None:
        offset = self._current_offset()
        if not offset or self._loading:
            return
        offset["rotation"] = [self.rotation_spins[a].value() for a in "XYZ"]
        self.viewport.update()

    def _on_width_changed(self) -> None:
        if self._loading or not self._is_face_target() or not self.viewport.rig:
            return
        self.viewport.rig.config[FACE_PLANE_KEY]["width_in"] = self.width_spin.value()
        self.viewport.update()

    def _on_axis_changed(self) -> None:
        cfg = self._current_joint_cfg()
        if not cfg or self._loading:
            return
        for axis in "XYZ":
            if getattr(self, f"_axis_btn_{axis}").isChecked():
                cfg["axis"] = axis
        self.viewport.update()

    def _on_invert_changed(self, checked: bool) -> None:
        cfg = self._current_joint_cfg()
        if not cfg or self._loading:
            return
        cfg["invert"] = checked
        self.viewport.update()

    def _reset_current(self) -> None:
        if not self._auto_backup:
            return
        target = self._current_target()
        if self._is_face_target():
            backup = self._auto_backup.get(FACE_PLANE_KEY)
            if backup:
                self.viewport.rig.config[FACE_PLANE_KEY] = json.loads(json.dumps(backup))
        elif self._is_body_target():
            backup = self._auto_backup["body"].get(f"{target}_offset")
            if backup:
                self.viewport.rig.config["body"][f"{target}_offset"] = json.loads(json.dumps(backup))
        else:
            cfg = self._current_joint_cfg()
            backup = self._auto_backup["joints"].get(target)
            if cfg and backup:
                cfg.update(json.loads(json.dumps(backup)))
        self._on_target_changed()
        self.viewport.update()

    def _save(self) -> None:
        if self.viewport.rig:
            self.viewport.rig.save()
