"""Tests for the CAD-derived robot model (assets/model + app/robot_model.py)."""

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

STUDIO_DIR = Path(__file__).resolve().parent.parent
if str(STUDIO_DIR) not in sys.path:
    sys.path.insert(0, str(STUDIO_DIR))

from app.kinematics import (  # noqa: E402
    BODY_LINK, JointDef, link_transforms, posed_joint_frame, transform_points,
)
from app.robot_model import GROUPS, Part, RobotModel  # noqa: E402

HIPS = ("R1", "R2", "L1", "L2")
KNEE_PARENT = {"R3": "R1", "L3": "L1", "R4": "R2", "L4": "L2"}
ALL_JOINTS = HIPS + tuple(KNEE_PARENT)
STAND = {"R1": 135, "R2": 45, "L1": 45, "L2": 135, "R4": 0, "R3": 180, "L3": 0, "L4": 180}
REST = {name: 90 for name in ALL_JOINTS}
#: how far (mm) a collision point may sit from the real surface of its link
COLLISION_TOLERANCE_MM = 0.3

# The joint table agreed with the renderer / physics code (mm, body frame).
EXPECTED_JOINTS = {
    "R1": ((1.5, None, -23.0), (0, 1, 0), +1),
    "R2": ((49.9, None, -23.0), (0, 1, 0), +1),
    "L1": ((1.5, None, 27.8), (0, 1, 0), +1),
    "L2": ((49.9, None, 27.8), (0, 1, 0), +1),
    "R3": ((-15.5, 15.2, -58.1), (0.7071, 0, -0.7071), -1),
    "L3": ((-15.5, 15.2, 62.9), (0.7071, 0, 0.7071), -1),
    "R4": ((66.9, 15.2, -58.1), (0.7071, 0, 0.7071), +1),
    "L4": ((66.9, 15.2, 62.9), (0.7071, 0, -0.7071), +1),
}


@pytest.fixture(scope="module")
def model() -> RobotModel:
    return RobotModel.load()


def side_sign(name: str) -> float:
    """+1 for the robot's left (+Z), -1 for its right (-Z)."""
    return 1.0 if name.startswith("L") else -1.0


def femur_direction(model: RobotModel, angles: dict, hip: str) -> np.ndarray:
    """Horizontal unit vector from the hip shaft towards the knee hinge (the
    offset along the knee shaft removed)."""
    knee = next(k for k, h in KNEE_PARENT.items() if h == hip)
    hip_pivot, _ = posed_joint_frame(model.joints, angles, hip)
    knee_pivot, knee_axis = posed_joint_frame(model.joints, angles, knee)
    v = knee_pivot - hip_pivot
    v = v - (v @ knee_axis) * knee_axis
    v[1] = 0.0
    return v / np.linalg.norm(v)


def foot_direction(model: RobotModel, angles: dict, knee: str) -> np.ndarray:
    """Unit vector from the knee hinge to the toe tip of the posed foot."""
    j = model.joints[knee]
    foot = model.part(f"foot_{knee}")
    pivot, axis = np.asarray(j.pivot), np.asarray(j.axis)
    rel = foot.vertices.astype(np.float64) - pivot
    radial = rel - np.outer(rel @ axis, axis)
    tip = radial[np.argmax(np.linalg.norm(radial, axis=1))]          # bind pose, relative to the hinge
    xf = link_transforms(model.joints, angles)[knee]
    v = xf[:3, :3] @ tip
    return v / np.linalg.norm(v)


# --------------------------------------------------------------------------- loading

def test_model_loads_and_is_cached(model):
    assert isinstance(model, RobotModel)
    assert RobotModel.load() is model
    assert len(model.parts) > 40
    names = [p.name for p in model.parts]
    assert len(set(names)) == len(names), "part names must be unique"
    assert model.bind_angles == {k: float(v) for k, v in STAND.items()}
    assert model.default_cover in model.cover_variants
    assert model.links == [BODY_LINK] + list(model.joints)


