"""Runtime loader for the CAD-derived robot model (pure numpy).

The assets in ``assets/model`` (``manifest.json`` + ``meshes.npz``) are
generated from the CAD STEP assembly by ``tools/build_model.py``; this module
only reads them.  No Qt, no OpenGL, no CAD kernel.

Everything is expressed in the conventions of :mod:`app.kinematics`:
millimetres, +Y up, robot front (OLED) facing -X, left = +Z, right = -Z, and
all geometry is in the **bind pose** (the firmware's STAND pose, see
``RobotModel.bind_angles``).  To pose the robot::

    model = RobotModel.load()
    xf = link_transforms(model.joints, angles)       # app.kinematics
    for part in model.parts_for():
        world = root @ xf[part.link]                  # 4x4, applies to part.vertices
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .kinematics import BODY_LINK, JointDef

__all__ = ["Part", "Material", "Screen", "RobotModel", "GROUPS"]

#: every value ``Part.group`` can take
GROUPS = ("shell", "frame", "leg", "pad", "servo", "horn", "screw",
          "electronics", "battery", "screen", "switch")

MANIFEST_NAME = "manifest.json"
FORMAT_NAME = "sesame-studio-robot-model"
FORMAT_VERSION = 1


@dataclass
class Part:
    name: str               # unique, e.g. "servo_hip_R1.body", "femur_R1", "screw_horn_R1_a"
    link: str               # "body" or a joint name ("R1".."L4")
    group: str              # one of GROUPS
    material: str           # key into RobotModel.materials
    vertices: np.ndarray    # (N,3) float32, bind pose, body frame, mm
    normals: np.ndarray     # (N,3) float32, unit
    colors: np.ndarray      # (N,3) float32 sRGB 0..1 (CAD colours, per vertex)
    indices: np.ndarray     # (M,3) uint32
    bounds_min: np.ndarray
    bounds_max: np.ndarray
    cover_variant: str | None = None   # only set on alternative top covers
    mass_kg: float = 0.0               # rough estimate; 0 for purely visual sub-parts
    source: str = ""                   # where the geometry comes from (CAD path / STL file)

    @property
    def triangle_count(self) -> int:
        return int(self.indices.shape[0])

    @property
    def vertex_count(self) -> int:
        return int(self.vertices.shape[0])


@dataclass
class Material:
    name: str
    roughness: float
    metallic: float
    emissive: float = 0.0
    tintable: bool = False   # printed plastic the user may recolour


@dataclass
class Screen:
    """The OLED's active 128x64 pixel area as a quad in the body frame (bind
    pose).  ``right`` is the direction of increasing pixel x as seen by a
    viewer looking at the robot's face, ``up`` points towards pixel row 0 and
    ``normal`` out of the face."""
    center: np.ndarray
    right: np.ndarray
    up: np.ndarray
    normal: np.ndarray
    width: float
    height: float

    def corners(self) -> np.ndarray:
        """(4,3) corners: top-left, top-right, bottom-right, bottom-left (as seen
        by a viewer facing the screen), i.e. pixel (0,0), (W,0), (W,H), (0,H)."""
        hx = self.right * (self.width / 2.0)
        hy = self.up * (self.height / 2.0)
        c = self.center
        return np.array([c - hx + hy, c + hx + hy, c + hx - hy, c - hx - hy], dtype=np.float64)


@dataclass
class RobotModel:
    joints: dict[str, JointDef]
    bind_angles: dict[str, float]
    parts: list[Part]
    materials: dict[str, Material]
    screen: Screen
    collision_points: dict[str, np.ndarray]   # link -> (K,3) float64, bind pose
    mass_kg: float
    com: np.ndarray
    bounds_min: np.ndarray                    # bind pose, default cover only
    bounds_max: np.ndarray
    cover_variants: list[str]
    default_cover: str
    link_mass_kg: dict[str, float] = field(default_factory=dict)     # per link, default cover
    link_com: dict[str, np.ndarray] = field(default_factory=dict)    # per link, bind pose
    manifest: dict = field(default_factory=dict, repr=False)          # the raw manifest.json
    model_dir: Path | None = None

    _cache = {}   # class-level: resolved model dir -> RobotModel

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, model_dir: Path | None = None) -> "RobotModel":
        """Load (and cache) the model.  Default dir = ``app.paths.ASSETS_DIR / "model"``."""
        if model_dir is None:
            from .paths import ASSETS_DIR
            model_dir = ASSETS_DIR / "model"
        model_dir = Path(model_dir).resolve()
        cached = cls._cache.get(model_dir)
        if cached is None:
            cached = cls._cache[model_dir] = cls._read(model_dir)
        return cached

    @classmethod
    def clear_cache(cls) -> None:
        cls._cache.clear()

    @classmethod
    def _read(cls, model_dir: Path) -> "RobotModel":
        manifest_path = model_dir / MANIFEST_NAME
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"robot model not found: {manifest_path} (generate it with tools/build_model.py)")
        with open(manifest_path, "r", encoding="utf-8") as fh:
            man = json.load(fh)
        if man.get("format") != FORMAT_NAME or int(man.get("version", 0)) != FORMAT_VERSION:
            raise ValueError(f"unsupported robot model format in {manifest_path}")

        with np.load(model_dir / man["buffers"]["file"]) as npz:
            positions = np.ascontiguousarray(npz["positions"], dtype=np.float32)
            normals_q = npz["normals"]
            colors_q = npz["colors"]
            indices = np.ascontiguousarray(npz["indices"], dtype=np.uint32)

        normals = normals_q.astype(np.float32)
        if np.issubdtype(normals_q.dtype, np.integer):
            normals /= float(np.iinfo(normals_q.dtype).max)
        length = np.sqrt(np.einsum("ij,ij->i", normals, normals))
        np.maximum(length, 1e-12, out=length)
        normals /= length[:, None]
        colors = colors_q.astype(np.float32)
        if np.issubdtype(colors_q.dtype, np.integer):
            colors /= 255.0

        parts: list[Part] = []
        for p in man["parts"]:
            v0, vn = int(p["vertex_offset"]), int(p["vertex_count"])
            t0, tn = int(p["triangle_offset"]), int(p["triangle_count"])
            parts.append(Part(
                name=p["name"], link=p["link"], group=p["group"], material=p["material"],
                vertices=positions[v0:v0 + vn], normals=normals[v0:v0 + vn],
                colors=colors[v0:v0 + vn], indices=indices[t0:t0 + tn],
                bounds_min=np.asarray(p["bounds_min"], dtype=np.float32),
                bounds_max=np.asarray(p["bounds_max"], dtype=np.float32),
                cover_variant=p.get("cover_variant"),
                mass_kg=float(p.get("mass_g", 0.0)) / 1000.0,
                source=p.get("source", ""),
            ))

        joints = {
            j["name"]: JointDef(
                name=j["name"], parent=j.get("parent"),
                pivot=tuple(float(x) for x in j["pivot"]),
                axis=tuple(float(x) for x in _unit(j["axis"])),      # exactly unit length
                sign=float(j["sign"]), bind_angle=float(j["bind_angle"]))
            for j in man["joints"]
        }
        materials = {name: Material(name=name, **props) for name, props in man["materials"].items()}
        s = man["screen"]
        screen = Screen(center=np.asarray(s["center"], dtype=np.float64),
                        right=_unit(s["right"]), up=_unit(s["up"]), normal=_unit(s["normal"]),
                        width=float(s["width"]), height=float(s["height"]))
        mass = man["mass"]
        return cls(
            joints=joints,
            bind_angles={k: float(v) for k, v in man["bind_angles"].items()},
            parts=parts,
            materials=materials,
            screen=screen,
            collision_points={k: np.asarray(v, dtype=np.float64).reshape(-1, 3)
                              for k, v in man["collision_points"].items()},
            mass_kg=float(mass["total_kg"]),
            com=np.asarray(mass["com"], dtype=np.float64),
            bounds_min=np.asarray(man["bounds_min"], dtype=np.float64),
            bounds_max=np.asarray(man["bounds_max"], dtype=np.float64),
            cover_variants=list(man["cover_variants"]),
            default_cover=man["default_cover"],
            link_mass_kg={k: float(v["mass_kg"]) for k, v in mass.get("links", {}).items()},
            link_com={k: np.asarray(v["com"], dtype=np.float64) for k, v in mass.get("links", {}).items()},
            manifest=man,
            model_dir=model_dir,
        )

    # ------------------------------------------------------------------ queries
    def parts_for(self, cover: str | None = None) -> list[Part]:
        """Parts to draw for a top-cover choice: everything that is not a cover
        variant plus the chosen (or default) cover."""
        cover = cover or self.default_cover
        if cover not in self.cover_variants:
            raise KeyError(f"unknown cover variant {cover!r}; available: {self.cover_variants}")
        return [p for p in self.parts if p.cover_variant in (None, cover)]

    def part(self, name: str) -> Part:
        for p in self.parts:
            if p.name == name:
                return p
        raise KeyError(name)

    @property
    def links(self) -> list[str]:
        """``"body"`` followed by the joint/link names."""
        return [BODY_LINK] + list(self.joints)


def _unit(v) -> np.ndarray:
    a = np.asarray(v, dtype=np.float64)
    n = float(np.linalg.norm(a))
    return a / n if n > 0 else a
