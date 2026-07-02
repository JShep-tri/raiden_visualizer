"""Calibration sanity overlay: project known 3D reference frames onto a still
camera frame so a human can eyeball whether the extrinsics are right.

We can't project the end-effector (robot_data.npz stores joint angles only, no EE
poses, and there's no URDF/FK here), but the calibration itself pins down fixed
frames in the ``left_arm_base`` coordinate system:

  * left arm base  = origin (0,0,0)
  * right arm base = bimanual_transform.right_base_to_left_base applied to origin

Drawing an XYZ axis triad at each lets you check alignment: if calibration is
good, each triad sits exactly on that arm's base in the image.

Extrinsic convention (verified against real frames): ``calibration_results.json``
stores each camera's POSE IN THE BASE FRAME as (rotation_matrix R, translation t),
so a base-frame point maps to camera coords by ``X_cam = R^T (X_base - t)``.
"""

import subprocess
from pathlib import Path

import numpy as np

# BGR colors for X/Y/Z axes (OpenCV convention: red/green/blue).
_AXIS_COLORS = ((0, 0, 255), (0, 255, 0), (255, 0, 0))
_AXIS_LEN_M = 0.12  # 12 cm triad


def extract_frame(mp4_path: Path, out_png: Path, frame_index: int = 0) -> None:
    """Pull a single frame from a decoded MP4 to PNG."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4_path),
         "-vf", f"select=eq(n\\,{frame_index})", "-vframes", "1", str(out_png)],
        check=True, capture_output=True,
    )


def _project(pts_base, R, t, K):
    """base-frame points (N,3) -> pixels (N,2) + camera-space z (for cull)."""
    cam = (R.T @ (pts_base - t).T).T          # X_cam = R^T (X_base - t)
    uv = (K @ cam.T).T
    return uv[:, :2] / uv[:, 2:3], cam[:, 2]


def draw_overlay(png_path: Path, camera_calib: dict, bimanual_transform, out_png: Path) -> bool:
    """Draw arm-base axis triads onto png_path using this camera's calibration.

    Returns True if an overlay was drawn (camera has usable extrinsics), else False.
    """
    import cv2

    ext = camera_calib.get("extrinsics")
    intr = camera_calib.get("intrinsics", {})
    if not ext or "camera_matrix" not in intr:
        return False  # wrist cameras are hand-eye (no scene extrinsics) — skip

    R = np.array(ext["rotation_matrix"], float)
    t = np.array(ext["translation_vector"], float)
    K = np.array(intr["camera_matrix"], float)

    img = cv2.imread(str(png_path))
    if img is None:
        return False
    H, W = img.shape[:2]
    cw, ch = intr.get("image_size", [W, H])
    Ks = K.copy()
    Ks[0] *= W / cw
    Ks[1] *= H / ch

    axes = np.array([[0, 0, 0], [_AXIS_LEN_M, 0, 0], [0, _AXIS_LEN_M, 0], [0, 0, _AXIS_LEN_M]], float)

    frames = [("left_base", axes)]
    if bimanual_transform is not None:
        T = np.array(bimanual_transform, float)  # right-frame pts -> left frame
        axes_r = (T @ np.c_[axes, np.ones(4)].T).T[:, :3]
        frames.append(("right_base", axes_r))

    drew = False
    for label, pts in frames:
        uv, z = _project(pts, R, t, Ks)
        if (z <= 0).any():
            continue  # behind the camera
        o = tuple(uv[0].astype(int))
        # Only draw if the origin is within (a margin around) the frame.
        if not (-W < o[0] < 2 * W and -H < o[1] < 2 * H):
            continue
        for i, col in zip((1, 2, 3), _AXIS_COLORS):
            cv2.line(img, o, tuple(uv[i].astype(int)), col, 3, cv2.LINE_AA)
        cv2.circle(img, o, 5, (0, 255, 255), -1)
        cv2.putText(img, label, (o[0] + 8, o[1] - 8), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (0, 255, 255), 2, cv2.LINE_AA)
        drew = True

    if drew:
        # Encode explicitly rather than cv2.imwrite(path): the cache writes to a
        # randomized temp filename whose extension OpenCV can't use to pick a codec.
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return False
        Path(out_png).write_bytes(buf.tobytes())
    return drew