def test_loader_is_pure_numpy():
    code = ("import sys; from app.robot_model import RobotModel; m = RobotModel.load(); "
            "bad = [n for n in ('PySide6', 'OpenGL', 'OCP', 'scipy', 'trimesh') if n in sys.modules]; "
            "assert not bad, bad; print(len(m.parts))")
    out = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=str(STUDIO_DIR),
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert int(out.stdout.strip()) > 40


def test_triangle_budget(model):
    total = sum(p.triangle_count for p in model.parts)
    assert 50_000 < total <= 350_000


# --------------------------------------------------------------------------- joints

def test_eight_joints_with_right_parents(model):
    assert set(model.joints) == set(ALL_JOINTS)
    for name in HIPS:
        assert model.joints[name].parent is None
    for name, parent in KNEE_PARENT.items():
        assert model.joints[name].parent == parent
    for name, j in model.joints.items():
        assert isinstance(j, JointDef) and j.name == name
        assert j.bind_angle == STAND[name]
        assert abs(np.linalg.norm(j.axis) - 1.0) < 1e-5
        assert j.sign in (1.0, -1.0)


def test_joint_table_matches_contract(model):
    for name, (pivot, axis, sign) in EXPECTED_JOINTS.items():
        j = model.joints[name]
        for got, want in zip(j.pivot, pivot):
            if want is not None:
                assert abs(got - want) < 0.1, (name, j.pivot)
        assert np.allclose(j.axis, axis, atol=1e-3), (name, j.axis)
        assert j.sign == sign, name
    # hip axes are vertical, knee axes horizontal and perpendicular to their femur
    for knee, hip in KNEE_PARENT.items():
        assert abs(model.joints[knee].axis[1]) < 1e-6
        d = femur_direction(model, dict(model.bind_angles), hip)
        assert abs(d @ np.asarray(model.joints[knee].axis)) < 1e-6


def test_bind_pose_is_identity(model):
    for angles in (dict(model.bind_angles), {}, STAND):
        xf = link_transforms(model.joints, angles)
        assert set(xf) == {BODY_LINK, *ALL_JOINTS}
        for name, m in xf.items():
            assert np.allclose(m, np.eye(4), atol=1e-12), name


def test_stand_pose_geometry(model):
    """In the bind/stand pose the femurs are splayed 45 degrees to the corners
    and the feet point straight down."""
    s = np.sqrt(0.5)
    for hip in HIPS:
        d = femur_direction(model, STAND, hip)
        front = -1.0 if hip.endswith("1") else 1.0          # leg 1 = front = -X
        assert np.allclose(d, [front * s, 0.0, side_sign(hip) * s], atol=1e-3), (hip, d)
    for knee in KNEE_PARENT:
        d = foot_direction(model, STAND, knee)
        assert d[1] < -0.98, (knee, d)


def test_rest_pose_all_90(model):
    """All servos at 90: femurs straight sideways, feet horizontal pointing outward."""
    xf = link_transforms(model.joints, REST)
    for hip in HIPS:
        d = femur_direction(model, REST, hip)
        assert np.allclose(d, [0.0, 0.0, side_sign(hip)], atol=1e-3), (hip, d)
    for knee in KNEE_PARENT:
        # exact: the direction that was "straight down" in the bind pose now points outward
        down_now = xf[knee][:3, :3] @ np.array([0.0, -1.0, 0.0])
        assert np.allclose(down_now, [0.0, 0.0, side_sign(knee)], atol=1e-3), (knee, down_now)
        d = foot_direction(model, REST, knee)          # hinge -> toe corner: a few degrees off the centre line
        assert abs(d[1]) < 0.15 and d[2] * side_sign(knee) > 0.98, (knee, d)
    # flat on its belly: nothing reaches below the bottom cover
    bottom = float(model.part("bottom_cover").bounds_min[1])
    for knee in KNEE_PARENT:
        foot = transform_points(xf[knee], model.part(f"foot_{knee}").vertices)
        assert foot[:, 1].min() > bottom


