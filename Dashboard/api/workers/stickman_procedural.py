"""Procedural 2D stickman renderer for ContentFactory.

CPU-only, ~0 VRAM, fully local & free. Draws each frame with Pillow (PIL) as a
skeletal stick figure (head + torso + 2 arms + 2 legs as line segments between
joints), animated by interpolating between a small set of keyframed poses. The
PNG frames are muxed to an mp4 with the project's FFmpeg.

This is the v1 baseline for render_model = "stickman-procedural". It is a sibling
of the Blender renderer (blender/stickman_render.py) and is INTERCHANGEABLE with
it: like the Blender path, it produces a SILENT video-only clip at the pipeline's
resolution/fps/codec (libx264 / yuv420p). The downstream footage assembler
(_footage_scene_clip) adds the voiceover audio and karaoke captions, so this
renderer deliberately does NOT burn captions or mux audio — that matches the
Blender path and keeps the two stickman modes drop-in equivalent.

Public entry point:
    render_clip(out_path, duration_s, width, height, fps=30, ffmpeg_bin="ffmpeg")

The rig and the pose table are intentionally simple and easy to extend later:
add named poses to POSES and sequence them in a Clip's `timeline`.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from dataclasses import dataclass

from PIL import Image, ImageDraw

# --- Rig -------------------------------------------------------------------
#
# The skeleton is defined in a normalized "stage" coordinate space: x,y in
# roughly [-1, 1], y UP (math convention). It is mapped to image pixels at draw
# time (y is flipped, because image y grows downward). A pose is a set of JOINT
# ANGLES (radians) for the limb segments; forward kinematics turns the angles
# into joint positions, which are drawn as thick lines + a head circle.
#
# Bone lengths (stage units). Tuned so the whole figure fits a 9:16 frame with
# margin. Angles are measured so that 0 = straight down (-y) for limbs and the
# torso, matching an idle standing pose; positive angle swings a limb forward
# (+x) for the camera-facing view.

HIP_Y = -0.15          # pelvis height (stage units, y-up)
TORSO_LEN = 0.62       # pelvis -> neck
NECK_TO_HEAD = 0.16    # neck -> head center
HEAD_R = 0.13          # head radius
UPPER_ARM = 0.26
LOWER_ARM = 0.24
UPPER_LEG = 0.30
LOWER_LEG = 0.30
SHOULDER_HALF = 0.0    # shoulders sit at the neck (single-line torso); kept for clarity
HIP_HALF = 0.07        # hips split left/right by this much in x


# A Pose is the joint angles that fully determine the skeleton. Angles are in
# radians. Convention: an angle rotates a bone away from "straight down". For
# arms/legs, two angles (hip/shoulder swing + knee/elbow bend) per limb.
@dataclass
class Pose:
    torso_lean: float = 0.0       # whole-body lean about the pelvis (+ leans forward/+x)
    head_tilt: float = 0.0        # head nod relative to torso
    # arms: shoulder swing (+ forward) and elbow bend (+ flexes the forearm forward)
    arm_l_shoulder: float = 0.18
    arm_l_elbow: float = 0.10
    arm_r_shoulder: float = -0.18
    arm_r_elbow: float = 0.10
    # legs: hip swing (+ forward) and knee bend (+ flexes the shin backward)
    leg_l_hip: float = 0.10
    leg_l_knee: float = 0.05
    leg_r_hip: float = -0.10
    leg_r_knee: float = 0.05

    def lerp(self, other: "Pose", t: float) -> "Pose":
        """Linear interpolation between this pose and another (t in [0,1])."""
        a, b = self, other
        return Pose(
            torso_lean=_mix(a.torso_lean, b.torso_lean, t),
            head_tilt=_mix(a.head_tilt, b.head_tilt, t),
            arm_l_shoulder=_mix(a.arm_l_shoulder, b.arm_l_shoulder, t),
            arm_l_elbow=_mix(a.arm_l_elbow, b.arm_l_elbow, t),
            arm_r_shoulder=_mix(a.arm_r_shoulder, b.arm_r_shoulder, t),
            arm_r_elbow=_mix(a.arm_r_elbow, b.arm_r_elbow, t),
            leg_l_hip=_mix(a.leg_l_hip, b.leg_l_hip, t),
            leg_l_knee=_mix(a.leg_l_knee, b.leg_l_knee, t),
            leg_r_hip=_mix(a.leg_r_hip, b.leg_r_hip, t),
            leg_r_knee=_mix(a.leg_r_knee, b.leg_r_knee, t),
        )


def _mix(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


# --- Pose library (easy to extend) -----------------------------------------
#
# Add new named poses here; reference them by name in a Clip timeline below.
POSES: dict[str, Pose] = {
    "idle": Pose(),
    "idle_sway_l": Pose(torso_lean=-0.05, head_tilt=-0.03,
                        arm_l_shoulder=0.22, arm_r_shoulder=-0.14,
                        leg_l_hip=0.06, leg_r_hip=-0.06),
    "idle_sway_r": Pose(torso_lean=0.05, head_tilt=0.03,
                        arm_l_shoulder=0.14, arm_r_shoulder=-0.22,
                        leg_l_hip=-0.06, leg_r_hip=0.06),
    # a friendly raised-arm gesture (right arm up + bent), as if presenting
    "gesture": Pose(torso_lean=0.02, head_tilt=0.04,
                    arm_l_shoulder=0.20, arm_l_elbow=0.15,
                    arm_r_shoulder=-1.15, arm_r_elbow=0.55,
                    leg_l_hip=0.08, leg_r_hip=-0.08),
    # walk-cycle extremes (opposite arm / opposite leg)
    "walk_a": Pose(arm_l_shoulder=0.55, arm_l_elbow=0.25,
                   arm_r_shoulder=-0.55, arm_r_elbow=0.25,
                   leg_l_hip=0.45, leg_l_knee=0.10,
                   leg_r_hip=-0.45, leg_r_knee=0.55),
    "walk_b": Pose(arm_l_shoulder=-0.55, arm_l_elbow=0.25,
                   arm_r_shoulder=0.55, arm_r_elbow=0.25,
                   leg_l_hip=-0.45, leg_l_knee=0.55,
                   leg_r_hip=0.45, leg_r_knee=0.10),
}


# A timeline is a list of (pose_name, hold_seconds) waypoints. The animator
# eases between consecutive waypoints. The whole timeline is looped to fill the
# requested clip duration, so it works for any scene length.
@dataclass
class Clip:
    timeline: list[tuple[str, float]]


# Default v1 motion: gentle idle sway + an occasional gesture. Calm, readable,
# loops seamlessly (ends where it began).
DEFAULT_CLIP = Clip(timeline=[
    ("idle", 0.6),
    ("idle_sway_l", 0.9),
    ("idle", 0.6),
    ("gesture", 1.0),
    ("idle", 0.5),
    ("idle_sway_r", 0.9),
    ("idle", 0.6),
])

# A walk loop, available for later use / extension.
WALK_CLIP = Clip(timeline=[("walk_a", 0.4), ("walk_b", 0.4)])


def _ease(t: float) -> float:
    """Smoothstep easing for natural-looking interpolation."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def pose_at(clip: Clip, t: float) -> Pose:
    """Return the interpolated pose at time `t` (seconds) within a looping clip."""
    total = sum(d for _, d in clip.timeline)
    if total <= 0:
        return POSES.get(clip.timeline[0][0], Pose())
    tt = t % total
    acc = 0.0
    for i, (name, dur) in enumerate(clip.timeline):
        if tt < acc + dur or i == len(clip.timeline) - 1:
            nxt_name = clip.timeline[(i + 1) % len(clip.timeline)][0]
            local = _ease((tt - acc) / dur) if dur > 0 else 0.0
            return POSES[name].lerp(POSES[nxt_name], local)
        acc += dur
    return POSES[clip.timeline[-1][0]]


