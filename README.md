# Splat Export Pipeline

A single-file Blender addon that prepares a scene for **Gaussian Splat training** by exporting ground-truth camera poses and a depth-derived point cloud, then converting both to COLMAP format.

The point is to **skip Structure-from-Motion entirely**. Blender already knows exactly where every camera is. Letting Postshot or COLMAP re-estimate those poses from rendered images throws away perfect data and re-derives it worse.

Tested end-to-end into [Postshot](https://www.jawset.com/); should work unmodified with [LichtFeld Studio](https://github.com/MrNeRF/LichtFeld-Studio) (untested).

---

## Install

**Edit > Preferences > Add-ons > Install...** → select `splat_export_pipeline.py` → tick the checkbox.

The panel appears in the 3D Viewport sidebar (press <kbd>N</kbd>) under **Splat Export**.

To try it without installing: open the file in the Scripting tab and hit **Run Script**. Same panel, just doesn't persist.

**Requires** `numpy`, which ships with Blender. No `pip install` needed — depth EXRs are read through Blender's own image loader specifically to avoid the OpenEXR Python wheel, which is painful to install into Blender's bundled interpreter.

---

## What your scene needs

These assumptions are load-bearing. The addon will not work correctly without them:

- **One timeline.** Not separate camera objects on their own frame loops.
- **Cameras switched by bound markers** — `Marker > Bind Camera to Markers`. The addon relies on Blender resolving the active camera per frame; it does not read `scene.camera` shot by shot.
- **All cameras share one focal length and sensor size.** A single `camera_angle_x` is written for the whole export. Per-frame intrinsics are not supported.
- **You render separately.** This addon exports data; it does not render. Submit to your farm (Deadline, etc.) with the same Frame Start/End/Step shown in the panel.

The reference setup it was built against: 2000 frames, 5 cameras in 400-frame blocks, cameras animated within each block.

---

## Usage

Run in order. Steps 1–2 before rendering, steps 3–5 after.

| Step | Button | When |
|---|---|---|
| 1 | **Export Camera Poses** | Before render → writes `transforms.json` |
| 2 | **Setup EXR Depth Output** | Before render, once → wires the compositor for Z-depth EXRs |
| 3 | **Generate Point Cloud** | After render → unprojects depth into `points.ply` |
| 4 | **Export COLMAP Format** | After step 3 → writes `colmap_export/` + copies images |
| 5 | **Validate with COLMAP** | Optional sanity check via `colmap model_analyzer` |

Then point Postshot / LichtFeld Studio at `colmap_export/` and **skip the SfM step**.

### Notes on individual steps

**Step 2 does not disturb your existing color output.** It checks for the Render Layers → Composite link before touching anything and only adds a File Output node alongside. Works with both the legacy `scene.node_tree` API and Blender 4.x/5.x's node-group compositor.

**Step 3 blocks the UI** for its full runtime with no visible progress bar unless the System Console is open. It writes a live `pointcloud_progress.log` beside the output, rewritten every 10 frames — open that in a text editor to check progress.

**Step 4 writes the COLMAP 4.x five-file schema** (`rigs`, `cameras`, `frames`, `images`, `points3D`). COLMAP 4.1 rejected the classic three-file model outright despite the docs claiming backward compatibility. `rigs.txt` is a trivial single-camera entry — a schema requirement, not a real multi-camera rig. If a future trainer errors on import, try deleting `rigs.txt` and `frames.txt` and keeping the classic three.

---

## Settings

| Setting | Default | Notes |
|---|---|---|
| Output Folder | `//splat_export/` | Everything else derives from this |
| Step | 5 | 1 = every frame; 5–10 is usually plenty for splat training |
| Points Per Frame | 2000 | Subsample cap before merging |
| Max Total Points | 200,000 | Final cap on the merged cloud |
| Depth Clip | 1e4 | Depth above this is treated as background and discarded |

---

## Contributing

Two things in here look like they could be simplified but should not be:

1. **Depth files are located with one `os.listdir()` and a regex frame table, never `glob.glob()` per frame.** The per-frame version scanned a network drive 400 separate times and hung for hours.
2. **All five operators call `resolve_output_dir()`.** Path handling was written inline several times and broke every time — including on a Windows case that mangles a relative default into a bare `\\name\` string that is neither valid UNC nor Blender's `//`, and crashes `os.makedirs`.

If you add an operator, call `resolve_output_dir()` rather than handling paths again.

---

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

Blender addons that use the `bpy` API are derivative works of Blender and must be GPL-compatible — this is Blender Foundation policy, not a preference. You are free to use, modify, and redistribute this, commercially included.
