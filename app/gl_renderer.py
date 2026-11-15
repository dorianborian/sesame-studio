"""OpenGL 3.3 core renderer for the Sesame robot viewport.

Owns every GL object (programs, buffers, framebuffers, textures) and knows
how to draw one frame from a plain :class:`Frame` description. It has no Qt
widget logic: ``app.viewport3d.Viewport3D`` builds a ``Frame`` each repaint
and calls :meth:`Renderer.render` with a current context.

Passes per frame (all geometry lives in ONE static VBO/EBO, per-link model
matrices are a uniform array, so each pass is a handful of draw calls):

1. shadow map   -- depth-only, orthographic from the key light, re-rendered
                   only when the pose / visible parts changed
2. scene        -- background, robot (PBR-ish), OLED, floor (grid + PCSS
                   shadows + contact darkening), OLED glow, ghost shell,
                   rotation gizmo; into a 4x MSAA sRGB framebuffer
3. mask         -- silhouettes of the hovered / selected limbs (MSAA)
4. composite    -- resolved scene + anti-aliased outlines -> widget FBO,
                   then 2D label sprites
(+) pick        -- link-id + depth buffer at logical resolution, rendered and
                   read back lazily (only when the view/pose changed and
                   somebody asks what is under the cursor)
"""

from __future__ import annotations

import ctypes
import math
import os
import sys

import numpy as np

import OpenGL

if not os.environ.get("SESAME_GL_DEBUG"):
    # PyOpenGL's per-call glGetError round trip costs more than our whole
    # frame; shader compile/link status is still checked explicitly.
    OpenGL.ERROR_CHECKING = False
    OpenGL.ERROR_LOGGING = False

from OpenGL import GL  # noqa: E402

from . import gl_shaders as S  # noqa: E402
from .gl_math import look_at, normalize, ortho  # noqa: E402

__all__ = ["Renderer", "Frame", "GLError", "srgb_to_linear", "hex_to_linear"]

SHADOW_SIZE = 2048
GROUPS = ("shell", "frame", "leg", "pad", "servo", "horn", "screw",
          "electronics", "battery", "screen", "switch")

_VERTEX_DTYPE = np.dtype([("pos", "<f4", 3), ("nrm", "<f4", 3), ("col", "u1", 4), ("ids", "u1", 4)])


class GLError(RuntimeError):
    pass


def srgb_to_linear(c) -> np.ndarray:
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def hex_to_linear(h: str) -> np.ndarray:
    h = h.lstrip("#")
    return srgb_to_linear([int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)])


# ---------------------------------------------------------------------------
# thin GL wrappers
# ---------------------------------------------------------------------------

class Program:
    def __init__(self, name: str, vs: str, fs: str):
        self.name = name
        self.id = GL.glCreateProgram()
        shaders = []
        for kind, src in ((GL.GL_VERTEX_SHADER, vs), (GL.GL_FRAGMENT_SHADER, fs)):
            sh = GL.glCreateShader(kind)
            GL.glShaderSource(sh, src)
            GL.glCompileShader(sh)
            if not GL.glGetShaderiv(sh, GL.GL_COMPILE_STATUS):
                log = GL.glGetShaderInfoLog(sh)
                log = log.decode("utf-8", "replace") if isinstance(log, bytes) else str(log)
                stage = "vertex" if kind == GL.GL_VERTEX_SHADER else "fragment"
                raise GLError(f"{name}: {stage} shader failed to compile:\n{log}")
            GL.glAttachShader(self.id, sh)
            shaders.append(sh)
        GL.glLinkProgram(self.id)
        if not GL.glGetProgramiv(self.id, GL.GL_LINK_STATUS):
            log = GL.glGetProgramInfoLog(self.id)
            log = log.decode("utf-8", "replace") if isinstance(log, bytes) else str(log)
            raise GLError(f"{name}: program failed to link:\n{log}")
        for sh in shaders:
            GL.glDetachShader(self.id, sh)
            GL.glDeleteShader(sh)
        self._loc: dict[str, int] = {}

    def use(self) -> "Program":
        GL.glUseProgram(self.id)
        return self

    def loc(self, name: str) -> int:
        loc = self._loc.get(name)
        if loc is None:
            loc = self._loc[name] = GL.glGetUniformLocation(self.id, name)
        return loc

    # setters assume the program is in use
    def f(self, name: str, v: float) -> None:
        GL.glUniform1f(self.loc(name), float(v))

    def i(self, name: str, v: int) -> None:
        GL.glUniform1i(self.loc(name), int(v))

    def v2(self, name: str, x: float, y: float) -> None:
        GL.glUniform2f(self.loc(name), float(x), float(y))

    def v3(self, name: str, v) -> None:
        GL.glUniform3f(self.loc(name), float(v[0]), float(v[1]), float(v[2]))

    def v4(self, name: str, v) -> None:
        GL.glUniform4f(self.loc(name), float(v[0]), float(v[1]), float(v[2]), float(v[3]))

    def v4n(self, name: str, arr: np.ndarray) -> None:
        arr = np.ascontiguousarray(arr, dtype=np.float32).reshape(-1, 4)
        if arr.shape[0]:
            GL.glUniform4fv(self.loc(name), arr.shape[0], arr)

    def m4(self, name: str, m: np.ndarray) -> None:
        GL.glUniformMatrix4fv(self.loc(name), 1, GL.GL_TRUE, np.ascontiguousarray(m, dtype=np.float32))

    def m4n(self, name: str, ms: np.ndarray) -> None:
        ms = np.ascontiguousarray(ms, dtype=np.float32).reshape(-1, 4, 4)
        GL.glUniformMatrix4fv(self.loc(name), ms.shape[0], GL.GL_TRUE, ms)