def test_hip_directions(model):
    """R1 above 90 swings the front-right femur forward (-X); R2 below 90 swings
    the rear-right femur rearward (+X); the left side mirrors that."""
    def swing(name, angle):
        return femur_direction(model, {**REST, name: angle}, name)
    assert swing("R1", 135)[0] < -0.5 and swing("R1", 180)[0] < -0.99
    assert swing("R2", 45)[0] > 0.5 and swing("R2", 0)[0] > 0.99
    assert swing("L1", 45)[0] < -0.5 and swing("L1", 0)[0] < -0.99
    assert swing("L2", 135)[0] > 0.5 and swing("L2", 180)[0] > 0.99
    for name in HIPS:                      # hips only yaw
        assert abs(swing(name, 30)[1]) < 1e-9


def test_hinges_stay_on_their_axes(model):
    """A link and its parent agree on where the hinge line is, in any pose."""
    angles = {"R1": 180, "R2": 20, "L1": 70, "L2": 110, "R3": 40, "L3": 90, "R4": 90, "L4": 150}
    xf = link_transforms(model.joints, angles)
    for name, j in model.joints.items():
        parent = xf[j.parent] if j.parent else xf[BODY_LINK]
        on_axis = np.asarray(j.pivot) + np.outer([-10.0, 0.0, 10.0], j.axis)
        assert np.allclose(transform_points(xf[name], on_axis), transform_points(parent, on_axis), atol=1e-9)
        m = xf[name]
        assert np.allclose(m[:3, :3] @ m[:3, :3].T, np.eye(3), atol=1e-9)
        assert abs(np.linalg.det(m[:3, :3]) - 1.0) < 1e-9


# --------------------------------------------------------------------------- parts

def test_parts_are_valid(model):
    links = set(model.links)
    for p in model.parts:
        assert isinstance(p, Part)
        assert p.link in links, p.name
        assert p.group in GROUPS, (p.name, p.group)
        assert p.material in model.materials, (p.name, p.material)
        n = p.vertices.shape[0]
        assert n >= 3 and p.vertices.shape == (n, 3) and p.vertices.dtype == np.float32
        assert p.normals.shape == (n, 3) and p.normals.dtype == np.float32
        assert p.colors.shape == (n, 3) and p.colors.dtype == np.float32
        assert p.indices.ndim == 2 and p.indices.shape[1] == 3 and p.indices.dtype == np.uint32
        assert p.indices.shape[0] >= 1
        assert int(p.indices.max()) < n, p.name
        assert np.isfinite(p.vertices).all() and np.isfinite(p.normals).all()
        assert np.allclose(np.linalg.norm(p.normals, axis=1), 1.0, atol=1e-3), p.name
        assert p.colors.min() >= 0.0 and p.colors.max() <= 1.0
        assert np.allclose(p.bounds_min, p.vertices.min(axis=0), atol=1e-3), p.name
        assert np.allclose(p.bounds_max, p.vertices.max(axis=0), atol=1e-3), p.name
        # no degenerate index triples, and the winding agrees with the vertex normals
        t = p.indices.astype(np.int64)
        assert (t[:, 0] != t[:, 1]).all() and (t[:, 1] != t[:, 2]).all() and (t[:, 0] != t[:, 2]).all()
        v = p.vertices.astype(np.float64)
        g = np.cross(v[t[:, 1]] - v[t[:, 0]], v[t[:, 2]] - v[t[:, 0]])
        agree = ((p.normals[t].sum(axis=1) * g).sum(axis=1) > 0).mean()
        assert agree > 0.97, (p.name, agree)


def test_materials(model):
    for name, m in model.materials.items():
        assert m.name == name
        assert 0.0 <= m.roughness <= 1.0 and 0.0 <= m.metallic <= 1.0 and m.emissive >= 0.0
    used = {p.material for p in model.parts}
    assert used <= set(model.materials)
    tintable = {p.material for p in model.parts if model.materials[p.material].tintable}
    assert {"pla_shell", "pla_frame", "pla_leg"} <= tintable
    for p in model.parts:
        if p.group in ("shell", "frame"):
            assert model.materials[p.material].tintable, p.name