# --- Forward kinematics + drawing ------------------------------------------

def _solve_joints(p: Pose) -> dict[str, tuple[float, float]]:
    """Turn a Pose into stage-space (x, y) joint positions (y up)."""
    # Pelvis is the root.
    pelvis = (0.0, HIP_Y)
    # Torso leans about the pelvis; neck is up + lean.
    neck = (pelvis[0] + math.sin(p.torso_lean) * TORSO_LEN,
            pelvis[1] + math.cos(p.torso_lean) * TORSO_LEN)
    head = (neck[0] + math.sin(p.torso_lean + p.head_tilt) * NECK_TO_HEAD,
            neck[1] + math.cos(p.torso_lean + p.head_tilt) * NECK_TO_HEAD)

    def limb(joint, a_root, a_bend, l1, l2, sign):
        """Two-segment limb hanging from `joint`. Angles measured from straight-down.
        `sign` flips the bend direction for L vs R so knees/elbows fold naturally."""
        base = p.torso_lean  # limbs inherit the body lean
        ang1 = base + a_root
        mid = (joint[0] + math.sin(ang1) * l1, joint[1] - math.cos(ang1) * l1)
        ang2 = ang1 + sign * a_bend
        end = (mid[0] + math.sin(ang2) * l2, mid[1] - math.cos(ang2) * l2)
        return mid, end

    shoulder = neck
    hip_l = (pelvis[0] - HIP_HALF, pelvis[1])
    hip_r = (pelvis[0] + HIP_HALF, pelvis[1])

    elbow_l, hand_l = limb(shoulder, p.arm_l_shoulder, p.arm_l_elbow, UPPER_ARM, LOWER_ARM, +1)
    elbow_r, hand_r = limb(shoulder, p.arm_r_shoulder, p.arm_r_elbow, UPPER_ARM, LOWER_ARM, -1)
    knee_l, foot_l = limb(hip_l, p.leg_l_hip, p.leg_l_knee, UPPER_LEG, LOWER_LEG, -1)
    knee_r, foot_r = limb(hip_r, p.leg_r_hip, p.leg_r_knee, UPPER_LEG, LOWER_LEG, +1)

    return {
        "pelvis": pelvis, "neck": neck, "head": head,
        "hip_l": hip_l, "hip_r": hip_r,
        "elbow_l": elbow_l, "hand_l": hand_l,
        "elbow_r": elbow_r, "hand_r": hand_r,
        "knee_l": knee_l, "foot_l": foot_l,
        "knee_r": knee_r, "foot_r": foot_r,
    }


