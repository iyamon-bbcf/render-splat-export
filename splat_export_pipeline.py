# SPDX-License-Identifier: GPL-3.0-or-later
"""
Splat Export Pipeline (UI addon)

One panel covering the full Gaussian Splat prep pipeline for a single timeline
where the active camera switches via bound markers (Marker > Bind Camera to Markers):

  1. Export Camera Poses  -> transforms.json
  2. Setup EXR Depth Output (one-time compositor wiring, doesn't touch color output)
  3. Generate Point Cloud  -> points.ply (run AFTER Deadline has rendered color+depth)

INSTALL:
  Edit > Preferences > Add-ons > Install... > select this file > enable checkbox.
  Panel appears in the 3D Viewport sidebar (press N) under "Splat Export".

OR RUN DIRECTLY:
  Scripting tab > open this file > Run Script. Panel appears the same way,
  just won't persist after closing the file.

WHAT IT DOES NOT DO:
  - Doesn't render anything -- submit to Deadline separately with the same
    Frame Start/End/Step shown in this panel, using your normal Blender submitter.
"""

import bpy
import os
import json
import math
import glob
import subprocess
import numpy as np
from mathutils import Matrix

bl_info = {
    "name": "Splat Export Pipeline",
    "author": "iyamon-bbcf",
    "version": (2, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Splat Export",
    "description": "Export camera poses, set up depth output, and build a point cloud for Gaussian Splat training",
    "category": "Import-Export",
}

# Blender camera (-Z forward, +Y up) -> OpenCV/COLMAP camera (+Z forward, +Y down)
BLENDER_TO_CV = Matrix((
    (1,  0,  0, 0),
    (0, -1,  0, 0),
    (0,  0, -1, 0),
    (0,  0,  0, 1),
))


def resolve_output_dir(props, report_fn):
    """
    Shared, robust output-dir resolver used by EVERY operator in this addon.
    Handles: real absolute paths, Blender's '//' relative-to-blend-file marker,
    and the broken '\\something\' pattern that shows up on Windows when a
    relative default gets mangled (not valid UNC, not valid '//' either).
    Returns the resolved absolute path, or None if report_fn was already called
    with an error (caller should return {'CANCELLED'} in that case).
    """
    import re
    raw = props.output_dir.strip()

    broken_unc = re.match(r'^\\\\([^\\]+)\\?$', raw)
    if broken_unc:
        if not bpy.data.filepath:
            report_fn({'ERROR'},
                      f"Output path '{raw}' isn't valid and the .blend file isn't saved, so it can't "
                      f"be resolved relative to it either. Type a full absolute path into Output Folder, "
                      f"e.g. D:\\splat_export\\, or save the file first.")
            return None
        raw = "//" + broken_unc.group(1) + "/"

    if raw.startswith("//") and not bpy.data.filepath:
        report_fn({'ERROR'},
                   "Output Folder is a relative path ('//...') but the .blend file hasn't been saved, "
                   "so it can't be resolved. Type a full absolute path into Output Folder, or save the file first.")
        return None

    resolved = bpy.path.abspath(raw)
    if not resolved or resolved.strip("\\/") == "":
        report_fn({'ERROR'}, f"Output Folder resolved to something invalid: '{resolved}'. "
                              f"Type a full absolute path directly into the field.")
        return None

    return resolved


def get_shared_intrinsics(cam_data, scene):
    render = scene.render
    width = render.resolution_x
    height = render.resolution_y
    focal_mm = cam_data.lens
    sensor_width_mm = cam_data.sensor_width
    camera_angle_x = 2 * math.atan(sensor_width_mm / (2 * focal_mm))
    return width, height, camera_angle_x


class SPLAT_OT_export_poses(bpy.types.Operator):
    bl_idname = "splat.export_poses"
    bl_label = "Export Camera Poses (transforms.json)"
    bl_description = "Steps through the frame range, resolves the active camera per frame via markers, writes transforms.json"

    def execute(self, context):
        scene = context.scene
        props = scene.splat_export_props

        output_dir = resolve_output_dir(props, self.report)
        if output_dir is None:
            return {'CANCELLED'}
        os.makedirs(output_dir, exist_ok=True)

        bound_markers = [m for m in scene.timeline_markers if m.camera is not None]
        if not bound_markers:
            self.report({'ERROR'}, "No camera-bound markers found. Marker > Bind Camera to Markers first.")
            return {'CANCELLED'}

        frame_start = props.frame_start
        frame_end = props.frame_end
        stride = props.stride
        target_frames = list(range(frame_start, frame_end + 1, stride))
        if not target_frames:
            self.report({'ERROR'}, "Frame range/stride produced zero frames -- check Start/End/Step")
            return {'CANCELLED'}

        orig_frame = scene.frame_current  # restore afterward, don't leave the timeline scrubbed

        scene.frame_set(target_frames[0])
        if scene.camera is None:
            self.report({'ERROR'}, f"No active camera resolved at frame {target_frames[0]}")
            scene.frame_set(orig_frame)
            return {'CANCELLED'}
        width, height, camera_angle_x = get_shared_intrinsics(scene.camera.data, scene)

        frames = []
        camera_usage = {}
        for f in target_frames:
            scene.frame_set(f)
            cam = scene.camera
            if cam is None:
                self.report({'WARNING'}, f"No active camera at frame {f}, skipped")
                continue

            mat = cam.matrix_world @ BLENDER_TO_CV
            frames.append({
                "file_path": f"./frames/frame_{f:04d}",
                "transform_matrix": [list(row) for row in mat],
                "blender_frame": f,
                "camera_name": cam.name,
            })
            camera_usage[cam.name] = camera_usage.get(cam.name, 0) + 1

        scene.frame_set(orig_frame)

        out = {
            "camera_angle_x": camera_angle_x,
            "w": width,
            "h": height,
            "frames": frames,
        }
        json_path = os.path.join(output_dir, "transforms.json")
        with open(json_path, "w") as fp:
            json.dump(out, fp, indent=2)

        summary = ", ".join(f"{k}: {v}" for k, v in camera_usage.items())
        self.report({'INFO'}, f"Wrote {len(frames)} poses -> {json_path}  ({summary})")
        return {'FINISHED'}


class SPLAT_OT_setup_depth_output(bpy.types.Operator):
    bl_idname = "splat.setup_depth_output"
    bl_label = "Setup EXR Depth Output (One-Time)"
    bl_description = (
        "Adds a Z-depth EXR output to the compositor alongside your existing color render. "
        "Safe to run once -- does not touch or disable your normal color output."
    )

    def execute(self, context):
        scene = context.scene
        props = scene.splat_export_props

        output_dir = resolve_output_dir(props, self.report)
        if output_dir is None:
            return {'CANCELLED'}

        # Enable the Z pass so depth data actually exists to output
        view_layer = context.view_layer
        view_layer.use_pass_z = True

        scene.use_nodes = True

        # Blender 4.x moved the compositor from scene.node_tree to a node GROUP
        # referenced by scene.compositing_node_group. Support both so this works
        # regardless of version.
        if hasattr(scene, "node_tree") and scene.node_tree is not None:
            tree = scene.node_tree
        elif hasattr(scene, "compositing_node_group"):
            if scene.compositing_node_group is None:
                scene.compositing_node_group = bpy.data.node_groups.new("Compositor Nodes", 'CompositorNodeTree')
            tree = scene.compositing_node_group
        else:
            self.report({'ERROR'}, "Could not find a compositor node tree for this Blender version.")
            return {'CANCELLED'}

        # Find (or create) the Render Layers node -- reuse existing if present so we
        # don't duplicate/disturb anything already wired up.
        rl = next((n for n in tree.nodes if n.type == 'R_LAYERS'), None)
        if rl is None:
            rl = tree.nodes.new("CompositorNodeRLayers")
            rl.location = (0, 0)

        # Make sure a Composite node exists and is connected to Image, so your
        # existing color output (saved via Output Properties) keeps working exactly
        # as before. If one already exists and is already linked, leave it alone.
        composite = next((n for n in tree.nodes if n.type == 'COMPOSITE'), None)
        if composite is None:
            composite = tree.nodes.new("CompositorNodeComposite")
            composite.location = (600, 200)
        image_already_linked = any(
            link.to_node == composite and link.to_socket.name == "Image"
            for link in tree.links
        )
        if not image_already_linked:
            tree.links.new(rl.outputs["Image"], composite.inputs["Image"])

        # Add the depth File Output node (skip if one's already there from a previous run)
        existing_depth_out = next(
            (n for n in tree.nodes if n.type == 'OUTPUT_FILE' and n.label == "Splat Depth Output"),
            None
        )
        if existing_depth_out is None:
            depth_out = tree.nodes.new("CompositorNodeOutputFile")
            depth_out.label = "Splat Depth Output"
            depth_out.location = (400, -200)
            tree.links.new(rl.outputs["Depth"], depth_out.inputs[0])
        else:
            depth_out = existing_depth_out

        # Config verified working in-editor -- applied every run so it self-heals if
        # someone fiddles with the node afterward.
        depth_out.base_path = os.path.join(output_dir, "frames")
        depth_out.file_slots[0].path = "depth_####"   # explicit padding -> depth_1802.exr, no stray labels
        depth_out.format.file_format = 'OPEN_EXR'
        depth_out.format.color_mode = 'BW'             # single channel, not RGBA
        depth_out.format.color_depth = '32'            # Float (Full)

        # "Save as Render" and color space live in slightly different places across
        # Blender versions -- set defensively, skip silently if the property doesn't
        # exist on this version rather than erroring the whole operator.
        try:
            depth_out.format.color_management = 'OVERRIDE'
            depth_out.format.view_settings.view_transform = 'Standard'
        except AttributeError:
            pass
        try:
            depth_out.format.linear_colorspace_settings.name = 'Non-Color'
        except AttributeError:
            pass

        self.report({'INFO'}, "Depth EXR output configured. Save the file, then submit to Deadline as normal.")
        return {'FINISHED'}


class SPLAT_OT_generate_pointcloud(bpy.types.Operator):
    bl_idname = "splat.generate_pointcloud"
    bl_label = "Generate Point Cloud from Depth EXRs"
    bl_description = (
        "Reads transforms.json + every matching rendered depth EXR, unprojects each to "
        "world-space points, merges them, and writes points.ply. Run this AFTER Deadline "
        "has finished rendering."
    )

    @staticmethod
    def build_depth_lookup(frames_dir):
        """
        List the folder ONCE and index by frame number, instead of calling glob()
        per-frame -- over a network drive, 400 separate directory scans is what
        was causing multi-minute-to-hour hangs. This is a single listdir() call.
        """
        import re
        lookup = {}
        for fname in os.listdir(frames_dir):
            if "depth" not in fname.lower() or not fname.lower().endswith(".exr"):
                continue
            m = re.search(r'(\d{3,6})', fname)
            if m:
                frame_num = int(m.group(1))
                lookup.setdefault(frame_num, os.path.join(frames_dir, fname))
        return lookup

    @staticmethod
    def read_depth_exr_bpy(path):
        img = bpy.data.images.load(path, check_existing=False)
        width, height = img.size
        pixels = np.array(img.pixels[:], dtype=np.float32).reshape(height, width, 4)
        depth = np.flipud(pixels[:, :, 0])  # Blender image rows are bottom-up
        bpy.data.images.remove(img)
        return depth

    @staticmethod
    def unproject(depth, c2w, fx, fy, cx, cy, depth_clip):
        h, w = depth.shape
        us, vs = np.meshgrid(np.arange(w), np.arange(h))
        valid = depth < depth_clip
        z = depth[valid]
        u = us[valid]
        v = vs[valid]
        x_cam = (u - cx) / fx * z
        y_cam = (v - cy) / fy * z
        points_cam = np.stack([x_cam, y_cam, z, np.ones_like(z)], axis=1)
        return (c2w @ points_cam.T).T[:, :3]

    def execute(self, context):
        scene = context.scene
        props = scene.splat_export_props

        output_dir = resolve_output_dir(props, self.report)
        if output_dir is None:
            return {'CANCELLED'}
        frames_dir = os.path.join(output_dir, "frames")

        json_path = os.path.join(output_dir, "transforms.json")
        if not os.path.exists(json_path):
            self.report({'ERROR'}, f"transforms.json not found at {json_path} -- export poses first.")
            return {'CANCELLED'}
        with open(json_path) as f:
            data = json.load(f)

        w, h, camera_angle_x = data["w"], data["h"], data["camera_angle_x"]
        fx = w / (2 * math.tan(camera_angle_x / 2))
        fy = fx
        cx, cy = w / 2, h / 2

        all_points = []
        missing = []
        depth_lookup = self.build_depth_lookup(frames_dir)
        print(f"[generate_pointcloud] Found {len(depth_lookup)} depth EXRs in {frames_dir}", flush=True)

        total = len(data["frames"])
        progress_log_path = os.path.join(output_dir, "pointcloud_progress.log")
        wm = context.window_manager
        wm.progress_begin(0, total)

        def write_progress(i, note=""):
            # Overwrites the file each time -- open it in Notepad and hit Ctrl+S-less
            # refresh (or just reopen) to check status without touching Blender's
            # frozen UI at all. Flushed immediately so it's accurate even mid-hang.
            try:
                with open(progress_log_path, "w") as pf:
                    pf.write(f"Processed {i}/{total} frames.\n")
                    pf.write(f"Depth EXRs found on disk: {len(depth_lookup)}\n")
                    if note:
                        pf.write(note + "\n")
            except Exception:
                pass  # never let logging itself break the operator

        write_progress(0, "Starting...")

        for i, entry in enumerate(data["frames"]):
            frame_num = entry.get("blender_frame")
            depth_path = depth_lookup.get(frame_num)
            if depth_path is None:
                missing.append(frame_num)
                continue

            depth = self.read_depth_exr_bpy(depth_path)
            c2w = np.array(entry["transform_matrix"], dtype=np.float64)
            points = self.unproject(depth, c2w, fx, fy, cx, cy, props.depth_clip)

            if len(points) > props.points_per_frame:
                idx = np.random.choice(len(points), props.points_per_frame, replace=False)
                points = points[idx]

            all_points.append(points)
            wm.progress_update(i + 1)
            if (i + 1) % 10 == 0:
                write_progress(i + 1)
                print(f"[generate_pointcloud] Processed {i + 1}/{total} frames...", flush=True)

        if not all_points:
            wm.progress_end()
            write_progress(total, "FAILED: no depth EXRs matched any transforms.json frame.")
            self.report({'ERROR'}, f"No depth EXRs found in {frames_dir} matching any transforms.json frame. "
                                    f"Has Deadline finished rendering?")
            return {'CANCELLED'}

        wm.progress_end()

        merged = np.concatenate(all_points, axis=0)
        if len(merged) > props.max_points_total:
            idx = np.random.choice(len(merged), props.max_points_total, replace=False)
            merged = merged[idx]

        ply_path = os.path.join(output_dir, "points.ply")
        with open(ply_path, "w") as f:
            f.write("ply\nformat ascii 1.0\n")
            f.write(f"element vertex {len(merged)}\n")
            f.write("property float x\nproperty float y\nproperty float z\n")
            f.write("end_header\n")
            for p in merged:
                f.write(f"{p[0]} {p[1]} {p[2]}\n")

        msg = f"Wrote {len(merged)} points from {len(all_points)} frames -> {ply_path}"
        if missing:
            msg += f"  ({len(missing)} frames had no matching depth EXR, e.g. {missing[:5]})"
        write_progress(total, f"DONE. {msg}")
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class SPLAT_OT_export_colmap(bpy.types.Operator):
    bl_idname = "splat.export_colmap"
    bl_label = "Export COLMAP Format (for Postshot)"
    bl_description = (
        "Converts transforms.json + points.ply into a COLMAP text-format folder "
        "(cameras/rigs/frames/images/points3D.txt), ready to drag into Postshot."
    )

    @staticmethod
    def rotmat_to_quaternion(R):
        trace = R[0, 0] + R[1, 1] + R[2, 2]
        if trace > 0:
            s = 0.5 / np.sqrt(trace + 1.0)
            qw = 0.25 / s
            qx = (R[2, 1] - R[1, 2]) * s
            qy = (R[0, 2] - R[2, 0]) * s
            qz = (R[1, 0] - R[0, 1]) * s
        elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
            s = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
            qw = (R[2, 1] - R[1, 2]) / s
            qx = 0.25 * s
            qy = (R[0, 1] + R[1, 0]) / s
            qz = (R[0, 2] + R[2, 0]) / s
        elif R[1, 1] > R[2, 2]:
            s = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
            qw = (R[0, 2] - R[2, 0]) / s
            qx = (R[0, 1] + R[1, 0]) / s
            qy = 0.25 * s
            qz = (R[1, 2] + R[2, 1]) / s
        else:
            s = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
            qw = (R[1, 0] - R[0, 1]) / s
            qx = (R[0, 2] + R[2, 0]) / s
            qy = (R[1, 2] + R[2, 1]) / s
            qz = 0.25 * s
        return qw, qx, qy, qz

    @staticmethod
    def find_image_for_frame(images_dir, frame_number):
        patterns = [f"*{frame_number:04d}*.png", f"*{frame_number:04d}*.jpg",
                    f"*{frame_number:05d}*.png", f"*{frame_number}*.png"]
        for pattern in patterns:
            matches = sorted(glob.glob(os.path.join(images_dir, pattern)))
            matches = [m for m in matches if "depth" not in os.path.basename(m).lower()]
            if matches:
                return matches[0]
        return None

    @staticmethod
    def read_ply_points(path):
        with open(path, "r") as f:
            lines = f.readlines()
        n_vertex = 0
        header_end = 0
        for i, line in enumerate(lines):
            if line.startswith("element vertex"):
                n_vertex = int(line.split()[-1])
            if line.strip() == "end_header":
                header_end = i + 1
                break
        points = []
        for line in lines[header_end:header_end + n_vertex]:
            x, y, z = map(float, line.split()[:3])
            points.append((x, y, z))
        return points

    def execute(self, context):
        import shutil
        scene = context.scene
        props = scene.splat_export_props

        output_dir = resolve_output_dir(props, self.report)
        if output_dir is None:
            return {'CANCELLED'}

        json_path = os.path.join(output_dir, "transforms.json")
        ply_path = os.path.join(output_dir, "points.ply")
        frames_dir = os.path.join(output_dir, "frames")
        out_dir = os.path.join(output_dir, "colmap_export")

        if not os.path.exists(json_path):
            self.report({'ERROR'}, f"transforms.json not found -- export poses first.")
            return {'CANCELLED'}
        if not os.path.exists(ply_path):
            self.report({'ERROR'}, f"points.ply not found -- generate the point cloud first.")
            return {'CANCELLED'}

        with open(json_path) as f:
            data = json.load(f)
        w, h, camera_angle_x = data["w"], data["h"], data["camera_angle_x"]
        fx = w / (2 * math.tan(camera_angle_x / 2))
        fy = fx
        cx, cy = w / 2, h / 2

        os.makedirs(out_dir, exist_ok=True)
        images_out_dir = os.path.join(out_dir, "images")
        os.makedirs(images_out_dir, exist_ok=True)

        with open(os.path.join(out_dir, "cameras.txt"), "w") as f:
            f.write("# CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n")
            f.write(f"1 PINHOLE {w} {h} {fx:.6f} {fy:.6f} {cx:.6f} {cy:.6f}\n")

        with open(os.path.join(out_dir, "rigs.txt"), "w") as f:
            f.write("# RIG_ID, NUM_SENSORS, REF_SENSOR_TYPE, REF_SENSOR_ID, SENSORS[]\n")
            f.write("1 1 CAMERA 1\n")

        images_lines = []
        frames_lines = []
        copied = 0
        missing_images = []
        for i, entry in enumerate(data["frames"], start=1):
            c2w = np.array(entry["transform_matrix"], dtype=np.float64)
            R_c2w = c2w[:3, :3]
            t_c2w = c2w[:3, 3]
            R_w2c = R_c2w.T
            t_w2c = -R_w2c @ t_c2w
            qw, qx, qy, qz = self.rotmat_to_quaternion(R_w2c)

            frame_num = entry.get("blender_frame", i)
            src_img = self.find_image_for_frame(frames_dir, frame_num)
            if src_img is None:
                missing_images.append(frame_num)
                continue

            img_name = os.path.basename(src_img)
            dst_img = os.path.join(images_out_dir, img_name)
            if not os.path.exists(dst_img):
                shutil.copy2(src_img, dst_img)
                copied += 1

            images_lines.append(f"{i} {qw:.9f} {qx:.9f} {qy:.9f} {qz:.9f} "
                                 f"{t_w2c[0]:.9f} {t_w2c[1]:.9f} {t_w2c[2]:.9f} 1 {img_name}")
            images_lines.append("")
            frames_lines.append(f"{i} 1 {qw:.9f} {qx:.9f} {qy:.9f} {qz:.9f} "
                                 f"{t_w2c[0]:.9f} {t_w2c[1]:.9f} {t_w2c[2]:.9f} 1 CAMERA 1 {i}")

        with open(os.path.join(out_dir, "images.txt"), "w") as f:
            f.write("# IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME\n")
            f.write("\n".join(images_lines) + "\n")

        with open(os.path.join(out_dir, "frames.txt"), "w") as f:
            f.write("# FRAME_ID, RIG_ID, RIG_FROM_WORLD[QW,QX,QY,QZ,TX,TY,TZ], NUM_DATA_IDS, DATA_IDS[]\n")
            f.write("\n".join(frames_lines) + "\n")

        points = self.read_ply_points(ply_path)
        with open(os.path.join(out_dir, "points3D.txt"), "w") as f:
            f.write("# POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[] (empty)\n")
            for pid, (x, y, z) in enumerate(points, start=1):
                f.write(f"{pid} {x:.6f} {y:.6f} {z:.6f} 128 128 128 1.0\n")

        msg = f"COLMAP export -> {out_dir}  ({len(images_lines)//2} poses, {copied} images copied, {len(points)} points)"
        if missing_images:
            msg += f"  WARNING: {len(missing_images)} frames missing images"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class SPLAT_OT_validate_colmap(bpy.types.Operator):
    bl_idname = "splat.validate_colmap"
    bl_label = "Validate with COLMAP"
    bl_description = "Runs 'colmap model_analyzer' on the exported folder to sanity-check it before Postshot"

    def execute(self, context):
        scene = context.scene
        props = scene.splat_export_props

        colmap_exe = bpy.path.abspath(props.colmap_exe_path.strip())
        if not colmap_exe or not os.path.exists(colmap_exe):
            self.report({'ERROR'}, "Set a valid path to colmap.exe first (folder icon next to the field).")
            return {'CANCELLED'}

        output_dir = resolve_output_dir(props, self.report)
        if output_dir is None:
            return {'CANCELLED'}
        colmap_dir = os.path.join(output_dir, "colmap_export")
        if not os.path.exists(colmap_dir):
            self.report({'ERROR'}, "colmap_export folder not found -- run 'Export COLMAP Format' first.")
            return {'CANCELLED'}

        try:
            result = subprocess.run(
                [colmap_exe, "model_analyzer", "--path", colmap_dir],
                capture_output=True, text=True, timeout=120
            )
        except Exception as e:
            self.report({'ERROR'}, f"Failed to run colmap.exe: {e}")
            return {'CANCELLED'}

        output_text = (result.stdout or "") + (result.stderr or "")

        # Show full output in a Blender text block so it doesn't get truncated
        # like a single self.report() line would.
        text_name = "COLMAP_Validation_Output"
        if text_name in bpy.data.texts:
            bpy.data.texts.remove(bpy.data.texts[text_name])
        text_block = bpy.data.texts.new(text_name)
        text_block.write(output_text if output_text.strip() else "(no output)")

        if result.returncode == 0:
            self.report({'INFO'}, f"COLMAP validation OK -- see '{text_name}' text block for details.")
        else:
            self.report({'ERROR'}, f"COLMAP validation failed (exit {result.returncode}) -- see '{text_name}' text block.")
        return {'FINISHED'}


class SplatExportProps(bpy.types.PropertyGroup):
    output_dir: bpy.props.StringProperty(
        name="Output Folder",
        description="Where transforms.json gets written",
        default="//splat_export/",
        subtype='DIR_PATH',
    )
    colmap_exe_path: bpy.props.StringProperty(
        name="colmap.exe",
        description="Path to colmap.exe (in the COLMAP install's bin folder), set once",
        default="",
        subtype='FILE_PATH',
    )
    frame_start: bpy.props.IntProperty(
        name="Frame Start",
        description="Defaults to the scene's Frame Range Start",
        default=1,
    )
    frame_end: bpy.props.IntProperty(
        name="Frame End",
        description="Defaults to the scene's Frame Range End",
        default=2000,
    )
    stride: bpy.props.IntProperty(
        name="Step",
        description="Defaults to the scene's Frame Range Step. 1 = every frame. 5-10 is usually plenty for splat training.",
        default=5,
        min=1,
    )
    depth_clip: bpy.props.FloatProperty(
        name="Depth Clip",
        description="Depth values above this are treated as 'no hit' (background) and discarded",
        default=1e4,
        min=0.0,
    )
    points_per_frame: bpy.props.IntProperty(
        name="Points Per Frame",
        description="Subsample cap per frame before merging -- keeps the merge from ballooning with 400 full-res depth maps",
        default=2000,
        min=100,
    )
    max_points_total: bpy.props.IntProperty(
        name="Max Total Points",
        description="Final subsample cap on the merged point cloud",
        default=200_000,
        min=1000,
    )


class SPLAT_OT_sync_from_scene(bpy.types.Operator):
    bl_idname = "splat.sync_frame_range"
    bl_label = "Sync from Output Properties"
    bl_description = "Copy Frame Start/End/Step from the scene's Output Properties > Frame Range panel"

    def execute(self, context):
        scene = context.scene
        props = scene.splat_export_props
        props.frame_start = scene.frame_start
        props.frame_end = scene.frame_end
        props.stride = scene.frame_step
        return {'FINISHED'}


class SPLAT_PT_panel(bpy.types.Panel):
    bl_label = "Splat Camera Export"
    bl_idname = "SPLAT_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Splat Export"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        props = scene.splat_export_props

        layout.prop(props, "output_dir")

        box = layout.box()
        box.label(text="Frame Range")
        box.prop(props, "frame_start")
        box.prop(props, "frame_end")
        box.prop(props, "stride")
        box.operator("splat.sync_frame_range", icon='FILE_REFRESH')

        layout.separator()

        bound_count = len([m for m in scene.timeline_markers if m.camera is not None])
        layout.label(text=f"Camera-bound markers: {bound_count}")
        if bound_count == 0:
            layout.label(text="Bind cameras to markers first!", icon='ERROR')

        n_frames = max(0, (props.frame_end - props.frame_start) // max(props.stride, 1) + 1)
        layout.label(text=f"Will export ~{n_frames} poses")

        layout.operator("splat.export_poses", icon='CAMERA_DATA')

        layout.separator()
        layout.operator("splat.setup_depth_output", icon='RENDERLAYERS')

        layout.separator()
        box2 = layout.box()
        box2.label(text="Point Cloud (after Deadline render)")
        box2.prop(props, "points_per_frame")
        box2.prop(props, "max_points_total")
        box2.prop(props, "depth_clip")
        box2.operator("splat.generate_pointcloud", icon='POINTCLOUD_DATA')

        layout.separator()
        box3 = layout.box()
        box3.label(text="COLMAP / Postshot Export")
        box3.operator("splat.export_colmap", icon='EXPORT')
        box3.prop(props, "colmap_exe_path")
        box3.operator("splat.validate_colmap", icon='CHECKMARK')


classes = (
    SplatExportProps,
    SPLAT_OT_export_poses,
    SPLAT_OT_setup_depth_output,
    SPLAT_OT_generate_pointcloud,
    SPLAT_OT_export_colmap,
    SPLAT_OT_validate_colmap,
    SPLAT_OT_sync_from_scene,
    SPLAT_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.splat_export_props = bpy.props.PointerProperty(type=SplatExportProps)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.splat_export_props


if __name__ == "__main__":
    register()