def test_parts_are_on_the_right_links(model):
    by_name = {p.name: p for p in model.parts}
    assert by_name["frame"].link == by_name["bottom_cover"].link == by_name["top_cover"].link == BODY_LINK
    for hip in HIPS:
        assert by_name[f"femur_{hip}"].link == hip
        assert by_name[f"servo_hip_{hip}.body"].link == BODY_LINK
        assert by_name[f"servo_hip_{hip}.horn"].link == hip
        for s in "ab":
            assert by_name[f"screw_horn_{hip}_{s}"].link == hip
            assert by_name[f"screw_mount_{hip}_{s}"].link == BODY_LINK
    for knee, hip in KNEE_PARENT.items():
        assert by_name[f"foot_{knee}"].link == knee
        assert by_name[f"servo_knee_{knee}.body"].link == knee        # bolted into the foot
        assert by_name[f"servo_knee_{knee}.horn"].link == hip         # horn stays with the femur
        for s in "ab":
            assert by_name[f"screw_horn_{knee}_{s}"].link == hip
            assert by_name[f"screw_mount_{knee}_{s}"].link == knee
    groups = {g: sum(1 for p in model.parts if p.group == g) for g in GROUPS}
    assert groups["servo"] == 8 and groups["horn"] == 8 and groups["screw"] == 32
    assert groups["battery"] == 1 and groups["screen"] >= 1 and groups["switch"] >= 1


def test_shaft_screws_and_horns_sit_on_the_joint_axes(model):
    """The horn-retaining screw (..._a) is coaxial with its joint; the horn hub too."""
    for name, j in model.joints.items():
        pivot, axis = np.asarray(j.pivot), np.asarray(j.axis)

        def axis_distance(points):
            rel = points.astype(np.float64) - pivot
            return np.linalg.norm(rel - np.outer(rel @ axis, axis), axis=1)

        screw = model.part(f"screw_horn_{name}_a")
        assert axis_distance(screw.vertices).max() < 3.0, name           # head radius ~2 mm
        centre = (screw.bounds_min + screw.bounds_max) / 2.0
        assert axis_distance(centre[None, :])[0] < 0.05, name
        horn = model.part(f"servo_{'hip' if name in HIPS else 'knee'}_{name}.horn")
        assert axis_distance(horn.vertices).min() < 2.0, name
        servo = model.part(f"servo_{'hip' if name in HIPS else 'knee'}_{name}.body")
        assert axis_distance(servo.vertices).min() < 0.5, name


def test_cover_variants(model):
    assert model.default_cover == "step"
    assert set(model.cover_variants) >= {"step"}
    all_cover_parts = [p for p in model.parts if p.cover_variant is not None]
    assert {p.cover_variant for p in all_cover_parts} == set(model.cover_variants)
    default = model.parts_for()
    assert default == model.parts_for(model.default_cover)
    assert [p.name for p in default if p.cover_variant] == ["top_cover"]
    for variant in model.cover_variants:
        parts = model.parts_for(variant)
        covers = [p for p in parts if p.cover_variant is not None]
        assert len(covers) == 1 and covers[0].cover_variant == variant
        assert covers[0].group == "shell" and covers[0].link == BODY_LINK
        # every cover sits on the frame: same footprint as the CAD cover
        ref = model.part("top_cover")
        assert np.allclose(covers[0].bounds_min, ref.bounds_min, atol=0.2), variant
        assert np.allclose(covers[0].bounds_max[[0, 2]], ref.bounds_max[[0, 2]], atol=0.2), variant
        assert len(parts) == len(default)
    with pytest.raises(KeyError):
        model.parts_for("no-such-cover")