def _draw_frame(p: Pose, width: int, height: int) -> Image.Image:
    """Render one frame of the skeleton to a PIL image."""
    img = Image.new("RGB", (width, height), (245, 247, 250))  # light clean background
    d = ImageDraw.Draw(img)

    # Map stage coords (x,y in ~[-1,1], y-up) to pixels. The figure stands a bit
    # below center so the head + raised gestures stay in frame.
    scale = min(width, height) * 0.42
    cx = width * 0.5
    cy = height * 0.56  # vertical anchor for the pelvis-ish midpoint

    def px(pt):
        return (cx + pt[0] * scale, cy - pt[1] * scale)

    j = _solve_joints(p)
    line_color = (26, 28, 34)
    lw = max(6, width // 110)

    def seg(a, b):
        d.line([px(j[a]), px(j[b])], fill=line_color, width=lw, joint="curve")

    # ground line (subtle), for grounding
    gy = cy - (HIP_Y - UPPER_LEG - LOWER_LEG) * scale
    d.line([(width * 0.12, gy), (width * 0.88, gy)], fill=(210, 214, 220), width=max(3, lw // 2))

    # torso
    seg("pelvis", "neck")
    # arms
    seg("neck", "elbow_l"); seg("elbow_l", "hand_l")
    seg("neck", "elbow_r"); seg("elbow_r", "hand_r")
    # legs
    seg("pelvis", "hip_l"); seg("hip_l", "knee_l"); seg("knee_l", "foot_l")
    seg("pelvis", "hip_r"); seg("hip_r", "knee_r"); seg("knee_r", "foot_r")

    # head (filled circle with a subtle outline so it reads on the light bg)
    hx, hy = px(j["head"])
    r = HEAD_R * scale
    d.ellipse([hx - r, hy - r, hx + r, hy + r], fill=line_color, outline=line_color)

    # rounded "caps" on hands/feet so joints don't look chopped
    for pt_name in ("hand_l", "hand_r", "foot_l", "foot_r"):
        ex, ey = px(j[pt_name])
        cr = lw * 0.6
        d.ellipse([ex - cr, ey - cr, ex + cr, ey + cr], fill=line_color)

    return img


# --- Public render entry ----------------------------------------------------

def render_clip(out_path: str, duration_s: float, width: int, height: int,
                fps: int = 30, ffmpeg_bin: str = "ffmpeg",
                clip: Clip | None = None) -> str:
    """Render a procedural stickman clip of `duration_s` seconds to `out_path`.

    Produces a SILENT mp4 (libx264 / yuv420p) at the given resolution/fps —
    byte-compatible with the rest of the pipeline and interchangeable with the
    Blender stickman path. Returns out_path.
    """
    clip = clip or DEFAULT_CLIP
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    n_frames = max(1, round((duration_s or 3.0) * fps))

    frames_dir = tempfile.mkdtemp(prefix="stickman_proc_")
    try:
        for i in range(n_frames):
            t = i / fps
            frame = _draw_frame(pose_at(clip, t), width, height)
            frame.save(os.path.join(frames_dir, f"frame_{i:05d}.png"))

        proc = subprocess.run(
            [ffmpeg_bin, "-y", "-framerate", str(fps),
             "-i", os.path.join(frames_dir, "frame_%05d.png"),
             "-frames:v", str(n_frames),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
             "-preset", "veryfast", "-crf", "20", out_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0 or not os.path.isfile(out_path):
            raise RuntimeError(f"ffmpeg mux failed: {(proc.stderr or '')[-800:]}")
        return out_path
    finally:
        # best-effort cleanup of the temp PNG sequence
        try:
            for f in os.listdir(frames_dir):
                os.remove(os.path.join(frames_dir, f))
            os.rmdir(frames_dir)
        except OSError:
            pass


if __name__ == "__main__":
    import sys
    _out = sys.argv[1] if len(sys.argv) > 1 else "stickman_proc.mp4"
    _dur = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    _w = int(sys.argv[3]) if len(sys.argv) > 3 else 1080
    _h = int(sys.argv[4]) if len(sys.argv) > 4 else 1920
    _fps = int(sys.argv[5]) if len(sys.argv) > 5 else 30
    _ff = os.environ.get("FFMPEG_BIN", "ffmpeg")
    render_clip(_out, _dur, _w, _h, _fps, _ff)
    print(f"[stickman-procedural] {_out} ({_dur}s, {_w}x{_h}@{_fps})")
