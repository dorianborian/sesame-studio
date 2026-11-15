"""Shared robot kinematics (pure numpy, no Qt / no OpenGL).

This is the single source of truth for how servo angles become link
transforms. The renderer (viewport3d), the physics sim (physics.py) and the
model loader (robot_model.py) all use it, so a pose always means the same
thing everywhere.

Conventions
-----------
* Units are millimetres. +Y is up, the robot's front (OLED face) looks down
  -X, its left side is +Z and its right side is -Z.
* All geometry is authored in the **bind pose**: the pose the CAD assembly
  (Sesame-ESP32.step) was exported in. ``JointDef.bind_angle`` is the servo
  angle (degrees, 0-180) each joint has in that pose.
* A joint rotates its link (and everything mounted on it) about the line
  through ``pivot`` along ``axis`` -- both expressed in bind-pose body
  coordinates -- by ``sign * (angle - bind_angle)`` degrees (right-handed).
* Link names are ``"body"`` plus one link per joint, named after the joint
  (``"R1"`` is the femur moved by servo R1, ``"R3"`` the foot moved by R3...).
* ``link_transforms()`` returns, per link, the 4x4 matrix that maps
  bind-pose body coordinates to posed body coordinates. Multiply by the body's
  world (root) transform to get world space.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

BODY_LINK = "body"


@dataclass(frozen=True)
class JointDef:
    name: str                              # servo name, e.g. "R1"
    parent: str | None                     # parent joint name, or None if mounted on the body
    pivot: tuple[float, float, float]      # point on the rotation axis (bind pose, body frame, mm)
    axis: tuple[float, float, float]       # unit rotation axis (bind pose, body frame)
    sign: float = 1.0                      # +1 / -1: servo direction relative to +axis
    bind_angle: float = 90.0               # servo angle (deg) in the bind pose

    def rotation_deg(self, angle: float) -> float:
        """Signed rotation about ``axis`` for a given servo angle."""
        return self.sign * (float(angle) - self.bind_angle)

    def angle_from_rotation(self, rotation_deg: float) -> float:
        """Inverse of rotation_deg()."""
        return self.bind_angle + rotation_deg / self.sign


def rotation_about_line(pivot, axis, degrees: float) -> np.ndarray:
    """4x4 rotation by ``degrees`` about the line through ``pivot`` along the
    unit vector ``axis`` (Rodrigues)."""
    x, y, z = (float(axis[0]), float(axis[1]), float(axis[2]))
    t = math.radians(degrees)
    c, s = math.cos(t), math.sin(t)
    k = 1.0 - c
    r = np.array([
        [c + x * x * k,     x * y * k - z * s, x * z * k + y * s],
        [y * x * k + z * s, c + y * y * k,     y * z * k - x * s],
        [z * x * k - y * s, z * y * k + x * s, c + z * z * k],
    ], dtype=np.float64)
    p = np.asarray(pivot, dtype=np.float64)
    m = np.eye(4, dtype=np.float64)
    m[:3, :3] = r
    m[:3, 3] = p - r @ p
    return m


def joint_order(joints: dict[str, JointDef]) -> list[str]:
    """Joint names ordered so every parent precedes its children."""
    ordered: list[str] = []
    pending = dict(joints)
    while pending:
        progressed = False
        for name in list(pending):
            parent = pending[name].parent
            if parent is None or parent in ordered:
                ordered.append(name)
                del pending[name]
                progressed = True
        if not progressed:
            raise ValueError(f"Joint hierarchy has a cycle or missing parent: {sorted(pending)}")
    return ordered


def link_transforms(joints: dict[str, JointDef], angles: dict[str, float]) -> dict[str, np.ndarray]:
    """Forward kinematics: {link name: 4x4 bind->posed matrix, body frame}.

    Missing angles fall back to the joint's bind angle. The result always
    contains ``"body"`` (identity)."""
    out: dict[str, np.ndarray] = {BODY_LINK: np.eye(4, dtype=np.float64)}
    for name in joint_order(joints):
        j = joints[name]
        local = rotation_about_line(j.pivot, j.axis, j.rotation_deg(angles.get(name, j.bind_angle)))
        parent = out[j.parent] if j.parent else out[BODY_LINK]
        out[name] = parent @ local
    return out


def transform_points(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Apply a 4x4 matrix to an (N, 3) array of points."""
    pts = np.asarray(points, dtype=np.float64)
    return pts @ matrix[:3, :3].T + matrix[:3, 3]


def posed_joint_frame(joints: dict[str, JointDef], angles: dict[str, float], name: str):
    """(pivot, axis) of joint ``name`` in the *posed* body frame -- i.e. where
    the hinge actually is right now, accounting for its parents' rotation."""
    j = joints[name]
    xf = link_transforms(joints, angles)
    parent = xf[j.parent] if j.parent else xf[BODY_LINK]
    pivot = transform_points(parent, np.asarray([j.pivot]))[0]
    axis = parent[:3, :3] @ np.asarray(j.axis, dtype=np.float64)
    return pivot, axis / (np.linalg.norm(axis) or 1.0)
