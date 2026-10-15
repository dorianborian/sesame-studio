"""Minimal STL loader (binary + ASCII) -> flat numpy vertex/normal arrays.

Kept intentionally small (no numpy-stl dependency) since we only need
triangle soups for OpenGL immediate rendering, not a full mesh toolkit.
"""

from __future__ import annotations

import struct

import numpy as np


class Mesh:
    __slots__ = ("vertices", "normals", "bounds_min", "bounds_max", "center")

    def __init__(self, vertices: np.ndarray, normals: np.ndarray):
        self.vertices = vertices  # (N*3, 3) float32
        self.normals = normals    # (N*3, 3) float32
        if len(vertices):
            self.bounds_min = vertices.min(axis=0)
            self.bounds_max = vertices.max(axis=0)
        else:
            self.bounds_min = np.zeros(3, dtype=np.float32)
            self.bounds_max = np.zeros(3, dtype=np.float32)
        self.center = (self.bounds_min + self.bounds_max) / 2.0


def load_stl(path: str) -> Mesh:
    with open(path, "rb") as fh:
        header = fh.read(80)
        rest = fh.read()

    # Binary STL: 80-byte header, uint32 triangle count, then 50 bytes/tri.
    if len(rest) >= 4:
        (tri_count,) = struct.unpack("<I", rest[:4])
        expected_len = 4 + tri_count * 50
        if expected_len == len(rest) and tri_count > 0:
            return _parse_binary(rest, tri_count)

    # Fall back to ASCII STL.
    text = (header + rest).decode("ascii", errors="ignore")
    if "facet normal" in text:
        return _parse_ascii(text)

    return Mesh(np.zeros((0, 3), dtype=np.float32), np.zeros((0, 3), dtype=np.float32))


def _parse_binary(data: bytes, tri_count: int) -> Mesh:
    verts = np.empty((tri_count * 3, 3), dtype=np.float32)
    norms = np.empty((tri_count * 3, 3), dtype=np.float32)
    offset = 4
    for i in range(tri_count):
        nx, ny, nz, v1x, v1y, v1z, v2x, v2y, v2z, v3x, v3y, v3z = struct.unpack_from(
            "<12f", data, offset
        )
        offset += 50  # 12 floats (48 bytes) + 2-byte attribute count
        base = i * 3
        verts[base] = (v1x, v1y, v1z)
        verts[base + 1] = (v2x, v2y, v2z)
        verts[base + 2] = (v3x, v3y, v3z)
        norms[base] = norms[base + 1] = norms[base + 2] = (nx, ny, nz)
    return Mesh(verts, norms)


def _parse_ascii(text: str) -> Mesh:
    verts_list: list[tuple[float, float, float]] = []
    norms_list: list[tuple[float, float, float]] = []
    normal = (0.0, 0.0, 1.0)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("facet normal"):
            parts = line.split()[2:5]
            normal = tuple(float(p) for p in parts)
        elif line.startswith("vertex"):
            parts = line.split()[1:4]
            verts_list.append(tuple(float(p) for p in parts))
            norms_list.append(normal)
    if not verts_list:
        return Mesh(np.zeros((0, 3), dtype=np.float32), np.zeros((0, 3), dtype=np.float32))
    return Mesh(
        np.array(verts_list, dtype=np.float32),
        np.array(norms_list, dtype=np.float32),
    )
