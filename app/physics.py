"""Lightweight rigid-body ground physics for the 3D preview (numpy only).

The robot is ONE rigid body (6-DOF root: centre-of-mass position + orientation
quaternion) whose *shape* is driven kinematically by the servo angles -- the
servos are position controlled, so limb dynamics / joint torques are not
simulated. The collision points of every link (bind-pose body coordinates,
moved by the same forward kinematics as ``app.kinematics``) collide with the
ground plane ``y = 0`` with Coulomb friction, so planted feet grip and the
BODY moves when the legs do: walking, turning, push-ups and flopping onto the
belly all emerge from the servo angles alone.

Solver (per substep of ~2 ms) -- implicit and position based
-------------------------------------------------------------
1. Servo model: the actual joint angles chase their targets (rate limit, a
   short spin-up, exponential ease-in) -> body-frame collision points for the
   substep (forward kinematics batched over all substeps of a ``step()``).
2. Predict the root pose from its velocity and gravity.
3. Contacts: a stiff unilateral spring along the normal and, tangentially, a
   "bristle" from the spot where the point touched down (its anchor) to where
   it is now. Inside the friction cone the bristle is a spring (static
   friction: a planted foot stays planted, nothing creeps); outside it the
   force is ``mu * N`` against the bristle and the anchor is dragged along
   (kinetic friction).
   Because the body has only 6 DOF, the implicit-Euler / XPBD position solve
   for ALL contacts at once is one 6x6 linear system
   ``(M/h^2 + J^T W J) d = -(J^T W e + M D/h^2)`` whatever the number of
   contact points -- no Gauss-Seidel sweep, no Python loop over points. Up to
   three such Newton passes per substep update the active set and the
   friction weights ``min(kt, mu*N/|bristle|)``. Those weights are a secant
   (majorising) model of the convex friction potential: a pass can only
   shorten a slip, so friction never overshoots, jams or adds energy.
4. Velocities are derived from the pose change (restitution 0; kinematically
   moving feet push the body naturally), mildly damped and clamped.
5. When nothing moves for a moment the body goes to sleep: ``step()`` then
   costs nothing and nothing can drift.

Being implicit, the update is unconditionally stable for any stiffness or time
step, and a static equilibrium is an exact fixed point of it (no rest jitter).
Everything is deterministic: same calls -> bit-identical state.

Units: millimetres, seconds, kilograms (forces in kg*mm/s^2). +Y is up, the
ground is the plane y = 0, the robot's front looks down -X, left is +Z.
No Qt / OpenGL imports here.
"""

from __future__ import annotations

import math

import numpy as np

from .kinematics import BODY_LINK, JointDef, joint_order, link_transforms

GRAVITY = 9810.0  # mm / s^2

_EPS = 1e-12


# ---------------------------------------------------------------------------
# Small quaternion helpers (w, x, y, z) on plain Python floats -- faster than
# numpy for single quaternions.
# ---------------------------------------------------------------------------

def _quat_mul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _quat_normalize(q):
    w, x, y, z = q
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if not (n > 0.0) or not math.isfinite(n):
        return (1.0, 0.0, 0.0, 0.0)
    return (w / n, x / n, y / n, z / n)


def _quat_from_rotvec(rx, ry, rz):
    a = math.sqrt(rx * rx + ry * ry + rz * rz)
    if a < 1e-9:
        return _quat_normalize((1.0, 0.5 * rx, 0.5 * ry, 0.5 * rz))
    s = math.sin(0.5 * a) / a
    return (math.cos(0.5 * a), rx * s, ry * s, rz * s)


def _quat_to_mat(q) -> np.ndarray:
    w, x, y, z = q
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return np.array([
        [1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy)],
        [2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx)],
        [2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy)],
    ], dtype=np.float64)


def _quat_from_yaw(yaw_rad: float):
    return (math.cos(0.5 * yaw_rad), 0.0, math.sin(0.5 * yaw_rad), 0.0)


def _yaw_of(rot: np.ndarray) -> float:
    """Heading (rotation about +Y, radians) of a body orientation matrix.
    For an upright body ``rot == R_y(yaw)``. Falls back to the body Z axis
    when the body X axis points (nearly) straight up or down."""
    fx, fz = float(rot[0, 0]), float(rot[2, 0])          # image of body +X
    if fx * fx + fz * fz > 0.25:
        return math.atan2(-fz, fx)
    return math.atan2(float(rot[0, 2]), float(rot[2, 2]))  # image of body +Z


