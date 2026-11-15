#!/usr/bin/env python
"""Build Sesame Studio's 3D robot model assets from the CAD STEP assembly.

    python -X utf8 tools/build_model.py <path-to-Sesame-ESP32.step> [--out assets/model]

Reads the XCAF assembly (names, colours incl. per-face colours, instance
transforms) with OpenCascade (``cadquery-ocp``), tessellates every part with
smooth B-rep surface normals, bakes the assembly transforms, classifies the
parts onto kinematic links, derives the 8 joints from the servo-shaft screws
and writes

    <out>/manifest.json   human-readable description (joints, parts, screen ...)
    <out>/meshes.npz      compressed vertex / normal / colour / index buffers

``app/robot_model.py`` is the (pure numpy) runtime loader for those files.

Build-time dependencies: cadquery-ocp, numpy, scipy.  None of them is needed
at runtime.

Conventions (identical to app/kinematics.py): millimetres, +Y up, the robot's
front (OLED) faces -X, left side = +Z, right side = -Z.  The geometry is kept
in the CAD assembly frame and in the pose the assembly was exported in (the
firmware's STAND pose) -- that is the *bind pose*.

The part classification below is specific to the names used in
Sesame-ESP32.step; anything it does not recognise is reported and kept as a
generic body part so nothing silently disappears.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.kinematics import JointDef, link_transforms, rotation_about_line  # noqa: E402

# --------------------------------------------------------------------------- #
# Project knowledge that cannot be read from the CAD file
# --------------------------------------------------------------------------- #

#: Servo angles (degrees) of the pose the STEP was exported in (firmware STAND).
BIND_ANGLES = {"R1": 135.0, "R2": 45.0, "L1": 45.0, "L2": 135.0,
               "R4": 0.0, "R3": 180.0, "L3": 0.0, "L4": 180.0}
HIP_JOINTS = ("R1", "R2", "L1", "L2")
#: knee joint -> hip joint (femur) it is mounted on
KNEE_PARENT = {"R3": "R1", "L3": "L1", "R4": "R2", "L4": "L2"}
JOINT_ORDER = ("R1", "R2", "L1", "L2", "R3", "L3", "R4", "L4")

#: OLED active area of a 0.96" 128x64 SSD1306 panel (mm)
SCREEN_W, SCREEN_H = 21.744, 10.864
SCREEN_PROUD = 0.05

#: physics collision points: max per link, and how far (mm) a mirrored point may sit
#: from the real surface of the link it is assigned to
COLLISION_LIMIT = 24
COLLISION_TOL_MM = 0.3

#: tessellation quality per class: (linear deflection mm, angular deflection rad)
TESS = {
    "printed": (0.03, 0.20),       # covers, frame, femurs, feet: smooth silhouettes
    "servo": (0.15, 0.70),         # SG90 bodies (mostly hidden, many tiny faces)
    "horn": (0.08, 0.50),
    "screw": (0.15, 1.00),         # ~4 mm heads; threads are modelled, keep them coarse
    "electronics": (0.12, 0.90),   # PCBs (hundreds of holes), OLED module
    "switch": (0.08, 0.55),        # rocker switch housing + rocker (visible)
    "hidden": (0.20, 1.00),        # switch internals (contacts, spring)
    "battery": (0.04, 0.25),
}

MATERIALS = {
    # name: roughness, metallic, emissive, tintable
    "pla_shell":      dict(roughness=0.55, metallic=0.0, emissive=0.0, tintable=True),
    "pla_frame":      dict(roughness=0.60, metallic=0.0, emissive=0.0, tintable=True),
    "pla_leg":        dict(roughness=0.55, metallic=0.0, emissive=0.0, tintable=True),
    "pla_accent":     dict(roughness=0.60, metallic=0.0, emissive=0.0, tintable=False),
    "servo_plastic":  dict(roughness=0.45, metallic=0.0, emissive=0.0, tintable=False),
    "horn_plastic":   dict(roughness=0.40, metallic=0.0, emissive=0.0, tintable=False),
    "metal_screw":    dict(roughness=0.35, metallic=1.0, emissive=0.0, tintable=False),
    "metal_contact":  dict(roughness=0.30, metallic=1.0, emissive=0.0, tintable=False),
    "pcb":            dict(roughness=0.50, metallic=0.1, emissive=0.0, tintable=False),
    "oled_glass":     dict(roughness=0.08, metallic=0.0, emissive=0.0, tintable=False),
    "battery_wrap":   dict(roughness=0.35, metallic=0.0, emissive=0.0, tintable=False),
    "connector":      dict(roughness=0.50, metallic=0.0, emissive=0.0, tintable=False),
}

# Mass model (see manifest["mass"]["method"]).
DENSITY_PLA_EFFECTIVE = 0.87e-3   # g/mm^3  (1.24 g/cm^3 PLA x ~0.7 average fill of walls + infill)
DENSITY_POM = 1.41e-3             # servo horns
DENSITY_STEEL = 7.9e-3            # screws
DENSITY_PLASTIC = 1.2e-3          # misc. solid plastic
MASS_SERVO_BODY_G = 8.6           # SG90 = 9 g including horn + screws
MASS_BATTERY_G = 40.0             # 2x 14500 Li-ion + wrap
MASS_DISTRO_G = 12.0              # distro PCB incl. the unmodelled MCU board, headers, wiring
MASS_OLED_G = 3.0
MASS_SWITCH_G = 3.0

DEFAULT_COLOR = (150, 150, 150)

STL_COVERS = {   # cover_variant -> STL path relative to the repo
    "enclosed": "assets/stl/top-covers/Top-Cover-Enclosed-v117.stl",
    "cat": "assets/stl/top-covers/Top-Cover-Cat-v100.stl",
    "no-ears": "assets/stl/top-covers/Top-Cover-No-Ears-v100.stl",
}
STL_SMOOTH_ANGLE_DEG = 35.0


# --------------------------------------------------------------------------- #
# Small numpy helpers
# --------------------------------------------------------------------------- #

def unit(v):
    v = np.asarray(v, dtype=np.float64)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def xf_points(m, p):
    return np.asarray(p, dtype=np.float64) @ m[:3, :3].T + m[:3, 3]


def xf_dirs(m, d):
    return np.asarray(d, dtype=np.float64) @ m[:3, :3].T


def line_distance(p0, d0, p1, d1):
    """(angle in degrees between directions, distance of point p1 from line p0+t*d0)."""
    d0, d1 = unit(d0), unit(d1)
    ang = math.degrees(math.acos(min(1.0, abs(float(d0 @ d1)))))
    w = np.asarray(p1, float) - np.asarray(p0, float)
    return ang, float(np.linalg.norm(w - (w @ d0) * d0))


def project_on_line(p0, d0, q):
    d0 = unit(d0)
    return np.asarray(p0, float) + ((np.asarray(q, float) - p0) @ d0) * d0


@dataclass
class Mesh:
    V: np.ndarray            # (n,3) float64
    N: np.ndarray            # (n,3) float64 unit
    C: np.ndarray            # (n,3) uint8
    T: np.ndarray            # (m,3) int64

    @staticmethod
    def concat(meshes: list["Mesh"]) -> "Mesh":
        if not meshes:
            return Mesh(np.zeros((0, 3)), np.zeros((0, 3)), np.zeros((0, 3), np.uint8), np.zeros((0, 3), np.int64))
        off, ts = 0, []
        for m in meshes:
            ts.append(m.T + off)
            off += len(m.V)
        return Mesh(np.concatenate([m.V for m in meshes]), np.concatenate([m.N for m in meshes]),
                    np.concatenate([m.C for m in meshes]), np.concatenate(ts))

    def transformed(self, m: np.ndarray) -> "Mesh":
        V = xf_points(m, self.V)
        N = xf_dirs(m, self.N)
        T = self.T
        if np.linalg.det(m[:3, :3]) < 0:      # mirror: keep outward winding
            T = T[:, [0, 2, 1]]
        return Mesh(V, N, self.C, T)

    def welded(self) -> "Mesh":
        """Merge vertices that share position, normal and colour (smooth seams between
        tangent-continuous faces collapse, hard edges stay split) and drop degenerate
        triangles."""
        if not len(self.V):
            return self
        key = np.concatenate([np.round(self.V * 2000.0), np.round(self.N * 500.0),
                              self.C.astype(np.float64)], axis=1).astype(np.int64)
        _, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
        inverse = inverse.reshape(-1)
        order = np.argsort(first)                 # keep original vertex order (better locality)
        rank = np.empty_like(order)
        rank[order] = np.arange(len(order))
        T = rank[inverse][self.T]
        ok = (T[:, 0] != T[:, 1]) & (T[:, 1] != T[:, 2]) & (T[:, 0] != T[:, 2])
        keep = first[order]
        N = self.N[keep]
        N = N / np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-12)
        return Mesh(self.V[keep], N, self.C[keep], T[ok])

    @property
    def tri_count(self) -> int:
        return int(len(self.T))


# --------------------------------------------------------------------------- #
# STEP / XCAF reading and tessellation (OpenCascade)
# --------------------------------------------------------------------------- #

class Cad:
    """Thin wrapper around the XCAF document: leaves with world transforms,
    colours and cached tessellations of the prototypes."""

    def __init__(self, step_path: Path):
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.TDocStd import TDocStd_Document
        from OCP.XCAFDoc import XCAFDoc_DocumentTool

        self.doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
        reader = STEPCAFControl_Reader()
        reader.SetColorMode(True)
        reader.SetNameMode(True)
        if reader.ReadFile(str(step_path)) != IFSelect_RetDone:
            raise SystemExit(f"cannot read STEP file: {step_path}")
        reader.Transfer(self.doc)
        self.st = XCAFDoc_DocumentTool.ShapeTool_s(self.doc.Main())
        self.ct = XCAFDoc_DocumentTool.ColorTool_s(self.doc.Main())
        self.protos: dict[str, Proto] = {}
        self.leaves: list[Leaf] = []
        self._walk_all()

    # -- labels ---------------------------------------------------------------
    def name(self, label) -> str:
        from OCP.TDataStd import TDataStd_Name
        n = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), n):
            return n.Get().ToExtString()
        return "?"

    def color(self, label):
        """Authored sRGB colour (0..255 triple) of a label, or None."""
        from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
        from OCP.XCAFDoc import XCAFDoc_ColorCurv, XCAFDoc_ColorGen, XCAFDoc_ColorSurf
        c = Quantity_Color()
        for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen, XCAFDoc_ColorCurv):
            if self.ct.GetColor_s(label, kind, c):
                # OCCT keeps colours linear; the STEP file stores sRGB.
                return tuple(int(round(max(0.0, min(1.0, x)) * 255)) for x in c.Values(Quantity_TOC_sRGB))
        return None

    @staticmethod
    def entry(label) -> str:
        from OCP.TCollection import TCollection_AsciiString
        from OCP.TDF import TDF_Tool
        s = TCollection_AsciiString()
        TDF_Tool.Entry_s(label, s)
        return s.ToCString()

    @staticmethod
    def matrix(trsf) -> np.ndarray:
        m = np.eye(4)
        for r in range(3):
            for c in range(4):
                m[r, c] = trsf.Value(r + 1, c + 1)
        return m

    # -- tree -----------------------------------------------------------------
    def _walk_all(self):
        from OCP.collections import Sequence_TDF_Label
        from OCP.TopLoc import TopLoc_Location
        free = Sequence_TDF_Label()
        self.st.GetFreeShapes(free)
        for i in range(1, free.Length() + 1):
            self._walk(free.Value(i), TopLoc_Location(), ())

    def _walk(self, label, loc, path):
        from OCP.collections import Sequence_TDF_Label
        from OCP.TDF import TDF_Label
        target = label
        if self.st.IsReference_s(label):
            ref = TDF_Label()
            self.st.GetReferredShape_s(label, ref)
            target = ref
        wloc = loc.Multiplied(self.st.GetLocation_s(label))
        path = path + (self.name(label),)
        if self.st.IsAssembly_s(target):
            comps = Sequence_TDF_Label()
            self.st.GetComponents_s(target, comps)
            for i in range(1, comps.Length() + 1):
                self._walk(comps.Value(i), wloc, path)
            return
        key = self.entry(target)
        proto = self.protos.get(key)
        if proto is None:
            proto = self.protos[key] = Proto(self, target)
        trsf = wloc.Transformation()
        if abs(trsf.ScaleFactor() - 1.0) > 1e-9:
            raise SystemExit(f"scaled instance not supported: {path}")
        self.leaves.append(Leaf(path=path[1:] if len(path) > 1 else path, ref=self.name(target),
                                proto=proto, matrix=self.matrix(trsf), inst_color=self.color(label)))


class Proto:
    """A prototype part (referred shape): B-rep queries + cached tessellation."""

    def __init__(self, cad: Cad, label):
        from OCP.collections import Sequence_TDF_Label
        self.cad = cad
        self.label = label
        self.name = cad.name(label)
        self.shape = cad.st.GetShape_s(label)
        self.color = cad.color(label)
        self._sub: dict[int, list] = {}
        subs = Sequence_TDF_Label()
        cad.st.GetSubShapes_s(label, subs)
        for i in range(1, subs.Length() + 1):
            lab = subs.Value(i)
            shp = cad.st.GetShape_s(lab)
            self._sub.setdefault(hash(shp), []).append((shp, cad.color(lab)))
        self.faces: list[FaceRec] | None = None
        self.tess_params = None
        self.flipped_normals = 0
        self._solids = None

    def sub_color(self, shape):
        for cand, col in self._sub.get(hash(shape), ()):
            if cand.IsSame(shape):
                return col
        return None

    # -- topology ---------------------------------------------------------------
    def solids(self):
        """[(solid shape, colour, volume mm^3, centre of mass)] in prototype coordinates."""
        if self._solids is None:
            from OCP.BRepGProp import BRepGProp
            from OCP.GProp import GProp_GProps
            from OCP.TopAbs import TopAbs_SOLID
            from OCP.TopExp import TopExp_Explorer
            out = []
            ex = TopExp_Explorer(self.shape, TopAbs_SOLID)
            while ex.More():
                s = ex.Current()
                g = GProp_GProps()
                BRepGProp.VolumeProperties_s(s, g)
                c = g.CentreOfMass()
                out.append((s, self.sub_color(s) or self.color, abs(g.Mass()), np.array([c.X(), c.Y(), c.Z()])))
                ex.Next()
            self._solids = out
        return self._solids

    def volume_and_com(self, solids=None):
        sel = [s for i, s in enumerate(self.solids()) if solids is None or i in solids]
        vol = sum(s[2] for s in sel)
        if vol <= 0:
            return 0.0, np.zeros(3)
        return vol, sum(s[2] * s[3] for s in sel) / vol

    def axes(self, solids=None):
        """Axes of all surfaces of revolution: [(point, dir, radius, kind, solid index)]."""
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder
        from OCP.TopAbs import TopAbs_FACE
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS
        out = []
        for si, (solid, *_rest) in enumerate(self.solids()):
            if solids is not None and si not in solids:
                continue
            ex = TopExp_Explorer(solid, TopAbs_FACE)
            while ex.More():
                a = BRepAdaptor_Surface(TopoDS.Face(ex.Current()))
                t = a.GetType()
                ax = None
                if t == GeomAbs_Cylinder:
                    ax, rad, kind = a.Cylinder().Axis(), a.Cylinder().Radius(), "cyl"
                elif t == GeomAbs_Cone:
                    ax, rad, kind = a.Cone().Axis(), a.Cone().RefRadius(), "cone"
                if ax is not None:
                    p, d = ax.Location(), ax.Direction()
                    out.append((np.array([p.X(), p.Y(), p.Z()]), np.array([d.X(), d.Y(), d.Z()]), rad, kind, si))
                ex.Next()
        return out

    # -- tessellation -----------------------------------------------------------
    def tessellate(self, lin: float, ang: float) -> list["FaceRec"]:
        if self.faces is not None:
            return self.faces
        from OCP.BRep import BRep_Tool
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepLib import BRepLib_ToolTriangulatedShape
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SHELL, TopAbs_SOLID
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopLoc import TopLoc_Location
        from OCP.TopoDS import TopoDS

        self.tess_params = (lin, ang)
        BRepMesh_IncrementalMesh(self.shape, lin, False, ang, True)
        faces: list[FaceRec] = []

        def add_faces(container, solid_idx, shell_idx, base_color):
            ex = TopExp_Explorer(container, TopAbs_FACE)
            while ex.More():
                face = TopoDS.Face(ex.Current())
                ex.Next()
                loc = TopLoc_Location()
                tri = BRep_Tool.Triangulation_s(face, loc)
                if tri is None or tri.NbNodes() == 0 or tri.NbTriangles() == 0:
                    continue
                BRepLib_ToolTriangulatedShape.ComputeNormals_s(face, tri)
                n = tri.NbNodes()
                V = np.empty((n, 3))
                N = np.empty((n, 3))
                for i in range(1, n + 1):
                    p = tri.Node(i)
                    d = tri.Normal(i)
                    V[i - 1] = (p.X(), p.Y(), p.Z())
                    N[i - 1] = (d.X(), d.Y(), d.Z())
                T = np.array([tri.Triangle(i).Get() for i in range(1, tri.NbTriangles() + 1)],
                             dtype=np.int64).reshape(-1, 3) - 1
                m = Cad.matrix(loc.Transformation())
                V = xf_points(m, V)
                N = xf_dirs(m, N)
                if face.Orientation() == TopAbs_REVERSED:
                    T = T[:, [0, 2, 1]]
                # Make the surface normals agree with the (outward) triangle winding and
                # repair degenerate ones (apex of a cone, pole of a sphere ...).
                g = np.cross(V[T[:, 1]] - V[T[:, 0]], V[T[:, 2]] - V[T[:, 0]])
                gv = np.zeros_like(V)
                for k in range(3):
                    np.add.at(gv, T[:, k], g)
                if float((gv * N).sum()) < 0.0:
                    N = -N
                    self.flipped_normals += 1
                ln = np.linalg.norm(N, axis=1)
                gl = np.linalg.norm(gv, axis=1)
                bad = (ln < 0.5) | ((N * gv).sum(axis=1) < 0.0)
                bad &= gl > 0
                N[bad] = gv[bad] / gl[bad, None]
                ln = np.linalg.norm(N, axis=1)
                N[ln > 0] /= ln[ln > 0, None]
                own = self.sub_color(face)
                stype = str(BRepAdaptor_Surface(face).GetType()).split("_")[-1]
                faces.append(FaceRec(solid_idx, shell_idx, own or base_color, own is not None, stype, V, N, T))

        ex_s = TopExp_Explorer(self.shape, TopAbs_SOLID)
        si = 0
        while ex_s.More():
            solid = ex_s.Current()
            scol = self.sub_color(solid) or self.color
            ex_h = TopExp_Explorer(solid, TopAbs_SHELL)
            hi = 0
            while ex_h.More():
                shell = ex_h.Current()
                add_faces(shell, si, hi, self.sub_color(shell) or scol)
                ex_h.Next()
                hi += 1
            ex_s.Next()
            si += 1
        # faces that are not part of any solid (none in the current CAD, but be safe)
        ex_f = TopExp_Explorer(self.shape, TopAbs_SHELL, TopAbs_SOLID)
        while ex_f.More():
            add_faces(ex_f.Current(), -1, 0, self.color)
            ex_f.Next()
        self.faces = faces
        return faces


@dataclass
class FaceRec:
    solid: int
    shell: int
    color: tuple | None
    own_color: bool
    stype: str
    V: np.ndarray
    N: np.ndarray
    T: np.ndarray


@dataclass
class Leaf:
    path: tuple
    ref: str
    proto: Proto
    matrix: np.ndarray
    inst_color: tuple | None = None
    used: bool = False

    @property
    def label(self) -> str:
        return "/".join(self.path)

    @property
    def origin(self) -> np.ndarray:
        return self.matrix[:3, 3].copy()

    def mesh(self, tess: str, pred=None) -> Mesh:
        """World-space mesh of this instance (optionally only faces where pred(face))."""
        lin, ang = TESS[tess]
        parts = []
        for f in self.proto.tessellate(lin, ang):
            if pred is not None and not pred(f):
                continue
            col = np.array(f.color or self.inst_color or DEFAULT_COLOR, dtype=np.uint8)
            parts.append(Mesh(f.V, f.N, np.tile(col, (len(f.V), 1)), f.T))
        return Mesh.concat(parts).transformed(self.matrix).welded()

    def world_axes(self, solids=None):
        return [(xf_points(self.matrix, p), unit(xf_dirs(self.matrix, d)), r, kind, si)
                for p, d, r, kind, si in self.proto.axes(solids)]

    def volume_and_com(self, solids=None):
        vol, com = self.proto.volume_and_com(solids)
        return vol, xf_points(self.matrix, com)

    def bounds(self, tess: str):
        lin, ang = TESS[tess]
        faces = self.proto.tessellate(lin, ang)
        if not faces:
            return None
        V = xf_points(self.matrix, np.concatenate([f.V for f in faces]))
        return V.min(axis=0), V.max(axis=0)


# --------------------------------------------------------------------------- #
# Output part
# --------------------------------------------------------------------------- #

@dataclass
class OutPart:
    name: str
    link: str
    group: str
    material: str
    mesh: Mesh
    source: str
    cover_variant: str | None = None
    mass_g: float = 0.0
    com: np.ndarray | None = None
    note: str | None = None
    collide: bool = True


# --------------------------------------------------------------------------- #
# STL cover variants
# --------------------------------------------------------------------------- #

def read_stl_triangles(path: Path) -> np.ndarray:
    data = path.read_bytes()
    (count,) = struct.unpack_from("<I", data, 80)
    if 84 + count * 50 != len(data):
        raise ValueError(f"{path.name}: not a binary STL")
    rec = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]),
                        count=count, offset=84)
    return rec["v"].astype(np.float64)


def smooth_stl_mesh(tris: np.ndarray, color, angle_deg: float) -> Mesh:
    """Weld an STL triangle soup and compute angle-thresholded smooth normals."""
    flat = tris.reshape(-1, 3)
    key = np.round(flat * 1000.0).astype(np.int64)
    _, inverse = np.unique(key, axis=0, return_inverse=True)
    inverse = inverse.reshape(-1, 3)                       # welded vertex id per corner
    cross = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    area2 = np.linalg.norm(cross, axis=1)
    ok = area2 > 1e-12
    tris, inverse, cross, area2 = tris[ok], inverse[ok], cross[ok], area2[ok]
    fn = cross / area2[:, None]
    incident: dict[int, list[int]] = {}
    for t, vids in enumerate(inverse):
        for v in vids:
            incident.setdefault(int(v), []).append(t)
    cos_thr = math.cos(math.radians(angle_deg))
    normals = np.empty((len(tris), 3, 3))
    for t in range(len(tris)):
        for k in range(3):
            adj = np.array(incident[int(inverse[t, k])])
            sel = adj[(fn[adj] @ fn[t]) >= cos_thr]
            n = (cross[sel]).sum(axis=0)                   # area weighted
            ln = np.linalg.norm(n)
            normals[t, k] = n / ln if ln > 0 else fn[t]
    n_corner = len(tris) * 3
    col = np.tile(np.array(color, dtype=np.uint8), (n_corner, 1))
    return Mesh(tris.reshape(-1, 3), normals.reshape(-1, 3), col,
                np.arange(n_corner, dtype=np.int64).reshape(-1, 3)).welded()


def mesh_volume_com(mesh: Mesh):
    a, b, c = mesh.V[mesh.T[:, 0]], mesh.V[mesh.T[:, 1]], mesh.V[mesh.T[:, 2]]
    v6 = np.einsum("ij,ij->i", a, np.cross(b, c))
    vol = v6.sum() / 6.0
    com = ((a + b + c) / 4.0 * v6[:, None]).sum(axis=0) / (6.0 * vol) if abs(vol) > 1e-9 else mesh.V.mean(axis=0)
    return abs(vol), com


# --------------------------------------------------------------------------- #
# Collision points
# --------------------------------------------------------------------------- #

def hull_vertices(points: np.ndarray) -> np.ndarray:
    from scipy.spatial import ConvexHull
    pts = np.unique(np.round(points, 3), axis=0)
    return pts[ConvexHull(pts).vertices]


def slab_corners(points: np.ndarray, direction, depth: float, ndirs: int = 8) -> list[np.ndarray]:
    """Corner points of the slab of ``points`` lying within ``depth`` of the extreme
    along ``direction``: the support points in ``ndirs`` in-plane directions."""
    d = unit(direction)
    s = points @ d
    slab = points[s >= s.max() - depth]
    a = unit(np.cross(d, [0.0, 0.0, 1.0] if abs(d[2]) < 0.9 else [1.0, 0.0, 0.0]))
    b = np.cross(d, a)
    out = []
    for k in range(ndirs):
        t = 2.0 * math.pi * (k + 0.5) / ndirs
        q = slab @ (math.cos(t) * a + math.sin(t) * b) + 1e-3 * (slab @ d)
        out.append(slab[int(np.argmax(q))])
    return out


def pick_collision_points(points: np.ndarray, mandatory: list[np.ndarray], limit: int = COLLISION_LIMIT) -> np.ndarray:
    """Decimate a point cloud to <= limit convex-hull vertices: the mandatory ones
    first (de-duplicated), then farthest-point sampling over the hull."""
    hull = hull_vertices(points)
    chosen: list[np.ndarray] = []
    for p in mandatory:
        p = hull[int(np.argmin(np.linalg.norm(hull - p, axis=1)))]      # snap onto the hull
        if all(np.linalg.norm(p - q) > 1.5 for q in chosen):
            chosen.append(p)
    chosen = chosen[:limit]
    dist = np.full(len(hull), np.inf)
    for q in chosen:
        dist = np.minimum(dist, np.linalg.norm(hull - q, axis=1))
    while len(chosen) < limit:
        i = int(np.argmax(dist))
        if dist[i] < 2.0:
            break
        chosen.append(hull[i])
        dist = np.minimum(dist, np.linalg.norm(hull - hull[i], axis=1))
    return np.round(np.array(chosen, dtype=np.float64), 3)


def pick_symmetric_collision_points(points: np.ndarray, mandatory: list[np.ndarray], z_mid: float,
                                    limit: int = COLLISION_LIMIT, snap: float = 0.75) -> np.ndarray:
    """Like pick_collision_points, but the result is mirror-symmetric about the plane
    z = z_mid: hull vertices of the +Z half are chosen and each gets its mirror image.
    A point within ``snap`` of the plane is moved onto it and used once.  Mandatory
    points on the -Z side are ignored (their +Z twins cover them)."""
    hull = hull_vertices(points)
    half = hull[hull[:, 2] >= z_mid - snap]

    def mirrored(p):
        return np.array([p[0], p[1], 2.0 * z_mid - p[2]])

    def canonical(p):
        p = np.round(half[int(np.argmin(np.linalg.norm(half - p, axis=1)))], 3)
        return np.array([p[0], p[1], z_mid]) if abs(p[2] - z_mid) <= snap else p

    def cost(p):
        return 1 if p[2] == z_mid else 2

    chosen: list[np.ndarray] = []
    used = 0
    for p in mandatory:
        if p[2] < z_mid - snap:
            continue
        p = canonical(p)
        if used + cost(p) <= limit and all(np.linalg.norm(p - q) > 1.5 for q in chosen):
            chosen.append(p)
            used += cost(p)
    dist = np.full(len(half), np.inf)
    for q in chosen:
        dist = np.minimum(dist, np.minimum(np.linalg.norm(half - q, axis=1),
                                           np.linalg.norm(half - mirrored(q), axis=1)))
    while used < limit:
        i = int(np.argmax(dist))
        if dist[i] < 2.0:
            break
        p = canonical(half[i])
        dist[i] = -1.0
        if used + cost(p) > limit:
            continue                                    # only an on-plane point still fits
        chosen.append(p)
        used += cost(p)
        dist = np.minimum(dist, np.minimum(np.linalg.norm(half - p, axis=1),
                                           np.linalg.norm(half - mirrored(p), axis=1)))
    out = chosen + [mirrored(p) for p in chosen if cost(p) == 2]
    return np.round(np.array(out, dtype=np.float64), 4)


def surface_distance(points: np.ndarray, mesh: Mesh):
    """Exact distance of each point to a triangle mesh.

    Returns (distance, outside, closest): ``closest[i]`` is the nearest point of the
    surface and ``outside[i]`` is True when the point lies on the outer side of its
    closest triangle (i.e. it floats off the surface rather than sitting inside
    the solid)."""
    A, B, C = mesh.V[mesh.T[:, 0]], mesh.V[mesh.T[:, 1]], mesh.V[mesh.T[:, 2]]
    lo, hi = np.minimum(np.minimum(A, B), C), np.maximum(np.maximum(A, B), C)
    points = np.asarray(points, dtype=np.float64)
    dist = np.empty(len(points))
    outside = np.zeros(len(points), dtype=bool)
    closest = np.empty((len(points), 3))
    for k, p in enumerate(points):
        lower = np.linalg.norm(np.maximum(0.0, np.maximum(lo - p, p - hi)), axis=1)   # to the triangle's box
        near = lower <= np.linalg.norm(A - p, axis=1).min() + 1e-9
        a, b, c = A[near], B[near], C[near]
        q = closest_points_on_triangles(p, a, b, c)
        d = np.linalg.norm(q - p, axis=1)
        i = int(np.argmin(d))
        dist[k] = d[i]
        closest[k] = q[i]
        outside[k] = float((p - q[i]) @ np.cross(b[i] - a[i], c[i] - a[i])) > 0.0
    return dist, outside, closest


def closest_points_on_triangles(p, a, b, c) -> np.ndarray:
    """Closest point to ``p`` on each triangle (a[i], b[i], c[i]) (Ericson, Real-Time
    Collision Detection 5.1.5, vectorised)."""
    ab, ac = b - a, c - a
    ap, bp, cp = p - a, p - b, p - c
    d1, d2 = (ab * ap).sum(axis=1), (ac * ap).sum(axis=1)
    d3, d4 = (ab * bp).sum(axis=1), (ac * bp).sum(axis=1)
    d5, d6 = (ab * cp).sum(axis=1), (ac * cp).sum(axis=1)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = va + vb + vc
        q = a + ab * (vb / denom)[:, None] + ac * (vc / denom)[:, None]            # face interior
        m = (va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0)                             # edge BC
        q[m] = (b + (c - b) * ((d4 - d3) / ((d4 - d3) + (d5 - d6)))[:, None])[m]
        m = (vb <= 0) & (d2 >= 0) & (d6 <= 0)                                       # edge AC
        q[m] = (a + ac * (d2 / (d2 - d6))[:, None])[m]
        m = (d6 >= 0) & (d5 <= d6)                                                  # vertex C
        q[m] = c[m]
        m = (vc <= 0) & (d1 >= 0) & (d3 <= 0)                                       # edge AB
        q[m] = (a + ab * (d1 / (d1 - d3))[:, None])[m]
        m = (d3 >= 0) & (d4 <= d3)                                                  # vertex B
        q[m] = b[m]
        m = (d1 <= 0) & (d2 <= 0)                                                   # vertex A
        q[m] = a[m]
    bad = ~np.isfinite(q).all(axis=1)                                               # degenerate triangle
    q[bad] = a[bad]
    return q


# --------------------------------------------------------------------------- #
# The build
# --------------------------------------------------------------------------- #

def quadrant_joint(p, centre, knee: bool) -> str:
    """Joint name from a position: front (x < centre) = leg 1, rear = leg 2,
    right (-Z) = R, left (+Z) = L.  Knees: R3/L3 on the front legs, R4/L4 rear."""
    side = "L" if p[2] > centre[2] else "R"
    front = p[0] < centre[0]
    if knee:
        return side + ("3" if front else "4")
    return side + ("1" if front else "2")


class Builder:
    def __init__(self, step_path: Path, out_dir: Path, verbose: bool = True):
        self.step_path = step_path
        self.out_dir = out_dir
        self.verbose = verbose
        self.notes: list[str] = []
        self.dropped: list[dict] = []
        self.parts: list[OutPart] = []
        self.checks: dict = {}

    def log(self, *a):
        if self.verbose:
            print(*a)

    def note(self, msg: str):
        self.notes.append(msg)
        self.log("NOTE:", msg)

    # -- helpers ------------------------------------------------------------------
    def take(self, pred) -> list[Leaf]:
        got = [l for l in self.cad.leaves if not l.used and not l.empty and pred(l)]
        for l in got:
            l.used = True
        return got

    def add(self, name, link, group, material, mesh, source, **kw) -> OutPart:
        if mesh.tri_count == 0:
            raise SystemExit(f"part {name} has no geometry")
        if any(p.name == name for p in self.parts):
            raise SystemExit(f"duplicate part name {name}")
        part = OutPart(name, link, group, material, mesh, source, **kw)
        self.parts.append(part)
        return part

    # -- main ---------------------------------------------------------------------
    def run(self):
        t0 = time.time()
        self.cad = cad = Cad(self.step_path)
        self.log(f"read {self.step_path.name}: {len(cad.leaves)} leaf instances, "
                 f"{len(cad.protos)} prototypes ({time.time() - t0:.1f}s)")
        for l in cad.leaves:
            l.empty = len(l.proto.solids()) == 0
            if l.empty:
                l.used = True
                self.dropped.append({"source": l.label, "reason": "empty in the STEP (hidden body in the CAD)"})

        self.derive_joints()
        self.build_servos_and_screws()
        self.build_printed_parts()
        self.build_body_parts()
        self.build_screen()
        self.build_stl_covers()
        left = [l for l in cad.leaves if not l.used]
        for l in left:                                   # never lose geometry silently
            self.note(f"unclassified CAD part kept as generic body part: {l.label}")
            self.add("misc_" + "".join(ch if ch.isalnum() else "_" for ch in l.ref).strip("_").lower(),
                     "body", "electronics", "connector", l.mesh("electronics"), l.label)
            l.used = True
        self.finish_mass()
        self.build_collision()
        self.validate()
        self.write()
        self.log(f"done in {time.time() - t0:.1f}s")

    # -- joints -------------------------------------------------------------------
    def derive_joints(self):
        cad = self.cad
        servo_bodies = [l for l in cad.leaves if not l.empty and "SG90" in l.ref and ".1_Pe" in l.ref]
        flat_heads = [l for l in cad.leaves if not l.empty and "Flat Head" in l.ref]
        if len(servo_bodies) != 8 or len(flat_heads) != 16:
            raise SystemExit(f"expected 8 servo bodies and 16 flat-head screws, got "
                             f"{len(servo_bodies)} / {len(flat_heads)}")

        def main_axis(leaf):
            axes = leaf.world_axes()
            cyl = [a for a in axes if a[3] == "cyl"] or axes
            p, d, *_ = max(cyl, key=lambda a: a[2])
            # all revolution surfaces of a screw are coaxial; verify
            for q, e, *_ in axes:
                ang, dist = line_distance(p, d, q, e)
                if ang > 0.5 or dist > 0.05:
                    raise SystemExit(f"screw {leaf.label} is not a body of revolution about one axis")
            return project_on_line(p, d, leaf.origin), d

        screw_axis = {id(l): main_axis(l) for l in flat_heads}

        # The output shaft of each servo = the revolution axis of its body that a
        # flat-head screw sits on (the horn-retaining screw).
        centre = np.mean([l.volume_and_com()[1] for l in servo_bodies], axis=0)
        self.body_centre = centre
        joints_geo: dict[str, dict] = {}
        self.servo_of: dict[str, Leaf] = {}
        for body in servo_bodies:
            best = None
            for p, d, r, kind, si in body.world_axes():
                if kind != "cyl" or r < 1.0:
                    continue
                for scr in flat_heads:
                    sp, sd = screw_axis[id(scr)]
                    ang, dist = line_distance(p, d, sp, sd)
                    if ang < 1.0 and dist < 0.6 and (best is None or dist < best[0]):
                        best = (dist, ang, scr, p, d)
            if best is None:
                raise SystemExit(f"no shaft screw found for servo {body.label}")
            dist, ang, scr, shaft_p, shaft_d = best
            sp, sd = screw_axis[id(scr)]
            knee = abs(sd[1]) < 0.5
            com = body.volume_and_com()[1]
            name = quadrant_joint(sp if not knee else com, centre, knee)
            if name in joints_geo:
                raise SystemExit(f"two servos map to joint {name}")
            joints_geo[name] = dict(pivot=sp, axis=sd, shaft_screw=scr, servo=body,
                                    shaft_offset_mm=dist, shaft_angle_deg=ang)
            self.servo_of[name] = body
        if set(joints_geo) != set(JOINT_ORDER):
            raise SystemExit(f"joints found: {sorted(joints_geo)}")

        joints: dict[str, JointDef] = {}
        down = np.array([0.0, -1.0, 0.0])
        for name in JOINT_ORDER:
            g = joints_geo[name]
            pivot, axis = g["pivot"], g["axis"]
            bind = BIND_ANGLES[name]
            if name in HIP_JOINTS:
                axis = axis if axis[1] > 0 else -axis                 # hips: +Y
                # femur direction = from the hip shaft to the knee axis, horizontal
                knee = [k for k, h in KNEE_PARENT.items() if h == name][0]
                kp, ka = joints_geo[knee]["pivot"], unit(joints_geo[knee]["axis"])
                w = kp - pivot
                w = w - (w @ ka) * ka                                  # remove the offset along the knee shaft
                w[1] = 0.0
                femur_dir = unit(w)
                g["femur_dir"] = femur_dir
                target = np.array([0.0, 0.0, 1.0 if name[0] == "L" else -1.0])   # all-90: straight sideways
                start = femur_dir
            else:
                axis = axis if axis[0] > 0 else -axis                 # knees: +X half space
                hip = joints_geo[KNEE_PARENT[name]]
                out = pivot - hip["pivot"]
                out = out - (out @ unit(axis)) * unit(axis)
                out[1] = 0.0
                target = unit(out)                                     # all-90: horizontal, pointing outward
                start = down                                           # bind: straight down
            best = None
            for sign in (1.0, -1.0):
                r = rotation_about_line((0, 0, 0), axis, sign * (90.0 - bind))[:3, :3]
                err = math.degrees(math.acos(max(-1.0, min(1.0, float((r @ start) @ target)))))
                if best is None or err < best[1]:
                    best = (sign, err)
            sign, err = best
            g["rest_error_deg"] = err
            if err > 2.0:
                raise SystemExit(f"joint {name}: bind angle {bind} is inconsistent with the CAD pose "
                                 f"(rest direction off by {err:.1f} deg)")
            joints[name] = JointDef(name=name, parent=KNEE_PARENT.get(name),
                                    pivot=tuple(float(round(x, 4)) + 0.0 for x in pivot),
                                    axis=tuple(float(round(x, 6)) + 0.0 for x in unit(axis)),
                                    sign=sign, bind_angle=bind)
            self.log(f"joint {name}: pivot={np.round(pivot, 3)} axis={np.round(axis, 4)} sign={sign:+.0f} "
                     f"bind={bind:.0f}  servo-shaft offset {g['shaft_offset_mm']:.3f} mm / "
                     f"{g['shaft_angle_deg']:.3f} deg, rest error {err:.3f} deg")
        self.joints = joints
        self.joints_geo = joints_geo
        off = {n: g["shaft_offset_mm"] for n, g in joints_geo.items() if g["shaft_offset_mm"] > 0.05}
        if off:
            self.note("servo output shaft is not exactly on the horn-screw axis for "
                      + ", ".join(f"{n} ({d:.2f} mm)" for n, d in off.items())
                      + " -- the CAD places those servo bodies/feet slightly off; the joint axis is the screw axis")

    def nearest_joint_axis(self, point, direction=None):
        """(joint name, distance) of the joint axis closest to a point (optionally
        requiring a parallel direction)."""
        best = None
        for name, j in self.joints.items():
            ang, dist = line_distance(j.pivot, j.axis, point, direction if direction is not None else j.axis)
            if direction is not None and ang > 5.0:
                continue
            if best is None or dist < best[1]:
                best = (name, dist)
        return best

    # -- servos, horns, screws ----------------------------------------------------
    def build_servos_and_screws(self):
        cad = self.cad
        horns = [l for l in cad.leaves if not l.empty and "SG90" in l.ref and ".2_Pe" in l.ref]
        horn_of: dict[str, Leaf] = {}
        for h in horns:
            # the horn hub is coaxial with its servo's shaft
            com = h.volume_and_com()[1]
            name, dist = min(((n, line_distance(j.pivot, j.axis, com, j.axis)[1]) for n, j in self.joints.items()),
                             key=lambda t: t[1])
            horn_of[name] = h
        for name in JOINT_ORDER:
            body = self.servo_of[name]
            knee = name in KNEE_PARENT
            prefix = f"servo_{'knee' if knee else 'hip'}_{name}"
            body_link = name if knee else "body"            # knee servo bodies are bolted into the foot
            horn_link = KNEE_PARENT[name] if knee else name  # horns are bolted to the femur
            vol, com = body.volume_and_com()
            self.add(prefix + ".body", body_link, "servo", "servo_plastic", body.mesh("servo"), body.label,
                     mass_g=MASS_SERVO_BODY_G, com=com)
            body.used = True
            if name in horn_of:
                h = horn_of[name]
                vol, com = h.volume_and_com()
                self.add(prefix + ".horn", horn_link, "horn", "horn_plastic", h.mesh("horn"), h.label,
                         mass_g=vol * DENSITY_POM, com=com)
                h.used = True
        # Front knee servos are "body only" copies in the CAD: synthesise their horns by
        # mirroring the rear ones (front/rear legs are mirror images about the plane
        # midway between the knee shafts).
        for name, twin in (("R3", "R4"), ("L3", "L4")):
            if name in horn_of or twin not in horn_of:
                continue
            a, b = np.array(self.joints[twin].pivot), np.array(self.joints[name].pivot)
            mirror = np.eye(4)
            mirror[0, 0] = -1.0
            mirror[0, 3] = a[0] + b[0]
            mirror[1, 3] = b[1] - a[1]
            mirror[2, 3] = b[2] - a[2]
            src = next(p for p in self.parts if p.name == f"servo_knee_{twin}.horn")
            self.add(f"servo_knee_{name}.horn", KNEE_PARENT[name], "horn", "horn_plastic",
                     src.mesh.transformed(mirror), src.source + " (mirrored)",
                     mass_g=src.mass_g, com=xf_points(mirror, src.com),
                     note=f"not in the CAD (front knee servos are body-only): mirrored from {src.name}")
            self.note(f"servo_knee_{name}.horn synthesised by mirroring servo_knee_{twin}.horn "
                      f"(the STEP has no horn on the front knee servos)")

        # flat-head screws: horn-retaining screw on the shaft (a) + horn-arm screw (b);
        # they stay with the horn, i.e. with the femur for hip *and* knee joints.
        per_joint: dict[str, list] = {}
        for scr in [l for l in cad.leaves if not l.empty and "Flat Head" in l.ref]:
            axes = scr.world_axes()
            d = max(axes, key=lambda a: a[2])[1]
            name, dist = self.nearest_joint_axis(scr.origin, d)
            per_joint.setdefault(name, []).append((dist, scr))
        for name in JOINT_ORDER:
            lst = sorted(per_joint.get(name, []), key=lambda t: t[0])
            if len(lst) != 2:
                raise SystemExit(f"joint {name}: expected 2 flat-head horn screws, found {len(lst)}")
            link = KNEE_PARENT.get(name, name)
            for suffix, (dist, scr) in zip("ab", lst):
                vol, com = scr.volume_and_com()
                self.add(f"screw_horn_{name}_{suffix}", link, "screw", "metal_screw", scr.mesh("screw"),
                         scr.label, mass_g=vol * DENSITY_STEEL, com=com,
                         note=("shaft (horn retaining) screw" if suffix == "a" else
                               f"horn arm screw, {dist:.2f} mm from the shaft axis"))
                scr.used = True

        # pan-head screws fasten the servo mounting tabs: they belong to whatever holds
        # that servo body (frame for hips, foot for knees).
        tabs: dict[str, list] = {}
        servo_boxes = {n: self.servo_of[n].bounds("servo") for n in JOINT_ORDER}
        for scr in [l for l in cad.leaves if not l.empty and "Pan Head" in l.ref]:
            p = scr.volume_and_com()[1]

            def box_dist(n):
                lo, hi = servo_boxes[n]
                return float(np.linalg.norm(np.maximum(0.0, np.maximum(lo - p, p - hi))))
            name = min(JOINT_ORDER, key=box_dist)
            if box_dist(name) > 2.0:
                raise SystemExit(f"pan-head screw {scr.label} is not on a servo")
            tabs.setdefault(name, []).append(scr)
        for name in JOINT_ORDER:
            lst = tabs.get(name, [])
            if len(lst) != 2:
                raise SystemExit(f"servo {name}: expected 2 mounting screws, found {len(lst)}")
            j = self.joints[name]
            lst.sort(key=lambda s: line_distance(j.pivot, j.axis, s.origin, j.axis)[1])   # a = nearer the shaft
            link = name if name in KNEE_PARENT else "body"
            for suffix, scr in zip("ab", lst):
                vol, com = scr.volume_and_com()
                self.add(f"screw_mount_{name}_{suffix}", link, "screw", "metal_screw", scr.mesh("screw"),
                         scr.label, mass_g=vol * DENSITY_STEEL, com=com)
                scr.used = True

    # -- printed parts ------------------------------------------------------------
    def build_printed_parts(self):
        cad = self.cad
        for kind, knee in (("femur", False), ("foot", True)):
            leaves = self.take(lambda l: l.ref.lower().startswith(kind))
            if len(leaves) != 4:
                raise SystemExit(f"expected 4 {kind} parts, found {len(leaves)}")
            for l in leaves:
                vol, com = l.volume_and_com()
                name = quadrant_joint(com, self.body_centre, knee)
                cad_name = l.ref.replace("-", "").replace("joint", "").replace(kind, "").upper()
                if cad_name != name:
                    self.note(f"CAD part {l.ref} sits in the {name} position (kept as {kind}_{name})")
                base = l.proto.color
                accent = [f for f in l.proto.tessellate(*TESS["printed"]) if f.own_color and f.color != base]
                self.add(f"{kind}_{name}", name, "leg", "pla_leg",
                         l.mesh("printed", (lambda f: not (f.own_color and f.color != base)) if accent else None),
                         l.label, mass_g=vol * DENSITY_PLA_EFFECTIVE, com=com)
                if accent:
                    self.add(f"{kind}_{name}.label", name, "leg", "pla_accent",
                             l.mesh("printed", lambda f: f.own_color and f.color != base), l.label,
                             note="differently coloured faces of the printed part (inlaid label)", collide=False)
        for ref, name, group, material in (("Internal-Frame", "frame", "frame", "pla_frame"),
                                           ("Bottom-Cover", "bottom_cover", "shell", "pla_shell")):
            (l,) = self.take(lambda l: l.ref == ref)
            vol, com = l.volume_and_com()
            self.add(name, "body", group, material, l.mesh("printed"), l.label,
                     mass_g=vol * DENSITY_PLA_EFFECTIVE, com=com)

    # -- body: cover compound, electronics, switch, battery -----------------------------
    def build_body_parts(self):
        cad = self.cad
        # The top cover lives in a nameless root compound together with two helper
        # bodies; identify the three solids by size.
        (comp,) = self.take(lambda l: l.proto.shape.ShapeType().name.endswith("COMPOUND")
                            and len(l.path) == 1 and len(l.proto.solids()) == 3 and "=>" in l.path[0])
        sol = comp.proto.solids()
        order = sorted(range(3), key=lambda i: -sol[i][2])
        i_cover = order[0]
        i_glass = next(i for i in order[1:] if self._is_box(comp, i))
        (i_block,) = [i for i in range(3) if i not in (i_cover, i_glass)]
        self.cover_leaf, self.cover_solid, self.glass_solid = comp, i_cover, i_glass
        vol, com = comp.volume_and_com({i_cover})
        self.add("top_cover", "body", "shell", "pla_shell", comp.mesh("printed", lambda f: f.solid == i_cover),
                 comp.label + f" solid {i_cover}", cover_variant="step",
                 mass_g=vol * DENSITY_PLA_EFFECTIVE, com=com)
        self.cover_color = sol[i_cover][1] or DEFAULT_COLOR
        self.add("screen_glass", "body", "screen", "oled_glass", comp.mesh("printed", lambda f: f.solid == i_glass),
                 comp.label + f" solid {i_glass}",
                 note="black display window block of the CAD cover; the screen quad sits on its front face")
        vol, com = comp.volume_and_com({i_block})
        self.add("header_block", "body", "electronics", "connector",
                 comp.mesh("printed", lambda f: f.solid == i_block), comp.label + f" solid {i_block}",
                 mass_g=vol * DENSITY_PLASTIC, com=com,
                 note="unnamed brown body next to the distro PCB (wiring/header placeholder in the CAD)")

        # OLED: the STEP contains two models of the same 0.96" module in (almost) the same
        # place.  Keep the detailed single-part one, drop the other assembly.
        (oled,) = self.take(lambda l: l.ref.startswith("OLED-128x64"))
        vol, com = oled.volume_and_com()
        self.add("oled_module", "body", "electronics", "pcb", oled.mesh("electronics"), oled.label,
                 mass_g=MASS_OLED_G, com=com,
                 note="sits ~1.6 mm behind 'screen_glass'; only visible with the glass/cover hidden")
        dup = self.take(lambda l: l.path[0].startswith("Display_OLED"))
        if dup:
            self.dropped.append({"source": dup[0].path[0], "reason":
                                 f"duplicate OLED module ({len(dup)} bodies) overlapping 'OLED-128x64-0.96 v1' "
                                 "and intersecting the display window block"})
            self.note("dropped duplicate OLED assembly 'Display_OLED_0.96_128x64 v5' (overlaps oled_module)")

        (bat,) = self.take(lambda l: "Battery" in l.ref)
        self.add("battery", "body", "battery", "battery_wrap", bat.mesh("battery"), bat.label,
                 mass_g=MASS_BATTERY_G, com=bat.volume_and_com()[1])
        (pcb,) = self.take(lambda l: "distro" in l.ref.lower())
        self.add("distro_pcb", "body", "electronics", "pcb", pcb.mesh("electronics"), pcb.label,
                 mass_g=MASS_DISTRO_G, com=pcb.volume_and_com()[1])

        sw = self.take(lambda l: l.path[0].startswith("KCD1"))
        by = {l.ref: l for l in sw}
        base, rocker = by["KCD1-B2_Base"], by["KCD1-B2_Rocker"]
        bsol = base.proto.solids()
        i_house = max(range(len(bsol)), key=lambda i: bsol[i][2])
        self.add("switch.housing", "body", "switch", "connector",
                 base.mesh("switch", lambda f: f.solid == i_house), base.label,
                 mass_g=MASS_SWITCH_G, com=base.volume_and_com()[1])
        self.add("switch.rocker", "body", "switch", "connector", rocker.mesh("switch"), rocker.label)
        metal = [base.mesh("switch", lambda f: f.solid != i_house)]
        metal += [l.mesh("hidden") for l in sw if l is not base and l is not rocker]
        self.add("switch.terminals", "body", "switch", "metal_contact", Mesh.concat(metal),
                 ", ".join(l.label for l in sw if l is not rocker))
        lo, hi = rocker.bounds("switch")
        self.checks["switch_rocker_bounds"] = [np.round(lo, 2).tolist(), np.round(hi, 2).tolist()]

    def _is_box(self, leaf: Leaf, solid_index: int) -> bool:
        faces = [f for f in leaf.proto.tessellate(*TESS["printed"]) if f.solid == solid_index]
        return len(faces) == 6 and all(f.stype == "Plane" for f in faces)

    # -- screen quad --------------------------------------------------------------
    def build_screen(self):
        """Active-area quad on the front face of the display window block, centred in
        the cover's window opening."""
        from OCP.BRep import BRep_Tool
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepTools import BRepTools
        from OCP.GeomAbs import GeomAbs_Plane
        from OCP.TopAbs import TopAbs_FACE, TopAbs_VERTEX, TopAbs_WIRE
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS

        leaf = self.cover_leaf
        faces = [f for f in leaf.proto.tessellate(*TESS["printed"]) if f.solid == self.glass_solid]
        best = None
        for f in faces:                                   # the face of the block looking most towards -X
            V = xf_points(leaf.matrix, f.V)
            n = unit(xf_dirs(leaf.matrix, f.N).sum(axis=0))
            if best is None or n[0] < best[0][0]:
                best = (n, V)
        normal, V = best
        offset = float((V @ normal).mean())
        right = unit(np.cross([0.0, 1.0, 0.0], normal))    # viewer's right when facing the screen
        if right[2] < 0:
            right = -right
        up = unit(np.cross(normal, right))
        glass_centre = (V.min(axis=0) + V.max(axis=0)) / 2.0

        # window opening: inner wire of the cover's outer face parallel to the glass
        window = None
        solid = leaf.proto.solids()[self.cover_solid][0]
        ex = TopExp_Explorer(solid, TopAbs_FACE)
        while ex.More():
            face = TopoDS.Face(ex.Current())
            ex.Next()
            a = BRepAdaptor_Surface(face)
            if a.GetType() != GeomAbs_Plane:
                continue
            outer = BRepTools.OuterWire_s(face)
            exw = TopExp_Explorer(face, TopAbs_WIRE)
            while exw.More():
                wire = exw.Current()
                exw.Next()
                if wire.IsSame(outer):
                    continue
                pts = []
                exv = TopExp_Explorer(wire, TopAbs_VERTEX)
                while exv.More():
                    p = BRep_Tool.Pnt_s(TopoDS.Vertex(exv.Current()))
                    pts.append(xf_points(leaf.matrix, [p.X(), p.Y(), p.Z()]))
                    exv.Next()
                pts = np.array(pts)
                if len(pts) < 4:
                    continue
                span = np.ptp(pts @ normal)
                d = float((pts @ normal).mean())
                w, h = np.ptp(pts @ right), np.ptp(pts @ up)
                # planar loop parallel to the glass, in front of it, big enough for the panel
                if span < 0.05 and 0.0 < d - offset < 3.0 and w >= SCREEN_W and h >= SCREEN_H:
                    c = np.array([(pts @ right).min() + w / 2, (pts @ up).min() + h / 2])
                    if window is None or d > window["d"]:
                        window = dict(d=d, w=float(w), h=float(h), c=c)
        if window is not None:
            cr, cu = window["c"]
            self.checks["screen_window"] = {"width": round(window["w"], 3), "height": round(window["h"], 3),
                                            "recess_mm": round(window["d"] - offset, 3)}
        else:
            self.note("cover window opening not found; screen centred on the glass block")
            cr, cu = float(glass_centre @ right), float(glass_centre @ up)
        centre = right * cr + up * cu + normal * (offset + SCREEN_PROUD)
        self.screen = dict(center=centre, right=right, up=up, normal=normal, width=SCREEN_W, height=SCREEN_H)
        self.log(f"screen: centre={np.round(centre, 3)} normal={np.round(normal, 4)} right={np.round(right, 4)} "
                 f"up={np.round(up, 4)}  window={self.checks.get('screen_window')}")

    # -- STL cover variants -----------------------------------------------------------
    def build_stl_covers(self):
        from scipy.spatial import cKDTree
        cover = next(p for p in self.parts if p.name == "top_cover")
        frame = next(p for p in self.parts if p.name == "frame")
        report = {}
        # 1) are the STL assets in the STEP's coordinate frame?  Compare the frame STL.
        aligned = True
        for stl, part in (("assets/stl/Internal-Frame-v121.stl", frame),
                          ("assets/stl/Bottom-Cover-v121.stl", next(p for p in self.parts if p.name == "bottom_cover"))):
            path = REPO_ROOT / stl
            if not path.exists():
                aligned = False
                continue
            tris = read_stl_triangles(path)
            sv = tris.reshape(-1, 3)
            d_bounds = max(np.abs(sv.min(axis=0) - part.mesh.V.min(axis=0)).max(),
                           np.abs(sv.max(axis=0) - part.mesh.V.max(axis=0)).max())
            dist, _ = cKDTree(self._surface_samples(part.mesh)).query(sv)
            report[Path(stl).name] = {"bounds_delta_mm": round(float(d_bounds), 3),
                                      "vertex_to_step_surface_p95_mm": round(float(np.percentile(dist, 95)), 3),
                                      "vertex_to_step_surface_max_mm": round(float(dist.max()), 3)}
            if np.percentile(dist, 95) > 0.5:
                aligned = False
        self.checks["stl_alignment"] = report
        self.cover_variants = ["step"]
        if not aligned:
            self.note("STL assets are not in the STEP frame; alternative top covers skipped")
            return
        cover_tree = cKDTree(self._surface_samples(cover.mesh))
        for variant, rel in STL_COVERS.items():
            path = REPO_ROOT / rel
            if not path.exists():
                self.note(f"cover STL missing: {rel}")
                continue
            tris = read_stl_triangles(path)
            mesh = smooth_stl_mesh(tris, self.cover_color, STL_SMOOTH_ANGLE_DEG)
            dist, _ = cover_tree.query(mesh.V)
            vol, com = mesh_volume_com(mesh)
            report[path.name] = {"bounds_min": np.round(mesh.V.min(axis=0), 2).tolist(),
                                 "bounds_max": np.round(mesh.V.max(axis=0), 2).tolist(),
                                 "vertices_within_0.1mm_of_step_cover": round(float((dist < 0.1).mean()), 3)}
            self.add("top_cover_" + variant.replace("-", "_"), "body", "shell", "pla_shell", mesh, rel,
                     cover_variant=variant, mass_g=vol * DENSITY_PLA_EFFECTIVE, com=com)
            self.cover_variants.append(variant)

    @staticmethod
    def _surface_samples(mesh: Mesh, spacing: float = 0.4) -> np.ndarray:
        """Vertices + extra points on large triangles, for nearest-surface queries."""
        a, b, c = mesh.V[mesh.T[:, 0]], mesh.V[mesh.T[:, 1]], mesh.V[mesh.T[:, 2]]
        edge = np.maximum.reduce([np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1),
                                  np.linalg.norm(a - c, axis=1)])
        out = [mesh.V]
        n = np.clip(np.ceil(edge / spacing).astype(int), 1, 200)
        for k in np.unique(n):
            if k <= 1:
                continue
            sel = n == k
            i, j = np.meshgrid(np.arange(k + 1), np.arange(k + 1), indexing="ij")
            m = (i + j) <= k
            w1, w2 = i[m] / k, j[m] / k
            w0 = 1 - w1 - w2
            out.append((w0[None, :, None] * a[sel][:, None] + w1[None, :, None] * b[sel][:, None]
                        + w2[None, :, None] * c[sel][:, None]).reshape(-1, 3))
        return np.concatenate(out)

    # -- mass ---------------------------------------------------------------------
    def default_parts(self) -> list[OutPart]:
        return [p for p in self.parts if p.cover_variant in (None, "step")]

    def finish_mass(self):
        links = {}
        for link in ("body",) + JOINT_ORDER:
            ps = [p for p in self.default_parts() if p.link == link and p.mass_g > 0]
            m = sum(p.mass_g for p in ps)
            com = sum(p.mass_g * p.com for p in ps) / m
            links[link] = (m, com)
        total = sum(m for m, _ in links.values())
        com = sum(m * c for m, c in links.values()) / total
        self.link_mass = links
        self.total_mass_g = total
        self.com = com
        self.log(f"mass estimate: {total:.1f} g, COM {np.round(com, 2)}")

    # -- collision points -----------------------------------------------------------
    def build_collision(self):
        """Collision points per link, <= COLLISION_LIMIT each, mirror-symmetric.

        The robot is left/right symmetric about the plane z = z_mid (midway between
        the L and R hip shafts) and its legs are front/rear symmetric about
        x = x_mid.  Independent point selection per link would break that symmetry
        (a physics artefact: one foot touches down before its twin), so:

        * body: symmetric about z_mid (points of the +Z half + their mirror images);
        * the rear-left leg (femur L2, foot L4) is the reference: hull vertices,
          mandatory features first (toe tip, sole corners, femur ends), then
          farthest-point sampling;
        * front-left = the reference mirrored about x_mid when that lands on the
          real front link (within COLLISION_TOL_MM), otherwise each mirrored point
          is moved onto the nearest point of the real front link's surface -- an
          equivalent contact set that stays on the actual mesh;
        * every right link = its left twin mirrored about z_mid, exactly.
        """
        j = self.joints
        z_mid = round((j["L1"].pivot[2] + j["R1"].pivot[2]) / 2.0, 4)
        x_mid = round((j["L1"].pivot[0] + j["L2"].pivot[0]) / 2.0, 4)
        for a, b in (("L2", "R2"), ("L3", "R3"), ("L4", "R4")):          # the joint table itself is symmetric
            assert abs((j[a].pivot[2] + j[b].pivot[2]) / 2.0 - z_mid) < 1e-3
        for a, b in (("R1", "R2"), ("L3", "L4"), ("R3", "R4")):
            assert abs((j[a].pivot[0] + j[b].pivot[0]) / 2.0 - x_mid) < 1e-3

        def parts_of(link):
            return [p for p in self.default_parts() if p.link == link and p.collide]

        surface = {link: Mesh.concat([p.mesh for p in parts_of(link)]) for link in ("body",) + JOINT_ORDER}

        def mirror_z(pts):
            return np.round(pts * [1.0, 1.0, -1.0] + [0.0, 0.0, 2.0 * z_mid], 4)

        def mirror_x(pts):
            return np.round(pts * [-1.0, 1.0, 1.0] + [2.0 * x_mid, 0.0, 0.0], 4)

        def leg_points(link):
            ps = parts_of(link)
            pts = np.concatenate([p.mesh.V for p in ps])
            mandatory: list[np.ndarray] = []
            if link in KNEE_PARENT:
                foot = next(p for p in ps if p.name == f"foot_{link}").mesh.V
                rel = foot - np.array(j[link].pivot)
                axis = np.array(j[link].axis)
                radial = rel - np.outer(rel @ axis, axis)
                toe_dir = unit(radial[int(np.argmax(np.linalg.norm(radial, axis=1)))])
                if abs(toe_dir[1]) > 0.9:                                       # bind pose: feet point straight
                    toe_dir = np.array([0.0, np.sign(toe_dir[1]), 0.0])         # down, the sole is horizontal
                mandatory.append(foot[int(np.argmax(foot @ toe_dir))])         # toe tip
                mandatory += slab_corners(foot, toe_dir, 0.05)                  # corners of the flat sole itself
                mandatory += slab_corners(foot, toe_dir, 1.5, 4)                # rounded edge around the sole
                mandatory += slab_corners(foot, toe_dir, 12.0, 4)               # lower shin outline
            else:
                femur = next(p for p in ps if p.name == f"femur_{link}").mesh.V
                d = self.joints_geo[link]["femur_dir"]
                mandatory += slab_corners(femur, d, 3.0)                        # outer end
                mandatory += slab_corners(femur, -d, 3.0, 4)                    # inner end (over the hip)
                mandatory.append(femur[int(np.argmax(femur[:, 1]))])
                mandatory.append(femur[int(np.argmin(femur[:, 1]))])
            return pick_collision_points(pts, mandatory)

        out: dict[str, np.ndarray] = {}
        how: dict[str, str] = {}

        # body
        ps = parts_of("body")
        pts = np.concatenate([p.mesh.V for p in ps])
        bottom = next(p for p in ps if p.name == "bottom_cover").mesh.V
        cover = next(p for p in ps if p.name == "top_cover").mesh.V
        mandatory = slab_corners(bottom, (0, -1, 0), 0.6)                      # underside corners
        mandatory += slab_corners(pts, (0, -1, 0), 8.0)                         # widest lower outline (frame)
        mandatory += slab_corners(cover, (0, 1, 0), 1.0, 4)                     # ear tips / top
        for d in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (-1, 1, 0), (1, 1, 0)):     # cover extremes (+Z most of ties)
            mandatory.append(cover[int(np.argmax(cover @ unit(d) + 1e-6 * cover[:, 2]))])
        out["body"] = pick_symmetric_collision_points(pts, mandatory, z_mid)
        how["body"] = "+Z half and its mirror image about z_mid"

        # legs: rear-left reference -> front-left -> right side
        for front, rear in (("L1", "L2"), ("L3", "L4")):
            out[rear] = leg_points(rear)
            how[rear] = "reference (hull vertices of the link)"
            cand = mirror_x(out[rear])
            dist, _, closest = surface_distance(cand, surface[front])
            if dist.max() <= COLLISION_TOL_MM:
                out[front] = cand
                how[front] = f"{rear} mirrored about x_mid (exact)"
            else:
                # The CAD places this link slightly off the mirror image of its twin.  Follow
                # it rigidly (so e.g. the sole corners stay coplanar), then put whatever is
                # still off the real surface onto its nearest point.
                image = mirror_x(surface[rear].V)
                real = surface[front].V
                shift = np.round(((real.min(axis=0) - image.min(axis=0)) + (real.max(axis=0) - image.max(axis=0))) / 2.0, 3)
                moved = cand + shift
                dist2, _, closest = surface_distance(moved, surface[front])
                out[front] = np.round(np.where(dist2[:, None] > 0.02, closest, moved), 3)
                how[front] = (f"{rear} mirrored about x_mid and shifted by {shift.tolist()} mm, the offset at which "
                              f"the CAD places the real {front} link (plain mirror image: up to {dist.max():.2f} mm "
                              f"off its surface; after the shift {dist2.max():.3f} mm, "
                              f"{int((dist2 > 0.02).sum())} points projected onto the surface)")
        for left in ("L1", "L2", "L3", "L4"):
            right = "R" + left[1]
            out[right] = mirror_z(out[left])
            how[right] = f"{left} mirrored about z_mid (exact)"

        self.collision = {link: out[link] for link in ("body",) + JOINT_ORDER}
        self.log(f"collision points: symmetric about z_mid = {z_mid}, legs about x_mid = {x_mid}")
        worst = 0.0
        for link, pts in self.collision.items():
            assert len(pts) <= COLLISION_LIMIT and len(np.unique(np.round(pts, 2), axis=0)) == len(pts), link
            dist, outside, _ = surface_distance(pts, surface[link])
            off = float(dist[outside].max()) if outside.any() else 0.0
            worst = max(worst, float(dist.max()))
            self.log(f"  {link:4s} {len(pts):2d} pts, max distance to the link's real surface {dist.max():.3f} mm "
                     f"(max outside it {off:.3f} mm) -- {how[link]}")
            if dist.max() > COLLISION_TOL_MM:
                self.note(f"collision points of {link} are up to {dist.max():.2f} mm off its surface")
        self.log(f"  worst deviation from the real surfaces: {worst:.3f} mm (tolerance {COLLISION_TOL_MM} mm)")

    # -- validation -----------------------------------------------------------------
    def validate(self):
        names = [p.name for p in self.parts]
        assert len(set(names)) == len(names)
        for p in self.parts:
            m = p.mesh
            assert m.T.min() >= 0 and m.T.max() < len(m.V), p.name
            ln = np.linalg.norm(m.N, axis=1)
            assert np.all(np.abs(ln - 1.0) < 1e-3), (p.name, ln.min(), ln.max())
            g = np.cross(m.V[m.T[:, 1]] - m.V[m.T[:, 0]], m.V[m.T[:, 2]] - m.V[m.T[:, 0]])
            agree = float(((m.N[m.T].sum(axis=1) * g).sum(axis=1) > 0).mean())
            if agree < 0.97:
                self.note(f"{p.name}: only {agree:.1%} of the triangles agree with their vertex normals")
            assert p.material in MATERIALS and p.link in ("body",) + JOINT_ORDER
        # behavioural checks of the joint table (also covered by tests/test_robot_model.py)
        xf = link_transforms(self.joints, dict(BIND_ANGLES))
        for m in xf.values():
            assert np.allclose(m, np.eye(4), atol=1e-12)
        flipped = sum(p.flipped_normals for p in self.cad.protos.values())
        self.checks["faces_with_inverted_surface_normals_fixed"] = int(flipped)

    # -- output ---------------------------------------------------------------------
    def write(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        pos, nrm, col, idx = [], [], [], []
        table = []
        v_off = t_off = 0
        for p in self.parts:
            m = p.mesh
            V32 = m.V.astype(np.float32)
            pos.append(V32)
            nrm.append(np.round(m.N * 32767.0).astype(np.int16))
            col.append(m.C.astype(np.uint8))
            idx.append(m.T.astype(np.uint32))
            entry = {
                "name": p.name, "link": p.link, "group": p.group, "material": p.material,
                "cover_variant": p.cover_variant,
                "vertex_offset": v_off, "vertex_count": len(V32),
                "triangle_offset": t_off, "triangle_count": len(m.T),
                "bounds_min": [round(float(x), 4) for x in V32.min(axis=0)],
                "bounds_max": [round(float(x), 4) for x in V32.max(axis=0)],
                "mass_g": round(float(p.mass_g), 3),
                "source": p.source,
            }
            if p.note:
                entry["note"] = p.note
            table.append(entry)
            v_off += len(V32)
            t_off += len(m.T)
        # Both files are written under temporary names and then moved into place, so a
        # running app that loads the model never sees a half-written asset.
        tmp_npz = self.out_dir / f"meshes.tmp-{os.getpid()}.npz"
        tmp_manifest = self.out_dir / f"manifest.tmp-{os.getpid()}.json"
        np.savez_compressed(tmp_npz,
                            positions=np.concatenate(pos), normals=np.concatenate(nrm),
                            colors=np.concatenate(col), indices=np.concatenate(idx))

        default = self.default_parts()
        lo = np.min([p.mesh.V.min(axis=0) for p in default], axis=0)
        hi = np.max([p.mesh.V.max(axis=0) for p in default], axis=0)
        r4 = lambda a: [round(float(x), 4) + 0.0 for x in a]    # noqa: E731  (+ 0.0: no "-0.0")
        r6 = lambda a: [round(float(x), 6) + 0.0 for x in a]    # noqa: E731
        import OCP
        manifest = {
            "format": "sesame-studio-robot-model",
            "version": 1,
            "generator": {
                "tool": "tools/build_model.py",
                "source": self.step_path.name,
                "source_bytes": self.step_path.stat().st_size,
                "source_sha256": hashlib.sha256(self.step_path.read_bytes()).hexdigest(),
                "ocp_version": getattr(OCP, "__version__", "?"),
                "tessellation": {k: {"linear_mm": v[0], "angular_rad": v[1]} for k, v in TESS.items()},
                "regenerate": "python -X utf8 tools/build_model.py <path-to>/Sesame-ESP32.step --out assets/model",
            },
            "conventions": {
                "units": "mm",
                "frame": "CAD assembly frame: +Y up, robot front (OLED) faces -X, left = +Z, right = -Z",
                "bind_pose": "pose of the STEP export = firmware STAND pose; all geometry is stored in it",
                "kinematics": "app/kinematics.py: a joint rotates its link about the line through `pivot` "
                              "along `axis` by sign * (angle - bind_angle) degrees (right-handed)",
                "links": "'body' + one link per joint: R1/R2/L1/L2 = femurs (hip yaw), "
                         "R3/L3/R4/L4 = feet (knee pitch; R3 on R1, L3 on L1, R4 on R2, L4 on L2)",
            },
            "bind_angles": {k: BIND_ANGLES[k] for k in JOINT_ORDER},
            "joints": [
                {"name": j.name, "parent": j.parent, "pivot": r4(j.pivot), "axis": r6(j.axis),
                 "sign": j.sign, "bind_angle": j.bind_angle,
                 "derived_from": self.joints_geo[j.name]["shaft_screw"].label,
                 "servo_shaft_offset_mm": round(self.joints_geo[j.name]["shaft_offset_mm"], 3)}
                for j in (self.joints[n] for n in JOINT_ORDER)
            ],
            "materials": MATERIALS,
            "cover_variants": self.cover_variants,
            "default_cover": "step",
            "screen": {
                "center": r4(self.screen["center"]), "right": r6(self.screen["right"]),
                "up": r6(self.screen["up"]), "normal": r6(self.screen["normal"]),
                "width": SCREEN_W, "height": SCREEN_H,
                "note": f"active 128x64 area of the 0.96in OLED, {SCREEN_PROUD} mm proud of the display window "
                        "block ('screen_glass'), centred in the cover's window opening; right = increasing "
                        "pixel x for a viewer facing the robot, up = towards pixel row 0",
            },
            "bounds_min": r4(lo), "bounds_max": r4(hi),
            "mass": {
                "total_kg": round(self.total_mass_g / 1000.0, 4),
                "com": r4(self.com),
                "links": {k: {"mass_kg": round(m / 1000.0, 5), "com": r4(c)} for k, (m, c) in self.link_mass.items()},
                "method": "Rough estimate, default cover. Printed parts: exact B-rep volume x "
                          f"{DENSITY_PLA_EFFECTIVE * 1000:.2f} g/cm3 (PLA 1.24 x ~0.7 fill). Servo bodies: "
                          f"{MASS_SERVO_BODY_G} g each (SG90 = 9 g with horn and screws); horns: volume x POM "
                          f"1.41; screws: volume x steel 7.9. Fixed guesses: battery {MASS_BATTERY_G:.0f} g "
                          f"(2x 14500), distro PCB incl. unmodelled MCU board and wiring {MASS_DISTRO_G:.0f} g, "
                          f"OLED module {MASS_OLED_G:.0f} g, switch {MASS_SWITCH_G:.0f} g. COMs are B-rep centres "
                          "of mass in the bind pose.",
            },
            "collision_points": {k: [r4(p) for p in v] for k, v in self.collision.items()},
            "buffers": {
                "file": "meshes.npz",
                "positions": "float32 (V,3) mm", "normals": "int16 (V,3), unit vector * 32767",
                "colors": "uint8 (V,3) sRGB", "indices": "uint32 (T,3), relative to the part's vertex_offset",
                "vertex_count": v_off, "triangle_count": t_off,
            },
            "parts": table,
            "dropped": self.dropped,
            "notes": self.notes,
            "checks": self.checks,
        }
        with open(tmp_manifest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(dump_json(manifest))
        changed = [name for tmp, name in ((tmp_npz, "meshes.npz"), (tmp_manifest, "manifest.json"))
                   if replace_atomically(tmp, self.out_dir / name)]
        self.log("updated: " + (", ".join(changed) if changed else "nothing (output identical)"))
        self.manifest = manifest
        sizes = {f.name: f.stat().st_size for f in sorted(self.out_dir.iterdir())}
        default_tris = sum(p.mesh.tri_count for p in default)
        self.log(f"parts: {len(self.parts)}  vertices: {v_off}  triangles: {t_off} "
                 f"(default cover only: {default_tris})")
        self.log("files:", ", ".join(f"{k} {v / 1024:.0f} KiB" for k, v in sizes.items()),
                 f"= {sum(sizes.values()) / 1e6:.2f} MB")


def replace_atomically(tmp: Path, dst: Path) -> bool:
    """Move ``tmp`` over ``dst`` (atomic: readers see the old or the new file, never a
    partial one).  ``dst`` is left untouched when the content is identical.  Returns
    True when ``dst`` changed."""
    if dst.exists() and dst.stat().st_size == tmp.stat().st_size and dst.read_bytes() == tmp.read_bytes():
        tmp.unlink()
        return False
    for attempt in range(100):
        try:
            os.replace(tmp, dst)
            return True
        except PermissionError:          # Windows: a reader has the file open right now
            if attempt == 99:
                raise
            time.sleep(0.1)
    return True


def dump_json(obj, indent: int = 2) -> str:
    """json.dumps with indentation, but short lists of scalars kept on one line."""
    def scalar_list(x):
        return isinstance(x, (list, tuple)) and all(not isinstance(v, (list, tuple, dict)) for v in x)

    def flat_dict(x):
        return isinstance(x, dict) and all(not isinstance(v, (list, tuple, dict)) for v in x.values()) and len(x) <= 5

    def enc(x, level):
        pad, pad1 = " " * (indent * level), " " * (indent * (level + 1))
        if isinstance(x, dict):
            if not x:
                return "{}"
            if flat_dict(x) and level > 1:
                return "{" + ", ".join(f"{json.dumps(k)}: {json.dumps(v, ensure_ascii=True)}" for k, v in x.items()) + "}"
            return "{\n" + ",\n".join(f"{pad1}{json.dumps(k)}: {enc(v, level + 1)}" for k, v in x.items()) + f"\n{pad}}}"
        if isinstance(x, (list, tuple)):
            if scalar_list(x):
                return json.dumps(list(x), ensure_ascii=True)
            return "[\n" + ",\n".join(pad1 + enc(v, level + 1) for v in x) + f"\n{pad}]"
        return json.dumps(x, ensure_ascii=True)

    return enc(obj, 0) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("step", type=Path, help="path to Sesame-ESP32.step")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "assets" / "model", help="output directory")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)
    if not args.step.is_file():
        ap.error(f"no such file: {args.step}")
    Builder(args.step.resolve(), args.out, verbose=not args.quiet).run()


if __name__ == "__main__":
    main()