def test_bounds(model):
    lo = np.min([p.bounds_min for p in model.parts_for()], axis=0)
    hi = np.max([p.bounds_max for p in model.parts_for()], axis=0)
    assert np.allclose(model.bounds_min, lo, atol=1e-3)
    assert np.allclose(model.bounds_max, hi, atol=1e-3)
    size = model.bounds_max - model.bounds_min
    assert 100 < size[0] < 160 and 70 < size[1] < 110 and 100 < size[2] < 160   # sane, in mm


# --------------------------------------------------------------------------- screen

def test_screen_quad_faces_outward(model):
    s = model.screen
    for v in (s.right, s.up, s.normal):
        assert abs(np.linalg.norm(v) - 1.0) < 1e-6
    assert abs(s.right @ s.up) < 1e-6 and abs(s.right @ s.normal) < 1e-6 and abs(s.up @ s.normal) < 1e-6
    assert np.allclose(np.cross(s.right, s.up), s.normal, atol=1e-6)     # right-handed, normal towards the viewer
    assert s.normal[0] < -0.8, "the screen looks out of the robot's face (-X)"
    assert s.up[1] > 0.8, "pixel row 0 is at the top"
    assert s.right[2] > 0.99, "pixel x grows towards the robot's left (+Z) = the viewer's right"
    assert abs(s.width - 21.744) < 0.3 and abs(s.height - 10.864) < 0.3
    assert abs(s.width / s.height - 2.0) < 0.01                          # 128 x 64 square pixels
    corners = s.corners()
    assert corners.shape == (4, 3)
    assert np.allclose(corners.mean(axis=0), s.center)
    assert np.allclose(corners[1] - corners[0], s.right * s.width)
    assert np.allclose(corners[0] - corners[3], s.up * s.height)


def test_screen_quad_sits_on_the_glass(model):
    s = model.screen
    glass = model.part("screen_glass")
    assert glass.group == "screen" and glass.link == BODY_LINK
    v = glass.vertices.astype(np.float64)
    depth = (v - s.center) @ s.normal
    assert 0.01 < -depth.max() < 0.2, "quad must sit just proud of the glass front face"
    # the quad lies within the glass outline ...
    u, w = (v - s.center) @ s.right, (v - s.center) @ s.up
    assert u.min() < -s.width / 2 and u.max() > s.width / 2
    assert w.min() < -s.height / 2 and w.max() > s.height / 2
    # ... and nothing of the default model is in front of it (it is visible)
    for p in model.parts_for():
        if p.link != BODY_LINK or p is glass:
            continue
        q = p.vertices.astype(np.float64) - s.center
        inside = (np.abs(q @ s.right) < s.width / 2 - 0.2) & (np.abs(q @ s.up) < s.height / 2 - 0.2)
        assert not (inside & (q @ s.normal > 0)).any(), p.name


# --------------------------------------------------------------------------- physics data