class RobotSim:
    """Ground-contact simulation of the Sesame robot driven by servo angles.

    See the module docstring for the algorithm. Public surface::

        sim = RobotSim(joints, collision_points)        # or RobotSim.from_model(model)
        sim.reset(angles)                               # at rest on the ground
        sim.set_targets(angles); sim.step(dt)           # servo-speed limited motion
        sim.set_angles_immediate(angles)                # direct manipulation
        sim.root, sim.angles, sim.link_world_transforms(), sim.contacts, sim.is_resting
    """

    # -- tunables (plain attributes; safe to change between steps) ----------
    #: contact normal stiffness per collision point (kg/s^2 == N/m)
    contact_stiffness = 4.0e4
    #: static-friction bristle stiffness per collision point (kg/s^2)
    friction_stiffness = 4.0e4
    #: velocity damping (1/s), always on
    linear_damping = 0.05
    angular_damping = 0.5
    #: extra damping (1/s) while touching the ground (rolling resistance)
    contact_linear_damping = 0.5
    contact_angular_damping = 3.0
    #: servo eases into its target over roughly this many degrees
    servo_ease_deg = 18.0
    #: servo spin-up time to full speed (s)
    servo_accel_time = 0.03
    #: set_angles_immediate() plays a jump out in quasi-static chunks of at most this many degrees
    immediate_chunk_deg = 2.5
    #: sleep thresholds
    sleep_linear_mm_s = 2.0
    sleep_angular_rad_s = 0.03
    sleep_time = 0.3
    #: hard safety clamps
    max_linear_mm_s = 3000.0
    max_angular_rad_s = 40.0
    #: solver passes per substep (upper bound) and the step size (mm) below
    #: which a substep counts as converged
    max_solves = 3
    solve_tolerance = 2.0e-3

    def __init__(self, joints: dict[str, JointDef], collision_points: dict[str, np.ndarray], *,
                 mass_kg: float = 0.3, com=None, friction: float = 0.9,
                 servo_speed_dps: float = 600.0, substeps: int = 8,
                 link_mass_kg: dict[str, float] | None = None,
                 link_com: dict | None = None):
        """``joints`` / ``collision_points`` as in ``app.kinematics`` /
        ``RobotModel`` (bind pose, body frame, mm). ``com`` defaults to the
        centre of the body link's collision points. If per-link masses and
        centres of mass are given (``link_mass_kg`` / ``link_com``, keyed by
        link name incl. ``"body"``), the centre of mass follows the leg pose
        and the inertia tensor is built from them; ``mass_kg`` stays the total."""
        self._joints = dict(joints)
        self._jn = joint_order(self._joints)
        self._jidx = {n: i for i, n in enumerate(self._jn)}
        nj = len(self._jn)

        # ---- joint tables for the batched forward kinematics --------------
        axes = np.zeros((nj, 3))
        self._piv = np.zeros((nj, 3))
        self._bind = np.zeros(nj)
        self._sgn_rad = np.zeros(nj)
        depth = [0] * nj
        parent_idx = [-1] * nj
        for i, n in enumerate(self._jn):
            j = self._joints[n]
            a = np.asarray(j.axis, dtype=np.float64)
            axes[i] = a          # used as given, exactly like kinematics.rotation_about_line
            self._piv[i] = j.pivot
            self._bind[i] = j.bind_angle
            self._sgn_rad[i] = math.radians(1.0) * j.sign
            if j.parent:
                parent_idx[i] = self._jidx[j.parent]
                depth[i] = depth[parent_idx[i]] + 1
        self._I3 = np.eye(3)
        self._K = np.zeros((nj, 3, 3))
        self._K[:, 0, 1] = -axes[:, 2]; self._K[:, 0, 2] = axes[:, 1]
        self._K[:, 1, 0] = axes[:, 2]; self._K[:, 1, 2] = -axes[:, 0]
        self._K[:, 2, 0] = -axes[:, 1]; self._K[:, 2, 1] = axes[:, 0]
        self._aaT = axes[:, :, None] * axes[:, None, :]
        self._levels = []
        for d in range(1, max(depth, default=0) + 1):
            ch = np.array([i for i in range(nj) if depth[i] == d], dtype=np.intp)
            pa = np.array([parent_idx[i] for i in ch], dtype=np.intp)
            self._levels.append((ch, pa))

        # ---- collision points (body-link points first) ---------------------
        body_pts, joint_pts, joint_of_pt = [], [], []
        body_names, joint_names = [], []
        for link, pts in collision_points.items():
            arr = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
            if arr.shape[0] == 0:
                continue
            if link == BODY_LINK:
                body_pts.append(arr)
                body_names += [link] * len(arr)
            elif link in self._jidx:
                joint_pts.append(arr)
                joint_of_pt += [self._jidx[link]] * len(arr)
                joint_names += [link] * len(arr)
            else:
                raise ValueError(f"collision_points: unknown link {link!r}")
        self._P_body = np.concatenate(body_pts) if body_pts else np.zeros((0, 3))
        self._P_joint = np.concatenate(joint_pts) if joint_pts else np.zeros((0, 3))
        self._pt_joint = np.array(joint_of_pt, dtype=np.intp)
        self._pt_link = body_names + joint_names
        self._nb = len(self._P_body)
        self._n = self._nb + len(self._P_joint)
        if self._n == 0:
            raise ValueError("collision_points is empty")
        self._Pj0 = np.ascontiguousarray(self._P_joint[:, 0:1])
        self._Pj1 = np.ascontiguousarray(self._P_joint[:, 1:2])
        self._Pj2 = np.ascontiguousarray(self._P_joint[:, 2:3])

        # ---- mass properties ------------------------------------------------
        self._mass = max(float(mass_kg), 1e-3)
        all_pts = np.concatenate([self._P_body, self._P_joint])
        lo, hi = all_pts.min(axis=0), all_pts.max(axis=0)
        if com is None:
            ref = self._P_body if self._nb else all_pts
            com = 0.5 * (ref.min(axis=0) + ref.max(axis=0))
        self._com = np.asarray(com, dtype=np.float64).reshape(3).copy()   # bind-pose COM
        self._lfrac = None            # per-joint-link mass fraction (pose-dependent COM)
        self._lcom = None             # per-joint-link COM, bind pose
        self._com_static = self._com  # COM contribution that does not move with the joints
        links = [BODY_LINK] + self._jn
        masses = {k: float(v) for k, v in (link_mass_kg or {}).items() if k in links}
        total = sum(masses.values())
        if (link_com and total > 0.0 and all(m >= 0.0 for m in masses.values())
                and all(k in link_com for k in masses)):
            frac = {k: m / total for k, m in masses.items()}
            cen = {k: np.asarray(link_com[k], dtype=np.float64).reshape(3) for k in masses}
            self._com = sum(frac[k] * cen[k] for k in masses)
            self._lfrac = np.array([frac.get(n, 0.0) for n in self._jn])
            self._lcom = np.array([cen.get(n, np.zeros(3)) for n in self._jn])
            self._com_static = frac.get(BODY_LINK, 0.0) * cen.get(BODY_LINK, np.zeros(3))
            # each link = solid box of its collision extent + parallel-axis term
            inertia = np.zeros((3, 3))
            for k in masses:
                pts_k = collision_points.get(k)
                ext_k = np.zeros(3)
                if pts_k is not None and len(pts_k):
                    arr = np.asarray(pts_k, dtype=np.float64).reshape(-1, 3)
                    ext_k = arr.max(axis=0) - arr.min(axis=0)
                ex, ey, ez = ext_k
                m_k = frac[k] * self._mass
                inertia += (m_k / 12.0) * np.diag(
                    [ey * ey + ez * ez, ex * ex + ez * ez, ex * ex + ey * ey])
                d = cen[k] - self._com
                inertia += m_k * (float(d @ d) * np.eye(3) - np.outer(d, d))
            self._inertia_body = inertia
        else:
            # Solid box spanning the whole robot, scaled down because most of
            # the mass (battery, electronics, hip servos) sits in the body.
            ex, ey, ez = np.maximum(hi - lo, 10.0)
            self._inertia_body = 0.7 * (self._mass / 12.0) * np.diag(
                [ey * ey + ez * ez, ex * ex + ez * ez, ex * ex + ey * ey])
        # keep the tensor well conditioned whatever the input
        floor = self._mass * 15.0 ** 2
        self._inertia_body = 0.5 * (self._inertia_body + self._inertia_body.T) + floor * 0.05 * np.eye(3)
        self._rmax = 1.25 * float(np.max(np.linalg.norm(all_pts - self._com, axis=1)))

        # ---- parameters -------------------------------------------------------
        self._friction = float(friction)
        self.servo_speed_dps = float(servo_speed_dps)
        self._substeps = max(1, int(substeps))
        self._h_nom = 1.0 / (60.0 * self._substeps)

        # ---- solver work buffers -----------------------------------------------
        n = self._n
        self._Jbuf = np.zeros((n, 3, 6))
        self._Jbuf[:, 0, 0] = 1.0
        self._Jbuf[:, 1, 1] = 1.0
        self._Jbuf[:, 2, 2] = 1.0
        # rot part of J: rows x,y,z = [0, rz, -ry], [-rz, 0, rx], [ry, -rx, 0]
        e = np.zeros((3, 3, 3))
        e[2, 0, 1] = 1.0; e[1, 0, 2] = -1.0
        e[2, 1, 0] = -1.0; e[0, 1, 2] = 1.0
        e[1, 2, 0] = 1.0; e[0, 2, 1] = -1.0
        self._E = e.reshape(3, 9)
        self._W = np.zeros((n, 3))
        self._A0 = np.zeros((6, 6))

        # ---- state ------------------------------------------------------------------
        self._ang = self._bind.copy()          # actual servo angles (deg)
        self._tgt = self._bind.copy()          # commanded servo angles (deg)
        self._jvel = np.zeros(nj)              # servo speeds (deg/s)
        self._p = np.zeros(3)                  # COM world position
        self._q = (1.0, 0.0, 0.0, 0.0)         # body orientation
        self._v = np.zeros(3)
        self._w = np.zeros(3)
        self._anchor = np.zeros((n, 3))        # friction anchors (world x, 0, z)
        self._nlag = np.zeros(n)               # normal force at the end of the last substep
        self._inc = np.zeros(n, dtype=bool)    # point currently in contact
        self._bc = None                        # (N,3) body-frame points rel. to the COM
        self._com_cur = self._com              # COM in the body frame for the current pose
        self._set_pose_tables(self._fk(self._ang[None, :]))
        self._asleep = False
        self._still_time = 0.0
        self._reset_cache: dict = {}
        self._settling = False
        self.reset({})

    # ------------------------------------------------------------------ factory
    @classmethod
    def from_model(cls, model, **kw) -> "RobotSim":
        """Build from an ``app.robot_model.RobotModel`` (``.joints``,
        ``.collision_points``, optional ``.mass_kg`` / ``.com``)."""
        if "mass_kg" not in kw and getattr(model, "mass_kg", None):
            kw["mass_kg"] = float(model.mass_kg)
        if "com" not in kw and getattr(model, "com", None) is not None:
            kw["com"] = model.com
        if "link_mass_kg" not in kw and getattr(model, "link_mass_kg", None) \
                and getattr(model, "link_com", None):
            kw["link_mass_kg"] = model.link_mass_kg
            kw["link_com"] = model.link_com
        return cls(model.joints, model.collision_points, **kw)

    # --------------------------------------------------------------- properties
    @property
    def root(self) -> np.ndarray:
        """4x4 float64 body -> world transform."""
        m = np.eye(4, dtype=np.float64)
        rot = _quat_to_mat(self._q)
        m[:3, :3] = rot
        m[:3, 3] = self._p - rot @ self._com_cur
        return m

    @property
    def angles(self) -> dict[str, float]:
        """Actual (slewed) joint angles in servo degrees."""
        return {n: float(self._ang[i]) for i, n in enumerate(self._jn)}

    @property
    def targets(self) -> dict[str, float]:
        """Commanded joint angles in servo degrees."""
        return {n: float(self._tgt[i]) for i, n in enumerate(self._jn)}

    @property
    def contacts(self) -> list:
        """Current ground contacts as ``(world_xyz ndarray(3), link_name)``."""
        idx = np.nonzero(self._inc)[0]
        if idx.size == 0:
            return []
        rot = _quat_to_mat(self._q)
        world = self._bc[idx] @ rot.T + self._p
        return [(world[k].copy(), self._pt_link[i]) for k, i in enumerate(idx)]

    @property
    def is_resting(self) -> bool:
        return self._asleep

    @property
    def friction(self) -> float:
        return self._friction

    @friction.setter
    def friction(self, value: float) -> None:
        value = max(0.0, float(value))
        if value != self._friction:
            self._friction = value
            self._reset_cache.clear()
            self.wake()

    @property
    def substeps(self) -> int:
        return self._substeps

    @substeps.setter
    def substeps(self, value: int) -> None:
        self._substeps = max(1, int(value))
        self._h_nom = 1.0 / (60.0 * self._substeps)
        self._reset_cache.clear()

    @property
    def linear_velocity(self) -> np.ndarray:
        """COM velocity (mm/s, world)."""
        return self._v.copy()

    @property
    def angular_velocity(self) -> np.ndarray:
        """Body angular velocity (rad/s, world)."""
        return self._w.copy()

    @property
    def servos_moving(self) -> bool:
        """True while any joint is still travelling to its target."""
        return bool(np.any(self._ang != self._tgt))

    def link_world_transforms(self) -> dict[str, np.ndarray]:
        """{link: 4x4 bind-pose body coords -> world} for the current state."""
        root = self.root
        return {k: root @ m for k, m in link_transforms(self._joints, self.angles).items()}

    def planar_pose(self) -> tuple[float, float, float]:
        """(x, z, yaw_deg) of the body origin / heading -- the convention
        ``reset()`` takes, so ``x, z, yaw = sim.planar_pose();
        sim.reset(angles, x=x, z=z, yaw_deg=yaw)`` re-seats the robot where it
        currently is. yaw is the rotation about +Y (positive = front swings
        towards +Z, i.e. a left turn)."""
        rot = _quat_to_mat(self._q)
        o = self._p - rot @ self._com_cur
        return float(o[0]), float(o[2]), math.degrees(_yaw_of(rot))

    # ----------------------------------------------------------------- control
    def wake(self) -> None:
        self._asleep = False
        self._still_time = 0.0

    def set_targets(self, angles: dict[str, float]) -> None:
        """Command servo targets; the joints slew there at servo speed."""
        changed = False
        for name, value in angles.items():
            i = self._jidx.get(name)
            if i is None:
                continue
            value = float(value)
            if not math.isfinite(value):
                continue
            if self._tgt[i] != value:
                self._tgt[i] = value
                changed = True
        if changed:
            self.wake()

    def set_angles_immediate(self, angles: dict[str, float]) -> None:
        """Jump joints (and their targets) straight to ``angles`` -- for direct
        manipulation. The jump is played out as a quasi-static sequence of
        small joint increments: planted feet keep gripping and push the body,
        but no velocity is injected, so even a 180 degree jump cannot launch
        the robot."""
        new = self._ang.copy()
        for name, value in angles.items():
            i = self._jidx.get(name)
            if i is None:
                continue
            value = float(value)
            if math.isfinite(value):
                new[i] = value
        delta = new - self._ang
        self._tgt[:] = new
        self._jvel[:] = 0.0
        biggest = float(np.max(np.abs(delta))) if delta.size else 0.0
        if biggest <= 0.0:
            if np.any(self._ang != self._tgt):
                self.wake()
            return
        chunks = max(1, int(math.ceil(biggest / max(self.immediate_chunk_deg, 0.1))))
        chunks = min(chunks, 240)
        frac = (np.arange(1, chunks + 1) / chunks)[:, None]
        traj = self._ang[None, :] + frac * delta[None, :]
        traj[-1] = new
        pts, coms = self._fk(traj)
        h = self._h_nom
        grounded = bool(self._inc.any())
        for k in range(chunks):
            self._substep(pts[k], h, False, self._anchor, self._inc, self._nlag)
        self._ang[:] = new
        self._bc = pts[-1]
        self._com_cur = coms[-1]
        if grounded and not self._inc.any():
            # A corner sweeping past can leave the body hovering a hair above
            # the ground (the quasi-static pass has no fall); seat it again.
            gap = float((self._bc @ _quat_to_mat(self._q)[1]).min() + self._p[1])
            if 0.0 <= gap < 5.0:
                self._p = self._p.copy()
                self._p[1] -= gap + 1e-3
                self._refresh_contacts()
        self._sanitize()
        self.wake()

    def reset(self, angles: dict[str, float], *, x: float = 0.0, z: float = 0.0,
              yaw_deg: float = 0.0) -> None:
        """Place the robot at rest ON the ground in this pose, body origin at
        world (x, z), heading ``yaw_deg`` about +Y. The resting orientation is
        found by letting the pose settle internally (deterministic, cached per
        pose), so nothing drops or shifts afterwards. Joints missing from
        ``angles`` take their bind angle."""
        for i, n in enumerate(self._jn):
            value = angles.get(n, self._bind[i])
            value = float(value)
            self._ang[i] = value if math.isfinite(value) else self._bind[i]
        self._tgt[:] = self._ang
        self._jvel[:] = 0.0
        self._set_pose_tables(self._fk(self._ang[None, :]))

        key = tuple(np.round(self._ang, 4).tolist())
        hit = self._reset_cache.get(key)
        if hit is None:
            hit = self._settle()
            if len(self._reset_cache) > 256:
                self._reset_cache.clear()
            self._reset_cache[key] = hit
        o_y, q_s = hit                              # canonical: origin at x=z=0, yaw 0

        q = _quat_normalize(_quat_mul(_quat_from_yaw(math.radians(float(yaw_deg))), q_s))
        rot = _quat_to_mat(q)
        self._q = q
        self._p = np.array([float(x), o_y, float(z)]) + rot @ self._com_cur
        self._v[:] = 0.0
        self._w[:] = 0.0
        self._refresh_contacts()
        self._asleep = True
        self._still_time = 0.0

    # -------------------------------------------------------------------- step
    def step(self, dt: float) -> None:
        """Advance the simulation by ``dt`` seconds (clamped to 0.05 s)."""
        if self._asleep:
            return
        dt = float(dt)
        if not (dt > 0.0):
            return
        dt = min(dt, 0.05)
        n = max(1, int(math.ceil(dt / self._h_nom - 1e-6)))
        h = dt / n

        traj = self._advance_servos(n, h)
        pts = coms = None
        if traj is not None:
            pts, coms = self._fk(traj)

        # Only points that can reach the ground during this step take part in
        # the contact solve (typically the soles, or the belly when lying).
        v, w = self._v, self._w
        spin = math.sqrt(w[0] * w[0] + w[1] * w[1] + w[2] * w[2])
        if pts is not None:
            spin += math.radians(max(self.servo_speed_dps, 1.0))
        reach = 3.0 + dt * (math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
                            + GRAVITY * dt + spin * self._rmax)
        row_y = _quat_to_mat(self._q)[1]
        height = self._bc @ row_y
        if pts is not None:
            np.minimum(height, pts[-1] @ row_y, out=height)
        cand = np.nonzero((height < reach - self._p[1]) | self._inc)[0]
        if cand.size == self._n:
            state = (self._anchor, self._inc, self._nlag)
            if pts is None:
                bc = self._bc
                for _ in range(n):
                    self._substep(bc, h, True, *state)
            else:
                for k in range(n):
                    self._substep(pts[k], h, True, *state)
        else:
            state = (self._anchor[cand], self._inc[cand], self._nlag[cand])
            if pts is None:
                bc = self._bc[cand]
                for _ in range(n):
                    self._substep(bc, h, True, *state)
            else:
                sub = pts[:, cand]
                for k in range(n):
                    self._substep(sub[k], h, True, *state)
            self._anchor[cand] = state[0]
            self._inc[:] = False
            self._inc[cand] = state[1]
            self._nlag[:] = 0.0
            self._nlag[cand] = state[2]
        if pts is not None:
            self._bc = pts[-1]
            self._com_cur = coms[-1]
        self._sanitize()

        # ---- sleeping --------------------------------------------------------------
        if traj is None:
            v, w = self._v, self._w
            lin = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
            ang = math.sqrt(w[0] * w[0] + w[1] * w[1] + w[2] * w[2])
            if lin < self.sleep_linear_mm_s and ang < self.sleep_angular_rad_s:
                self._still_time += dt
                limit = 0.1 if self._settling else self.sleep_time
                if self._still_time >= limit and bool(self._inc.any()):
                    self._asleep = True
                    self._v[:] = 0.0
                    self._w[:] = 0.0
            else:
                self._still_time = 0.0
        else:
            self._still_time = 0.0

    # ================================================================ internals
    def _advance_servos(self, n: int, h: float):
        """Integrate the servo model over ``n`` substeps of ``h`` seconds.
        Returns the (n, J) joint-angle trajectory, or None if nothing moves."""
        ang, tgt, vel = self._ang, self._tgt, self._jvel
        moving = [i for i in range(len(self._jn)) if ang[i] != tgt[i]]
        if not moving:
            return None
        vmax = max(1.0, float(self.servo_speed_dps))
        kp = vmax / max(float(self.servo_ease_deg), 0.5)
        dv_max = vmax / max(float(self.servo_accel_time), h) * h
        traj = np.empty((n, len(self._jn)))
        traj[:] = ang
        for i in moving:
            a = float(ang[i]); t = float(tgt[i]); v = float(vel[i])
            col = []
            for _ in range(n):
                e = t - a
                vd = kp * e
                if vd > vmax:
                    vd = vmax
                elif vd < -vmax:
                    vd = -vmax
                dv = vd - v
                if dv > dv_max:
                    dv = dv_max
                elif dv < -dv_max:
                    dv = -dv_max
                v += dv
                step = v * h
                if abs(e) < 0.02 or (step > 0.0) == (e > 0.0) and abs(step) >= abs(e):
                    a = t
                    v = 0.0
                else:
                    a += step
                col.append(a)
            traj[:, i] = col
            ang[i] = a
            vel[i] = v
        return traj

    def _fk(self, traj: np.ndarray):
        """Batched forward kinematics: (S, J) servo angles -> ((S, N, 3)
        collision points in the body frame relative to the COM, (S, 3) COM in
        the body frame)."""
        s_count = traj.shape[0]
        out = np.empty((s_count, self._n, 3))
        nb = self._nb
        th = (traj - self._bind) * self._sgn_rad
        c = np.cos(th)[:, :, None, None]
        s = np.sin(th)[:, :, None, None]
        rl = c * self._I3 + s * self._K + (1.0 - c) * self._aaT          # (S, J, 3, 3)
        tl = self._piv - np.matmul(rl, self._piv[:, :, None])[..., 0]     # (S, J, 3)
        for ch, pa in self._levels:
            rp = rl[:, pa]
            tl[:, ch] = np.matmul(rp, tl[:, ch][..., None])[..., 0] + tl[:, pa]
            rl[:, ch] = np.matmul(rp, rl[:, ch])
        if self._lfrac is None:
            coms = np.empty((s_count, 3))
            coms[:] = self._com
        else:
            link_c = np.matmul(rl, self._lcom[:, :, None])[..., 0] + tl   # (S, J, 3)
            coms = self._lfrac @ link_c + self._com_static                 # (S, 3)
        shift = coms[:, None, :]
        if nb:
            out[:, :nb] = self._P_body - shift
        if self._n > nb:
            rg = rl[:, self._pt_joint]                                     # (S, Nj, 3, 3)
            pts = rg[..., 0] * self._Pj0
            pts += rg[..., 1] * self._Pj1
            pts += rg[..., 2] * self._Pj2
            pts += tl[:, self._pt_joint]
            pts -= shift
            out[:, nb:] = pts
        return out, coms

    def _set_pose_tables(self, fk_result) -> None:
        pts, coms = fk_result
        self._bc = pts[-1]
        self._com_cur = coms[-1]

    def _substep(self, bc: np.ndarray, h: float, dynamic: bool,
                 anchor: np.ndarray, inc: np.ndarray, nlag: np.ndarray) -> None:
        """One implicit position-based step of the root against the ground.

        ``bc``: (M, 3) collision points in the body frame relative to the COM
        for the joint angles at the END of this substep; ``anchor`` (M, 3; y
        is always 0), ``inc`` (M,) and ``nlag`` (M,) are their friction
        anchors, in-contact flags and last normal forces (updated in place).
        ``dynamic=False`` is the quasi-static variant used by
        set_angles_immediate(): no velocity in the prediction and velocities
        are left untouched afterwards."""
        p0 = self._p
        q0 = self._q
        hh = h * h
        if dynamic:
            v = self._v
            w = self._w
            pp = p0 + v * h
            qp = _quat_normalize(_quat_mul(
                _quat_from_rotvec(w[0] * h, w[1] * h, w[2] * h), q0))
        else:
            pp = p0.copy()
            qp = q0
        pp[1] -= GRAVITY * hh
        rot = _quat_to_mat(qp)
        p = pp
        q = qp
        touching = False
        m = bc.shape[0]

        if m:
            kn = self.contact_stiffness
            kt = self.friction_stiffness
            neg_kn = -kn
            mu = self._friction
            m_h2 = self._mass / hh
            max_solves = self.max_solves
            tol = self.solve_tolerance
            n_solves = 0
            done = False
            prev_free = False
            prev_act = None
            moved = None
            # friction bound of the first pass: mu * (normal force at the end of
            # the previous substep); later passes use this substep's own forces
            lim = mu * nlag

            while True:
                # ---- evaluate the contacts at the current iterate ----------------
                rw = bc @ rot.T
                err = rw + p
                err -= anchor                           # x,z: bristle (offset from the anchor); y: height
                height = err[:, 1]
                act = height < 0.0
                n_act = int(np.count_nonzero(act))
                if n_act == 0:
                    touching = False
                    break
                touching = True
                new = act > inc
                if new.any():                           # touch-down: plant the anchor there
                    anchor[new, 0] = rw[new, 0] + p[0]
                    anchor[new, 2] = rw[new, 2] + p[2]
                    err[new, 0] = 0.0
                    err[new, 2] = 0.0
                    inc |= new
                nf = np.maximum(height * neg_kn, 0.0)   # normal forces (0 where inactive)
                if n_solves:
                    lim = mu * nf
                # Friction force = -coef * bristle with coef = min(kt, mu*N/|bristle|):
                # the bristle spring inside the cone (stick), mu*N against the
                # offset outside it (slide). The same coef is used as stiffness
                # (secant / majorising quadratic of the convex friction
                # potential), so a pass can only shorten the slip: friction never
                # overshoots, reverses or pumps energy in, whatever N is.
                coef = lim / np.maximum(np.hypot(err[:, 0], err[:, 2]), _EPS)
                np.minimum(coef, kt, out=coef)
                coef *= act
                free = int(np.count_nonzero(coef == kt)) == n_act   # all inside their cones
                if n_solves and (done or (free and prev_free
                                          and prev_act.tobytes() == act.tobytes())):
                    break                               # good enough / the linear solve was exact
                prev_free, prev_act = free, act

                # ---- assemble and solve the 6x6 Newton system --------------------
                wbuf = self._W[:m]
                wbuf[:, 0] = coef
                wbuf[:, 1] = kn * act
                wbuf[:, 2] = coef
                if moved is None:
                    # Jacobian of the point positions w.r.t. (dp, dtheta); the
                    # lever arms barely change within a substep, so it is built once
                    jb = self._Jbuf[:m]
                    jb[:, :, 3:] = (rw @ self._E).reshape(m, 3, 3)
                    jf = jb.reshape(3 * m, 6)
                    jft = jf.T
                    a0 = self._A0
                    a0[0, 0] = a0[1, 1] = a0[2, 2] = m_h2
                    a0[3:, 3:] = (rot @ self._inertia_body @ rot.T) / hh
                    grad = jft @ (wbuf * err).reshape(3 * m)
                    moved = np.zeros(6)                 # (dp, dtheta) accumulated since the prediction
                else:
                    grad = jft @ (wbuf * err).reshape(3 * m)
                    grad += a0 @ moved                  # inertia: M (pose - prediction) / h^2
                kmat = (jft * wbuf.reshape(3 * m)) @ jf
                kmat += a0
                d = np.linalg.solve(kmat, grad)        # the Newton step is -d
                dp = d[:3]
                dth = d[3:]
                lin = math.sqrt(dp[0] * dp[0] + dp[1] * dp[1] + dp[2] * dp[2])
                ang = math.sqrt(dth[0] * dth[0] + dth[1] * dth[1] + dth[2] * dth[2])
                if not (lin < 1e12 and ang < 1e12):     # NaN / inf: give up on this substep
                    break
                scale = -1.0
                if lin > 25.0:                          # robustness clamps on one step
                    scale = -25.0 / lin
                if ang * -scale > 0.3:
                    scale = -0.3 / ang
                d *= scale
                moved += d
                p = p + dp
                q = _quat_normalize(_quat_mul(_quat_from_rotvec(dth[0], dth[1], dth[2]), q))
                rot = _quat_to_mat(q)
                n_solves += 1
                if n_solves > 1:
                    if lin < tol and ang < tol * 0.02:
                        # converged: the contact state just evaluated is still
                        # valid (it moved by less than tol), skip the re-evaluation
                        err[:, 0] += dp[0]
                        err[:, 2] += dp[2]
                        break
                    # close enough after two passes, or out of passes: one last
                    # evaluation for the bookkeeping, no further solve
                    done = n_solves >= max_solves or (lin < 10.0 * tol and ang < tol * 0.2)
                else:
                    done = n_solves >= max_solves

            # ---- contact bookkeeping (anchors dragged by sliding points) ---------
            if touching:
                slide = coef < kt
                slide &= act
                if slide.any():
                    # leave the anchor one cone-radius (mu*N/kt) behind the point
                    creep = 1.0 - coef[slide] / kt
                    anchor[slide, 0] += err[slide, 0] * creep
                    anchor[slide, 2] += err[slide, 2] * creep
                inc[:] = act
                nlag[:] = nf
            else:
                inc[:] = False
                nlag[:] = 0.0

        self._p = p
        self._q = q
        if not dynamic:
            return

        # ---- velocities from the pose change ---------------------------------------
        v = (p - p0) / h
        dq = _quat_mul(q, (q0[0], -q0[1], -q0[2], -q0[3]))
        sgn = 2.0 / h if dq[0] >= 0.0 else -2.0 / h
        w = np.array([dq[1] * sgn, dq[2] * sgn, dq[3] * sgn])
        cl = self.linear_damping
        ca = self.angular_damping
        if touching:
            cl += self.contact_linear_damping
            ca += self.contact_angular_damping
        if self._settling:
            cl += 4.0
            ca += 10.0
        v *= 1.0 / (1.0 + cl * h)
        w *= 1.0 / (1.0 + ca * h)
        lin = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
        if lin > self.max_linear_mm_s:
            v *= self.max_linear_mm_s / lin
        ang = math.sqrt(w[0] * w[0] + w[1] * w[1] + w[2] * w[2])
        if ang > self.max_angular_rad_s:
            w *= self.max_angular_rad_s / ang
        self._v = v
        self._w = w

    def _sanitize(self) -> None:
        """Last line of defence: never let a non-finite state escape."""
        ok = (np.all(np.isfinite(self._p)) and np.all(np.isfinite(self._v))
              and np.all(np.isfinite(self._w)) and all(math.isfinite(c) for c in self._q))
        if ok:
            return
        self._q = (1.0, 0.0, 0.0, 0.0)
        self._v = np.zeros(3)
        self._w = np.zeros(3)
        p = np.where(np.isfinite(self._p), self._p, 0.0)
        p[1] = -float(self._bc[:, 1].min())
        self._p = p
        self._inc[:] = False
        self._nlag[:] = 0.0

    def _refresh_contacts(self) -> None:
        """Rebuild the contact set / friction anchors for the current pose."""
        rot = _quat_to_mat(self._q)
        rw = self._bc @ rot.T
        pen = np.maximum(-(rw[:, 1] + self._p[1]), 0.0)
        self._inc[:] = pen > 0.0
        self._nlag[:] = self.contact_stiffness * pen
        self._anchor[:, 0] = rw[:, 0] + self._p[0]
        self._anchor[:, 2] = rw[:, 2] + self._p[2]

    def _settle(self):
        """Find the resting pose for the current joint angles. Returns
        (body-origin height, orientation quaternion) normalised to body origin
        x = z = 0 and heading 0."""
        saved_targets = self._tgt.copy()
        self._q = (1.0, 0.0, 0.0, 0.0)
        self._p = np.array([0.0, -float(self._bc[:, 1].min()), 0.0])
        self._v = np.zeros(3)
        self._w = np.zeros(3)
        self._inc[:] = False
        self._nlag[:] = 0.0
        self._asleep = False
        self._still_time = 0.0
        self._settling = True
        try:
            for _ in range(600):                 # at most 10 simulated seconds
                self.step(1.0 / 60.0)
                if self._asleep:
                    break
        finally:
            self._settling = False
        self._tgt[:] = saved_targets
        rot = _quat_to_mat(self._q)
        yaw = _yaw_of(rot)
        q = _quat_normalize(_quat_mul(_quat_from_yaw(-yaw), self._q))
        origin_y = float((self._p - rot @ self._com_cur)[1])
        return origin_y, q
