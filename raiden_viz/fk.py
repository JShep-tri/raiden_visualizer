"""Forward kinematics for the YAM 6-DOF arm — pure numpy, no mujoco/mink.

The raw datasets store joint angles only (no end-effector pose), so to project
the EE into image space we chain the URDF's per-joint transforms. yam.urdf is a
clean 6-revolute serial chain; FK is just
    T = prod_i  T_origin(i) . T_rotate(axis_i, q_i)
and the EE position is the resulting translation, in the arm's base frame — which
is exactly the calibration's ``left_arm_base`` coordinate frame.

Note: the chain ends at the wrist flange (link_6); there's no gripper/fingertip
offset in the URDF, so the EE point is the flange. For a short future-trace this
is plenty accurate.
"""

import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

import numpy as np

_URDF = Path(__file__).resolve().parent / "urdf" / "yam.urdf"

# Fixed transform from the URDF's tip link (link_6, the wrist flange) to the
# actual grasp point ("grasp_site" in raiden's MuJoCo model). Recovered by
# matching this URDF's FK against raiden's kinematics (i2rt Kinematics on the
# yam_4310_linear model) across configs — verified constant. It's a 90° rotation
# about the tool Z plus ~13.5 cm down the tool axis (the gripper length), so the
# EE we report is the grasp point, matching how raiden resolves EE poses.
_T_FLANGE_TO_GRASP = np.array([
    [0.0, 1.0, 0.0, -0.00065],
    [-1.0, 0.0, 0.0, 0.00615],
    [0.0, 0.0, 1.0, 0.13465],
    [0.0, 0.0, 0.0, 1.0],
])


def _rpy_to_R(rpy):
    r, p, y = rpy
    cr, sr = np.cos(r), np.sin(r)
    cp, sp = np.cos(p), np.sin(p)
    cy, sy = np.cos(y), np.sin(y)
    Rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
    Ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
    Rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]])
    return Rz @ Ry @ Rx


def _axis_angle_R(axis, q):
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    x, y, z = a
    c, s = np.cos(q), np.sin(q)
    C = 1 - c
    return np.array([
        [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
    ])


def _T(R, p):
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = p
    return M


@lru_cache(maxsize=1)
def _chain():
    """Ordered list of (origin_T, axis, is_revolute) from base_link to the tip."""
    root = ET.parse(_URDF).getroot()
    joints = {}
    for j in root.findall("joint"):
        o = j.find("origin")
        a = j.find("axis")
        joints[j.get("name")] = {
            "parent": j.find("parent").get("link"),
            "child": j.find("child").get("link"),
            "xyz": [float(v) for v in (o.get("xyz", "0 0 0").split())] if o is not None else [0, 0, 0],
            "rpy": [float(v) for v in (o.get("rpy", "0 0 0").split())] if o is not None else [0, 0, 0],
            "axis": [float(v) for v in a.get("xyz").split()] if a is not None else [0, 0, 1],
            "type": j.get("type"),
        }
    by_parent = {jd["parent"]: (n, jd) for n, jd in joints.items()}
    chain = []
    link = "base_link"
    while link in by_parent:
        _n, jd = by_parent[link]
        chain.append((_T(_rpy_to_R(jd["rpy"]), np.array(jd["xyz"])), jd["axis"], jd["type"] == "revolute"))
        link = jd["child"]
    return chain


def ee_position(q) -> np.ndarray:
    """Grasp-point xyz in the arm base frame for one joint vector q (len>=6)."""
    M = np.eye(4)
    for (origin_T, axis, revolute), qi in zip(_chain(), q):
        M = M @ origin_T
        if revolute:
            M = M @ _T(_axis_angle_R(axis, float(qi)), np.zeros(3))
    M = M @ _T_FLANGE_TO_GRASP  # flange -> grasp point (matches raiden's EE)
    return M[:3, 3]


def ee_trajectory(joint_series) -> np.ndarray:
    """(N,3) EE positions for an (N, >=6) array of joint angles."""
    q = np.asarray(joint_series, float)
    return np.array([ee_position(row) for row in q])
