# Sesame Studio 3.0 — audit and overhaul notes

What made 2.0 feel clunky, measured where possible, and what replaced it.
File/line references in the "found" columns are to the 2.0 sources (commit `2377727`).

## Headline findings

| # | Finding | Where (2.0) | Effect | Fix in 3.0 |
|---|---|---|---|---|
| 1 | The app shipped **unthemed** — the stylesheet was imported but never applied after an earlier refactor | `sesame_studio.py:24,39-45` | Native light/mixed widgets; most of the "dated" look | `theme.apply_theme(app)` at startup; new design system |
| 2 | Every inspector slider tick **rebuilt the whole timeline scene** and re-emitted selection, which re-posed the viewport twice | `timeline.py:384-392, 469-519` | 10.8 ms per tick at 400 frames, plus a signal feedback loop | O(1) edit path: 0.018 ms per tick; no re-emit |
| 3 | The face painter **regenerated the full C byte array on every mouse move** | `face_editor.py:425-427, 494-501` | A 200-point stroke took 3.2 s | Array generated only when its tab is visible (debounced): 9 ms per stroke |
| 4 | 3D view used the fixed-function pipeline and **re-sent every vertex from numpy each frame**; STL parts were positioned with hand-guessed pivots | `viewport3d.py:148-157, 222-231, 160-220` | Flat look, misaligned joints, a 340-line calibration panel to compensate | OpenGL 3.3 renderer, one static vertex buffer, CAD-exact model; calibration removed |
| 5 | **No way to pose in 3D** — no picking, hover or selection; sliders only | `viewport3d.py:727-779` | Posing by trial and error | Hover/click/drag joints, gizmo, IK, mirror posing |
| 6 | **No undo anywhere** in sequence editing | — | Every mistake was permanent | `QUndoStack` over all timeline/pose edits, drags coalesced |
| 7 | STL loader parsed triangles in a **per-triangle Python loop** at startup | `stl_loader.py:49-63` | Slow launch | Model loads from a compressed array file in ~50 ms |
| 8 | The splash was shown **after** the main window had been built | `sesame_studio.py:47-62` | 1.4 s of pure delay | Splash covers the real load; ~0.65 s minimum |

## Timeline

- Drag-retime destroyed the block being dragged on each mouse move, so resize drags stuck (`timeline.py:95-100, 471-473`).
- Blocks were laid out with 3 px gaps but time↔x was linear, so the playhead drifted ~25 ms per frame and selecting a frame showed a pose interpolated into the *next* frame (`:487-500` vs `:463-467`).
- A face was only announced in the last 0.1 % of a frame, so faces almost never changed during playback (`:556`).
- Playback advanced by a fixed tick instead of elapsed time; Play always restarted from 0; no pause, loop toggle or time readout (`:579-608`).
- Fixed pixels-per-millisecond: no zoom, sub-25 ms frames were 3 px slivers (`:31-32`).
- "Add Frame" cloned the last frame regardless of selection (`:374-382`).
- Clicking a built-in frame fired a "read-only" toast every time (`:83-87`).
- Reorder was "swap when dragged past half a width" with no drop marker (`:101-107`).

Now: one custom-painted canvas with a cached static layer; a single piecewise time map shared by blocks, ruler, playhead and tracks; elapsed-time playback; zoom/fit; multi-select; clipboard; undo. Full repaint ≈ 3 ms at 400 frames.

## Face painter and library

- `image_to_pixels` made 8192 `QColor` calls per conversion (4.5 ms); flood fill was a Python stack with `setPixel` (153 ms full canvas) (`face_editor.py:30-35, 135-152`).
- One undo stack was shared across frames, so undo after switching frames pasted another frame's pixels (`:77-81, 439-446`).
- Fixed 8× canvas, no zoom or pan (`:25, 64`).
- The text tool monkey-patched `mousePressEvent` and the font list was truncated to the first 200 families (`:314, 413-423`).
- Shift lost pixels instead of wrapping (`:117-124`).
- Esc/Cancel silently discarded work; name validation only at save via modal boxes (`:503-516`).
- The library re-rasterised every face on each search keystroke (11.5 ms) and showed a text list (`face_library.py:152-174`).

Now: numpy core (`face_core.py`), per-frame bit-packed history, symmetry, marquee select/move, image import, modeless editor with live preview on the robot. Flood fill 1.3 ms; library refresh 0.25 ms.

## 3D viewport

- One directional light, no shadows or materials; grid drawn in immediate mode each frame with a hard edge (`viewport3d.py:327-337, 387-399`).
- Face texture built with a per-pixel Python generator on every change (`:472-489`).
- Camera had no damping, fixed-step zoom to centre, no framing or presets, and no clamp on the pan target (`:741-785`).
- `show_labels` was never drawn; `angleHovered` was never emitted.

Now: ~0.7 ms CPU per frame at 1280×800 with shadows, ambient occlusion and outlines; hover pick 11 µs cached; zero repaints when idle.

## Shell and workflow

- A `QGraphicsOpacityEffect` fade was attached to the OpenGL viewport and never removed (`main_window.py:526-536`).
- New Project blocked on a name prompt (`:319`).
- A whole toolbar row held one "Board:" combo (`:181-185`).
- Sequence edits in `face-bitmaps.h` mode marked the header as modified (`:293-301`).
- Saving could report success-by-default: the unsaved-changes prompt returned "OK to continue" after Save even if the save was cancelled (`:396-411`).
- Menus wired private methods directly and had no icons; status bar only ever said "Ready".
- The GUI test wrote into the user's real settings (recent files) (`tests/test_face_bitmaps_gui.py`).

Now: no title-bar chrome on docks, Pose/Faces tabs on top, single compact toolbar, board choice under **Sequence ▸ Target Board**, projects start immediately as "Untitled" in the Stand pose, tests use throwaway settings.

## Physics preview — what it models and what it doesn't

Single rigid body driven by servo angles; ground contact with static/kinetic friction solved as one 6×6 system per pass; servo model with speed limit (600 °/s default) and ease-in. ~0.8 ms per 60 Hz step; asleep costs nothing.

Not modelled: servo stall/backlash/load slow-down, self-collision, the firmware's 20 ms stagger between servo writes, limb inertia reactions. Mass (0.229 kg) and centre of mass are estimates from part volumes, not measured.

Built-in walks and turns are the firmware's remastered "V5" gaits (sesame-robot commit `bb87021`). In the sim, on the CAD model, they travel about 52 mm per 0.4 s cycle forward or backward with 1–2° of yaw per cycle, and turn about 52–60° per cycle. The gaits they replaced veered 8–20° per cycle. None of these figures has been compared with a real robot.

## CAD model notes

- Joint pivots/axes are derived from the servo shaft screws; bind pose = firmware STAND.
- The CAD has no horns on the two front knee servos; they are mirrored from the rear ones.
- The CAD's OLED module sits ~1.6–3 mm behind and ~1.2 mm above the cover window, so the on-robot face is drawn on the window glass.
- `Display_OLED_0.96_128x64 v5` (a second OLED model overlapping the first) and 31 empty CAD leaves are dropped; all are listed under `dropped` in `assets/model/manifest.json`.

## Not done / not verified

- `create_trailer.py` is choreographed against the 2.0 UI (it drives the removed calibration panel) and needs re-choreographing.
- The frozen Windows executable has not been rebuilt or run with these changes.
- Rendering was only run on one machine (NVIDIA, OpenGL 3.3 core, 150 % scaling).