def _gen(fn) -> int:
    v = fn(1)
    try:
        return int(v)
    except TypeError:
        return int(v[0])


def _check_fbo(what: str) -> None:
    status = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER)
    if status != GL.GL_FRAMEBUFFER_COMPLETE:
        raise GLError(f"{what} framebuffer incomplete (status 0x{int(status):04x})")


# ---------------------------------------------------------------------------
# frame description
# ---------------------------------------------------------------------------

class Frame:
    """Everything the renderer needs for one frame (plain data)."""

    def __init__(self):
        self.view_proj = np.eye(4)
        self.eye = np.zeros(3)
        self.link_mats = np.tile(np.eye(4, dtype=np.float32), (S.MAX_LINKS, 1, 1))
        self.cover: str | None = None
        self.ghost = False
        self.tint = None                     # linear rgb or None
        self.grid = True
        self.shadows = True
        self.hover_links: tuple = ()         # link indices
        self.selected_links: tuple = ()
        self.glow = np.zeros((S.MAX_LINKS, 4), dtype=np.float32)
        self.floor_focus = (0.0, 0.0)
        self.light_center = np.zeros(3)      # world centre of the shadow frustum
        self.blobs = np.zeros((0, 4), dtype=np.float32)
        self.contacts = np.zeros((0, 4), dtype=np.float32)
        self.gizmo: dict | None = None       # center,u,v,cur,range,dir,hot
        self.sprites: list = []              # (texture, x, y, w, h, opacity, anchor|None), framebuffer px;
                                             # anchor = (x, y in logical px, link index): dimmed when
                                             # that link is not what is visible there
        self.horizon = 0.5
        self.fb_size = (1, 1)
        self.proj = np.eye(4)
        self.pick_size = (1, 1)              # logical pixels: resolution of the link-id buffer
        self.pick_key = None                 # identifies camera + pose + visible parts
        self.outline_px = 2.5
        self.ao = True


# ---------------------------------------------------------------------------
# renderer
# ---------------------------------------------------------------------------

