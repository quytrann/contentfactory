"""Headless Blender stickman renderer for ContentFactory.

Run:
    blender -b -P stickman_render.py -- <out.mp4> [frames] [width] [height] [fps]

Builds a simple stick figure (sphere head + cylinder torso/limbs, Blender is Z-up),
gives it a looping marching animation, and renders an .mp4 with the Workbench engine
(fast, deterministic, needs no GPU lighting). Defaults: 60 frames, 1080x1920, 30 fps.

This is the baseline stickman renderer — refine the rig/animation here; the pipeline
calls it via subprocess for render_model = "stickman-blender".
"""

import math
import os
import subprocess
import sys
import tempfile

import bpy
from mathutils import Matrix


def _args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    out = argv[0] if len(argv) > 0 else "//stickman.mp4"
    frames = int(argv[1]) if len(argv) > 1 else 60
    width = int(argv[2]) if len(argv) > 2 else 1080
    height = int(argv[3]) if len(argv) > 3 else 1920
    fps = int(argv[4]) if len(argv) > 4 else 30
    return out, frames, width, height, fps


def _clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.cameras):
        for block in list(coll):
            coll.remove(block)


def _limb(name, length, joint, radius=0.06):
    """A bone-like cylinder whose ORIGIN sits at `joint` (the top) and whose geometry
    hangs down -Z by `length`. Rotating it about its origin swings the limb."""
    bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=length, vertices=12)
    obj = bpy.context.active_object
    obj.name = name
    obj.data.transform(Matrix.Translation((0, 0, -length / 2)))  # joint at origin, hang down
    obj.location = joint
    return obj


def _ball(name, r, loc):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=r, location=loc, segments=24, ring_count=12)
    obj = bpy.context.active_object
    obj.name = name
    return obj


def _swing(obj, phase, frames, amp=0.55, cycles=2):
    """Keyframe a forward/back swing (rotation about X) for a marching loop."""
    for f in range(1, frames + 1):
        t = (f - 1) / max(1, frames - 1)
        obj.rotation_euler = (amp * math.sin(2 * math.pi * cycles * t + phase), 0.0, 0.0)
        obj.keyframe_insert(data_path="rotation_euler", frame=f)


def build_stickman(frames):
    shoulder_z, hip_z = 1.55, 0.95
    # torso (static), head
    torso = _limb("torso", shoulder_z - hip_z, joint=(0, 0, shoulder_z), radius=0.07)
    _ball("head", 0.16, (0, 0, shoulder_z + 0.16))
    # arms from the shoulders, legs from the hips
    ra = _limb("arm_R", 0.55, joint=(0.18, 0, shoulder_z))
    la = _limb("arm_L", 0.55, joint=(-0.18, 0, shoulder_z))
    rl = _limb("leg_R", 0.85, joint=(0.10, 0, hip_z), radius=0.07)
    ll = _limb("leg_L", 0.85, joint=(-0.10, 0, hip_z), radius=0.07)
    # opposite-arm/opposite-leg marching gait
    _swing(ra, phase=0.0, frames=frames)
    _swing(ll, phase=0.0, frames=frames)
    _swing(la, phase=math.pi, frames=frames)
    _swing(rl, phase=math.pi, frames=frames)
    return torso


def setup_camera():
    cam_data = bpy.data.cameras.new("Cam")
    cam = bpy.data.objects.new("Cam", cam_data)
    bpy.context.scene.collection.objects.link(cam)
    cam.location = (0.0, -4.2, 1.25)
    cam.rotation_euler = (math.radians(90), 0.0, 0.0)  # look toward +Y at the figure
    bpy.context.scene.camera = cam


def setup_scene(scene, frames, width, height, fps, frames_dir):
    # White background, dark flat-shaded figure (clean silhouette) via Workbench.
    world = bpy.data.worlds[0] if bpy.data.worlds else bpy.data.worlds.new("World")
    world.use_nodes = False
    world.color = (1.0, 1.0, 1.0)
    scene.world = world

    scene.render.engine = "BLENDER_WORKBENCH"
    shading = scene.display.shading
    shading.light = "FLAT"
    shading.color_type = "SINGLE"
    shading.single_color = (0.10, 0.10, 0.12)

    scene.frame_start = 1
    scene.frame_end = frames
    scene.render.fps = fps
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100

    # This Blender build exposes no FFMPEG file format, so render a PNG sequence and
    # mux it to mp4 afterwards with the project's external FFmpeg (FFMPEG_BIN).
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = os.path.join(frames_dir, "frame_")


def mux_to_mp4(frames_dir, out, fps):
    ffmpeg = os.environ.get("FFMPEG_BIN", "ffmpeg")
    subprocess.run(
        [ffmpeg, "-y", "-framerate", str(fps),
         "-i", os.path.join(frames_dir, "frame_%04d.png"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", out],
        check=True,
    )


def main():
    out, frames, width, height, fps = _args()
    _clear_scene()
    build_stickman(frames)
    setup_camera()
    frames_dir = tempfile.mkdtemp(prefix="stickman_")
    setup_scene(bpy.context.scene, frames, width, height, fps, frames_dir)
    bpy.ops.render.render(animation=True)
    mux_to_mp4(frames_dir, out, fps)
    print(f"[stickman] rendered {frames} frames -> {out}")


if __name__ == "__main__":
    main()
