"""Small camera / projection / ray helpers for the 3D viewport (pure numpy).

Conventions: matrices are 4x4 float64, row-major, column vectors
(``clip = proj @ view @ world``), i.e. exactly what ``app.kinematics``
produces. Upload to GL with ``transpose=GL_TRUE``.
World: millimetres, +Y up. Window coordinates are Qt logical pixels with the
origin at the TOP-left.
"""

from __future__ import annotations

import math

import numpy as np

__all__ = [
    "perspective", "ortho", "look_at", "orbit_eye", "normalize", "project_points",
    "project_point", "screen_ray", "ray_plane", "signed_angle_about", "rotate_about_axis",
    "wrap_degrees", "shortest_angle_delta",
]


def normalize(v) -> np.ndarray:
    a = np.asarray(v, dtype=np.float64)
    n = float(np.linalg.norm(a))
    return a / n if n > 1e-12 else a


def perspective(fovy_deg: float, aspect: float, near: float, far: float) -> np.ndarray:
    f = 1.0 / math.tan(math.radians(fovy_deg) * 0.5)
    m = np.zeros((4, 4), dtype=np.float64)
    m[0, 0] = f / max(aspect, 1e-6)
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = 2.0 * far * near / (near - far)
    m[3, 2] = -1.0
    return m


def ortho(left: float, right: float, bottom: float, top: float, near: float, far: float) -> np.ndarray:
    m = np.eye(4, dtype=np.float64)
    m[0, 0] = 2.0 / (right - left)
    m[1, 1] = 2.0 / (top - bottom)
    m[2, 2] = -2.0 / (far - near)
    m[0, 3] = -(right + left) / (right - left)
    m[1, 3] = -(top + bottom) / (top - bottom)
    m[2, 3] = -(far + near) / (far - near)
    return m


def look_at(eye, target, up=(0.0, 1.0, 0.0)) -> np.ndarray:
    eye = np.asarray(eye, dtype=np.float64)
    f = normalize(np.asarray(target, dtype=np.float64) - eye)
    upv = np.asarray(up, dtype=np.float64)
    s = np.cross(f, upv)
    if float(np.linalg.norm(s)) < 1e-6:           # looking straight up/down
        s = np.cross(f, np.array([0.0, 0.0, 1.0]))
    s = normalize(s)
    u = np.cross(s, f)
    m = np.eye(4, dtype=np.float64)
    m[0, :3] = s
    m[1, :3] = u
    m[2, :3] = -f
    m[:3, 3] = -(m[:3, :3] @ eye)
    return m


def orbit_eye(target, yaw_deg: float, pitch_deg: float, distance: float) -> np.ndarray:
    """eye = target + distance * (cos(pitch)cos(yaw), sin(pitch), cos(pitch)sin(yaw))."""
    yaw, pitch = math.radians(yaw_deg), math.radians(pitch_deg)
    cp = math.cos(pitch)
    return np.asarray(target, dtype=np.float64) + distance * np.array(
        [cp * math.cos(yaw), math.sin(pitch), cp * math.sin(yaw)], dtype=np.float64)


def project_points(view_proj: np.ndarray, points, width: float, height: float):
    """World points (N,3) -> (xy (N,2) in top-left pixel coords, w (N,)).
    ``w <= 0`` means behind the camera (xy is then meaningless)."""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    clip = pts @ view_proj[:, :3].T + view_proj[:, 3]
    w = clip[:, 3]
    safe = np.where(np.abs(w) < 1e-9, 1e-9, w)
    ndc = clip[:, :2] / safe[:, None]
    xy = np.empty_like(ndc)
    xy[:, 0] = (ndc[:, 0] * 0.5 + 0.5) * width
    xy[:, 1] = (1.0 - (ndc[:, 1] * 0.5 + 0.5)) * height
    return xy, w


def project_point(view_proj: np.ndarray, point, width: float, height: float):
    """Single world point -> (x, y) in pixels, or None when behind the camera."""
    xy, w = project_points(view_proj, [point], width, height)
    if w[0] <= 1e-6:
        return None
    return float(xy[0, 0]), float(xy[0, 1])


def screen_ray(inv_view_proj: np.ndarray, x: float, y: float, width: float, height: float):
    """(origin, unit direction) of the world ray through pixel (x, y)."""
    nx = 2.0 * x / max(width, 1.0) - 1.0
    ny = 1.0 - 2.0 * y / max(height, 1.0)
    near = inv_view_proj @ np.array([nx, ny, -1.0, 1.0])
    far = inv_view_proj @ np.array([nx, ny, 1.0, 1.0])
    near = near[:3] / near[3]
    far = far[:3] / far[3]
    return near, normalize(far - near)


def ray_plane(origin, direction, plane_point, plane_normal):
    """Intersection of a ray with a plane, or None if (nearly) parallel or
    behind the ray origin."""
    denom = float(np.dot(direction, plane_normal))
    if abs(denom) < 1e-9:
        return None
    t = float(np.dot(np.asarray(plane_point) - origin, plane_normal)) / denom
    if t <= 0.0:
        return None
    return origin + direction * t


def signed_angle_about(axis, a, b) -> float:
    """Signed angle (degrees, right-handed about ``axis``) from vector ``a``
    to vector ``b`` after projecting both onto the plane normal to ``axis``."""
    axis = np.asarray(axis, dtype=np.float64)
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    a = a - axis * float(np.dot(a, axis))
    b = b - axis * float(np.dot(b, axis))
    return math.degrees(math.atan2(float(np.dot(axis, np.cross(a, b))), float(np.dot(a, b))))


def rotate_about_axis(v, axis, degrees: float) -> np.ndarray:
    """Rodrigues rotation of direction ``v`` about unit ``axis``."""
    v = np.asarray(v, dtype=np.float64)
    k = np.asarray(axis, dtype=np.float64)
    t = math.radians(degrees)
    c, s = math.cos(t), math.sin(t)
    return v * c + np.cross(k, v) * s + k * float(np.dot(k, v)) * (1.0 - c)


def wrap_degrees(a: float) -> float:
    """Wrap to (-180, 180]."""
    a = (a + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


def shortest_angle_delta(a_from: float, a_to: float) -> float:
    return wrap_degrees(a_to - a_from)