def test_collision_points(model):
    assert set(model.collision_points) == set(model.links)
    for link, pts in model.collision_points.items():
        assert pts.dtype == np.float64 and pts.ndim == 2 and pts.shape[1] == 3
        assert 4 <= len(pts) <= 24, link
        assert np.isfinite(pts).all()
        verts = np.concatenate([p.vertices for p in model.parts_for() if p.link == link]).astype(np.float64)
        lo, hi = verts.min(axis=0), verts.max(axis=0)
        # (right-hand links carry the mirror image of their left twin's points, which the
        # CAD reproduces to within COLLISION_TOLERANCE_MM)
        assert (pts >= lo - COLLISION_TOLERANCE_MM).all() and (pts <= hi + COLLISION_TOLERANCE_MM).all(), link
        assert len(np.unique(np.round(pts, 2), axis=0)) == len(pts), f"{link}: duplicate points"
    # feet: toe tip + sole corners (the foot points straight down in the bind pose)
    for knee in KNEE_PARENT:
        foot = model.part(f"foot_{knee}")
        pts = model.collision_points[knee]
        sole = pts[pts[:, 1] < foot.bounds_min[1] + 1.5]
        assert abs(pts[:, 1].min() - foot.bounds_min[1]) < 0.05, knee
        assert len(sole) >= 4, knee
        assert np.ptp(sole[:, 0]) > 3.0 and np.ptp(sole[:, 2]) > 3.0, knee
    # femurs: outer end (towards the knee) and inner end
    for hip in HIPS:
        j = model.joints[hip]
        d = femur_direction(model, dict(model.bind_angles), hip)
        femur = model.part(f"femur_{hip}").vertices.astype(np.float64)
        reach = (femur - np.asarray(j.pivot)) @ d
        got = (model.collision_points[hip] - np.asarray(j.pivot)) @ d
        assert got.max() > reach.max() - 1.0 and got.min() < reach.min() + 3.0, hip
    # body: bottom cover underside corners and top cover extremes
    body = model.collision_points[BODY_LINK]
    bottom, cover = model.part("bottom_cover"), model.part("top_cover")
    under = body[body[:, 1] < bottom.bounds_min[1] + 0.7]
    assert len(under) >= 4
    assert np.ptp(under[:, 0]) > 50 and np.ptp(under[:, 2]) > 20
    assert abs(body[:, 1].max() - cover.bounds_max[1]) < 0.05
    assert abs(body[:, 0].min() - cover.bounds_min[0]) < 0.5
    assert body[:, 0].max() > cover.bounds_max[0] - 0.5


def _mirror_planes(model):
    """(z_mid, x_mid): the robot's left/right and front/rear symmetry planes."""
    j = model.joints
    return (j["L1"].pivot[2] + j["R1"].pivot[2]) / 2.0, (j["L1"].pivot[0] + j["L2"].pivot[0]) / 2.0


def _set_distance(a, b):
    """Hausdorff distance between two point sets."""
    d = np.linalg.norm(a[:, None, :] - b[None, :, :], axis=2)
    return max(d.min(axis=1).max(), d.min(axis=0).max())


def test_collision_points_are_mirror_symmetric(model):
    """Twin links must carry mirror-image contact sets, otherwise a symmetric pose
    touches the ground asymmetrically (one foot lands first and the robot yaws)."""
    z_mid, x_mid = _mirror_planes(model)
    j = model.joints
    for a, b in (("L2", "R2"), ("L3", "R3"), ("L4", "R4")):       # the joint table is symmetric too
        assert abs((j[a].pivot[2] + j[b].pivot[2]) / 2.0 - z_mid) < 1e-3
    for a, b in (("R1", "R2"), ("L3", "L4"), ("R3", "R4")):
        assert abs((j[a].pivot[0] + j[b].pivot[0]) / 2.0 - x_mid) < 1e-3

    def mirror_z(p):
        return p * [1.0, 1.0, -1.0] + [0.0, 0.0, 2.0 * z_mid]

    def mirror_x(p):
        return p * [-1.0, 1.0, 1.0] + [2.0 * x_mid, 0.0, 0.0]

    cp = model.collision_points
    # left <-> right: exact mirror images
    for left, right in (("L1", "R1"), ("L2", "R2"), ("L3", "R3"), ("L4", "R4")):
        assert len(cp[left]) == len(cp[right]), (left, right)
        assert _set_distance(cp[right], mirror_z(cp[left])) < 1e-3, (left, right)
    # the body is symmetric about the same plane
    assert _set_distance(cp[BODY_LINK], mirror_z(cp[BODY_LINK])) < 1e-3
    # front <-> rear: femurs are exact mirror images; the CAD places the front feet a few
    # tenths of a millimetre off the mirrored rear ones, so their sets are equivalent
    # (same points, each on its own foot's surface) rather than identical
    for front, rear in (("L1", "L2"), ("R1", "R2")):
        assert _set_distance(cp[front], mirror_x(cp[rear])) < 1e-3, (front, rear)
    for front, rear in (("L3", "L4"), ("R3", "R4")):
        assert len(cp[front]) == len(cp[rear]), (front, rear)
        assert _set_distance(cp[front], mirror_x(cp[rear])) < 0.75, (front, rear)

    # consequence: a left/right symmetric pose has left/right symmetric contacts
    pose = {"R1": 110, "L1": 70, "R2": 60, "L2": 120, "R3": 130, "L3": 50, "R4": 40, "L4": 140}
    xf = link_transforms(model.joints, pose)
    for left, right in (("L1", "R1"), ("L2", "R2"), ("L3", "R3"), ("L4", "R4")):
        a = transform_points(xf[left], cp[left])
        b = transform_points(xf[right], cp[right])
        assert _set_distance(b, mirror_z(a)) < 1e-3, (left, right)
        assert abs(a[:, 1].min() - b[:, 1].min()) < 1e-3, (left, right)