class Renderer:
    # lighting rig (world space; the robot's front looks down -X)
    KEY_DIR = normalize([-0.50, 0.78, -0.37])
    FILL_DIR = normalize([-0.25, 0.30, 0.92])
    RIM_DIR = normalize([0.80, 0.42, 0.25])
    KEY_COLOR = np.array([1.00, 0.96, 0.90]) * 3.0
    FILL_COLOR = np.array([0.62, 0.72, 0.95]) * 0.75
    RIM_COLOR = np.array([0.80, 0.88, 1.00]) * 1.25
    SKY_COLOR = np.array([0.50, 0.53, 0.60]) * 0.62
    HORIZON_COLOR = np.array([0.30, 0.31, 0.35]) * 0.55
    GROUND_COLOR = np.array([0.10, 0.10, 0.115]) * 0.55
    OLED_COLOR = np.array([0.55, 0.86, 1.00]) * 2.4
    LIGHT_TAN = 0.085            # key light angular radius (soft shadow width)

    def __init__(self):
        self.ready = False
        self.samples = 4
        self.fb_w = self.fb_h = 0
        self.pick_w = self.pick_h = 0
        self._shadow_key = None
        self._pick_content_key = None
        self.ao_radius = 5.5
        self.ao_strength = 0.62
        self._face_any = False
        self._sprite_tex: dict = {}
        self._sprite_order: list = []
        self.shadow_radius = 150.0
        self.materials: list[str] = []
        self.links: list[str] = []
        self.covers: list[str] = []
        self.default_cover: str | None = None
        self.stats = {"triangles": 0, "vertices": 0, "draw_calls": 0}

    # ------------------------------------------------------------------ setup
    def initialize(self, model) -> None:
        """Compile programs and upload the model. Needs a current 3.3 core context."""
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
        self.max_samples = int(GL.glGetIntegerv(GL.GL_MAX_SAMPLES))
        self.samples = max(1, min(4, self.max_samples))

        P = Program
        self.p_robot = P("robot", S.ROBOT_VS, S.ROBOT_FS)
        self.p_depth = P("depth", S.ROBOT_VS, S.DEPTH_FS)
        self.p_pick = P("pick", S.ROBOT_VS, S.PICK_FS)
        self.p_mask = P("mask", S.ROBOT_VS, S.MASK_FS)
        self.p_bg = P("background", S.FULLSCREEN_VS, S.BACKGROUND_FS)
        self.p_floor = P("floor", S.FLOOR_VS, S.FLOOR_FS)
        self.p_screen = P("screen", S.SCREEN_VS, S.SCREEN_FS)
        self.p_gizmo = P("gizmo", S.GIZMO_VS, S.GIZMO_FS)
        self.p_comp = P("composite", S.FULLSCREEN_VS, S.COMPOSITE_FS)
        self.p_sprite = P("sprite", S.SPRITE_VS, S.SPRITE_FS)
        self.p_ao = P("ao", S.FULLSCREEN_VS, S.AO_FS)

        self._build_robot(model)
        self._build_static_geometry(model)
        self._build_shadow_map()
        self._build_face_texture()
        self._set_static_uniforms()
        self.ready = True

    def _build_robot(self, model) -> None:
        self.links = list(model.links)
        if len(self.links) > S.MAX_LINKS:
            raise GLError(f"model has {len(self.links)} links, renderer supports {S.MAX_LINKS}")
        link_index = {n: i for i, n in enumerate(self.links)}
        self.materials = list(model.materials)
        if len(self.materials) > S.MAX_MATERIALS:
            raise GLError("too many materials")
        mat_index = {n: i for i, n in enumerate(self.materials)}
        self.covers = list(model.cover_variants)
        self.default_cover = model.default_cover

        def bucket_of(part) -> str:
            if part.cover_variant is not None:
                return "cover:" + part.cover_variant
            return "shell" if part.group == "shell" else "base"

        bucket_names = ["base", "shell"] + ["cover:" + c for c in self.covers]
        order = {b: i for i, b in enumerate(bucket_names)}
        parts = sorted(model.parts, key=lambda p: (order[bucket_of(p)], link_index[p.link]))

        total_v = sum(int(p.vertices.shape[0]) for p in parts)
        total_i = sum(int(p.indices.size) for p in parts)
        verts = np.zeros(total_v, dtype=_VERTEX_DTYPE)
        indices = np.empty(total_i, dtype=np.uint32)
        self.ranges: dict[tuple[str, int], list[int]] = {}      # (bucket, link) -> [first, count]
        self.bucket_ranges: dict[str, list[int]] = {b: [0, 0] for b in bucket_names}
        v0 = i0 = 0
        for p in parts:
            n = int(p.vertices.shape[0])
            m = int(p.indices.size)
            block = verts[v0:v0 + n]
            block["pos"] = p.vertices
            block["nrm"] = p.normals
            block["col"][:, :3] = np.clip(np.rint(p.colors * 255.0), 0, 255).astype(np.uint8)
            block["col"][:, 3] = 255
            mat = model.materials.get(p.material)
            block["ids"][:, 0] = link_index[p.link]
            block["ids"][:, 1] = mat_index.get(p.material, 0)
            block["ids"][:, 2] = 1 if (mat is not None and mat.tintable) else 0
            block["ids"][:, 3] = GROUPS.index(p.group) if p.group in GROUPS else 255
            indices[i0:i0 + m] = p.indices.reshape(-1) + v0
            b = bucket_of(p)
            key = (b, link_index[p.link])
            r = self.ranges.get(key)
            if r is None:
                self.ranges[key] = [i0, m]
            else:
                r[1] += m
            br = self.bucket_ranges[b]
            if br[1] == 0:
                br[0] = i0
            br[1] += m
            v0 += n
            i0 += m

        self.stats["vertices"] = total_v
        self.stats["triangles"] = total_i // 3

        self.vao_robot = _gen(GL.glGenVertexArrays)
        GL.glBindVertexArray(self.vao_robot)
        self.vbo_robot = _gen(GL.glGenBuffers)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo_robot)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, verts.nbytes, verts, GL.GL_STATIC_DRAW)
        stride = _VERTEX_DTYPE.itemsize
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, stride, ctypes.c_void_p(0))
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 3, GL.GL_FLOAT, GL.GL_FALSE, stride, ctypes.c_void_p(12))
        GL.glEnableVertexAttribArray(2)
        GL.glVertexAttribPointer(2, 4, GL.GL_UNSIGNED_BYTE, GL.GL_TRUE, stride, ctypes.c_void_p(24))
        GL.glEnableVertexAttribArray(3)
        GL.glVertexAttribIPointer(3, 4, GL.GL_UNSIGNED_BYTE, stride, ctypes.c_void_p(28))
        self.ebo_robot = _gen(GL.glGenBuffers)
        GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, self.ebo_robot)
        GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices, GL.GL_STATIC_DRAW)
        GL.glBindVertexArray(0)

        mats = np.zeros((S.MAX_MATERIALS, 4), dtype=np.float32)
        for name, i in mat_index.items():
            m = model.materials[name]
            mats[i] = (m.roughness, m.metallic, m.emissive, 1.0 if m.tintable else 0.0)
        self._material_table = mats

        diag = float(np.linalg.norm(np.asarray(model.bounds_max) - np.asarray(model.bounds_min)))
        self.shadow_radius = max(60.0, diag * 0.78)

    def _build_static_geometry(self, model) -> None:
        # unit quad (triangle strip) for floor + gizmo
        quad = np.array([[-1, -1], [1, -1], [-1, 1], [1, 1]], dtype=np.float32)
        self.vao_quad = _gen(GL.glGenVertexArrays)
        GL.glBindVertexArray(self.vao_quad)
        self.vbo_quad = _gen(GL.glGenBuffers)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo_quad)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, quad.nbytes, quad, GL.GL_STATIC_DRAW)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, GL.GL_FALSE, 8, ctypes.c_void_p(0))

        # attribute-less VAO for fullscreen triangles / sprites
        self.vao_empty = _gen(GL.glGenVertexArrays)

        # OLED quad + a larger halo quad (bind pose, body frame)
        scr = model.screen
        n = np.asarray(scr.normal, dtype=np.float64)
        right = np.asarray(scr.right, dtype=np.float64)
        up = np.asarray(scr.up, dtype=np.float64)
        c = np.asarray(scr.center, dtype=np.float64)
        self.screen_normal = n

        def quad_at(margin_uv: float, lift: float) -> np.ndarray:
            rows = []
            for u, v in ((0, 0), (1, 0), (0, 1), (1, 1)):            # strip: TL, TR, BL, BR
                uu = -margin_uv + u * (1 + 2 * margin_uv)
                vv = -margin_uv * 2 + v * (1 + 4 * margin_uv)        # same margin in mm (2:1 panel)
                pos = (c + right * (uu - 0.5) * scr.width - up * (vv - 0.5) * scr.height + n * lift)
                rows.append([pos[0], pos[1], pos[2], uu, vv])
            return np.array(rows, dtype=np.float32)

        data = np.concatenate([quad_at(0.0, 0.03), quad_at(0.22, 0.35)])
        self.vao_screen = _gen(GL.glGenVertexArrays)
        GL.glBindVertexArray(self.vao_screen)
        self.vbo_screen = _gen(GL.glGenBuffers)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo_screen)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, data.nbytes, data, GL.GL_STATIC_DRAW)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 20, ctypes.c_void_p(0))
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 2, GL.GL_FLOAT, GL.GL_FALSE, 20, ctypes.c_void_p(12))
        GL.glBindVertexArray(0)

    def _build_shadow_map(self) -> None:
        self.tex_shadow = _gen(GL.glGenTextures)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_shadow)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_DEPTH_COMPONENT24, SHADOW_SIZE, SHADOW_SIZE, 0,
                        GL.GL_DEPTH_COMPONENT, GL.GL_FLOAT, None)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_BORDER)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_BORDER)
        GL.glTexParameterfv(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_BORDER_COLOR,
                            np.array([1.0, 1.0, 1.0, 1.0], dtype=np.float32))
        self.fbo_shadow = _gen(GL.glGenFramebuffers)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo_shadow)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_DEPTH_ATTACHMENT, GL.GL_TEXTURE_2D,
                                  self.tex_shadow, 0)
        GL.glDrawBuffer(GL.GL_NONE)
        GL.glReadBuffer(GL.GL_NONE)
        _check_fbo("shadow")
        # two views of the same depth texture: hardware PCF and raw depth
        self.smp_shadow_cmp = _gen(GL.glGenSamplers)
        self.smp_shadow_raw = _gen(GL.glGenSamplers)
        for smp, filt in ((self.smp_shadow_cmp, GL.GL_LINEAR), (self.smp_shadow_raw, GL.GL_NEAREST)):
            GL.glSamplerParameteri(smp, GL.GL_TEXTURE_MIN_FILTER, filt)
            GL.glSamplerParameteri(smp, GL.GL_TEXTURE_MAG_FILTER, filt)
            GL.glSamplerParameteri(smp, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_BORDER)
            GL.glSamplerParameteri(smp, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_BORDER)
            GL.glSamplerParameterfv(smp, GL.GL_TEXTURE_BORDER_COLOR,
                                    np.array([1.0, 1.0, 1.0, 1.0], dtype=np.float32))
        GL.glSamplerParameteri(self.smp_shadow_cmp, GL.GL_TEXTURE_COMPARE_MODE, GL.GL_COMPARE_REF_TO_TEXTURE)
        GL.glSamplerParameteri(self.smp_shadow_cmp, GL.GL_TEXTURE_COMPARE_FUNC, GL.GL_LEQUAL)
        GL.glSamplerParameteri(self.smp_shadow_raw, GL.GL_TEXTURE_COMPARE_MODE, GL.GL_NONE)

    def _build_face_texture(self) -> None:
        self.tex_face = _gen(GL.glGenTextures)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_face)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR_MIPMAP_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_BORDER)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_BORDER)
        GL.glTexParameterfv(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_BORDER_COLOR,
                            np.array([0.0, 0.0, 0.0, 0.0], dtype=np.float32))
        self.upload_face(None)

    def upload_face(self, pixels: np.ndarray | None) -> None:
        """pixels: (64, 128) uint8 (0 / 255), row 0 = top; None = blank."""
        if pixels is None:
            pixels = np.zeros((64, 128), dtype=np.uint8)
        pixels = np.ascontiguousarray(pixels, dtype=np.uint8)
        self._face_any = bool(pixels.any())
        h, w = pixels.shape
        self._face_size = (w, h)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_face)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_R8, w, h, 0, GL.GL_RED, GL.GL_UNSIGNED_BYTE, pixels)
        GL.glGenerateMipmap(GL.GL_TEXTURE_2D)

    def _set_static_uniforms(self) -> None:
        for p in (self.p_robot, self.p_floor, self.p_screen):
            p.use()
            p.v3("uKeyDir", self.KEY_DIR)
            p.v3("uKeyColor", self.KEY_COLOR)
            p.v3("uFillDir", self.FILL_DIR)
            p.v3("uFillColor", self.FILL_COLOR)
            p.v3("uRimDir", self.RIM_DIR)
            p.v3("uRimColor", self.RIM_COLOR)
            p.v3("uSkyColor", self.SKY_COLOR)
            p.v3("uGroundColor", self.GROUND_COLOR)
            p.v3("uHorizonColor", self.HORIZON_COLOR)
        for p in (self.p_robot, self.p_floor):
            p.use()
            p.i("uShadowMap", 1)
            p.i("uShadowDepth", 2)
        self.p_robot.use()
        self.p_robot.v4n("uMaterial", self._material_table)
        self.p_screen.use()
        self.p_screen.i("uFace", 0)
        self.p_screen.v3("uLitColor", self.OLED_COLOR)
        self.p_comp.use()
        self.p_comp.i("uScene", 0)
        self.p_comp.i("uMask", 1)
        self.p_comp.i("uAO", 2)
        self.p_sprite.use()
        self.p_sprite.i("uTex", 0)
        self.p_sprite.i("uIds", 1)
        self.p_ao.use()
        self.p_ao.i("uDepth", 0)
        GL.glUseProgram(0)

    def set_theme_colors(self, *, bg_top, bg_bottom, bg_glow, floor, grid, accent, hover, select) -> None:
        """All colours linear RGB."""
        self.p_bg.use()
        self.p_bg.v3("uTop", bg_top)
        self.p_bg.v3("uBottom", bg_bottom)
        self.p_bg.v3("uGlow", bg_glow)
        self.p_floor.use()
        self.p_floor.v3("uFloorColor", floor)
        self.p_floor.v3("uGridColor", grid)
        self.p_floor.v3("uAccent", accent)
        self.p_gizmo.use()
        self.p_gizmo.v3("uAccent", accent)
        self.p_comp.use()
        self.p_comp.v3("uHoverColor", hover)
        self.p_comp.v3("uSelectColor", select)
        GL.glUseProgram(0)

    # ------------------------------------------------------------ framebuffers
    def resize(self, fb_w: int, fb_h: int) -> None:
        fb_w, fb_h = max(1, int(fb_w)), max(1, int(fb_h))
        if (fb_w, fb_h) == (self.fb_w, self.fb_h):
            return
        self._delete_targets()
        self.fb_w, self.fb_h = fb_w, fb_h
        n = self.samples

        def rbo(fmt) -> int:
            r = _gen(GL.glGenRenderbuffers)
            GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, r)
            if n > 1:
                GL.glRenderbufferStorageMultisample(GL.GL_RENDERBUFFER, n, fmt, fb_w, fb_h)
            else:
                GL.glRenderbufferStorage(GL.GL_RENDERBUFFER, fmt, fb_w, fb_h)
            return r

        def tex(internal, fmt) -> int:
            t = _gen(GL.glGenTextures)
            GL.glBindTexture(GL.GL_TEXTURE_2D, t)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, internal, fb_w, fb_h, 0, fmt, GL.GL_UNSIGNED_BYTE, None)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
            return t

        # multisampled scene
        self.rbo_scene_color = rbo(GL.GL_SRGB8_ALPHA8)
        self.rbo_scene_depth = rbo(GL.GL_DEPTH_COMPONENT24)
        self.fbo_scene = _gen(GL.glGenFramebuffers)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo_scene)
        GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_RENDERBUFFER, self.rbo_scene_color)
        GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_DEPTH_ATTACHMENT, GL.GL_RENDERBUFFER, self.rbo_scene_depth)
        _check_fbo("scene")
        # resolved scene
        self.tex_scene = tex(GL.GL_SRGB8_ALPHA8, GL.GL_RGBA)
        self.tex_depth = _gen(GL.glGenTextures)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_depth)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_DEPTH_COMPONENT24, fb_w, fb_h, 0,
                        GL.GL_DEPTH_COMPONENT, GL.GL_FLOAT, None)
        for pname, val in ((GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST), (GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST),
                           (GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE), (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)):
            GL.glTexParameteri(GL.GL_TEXTURE_2D, pname, val)
        self.fbo_resolve = _gen(GL.glGenFramebuffers)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo_resolve)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, self.tex_scene, 0)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_DEPTH_ATTACHMENT, GL.GL_TEXTURE_2D, self.tex_depth, 0)
        _check_fbo("resolve")
        # ambient occlusion
        self.tex_ao = tex(GL.GL_R8, GL.GL_RED)
        self.fbo_ao = _gen(GL.glGenFramebuffers)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo_ao)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, self.tex_ao, 0)
        _check_fbo("ao")
        # multisampled silhouette mask + resolve
        self.rbo_mask = rbo(GL.GL_RG8)
        self.fbo_mask = _gen(GL.glGenFramebuffers)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo_mask)
        GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_RENDERBUFFER, self.rbo_mask)
        _check_fbo("mask")
        self.tex_mask = tex(GL.GL_RG8, GL.GL_RG)
        self.fbo_mask_resolve = _gen(GL.glGenFramebuffers)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo_mask_resolve)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, self.tex_mask, 0)
        _check_fbo("mask resolve")
        self._mask_dirty = True

    def _delete_targets(self) -> None:
        if not self.fb_w:
            return
        GL.glDeleteFramebuffers(5, np.array([self.fbo_scene, self.fbo_resolve, self.fbo_mask,
                                             self.fbo_mask_resolve, self.fbo_ao], dtype=np.uint32))
        GL.glDeleteRenderbuffers(3, np.array([self.rbo_scene_color, self.rbo_scene_depth, self.rbo_mask],
                                             dtype=np.uint32))
        GL.glDeleteTextures(4, np.array([self.tex_scene, self.tex_mask, self.tex_depth, self.tex_ao],
                                        dtype=np.uint32))
        self.fb_w = self.fb_h = 0

    def _ensure_pick_target(self, w: int, h: int) -> None:
        if (w, h) == (self.pick_w, self.pick_h):
            return
        if self.pick_w:
            GL.glDeleteFramebuffers(1, np.array([self.fbo_pick], dtype=np.uint32))
            GL.glDeleteRenderbuffers(1, np.array([self.rbo_pick_depth], dtype=np.uint32))
            GL.glDeleteTextures(1, np.array([self.tex_pick], dtype=np.uint32))
        self.pick_w, self.pick_h = w, h
        self._pick_content_key = None
        self.tex_pick = _gen(GL.glGenTextures)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_pick)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_R8, w, h, 0, GL.GL_RED, GL.GL_UNSIGNED_BYTE, None)
        for pname, val in ((GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST), (GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST),
                           (GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE), (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)):
            GL.glTexParameteri(GL.GL_TEXTURE_2D, pname, val)
        self.rbo_pick_depth = _gen(GL.glGenRenderbuffers)
        GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, self.rbo_pick_depth)
        GL.glRenderbufferStorage(GL.GL_RENDERBUFFER, GL.GL_DEPTH_COMPONENT32F, w, h)
        self.fbo_pick = _gen(GL.glGenFramebuffers)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo_pick)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, self.tex_pick, 0)
        GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_DEPTH_ATTACHMENT, GL.GL_RENDERBUFFER, self.rbo_pick_depth)
        _check_fbo("pick")

    # ---------------------------------------------------------------- sprites
    def sprite_texture(self, key, image_factory):
        """Texture for a 2D label, created on first use from a QImage
        (Format_RGBA8888_Premultiplied). Returns (texture, width_px, height_px)."""
        hit = self._sprite_tex.get(key)
        if hit is not None:
            return hit
        img = image_factory()
        w, h = img.width(), img.height()
        ptr = img.constBits()
        data = np.frombuffer(ptr, dtype=np.uint8, count=img.sizeInBytes()).reshape(h, img.bytesPerLine())
        data = np.ascontiguousarray(data[:, :w * 4])
        t = _gen(GL.glGenTextures)
        GL.glBindTexture(GL.GL_TEXTURE_2D, t)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        hit = self._sprite_tex[key] = (t, w, h)
        self._sprite_order.append(key)
        if len(self._sprite_order) > 400:                    # evict the oldest labels
            old = self._sprite_order[:150]
            del self._sprite_order[:150]
            ids = [self._sprite_tex.pop(k)[0] for k in old if k in self._sprite_tex]
            if ids:
                GL.glDeleteTextures(len(ids), np.array(ids, dtype=np.uint32))
        return hit

    # ----------------------------------------------------------------- drawing
    def _draw_range(self, first: int, count: int) -> None:
        if count > 0:
            GL.glDrawElements(GL.GL_TRIANGLES, count, GL.GL_UNSIGNED_INT, ctypes.c_void_p(first * 4))
            self.stats["draw_calls"] += 1

    def _cover_bucket(self, frame: Frame) -> str | None:
        cover = frame.cover if frame.cover in self.covers else self.default_cover
        return ("cover:" + cover) if cover else None

    def _draw_opaque(self, frame: Frame) -> None:
        """Everything that is solid this frame (the ghost shell is not)."""
        self._draw_range(*self.bucket_ranges["base"])
        if not frame.ghost:
            self._draw_range(*self.bucket_ranges["shell"])
            cb = self._cover_bucket(frame)
            if cb:
                self._draw_range(*self.bucket_ranges[cb])

    def _draw_links(self, frame: Frame, links) -> None:
        buckets = ["base", "shell"]
        cb = self._cover_bucket(frame)
        if cb:
            buckets.append(cb)
        for li in links:
            for b in buckets:
                r = self.ranges.get((b, li))
                if r:
                    self._draw_range(r[0], r[1])

    def _shadow_matrix(self, frame: Frame):
        r = self.shadow_radius
        c = np.asarray(frame.light_center, dtype=np.float64)
        dist = r * 2.0
        view = look_at(c + self.KEY_DIR * dist, c)
        near, far = dist - r * 1.2, dist + r * 3.2
        proj = ortho(-r, r, -r, r, near, far)
        bias = np.array([[0.5, 0, 0, 0.5], [0, 0.5, 0, 0.5], [0, 0, 0.5, 0.5], [0, 0, 0, 1.0]])
        return proj @ view, bias @ proj @ view, (2.0 * r, far - near)

    def render(self, frame: Frame, target_fbo: int) -> None:
        if not self.ready:
            return
        self.stats["draw_calls"] = 0
        fb_w, fb_h = frame.fb_size
        self.resize(fb_w, fb_h)
        links32 = np.ascontiguousarray(frame.link_mats, dtype=np.float32)

        # ---- 1. shadow map (only when the casters changed) ----------------
        light_vp, shadow_mat, (size_mm, range_mm) = self._shadow_matrix(frame)
        if frame.shadows:
            key = (links32.tobytes(), frame.cover, frame.ghost,
                   tuple(np.round(frame.light_center, 2)))
            if key != self._shadow_key:
                self._shadow_key = key
                GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo_shadow)
                GL.glViewport(0, 0, SHADOW_SIZE, SHADOW_SIZE)
                GL.glEnable(GL.GL_DEPTH_TEST)
                GL.glDepthFunc(GL.GL_LESS)
                GL.glDepthMask(GL.GL_TRUE)
                GL.glDisable(GL.GL_BLEND)
                GL.glDisable(GL.GL_CULL_FACE)
                GL.glClear(GL.GL_DEPTH_BUFFER_BIT)
                GL.glEnable(GL.GL_POLYGON_OFFSET_FILL)
                GL.glPolygonOffset(1.5, 2.0)
                p = self.p_depth.use()
                p.m4("uViewProj", light_vp)
                p.m4n("uLink", links32)
                GL.glBindVertexArray(self.vao_robot)
                self._draw_opaque(frame)
                GL.glDisable(GL.GL_POLYGON_OFFSET_FILL)

        # ---- 2. scene ------------------------------------------------------
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo_scene)
        GL.glViewport(0, 0, fb_w, fb_h)
        GL.glEnable(GL.GL_FRAMEBUFFER_SRGB)
        if self.samples > 1:
            GL.glEnable(GL.GL_MULTISAMPLE)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glDisable(GL.GL_BLEND)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glClear(GL.GL_DEPTH_BUFFER_BIT)

        # background
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glDepthMask(GL.GL_FALSE)
        p = self.p_bg.use()
        p.f("uHorizon", frame.horizon)
        GL.glBindVertexArray(self.vao_empty)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LEQUAL)

        # shadow samplers
        GL.glActiveTexture(GL.GL_TEXTURE1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_shadow)
        GL.glBindSampler(1, self.smp_shadow_cmp)
        GL.glActiveTexture(GL.GL_TEXTURE2)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_shadow)
        GL.glBindSampler(2, self.smp_shadow_raw)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        shadow_params = (size_mm, range_mm, self.LIGHT_TAN, 1.6 / SHADOW_SIZE)

        # robot
        p = self.p_robot.use()
        p.m4("uViewProj", frame.view_proj)
        p.m4n("uLink", links32)
        p.v3("uEye", frame.eye)
        p.m4("uShadowMat", shadow_mat)
        p.f("uShadowOn", 1.0 if frame.shadows else 0.0)
        p.v4("uShadowParams", shadow_params)
        p.v4n("uLinkGlow", frame.glow)
        if frame.tint is not None:
            p.v4("uTint", (frame.tint[0], frame.tint[1], frame.tint[2], 1.0))
        else:
            p.v4("uTint", (0.0, 0.0, 0.0, 0.0))
        p.f("uGhost", 0.0)
        GL.glBindVertexArray(self.vao_robot)
        self._draw_opaque(frame)

        # OLED panel
        root = links32[0]
        n_world = root[:3, :3].astype(np.float64) @ self.screen_normal
        p = self.p_screen.use()
        p.m4("uViewProj", frame.view_proj)
        p.m4("uModel", root)
        p.v3("uEye", frame.eye)
        p.v3("uNormal", n_world)
        p.v2("uFaceSize", *self._face_size)
        p.f("uGlowPass", 0.0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_face)
        GL.glBindVertexArray(self.vao_screen)
        GL.glEnable(GL.GL_POLYGON_OFFSET_FILL)
        GL.glPolygonOffset(-2.0, -4.0)
        GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
        GL.glDisable(GL.GL_POLYGON_OFFSET_FILL)
        self.stats["draw_calls"] += 1

        # floor
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFuncSeparate(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)
        p = self.p_floor.use()
        p.m4("uViewProj", frame.view_proj)
        p.v3("uEye", frame.eye)
        p.v3("uFocus", (frame.floor_focus[0], 0.0, frame.floor_focus[1]))
        p.f("uExtent", 900.0)
        p.f("uFadeRadius", 520.0)
        p.f("uGridOn", 1.0 if frame.grid else 0.0)
        p.m4("uShadowMat", shadow_mat)
        p.f("uShadowOn", 1.0 if frame.shadows else 0.0)
        p.v4("uShadowParams", shadow_params)
        nb = min(len(frame.blobs), S.MAX_BLOBS)
        p.i("uBlobCount", nb)
        if nb:
            p.v4n("uBlobs", frame.blobs[:nb])
        nc = min(len(frame.contacts), S.MAX_CONTACTS)
        p.i("uContactCount", nc)
        if nc:
            p.v4n("uContacts", frame.contacts[:nc])
        GL.glBindVertexArray(self.vao_quad)
        GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
        self.stats["draw_calls"] += 1

        # OLED halo (additive)
        if self._face_any:
            GL.glBlendFunc(GL.GL_ONE, GL.GL_ONE)
            GL.glDepthMask(GL.GL_FALSE)
            p = self.p_screen.use()
            p.f("uGlowPass", 1.0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_face)
            GL.glBindVertexArray(self.vao_screen)
            GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 4, 4)
            GL.glDepthMask(GL.GL_TRUE)
            self.stats["draw_calls"] += 1

        # ghost shell
        if frame.ghost:
            GL.glBlendFuncSeparate(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)
            GL.glDepthMask(GL.GL_FALSE)
            p = self.p_robot.use()
            p.f("uGhost", 1.0)
            p.f("uShadowOn", 0.0)
            GL.glBindVertexArray(self.vao_robot)
            self._draw_range(*self.bucket_ranges["shell"])
            cb = self._cover_bucket(frame)
            if cb:
                self._draw_range(*self.bucket_ranges[cb])
            GL.glDepthMask(GL.GL_TRUE)

        # rotation gizmo: visible part at full strength, occluded part faded
        if frame.gizmo is not None:
            g = frame.gizmo
            GL.glBlendFunc(GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)      # premultiplied
            GL.glDepthMask(GL.GL_FALSE)
            p = self.p_gizmo.use()
            p.m4("uViewProj", frame.view_proj)
            p.v3("uCenter", g["center"])
            p.v3("uU", g["u"])
            p.v3("uV", g["v"])
            p.f("uCur", g["cur"])
            p.v2("uRange", g["range"][0], g["range"][1])
            p.f("uDir", g["dir"])
            p.f("uHot", g.get("hot", 0.0))
            GL.glBindVertexArray(self.vao_quad)
            GL.glDepthFunc(GL.GL_GREATER)
            p.f("uAlpha", 0.30)
            GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
            GL.glDepthFunc(GL.GL_LEQUAL)
            p.f("uAlpha", 1.0)
            GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
            GL.glDepthMask(GL.GL_TRUE)
            self.stats["draw_calls"] += 2

        GL.glDisable(GL.GL_BLEND)
        GL.glBindSampler(1, 0)
        GL.glBindSampler(2, 0)

        # resolve MSAA
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, self.fbo_scene)
        GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, self.fbo_resolve)
        GL.glBlitFramebuffer(0, 0, fb_w, fb_h, 0, 0, fb_w, fb_h,
                             GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT, GL.GL_NEAREST)
        GL.glDisable(GL.GL_FRAMEBUFFER_SRGB)

        # ---- 2b. link ids (picking + label occlusion), logical resolution ----
        self._render_ids(frame, links32)

        # ---- 2c. ambient occlusion ---------------------------------------------
        ao_on = bool(frame.ao) and self.ao_strength > 0.0
        if ao_on:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo_ao)
            GL.glViewport(0, 0, fb_w, fb_h)
            GL.glDisable(GL.GL_DEPTH_TEST)
            GL.glDisable(GL.GL_BLEND)
            p = self.p_ao.use()
            p.m4("uInvProj", np.linalg.inv(frame.proj))
            p.v2("uTexel", 1.0 / fb_w, 1.0 / fb_h)
            p.f("uRadius", self.ao_radius)
            p.f("uProjScale", float(frame.proj[1, 1]) * 0.5 * fb_h)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_depth)
            GL.glBindVertexArray(self.vao_empty)
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
            self.stats["draw_calls"] += 1

        # ---- 3. silhouette mask ---------------------------------------------
        mask_on = bool(frame.hover_links or frame.selected_links)
        if mask_on:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo_mask)
            GL.glViewport(0, 0, fb_w, fb_h)
            GL.glClearColor(0.0, 0.0, 0.0, 0.0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            GL.glDisable(GL.GL_DEPTH_TEST)
            GL.glEnable(GL.GL_BLEND)
            GL.glBlendFunc(GL.GL_ONE, GL.GL_ONE)
            p = self.p_mask.use()
            p.m4("uViewProj", frame.view_proj)
            p.m4n("uLink", links32)
            GL.glBindVertexArray(self.vao_robot)
            if frame.hover_links:
                p.v4("uMaskValue", (1.0, 0.0, 0.0, 0.0))
                self._draw_links(frame, frame.hover_links)
            if frame.selected_links:
                p.v4("uMaskValue", (0.0, 1.0, 0.0, 0.0))
                self._draw_links(frame, frame.selected_links)
            GL.glDisable(GL.GL_BLEND)
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, self.fbo_mask)
            GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, self.fbo_mask_resolve)
            GL.glBlitFramebuffer(0, 0, fb_w, fb_h, 0, 0, fb_w, fb_h, GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST)

        # ---- 4. composite to the widget framebuffer -------------------------
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, target_fbo)
        GL.glViewport(0, 0, fb_w, fb_h)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glDepthMask(GL.GL_FALSE)
        p = self.p_comp.use()
        p.v2("uTexel", 1.0 / fb_w, 1.0 / fb_h)
        p.f("uOutlinePx", frame.outline_px)
        p.f("uMaskOn", 1.0 if mask_on else 0.0)
        p.f("uAOStrength", self.ao_strength if ao_on else 0.0)
        GL.glActiveTexture(GL.GL_TEXTURE2)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_ao)
        GL.glActiveTexture(GL.GL_TEXTURE1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_mask)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_scene)
        GL.glBindVertexArray(self.vao_empty)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        self.stats["draw_calls"] += 1

        if frame.sprites:
            GL.glEnable(GL.GL_BLEND)
            GL.glBlendFunc(GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)
            p = self.p_sprite.use()
            p.v2("uViewport", fb_w, fb_h)
            pw, ph = max(1, self.pick_w), max(1, self.pick_h)
            p.v2("uIdTexel", 1.0 / pw, 1.0 / ph)
            GL.glActiveTexture(GL.GL_TEXTURE1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex_pick)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            for tex, x, y, w, h, opacity, anchor in frame.sprites:
                GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
                p.v4("uRect", (x, y, w, h))
                p.f("uOpacity", opacity)
                if anchor is None:
                    p.v4("uAnchor", (0.0, 0.0, 0.0, 0.0))
                else:
                    p.v4("uAnchor", (anchor[0] / pw, 1.0 - anchor[1] / ph, (anchor[2] + 1.0) / 255.0, 1.0))
                GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
            self.stats["draw_calls"] += len(frame.sprites)
            GL.glDisable(GL.GL_BLEND)

        GL.glDepthMask(GL.GL_TRUE)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)

    # ------------------------------------------------------------------ picking
    def _render_ids(self, frame: Frame, links32: np.ndarray) -> None:
        w, h = max(1, int(frame.pick_size[0])), max(1, int(frame.pick_size[1]))
        self._ensure_pick_target(w, h)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo_pick)
        GL.glViewport(0, 0, w, h)
        GL.glDisable(GL.GL_BLEND)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glDisable(GL.GL_MULTISAMPLE)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LESS)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glClearColor(0.0, 0.0, 0.0, 0.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        p = self.p_pick.use()
        p.m4("uViewProj", frame.view_proj)
        p.m4n("uLink", links32)
        GL.glBindVertexArray(self.vao_robot)
        self._draw_opaque(frame)
        if self.samples > 1:
            GL.glEnable(GL.GL_MULTISAMPLE)
        self._pick_content_key = frame.pick_key

    def read_pick(self, frame: Frame):
        """Link ids + depth under every logical pixel, for ``frame``'s camera
        and pose. Re-uses the id buffer of the last rendered frame when it
        still matches (then this is just two read-backs).
        Returns (ids uint8 (h, w): 0 = nothing, i + 1 = link i; depth float32
        (h, w): window depth 0..1), both with row 0 = TOP of the view."""
        w, h = max(1, int(frame.pick_size[0])), max(1, int(frame.pick_size[1]))
        if (frame.pick_key is None or frame.pick_key != self._pick_content_key
                or (w, h) != (self.pick_w, self.pick_h)):
            self._render_ids(frame, np.ascontiguousarray(frame.link_mats, dtype=np.float32))
            GL.glBindVertexArray(0)
            GL.glUseProgram(0)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo_pick)
        GL.glReadBuffer(GL.GL_COLOR_ATTACHMENT0)
        ids = np.empty((h, w), dtype=np.uint8)
        depth = np.empty((h, w), dtype=np.float32)
        GL.glReadPixels(0, 0, w, h, GL.GL_RED, GL.GL_UNSIGNED_BYTE, ids)
        GL.glReadPixels(0, 0, w, h, GL.GL_DEPTH_COMPONENT, GL.GL_FLOAT, depth)
        return ids[::-1], depth[::-1]

    def info(self) -> dict:
        def s(name):
            v = GL.glGetString(name)
            return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
        return {"vendor": s(GL.GL_VENDOR), "renderer": s(GL.GL_RENDERER), "version": s(GL.GL_VERSION),
                "glsl": s(GL.GL_SHADING_LANGUAGE_VERSION), "samples": self.samples, **self.stats}


def log_gl_error(context: str, exc: BaseException) -> None:
    print(f"[viewport3d] {context}: {exc}", file=sys.stderr)