def _distance_to_surface(points, vertices, triangles):
    """Exact distance of each point to a triangle mesh (closest point on triangle,
    Ericson 5.1.5), brute force over the triangles near the point."""
    a, b, c = (vertices[triangles[:, k]] for k in range(3))
    lo, hi = np.minimum(np.minimum(a, b), c), np.maximum(np.maximum(a, b), c)
    out = np.empty(len(points))
    for i, p in enumerate(points):
        box = np.linalg.norm(np.maximum(0.0, np.maximum(lo - p, p - hi)), axis=1)
        near = box <= np.linalg.norm(a - p, axis=1).min() + 1e-9
        ta, tb, tc = a[near], b[near], c[near]
        ab, ac = tb - ta, tc - ta
        normal = np.cross(ab, ac)
        area2 = np.linalg.norm(normal, axis=1)
        ok = area2 > 1e-12
        ta, tb, tc, ab, ac, normal = ta[ok], tb[ok], tc[ok], ab[ok], ac[ok], normal[ok] / area2[ok, None]
        # project onto the triangle's plane; inside -> plane distance, else nearest edge
        ap = p - ta
        h = (ap * normal).sum(axis=1)
        q = p - h[:, None] * normal
        inside = np.ones(len(ta), dtype=bool)
        best = np.full(len(ta), np.inf)
        for s, e in ((ta, tb), (tb, tc), (tc, ta)):
            edge = e - s
            inside &= (np.cross(edge, q - s) * normal).sum(axis=1) >= 0.0
            t = np.clip(((p - s) * edge).sum(axis=1) / (edge * edge).sum(axis=1), 0.0, 1.0)
            best = np.minimum(best, np.linalg.norm(p - (s + t[:, None] * edge), axis=1))
        out[i] = np.where(inside, np.abs(h), best).min()
    return out


def test_collision_points_lie_on_the_link_surface(model):
    """Every collision point is on (or within COLLISION_TOLERANCE_MM of) the real mesh
    of its link -- mirrored points included."""
    for link, pts in model.collision_points.items():
        parts = [p for p in model.parts_for() if p.link == link]
        offsets = np.cumsum([0] + [p.vertex_count for p in parts])
        vertices = np.concatenate([p.vertices for p in parts]).astype(np.float64)
        triangles = np.concatenate([p.indices.astype(np.int64) + o for p, o in zip(parts, offsets)])
        dist = _distance_to_surface(pts, vertices, triangles)
        assert dist.max() <= COLLISION_TOLERANCE_MM, (link, float(dist.max()))


def test_mass_estimate(model):
    assert 0.15 < model.mass_kg < 0.35
    assert (model.com > model.bounds_min).all() and (model.com < model.bounds_max).all()
    assert set(model.link_mass_kg) == set(model.links)
    assert abs(sum(model.link_mass_kg.values()) - model.mass_kg) < 1e-3
    for knee in KNEE_PARENT:                       # a foot carries its servo
        assert model.link_mass_kg[knee] > 0.009
    # roughly symmetric robot: COM near the middle of the hips, between left and right
    hips = np.mean([model.joints[h].pivot for h in HIPS], axis=0)
    assert abs(model.com[0] - hips[0]) < 5.0 and abs(model.com[2] - hips[2]) < 3.0
