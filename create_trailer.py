#!/usr/bin/env python3
"""Sesame Studio Official Video Trailer Generator (v2 - Director's Cut).

Creates a 720p 24fps ~25.25-second trailer showcasing all of Sesame Studio's
real features, frame-accurately synchronized to '1-13. Mix Me In, Too.mp3'.
Captures native high-DPI frames from the live PySide6 + OpenGL application,
applies smooth virtual camera pan/zoom, tracks exact Qt widget coordinates
for cursor interactions, and encodes to H.264/AAC MP4 via ffmpeg.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# 1. Setup app paths and desktop GL format before Qt imports
STUDIO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(STUDIO_DIR))

from app.viewport3d import apply_desktop_gl_format
apply_desktop_gl_format()

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QImage, QPixmap
from PySide6.QtWidgets import QApplication

from app.theme import STYLESHEET
from app.project import Project, Face
from app.main_window import MainWindow, CodeOutputDialog, RECENT_KEY
from app.face_editor import FaceEditorDialog, frame_bytes_to_image, image_to_pixels
from app.project import pixels_to_frame
from app.timeline import lerp_angles
from app.widgets.toast import show_toast
from app.builtin_sequences import DANCE, WAVE, PUSHUP, CUTE, WORM, STAND_STATE, REST_STATE
from app.builtin_faces import BUILTIN_FACES
from app.paths import LOGO_PATH, FIRMWARE_DIR

# Trailer Constants
WIDTH = 1280
HEIGHT = 720
FPS = 24
TOTAL_FRAMES = 606  # 25.25 seconds @ 24fps
AUDIO_PATH = "C:/Users/doria/Downloads/1-13. Mix Me In, Too.mp3"
FALLBACK_AUDIO = "C:/Users/doria/.gemini/antigravity/brain/f418e488-ab74-4bf0-863f-06e99da6ed4d/.user_uploaded/uploaded_media_1794181860908.mp3"
OUTPUT_MP4 = str(STUDIO_DIR / "dist" / "SesameStudio_Trailer.mp4")

# Fonts
FONT_HERO = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 46)
FONT_HERO_SUB = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 20)
FONT_TITLE_BIG = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 38)
FONT_TITLE_SUB = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 17)
FONT_BOLD = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 19)
FONT_SUB = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 14)
FONT_TAG = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 11)
FONT_PUNCH = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 40)
FONT_PUNCH_SUB = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 15)
FONT_PILL = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 13)
FONT_MONO = ImageFont.truetype("C:/Windows/Fonts/consolab.ttf", 15)


def load_audio_rms(audio_path: str, n_frames: int) -> np.ndarray:
    """Extracts per-frame RMS energy (normalized 0..1) from the audio file."""
    cmd = ["ffmpeg", "-i", audio_path, "-f", "s16le", "-ac", "1", "-ar", "24000", "-"]
    raw = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    spf = 1000  # 24000 Hz / 24 fps
    rms = np.zeros(n_frames, dtype=np.float32)
    for i in range(n_frames):
        chunk = samples[i * spf:(i + 1) * spf]
        if len(chunk) > 0:
            rms[i] = float(np.sqrt(np.mean(chunk ** 2)))
    max_val = float(np.max(rms)) or 1.0
    return np.clip(rms / max_val, 0.0, 1.0)


def qpixmap_to_pil_native(pix: QPixmap) -> Image.Image:
    """Converts a QPixmap to a native-resolution RGB PIL Image (e.g. 1920x1080 at 1.5x DPI)."""
    qimg = pix.toImage().convertToFormat(QImage.Format_RGB888)
    w, h = qimg.width(), qimg.height()
    ptr = qimg.constBits()
    bpl = qimg.bytesPerLine()
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape((h, bpl))
    arr = arr[:, :w * 3].reshape((h, w, 3))
    return Image.fromarray(arr, "RGB")


def apply_camera(
    raw_im: Image.Image,
    cam_cx: float,
    cam_cy: float,
    zoom: float = 1.0,
) -> tuple[Image.Image, float, float, float]:
    """Crops and scales a native high-DPI image according to a logical (1280x720) camera.

    Returns (frame_1280x720, vx0, vy0, zoom) so logical widget coordinates (lx, ly)
    can be mapped to screen coordinates via:
        sx = (lx - vx0) * zoom
        sy = (ly - vy0) * zoom
    """
    zoom = max(1.0, float(zoom))
    vw = WIDTH / zoom
    vh = HEIGHT / zoom
    vx0 = max(0.0, min(WIDTH - vw, cam_cx - vw / 2.0))
    vy0 = max(0.0, min(HEIGHT - vh, cam_cy - vh / 2.0))

    if zoom <= 1.001:
        if raw_im.size != (WIDTH, HEIGHT):
            return raw_im.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS), 0.0, 0.0, 1.0
        return raw_im.copy(), 0.0, 0.0, 1.0

    scale_x = raw_im.width / WIDTH
    scale_y = raw_im.height / HEIGHT
    box = (
        int(round(vx0 * scale_x)),
        int(round(vy0 * scale_y)),
        int(round((vx0 + vw) * scale_x)),
        int(round((vy0 + vh) * scale_y)),
    )
    cropped = raw_im.crop(box).resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    return cropped, vx0, vy0, zoom


def map_to_cam(lx: float, ly: float, vx0: float, vy0: float, zoom: float) -> tuple[float, float]:
    return (lx - vx0) * zoom, (ly - vy0) * zoom


def widget_center_in_win(widget, win) -> tuple[float, float]:
    tl = widget.mapTo(win, QPoint(0, 0))
    return float(tl.x() + widget.width() * 0.5), float(tl.y() + widget.height() * 0.5)


def slider_thumb_in_win(slider, win) -> tuple[float, float]:
    tl = slider.mapTo(win, QPoint(0, 0))
    span = max(1, slider.maximum() - slider.minimum())
    frac = (slider.value() - slider.minimum()) / span
    hx = tl.x() + 8.0 + frac * max(10.0, slider.width() - 16.0)
    hy = tl.y() + slider.height() * 0.5
    return float(hx), float(hy)


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def bezier_interp(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    t = smoothstep(t)
    u = 1.0 - t
    x = u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0]
    y = u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]
    return (x, y)


def draw_cursor(
    overlay: ImageDraw.ImageDraw,
    x: float,
    y: float,
    click_progress: float = 0.0,
    is_dragging: bool = False,
) -> None:
    """Draws a crisp high-contrast OS cursor with optional click or drag ring."""
    if click_progress > 0.0:
        r = 8 + click_progress * 24
        alpha = int(max(0.0, min(1.0, 1.0 - click_progress)) * 240)
        overlay.ellipse(
            [x - r, y - r, x + r, y + r],
            outline=(255, 112, 67, alpha),
            width=3,
        )
        r2 = 4 + click_progress * 12
        overlay.ellipse(
            [x - r2, y - r2, x + r2, y + r2],
            fill=(255, 160, 110, int(alpha * 0.45)),
        )
    elif is_dragging:
        overlay.ellipse(
            [x - 12, y - 12, x + 12, y + 12],
            fill=(255, 112, 67, 55),
            outline=(255, 112, 67, 190),
            width=2,
        )

    # Drop shadow
    pts_shadow = [
        (x + 2, y + 2), (x + 2, y + 24), (x + 7, y + 19),
        (x + 12, y + 29), (x + 16, y + 27), (x + 11, y + 17), (x + 19, y + 17),
    ]
    overlay.polygon(pts_shadow, fill=(0, 0, 0, 140))

    # Crisp arrow pointer
    pts = [
        (x, y), (x, y + 22), (x + 5, y + 17),
        (x + 10, y + 27), (x + 14, y + 25), (x + 9, y + 15), (x + 17, y + 15),
    ]
    overlay.polygon(pts, fill=(255, 255, 255, 255), outline=(15, 15, 20, 255))


def draw_callout_badge(
    overlay: ImageDraw.ImageDraw,
    title: str,
    subtitle: str,
    tag: str = "FEATURE",
    pos: str = "bottom-left",
    alpha: float = 1.0,
    beat_energy: float = 0.0,
) -> None:
    """Draws a modern glassmorphic callout badge with audio-reactive accent bar."""
    if alpha <= 0.01:
        return
    bw, bh = 540, 74
    if pos == "bottom-left":
        bx, by = 32, HEIGHT - bh - 26
    elif pos == "top-left":
        bx, by = 32, 52
    elif pos == "bottom-right":
        bx, by = WIDTH - bw - 32, HEIGHT - bh - 26
    else:
        bx, by = 32, HEIGHT - bh - 26

    a_box = int(alpha * 235)
    a_border = int(alpha * (160 + min(95, int(beat_energy * 95))))
    a_text = int(alpha * 255)
    a_sub = int(alpha * 225)

    # Shadow
    overlay.rounded_rectangle(
        [bx + 4, by + 6, bx + bw + 4, by + bh + 6],
        radius=12,
        fill=(0, 0, 0, int(alpha * 130)),
    )
    # Main card
    overlay.rounded_rectangle(
        [bx, by, bx + bw, by + bh],
        radius=12,
        fill=(14, 15, 20, a_box),
        outline=(255, 112, 67, a_border),
        width=2,
    )
    # Left glowing accent bar
    overlay.rounded_rectangle(
        [bx + 6, by + 10, bx + 11, by + bh - 10],
        radius=3,
        fill=(255, 112, 67, a_text),
    )
    # Tag pill
    tag_str = tag.upper()
    tw = overlay.textlength(tag_str, font=FONT_TAG)
    overlay.rounded_rectangle(
        [bx + 22, by + 9, bx + 34 + tw, by + 25],
        radius=4,
        fill=(255, 112, 67, int(alpha * 45)),
        outline=(255, 112, 67, int(alpha * 140)),
        width=1,
    )
    overlay.text((bx + 28, by + 10), tag_str, font=FONT_TAG, fill=(255, 145, 100, a_text))
    overlay.text((bx + 22, by + 27), title, font=FONT_BOLD, fill=(255, 255, 255, a_text))
    overlay.text((bx + 22, by + 50), subtitle, font=FONT_SUB, fill=(185, 195, 210, a_sub))


def add_top_progress_and_frame_polish(
    im: Image.Image,
    overlay: Image.Image,
    beat_energy: float = 0.0,
    flash_alpha: float = 0.0,
) -> Image.Image:
    """Composites the RGBA overlay, adds a subtle cinematic border, and optional beat flash."""
    base = Image.alpha_composite(im.convert("RGBA"), overlay)
    if flash_alpha > 0.005:
        flash = Image.new("RGBA", (WIDTH, HEIGHT), (255, 140, 90, int(min(1.0, flash_alpha) * 95)))
        base = Image.alpha_composite(base, flash)
    return base.convert("RGB")


def pose_sequence_smooth(seq, time_ms: float) -> tuple[dict[str, int], str | None]:
    """Interpolates servo angles smoothly at arbitrary millisecond timestamp in a Sequence."""
    frames = seq.frames
    if not frames:
        return dict(STAND_STATE), None
    total = sum(max(1, f.delay_ms) for f in frames)
    t_mod = time_ms % max(1, total)
    cum = 0.0
    for i, frame in enumerate(frames):
        dur = max(1, frame.delay_ms)
        end = cum + dur
        if t_mod <= end or i == len(frames) - 1:
            prev = frames[i - 1] if i > 0 else frames[-1]
            t = smoothstep((t_mod - cum) / dur)
            angles = lerp_angles(prev.angles, frame.angles, t)
            face = frame.face or prev.face
            return angles, face
        cum = end
    return dict(frames[-1].angles), frames[-1].face


def build_star_wink_frames(happy_face: Face) -> list[QImage]:
    """Creates a clean 3-frame expression ('star_wink') for the Face Painter demo:
    F0: happy face ( ◕ ω ◕ )
    F1: winking right eye + sparkle (drawn progressively during Scene 5)
    F2: full wink with heart/star accent
    """
    f0 = frame_bytes_to_image(bytes(happy_face.frames[0]))

    # F1 base: copy F0, clear the right eye area (x: 80..112, y: 14..42) so F0's open eye
    # shows through as a dim grey onion-skin while we draw the '>' wink!
    f1_base = f0.copy()
    p = QPainter(f1_base)
    p.fillRect(78, 14, 34, 30, QColor("black"))
    p.end()

    # F2: completed wink + star
    f2 = f1_base.copy()
    p2 = QPainter(f2)
    p2.setPen(QPen(QColor("white"), 3))
    p2.drawLine(82, 22, 98, 29)
    p2.drawLine(98, 29, 82, 36)
    # Little 4-point star at top-right
    p2.setPen(QPen(QColor("white"), 2))
    p2.drawLine(114, 10, 114, 22)
    p2.drawLine(108, 16, 120, 16)
    p2.end()

    return [f0, f1_base, f2]


def build_trailer():
    audio_file = AUDIO_PATH if os.path.exists(AUDIO_PATH) else FALLBACK_AUDIO
    print("=" * 68)
    print("Sesame Studio Official Trailer Generator v2 (720p 24fps)")
    print(f"Target duration: {TOTAL_FRAMES / FPS:.2f}s ({TOTAL_FRAMES} frames)")
    print(f"Audio source:    {audio_file}")
    print(f"Output target:   {OUTPUT_MP4}")
    print("=" * 68)

    os.makedirs(os.path.dirname(OUTPUT_MP4), exist_ok=True)
    rms = load_audio_rms(audio_file, TOTAL_FRAMES)

    # Initialize PySide6 application
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)

    win = MainWindow()
    # Populate realistic recent files so the Welcome screen looks authentic and clean
    clean_recent = [
        str(FIRMWARE_DIR / "face-bitmaps.h"),
        str(STUDIO_DIR / "examples" / "dance_routine.sesproj"),
        str(STUDIO_DIR / "examples" / "expressive_emotes.sesproj"),
    ]
    win.settings.setValue(RECENT_KEY, clean_recent)
    win.welcome_page.set_recent([
        "C:/Projects/sesame-robot/firmware/face-bitmaps.h",
        "C:/Projects/sesame-robot/animations/dance_routine.sesproj",
        "C:/Projects/sesame-robot/animations/expressive_emotes.sesproj",
    ])

    win.resize(WIDTH, HEIGHT)
    win.show()
    app.processEvents()

    # Ensure viewport rig is initialized and OLED face plane sits flush in front bezel
    win.stack.setCurrentWidget(win.viewport)
    win.viewport.setGraphicsEffect(None)
    app.processEvents()
    if win.viewport.rig:
        win.viewport.rig.config["face_plane"]["position"][0] = -11.2

    # Preload project sequences and faces
    win.project = Project(name="Sesame Studio Showcase")
    win._load_project_into_ui()
    win.timeline.load_project_sequences({
        "dance": DANCE,
        "wave": WAVE,
        "pushup": PUSHUP,
        "cute": CUTE,
        "worm": WORM,
    })
    win.face_library.load_faces(dict(BUILTIN_FACES))
    app.processEvents()

    logo_img = Image.open(LOGO_PATH).convert("RGBA") if os.path.exists(LOGO_PATH) else None

    # Setup FFmpeg encoding pipeline
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{WIDTH}x{HEIGHT}",
        "-pix_fmt", "rgb24",
        "-r", str(FPS),
        "-i", "-",
        "-i", audio_file,
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "17",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-af", f"afade=t=out:st={TOTAL_FRAMES / FPS - 0.55:.2f}:d=0.55",
        "-shortest",
        OUTPUT_MP4,
    ]
    pipe = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def write_frame(im: Image.Image) -> None:
        if im.size != (WIDTH, HEIGHT):
            im = im.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        pipe.stdin.write(im.tobytes())

    # =========================================================================
    # ACT I: THE ORCHESTRAL RISER INTRO (Frames 0 - 99 | 0.00s - 4.13s)
    #   Part A (0 - 47): Full-Screen 3D Hero Robot Awakening + Title Reveal
    #   Part B (48 - 99): Welcome Page -> Zoom & Click "New Project" before beat drop
    # =========================================================================
    print("[Act 1/8] Orchestral Riser Intro: 3D Hero Reveal & Launch (Frames 0-99)...")
    win.stack.setCurrentWidget(win.viewport)
    win._set_editor_docks_visible(False)
    win.menuBar().setVisible(False)
    win.statusBar().setVisible(False)
    for tb in win.findChildren(type(win.menuBar())):
        pass
    app.processEvents()

    for f in range(0, 48):
        t = f / 47.0
        st = smoothstep(t)
        energy = float(rms[f])

        # Robot rises from REST_STATE to STAND_STATE and waves hello!
        if f < 24:
            pose = lerp_angles(REST_STATE, STAND_STATE, smoothstep(f / 23.0))
            face_name = "sleepy" if f < 10 else "happy"
        else:
            wave_angles, _ = pose_sequence_smooth(WAVE, (f - 24) * 85.0)
            pose = lerp_angles(STAND_STATE, wave_angles, smoothstep((f - 24) / 10.0))
            face_name = "excited" if f >= 34 else "happy"

        win.viewport.set_angles(pose)
        win.viewport.set_face_preview(win._resolve_face_pixels(face_name))
        # Orbit around the FRONT of the robot (yaw 148 -> 176), shifting target right so robot sits on LEFT half!
        win.viewport._yaw = 148.0 + 28.0 * st
        win.viewport._pitch = 14.0 + 6.0 * st
        win.viewport._distance = 175.0 - 25.0 * st
        win.viewport._target = np.array([15.0, 26.0, 48.0], dtype=np.float32)
        win.viewport.update()
        app.processEvents()

        raw_im = qpixmap_to_pil_native(win.viewport.grab())
        frame_im = raw_im.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

        # True RGBA glow + hero typography on the right half
        glow_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow_layer)
        halo_r = int(200 + 80 * energy)
        gdraw.ellipse(
            [860 - halo_r, 340 - halo_r, 860 + halo_r, 340 + halo_r],
            fill=(255, 112, 67, int(55 + 45 * energy)),
        )
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(50))

        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        overlay = Image.alpha_composite(overlay, glow_layer)
        draw = ImageDraw.Draw(overlay)

        text_alpha = int(smoothstep(min(1.0, f / 14.0)) * 255)
        # Right-side glass hero card
        cx0, cy0, cw, ch = 640, 175, 575, 360
        draw.rounded_rectangle(
            [cx0, cy0, cx0 + cw, cy0 + ch],
            radius=18,
            fill=(14, 15, 22, int(text_alpha * 0.84)),
            outline=(255, 112, 67, int(text_alpha * 0.75)),
            width=2,
        )
        if logo_img:
            lw = 88
            r_logo = logo_img.resize((lw, lw), Image.Resampling.LANCZOS)
            overlay.paste(r_logo, (cx0 + 32, cy0 + 30), r_logo)

        draw.rounded_rectangle(
            [cx0 + 136, cy0 + 38, cx0 + 330, cy0 + 62],
            radius=6,
            fill=(255, 112, 67, int(text_alpha * 0.22)),
            outline=(255, 112, 67, int(text_alpha * 0.8)),
        )
        draw.text((cx0 + 148, cy0 + 43), "OFFICIAL RELEASE v2.0", font=FONT_TAG, fill=(255, 150, 105, text_alpha))
        draw.text((cx0 + 136, cy0 + 68), "SESAME STUDIO", font=FONT_TITLE_BIG, fill=(255, 255, 255, text_alpha))
        draw.text(
            (cx0 + 34, cy0 + 136),
            "3D Animation & OLED Expression Suite",
            font=FONT_HERO_SUB,
            fill=(255, 145, 100, text_alpha),
        )
        bullets = [
            "• Real-Time 3D STL Forward Kinematics",
            "• Premiere-Style Keyframe Motion Sequencer",
            "• 128×64 1-Bit OLED Pixel & Animation Painter",
            "• Direct Arduino face-bitmaps.h Firmware Sync",
        ]
        for bi, btxt in enumerate(bullets):
            b_appear = smoothstep(max(0.0, min(1.0, (f - 8 - bi * 5) / 8.0)))
            ba = int(b_appear * 235)
            draw.text((cx0 + 38, cy0 + 182 + bi * 34), btxt, font=FONT_SUB, fill=(215, 225, 240, ba))

        frame_im = add_top_progress_and_frame_polish(frame_im, overlay, energy)
        write_frame(frame_im)

    # Restore menu/statusbar and show Welcome Page for Part B (Frames 48 - 99)
    win.menuBar().setVisible(True)
    win.statusBar().setVisible(True)
    win.stack.setCurrentWidget(win.welcome_page)
    win._set_editor_docks_visible(False)
    app.processEvents()

    # Locate the "New Project" Card in WelcomePage
    from app.welcome import Card
    welcome_cards = win.welcome_page.findChildren(Card)
    new_proj_btn = welcome_cards[0]
    btn_lx, btn_ly = widget_center_in_win(new_proj_btn, win)

    welcome_raw = qpixmap_to_pil_native(win.grab())

    for f in range(48, 100):
        sf = f - 48
        t = sf / 51.0
        energy = float(rms[f])

        # Smooth camera zoom toward the Welcome Page action buttons & recent files
        zoom = 1.0 + 0.28 * smoothstep(t)
        cam_cx = WIDTH * 0.5 + (btn_lx + 80 - WIDTH * 0.5) * smoothstep(t)
        cam_cy = HEIGHT * 0.5 + (btn_ly + 40 - HEIGHT * 0.5) * smoothstep(t)
        frame_im, vx0, vy0, cur_z = apply_camera(welcome_raw, cam_cx, cam_cy, zoom)

        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Cursor glides to "New Project" and clicks right before the beat drop at Frame 100
        start_pt = (WIDTH * 0.72, HEIGHT * 0.75)
        target_pt = map_to_cam(btn_lx, btn_ly, vx0, vy0, cur_z)
        ctrl_pt = ((start_pt[0] + target_pt[0]) * 0.5 - 60, min(start_pt[1], target_pt[1]) - 40)
        cx, cy = bezier_interp(start_pt, ctrl_pt, target_pt, min(1.0, sf / 36.0))
        click_p = (sf - 38) / 10.0 if sf >= 38 else 0.0

        draw_cursor(draw, cx, cy, click_progress=max(0.0, min(1.0, click_p)))
        draw_callout_badge(
            draw,
            "STANDALONE DESKTOP SUITE",
            "Create Projects or Open Arduino face-bitmaps.h Directly",
            tag="GETTING STARTED",
            pos="bottom-left",
            beat_energy=energy,
        )

        flash = (sf - 47) / 4.0 * 0.6 if sf >= 47 else 0.0
        frame_im = add_top_progress_and_frame_polish(frame_im, overlay, energy, flash_alpha=flash)
        write_frame(frame_im)

    # =========================================================================
    # ACT II: 3D POSABLE VIEWPORT & LIVE SERVO KINEMATICS (Frames 100 - 186 | 4.17s - 7.75s)
    #   Beat drops at Frame 100! Zoom in on 3D Robot (front view!) + Inspector sliders
    # =========================================================================
    print("[Act 2/8] 3D Posable Viewport & Live Servo Sliders (Frames 100-186)...")
    win.stack.setCurrentWidget(win.viewport)
    win._set_editor_docks_visible(True)
    win.inspector_dock.raise_()
    win.viewport.reset_view()
    if win.viewport.rig:
        win.viewport.rig.config["face_plane"]["position"][0] = -11.2
    app.processEvents()

    for f in range(100, 187):
        sf = f - 100
        t = sf / 86.0
        energy = float(rms[f])

        # Keep camera facing the robot's expressive front face!
        win.viewport._yaw = 152.0 + 36.0 * math.sin(t * math.pi)
        win.viewport._pitch = 18.0 + 5.0 * math.sin(t * math.pi * 2.0)
        win.viewport._distance = 148.0

        angles = dict(STAND_STATE)
        if sf < 28:
            # Drag L1 slider
            active_servo = "L1"
            p_t = sf / 27.0
            angles["L1"] = int(90 + 55 * math.sin(p_t * math.pi * 2.0))
            angles["R1"] = int(90 - 45 * math.sin(p_t * math.pi * 2.0))
            face_name = "happy"
        elif sf < 56:
            # Drag R3 arm slider
            active_servo = "R3"
            p_t = (sf - 28) / 27.0
            angles["R3"] = int(90 + 65 * math.sin(p_t * math.pi * 2.0))
            angles["L3"] = int(90 - 65 * math.sin(p_t * math.pi * 2.0))
            angles["R1"] = int(70 + 25 * math.sin(p_t * math.pi))
            face_name = "surprised" if p_t > 0.35 else "happy"
        else:
            # Drag L4 / full dynamic pose
            active_servo = "L3"
            p_t = (sf - 56) / 30.0
            angles["L3"] = int(35 + 110 * (0.5 - 0.5 * math.cos(p_t * math.pi * 2.0)))
            angles["R3"] = int(145 - 110 * (0.5 - 0.5 * math.cos(p_t * math.pi * 2.0)))
            angles["L2"] = int(90 - 35 * math.sin(p_t * math.pi))
            angles["R2"] = int(90 + 35 * math.sin(p_t * math.pi))
            face_name = "cute"

        win.viewport.set_angles(angles)
        win.inspector.load_angles(angles)
        win.viewport.set_face_preview(win._resolve_face_pixels(face_name))
        win.viewport.update()
        app.processEvents()

        # Get exact logical position of the active servo slider handle
        active_slider = win.inspector.rows[active_servo].slider
        sl_lx, sl_ly = slider_thumb_in_win(active_slider, win)

        # Zoom in (1.28x) centered on the right 3/4 of the window so both the 3D robot
        # AND the Inspector sliders are large, crisp, and side-by-side!
        zoom = 1.0 + 0.26 * smoothstep(min(1.0, sf / 12.0))
        cam_cx = 740.0
        cam_cy = 290.0

        raw_im = qpixmap_to_pil_native(win.grab())
        frame_im, vx0, vy0, cur_z = apply_camera(raw_im, cam_cx, cam_cy, zoom)

        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        cx, cy = map_to_cam(sl_lx, sl_ly, vx0, vy0, cur_z)
        draw_cursor(draw, cx, cy, is_dragging=True)

        draw_callout_badge(
            draw,
            "REAL-TIME 3D FORWARD KINEMATICS",
            "Pose All 8 Servos with Live Printed STL Geometry & OLED Preview",
            tag="3D VIEWPORT & INSPECTOR",
            pos="bottom-left",
            beat_energy=energy,
        )

        flash = 0.45 if sf == 0 else 0.0
        frame_im = add_top_progress_and_frame_polish(frame_im, overlay, energy, flash_alpha=flash)
        write_frame(frame_im)

    # =========================================================================
    # ACT III: 3D JOINT CALIBRATION & INTERACTIVE GIZMOS (Frames 187 - 273 | 7.79s - 11.38s)
    #   Enable Calibration Mode -> Highlight L1/R1 in gold -> Drag 3D RGB Gizmo!
    # =========================================================================
    print("[Act 3/8] 3D Joint Calibration & Interactive Gizmos (Frames 187-273)...")
    # Temporarily hide timeline dock during calibration so JointCalibrationPanel has full vertical space
    win.timeline_dock.setVisible(False)
    win.calib_dock.raise_()
    app.processEvents()

    idx_l1 = win.calibration_panel.target_combo.findData("L1")
    win.calibration_panel.target_combo.setCurrentIndex(idx_l1)
    win.calibration_panel.pivot_radio.setChecked(True)
    win.calibration_panel.calib_check.setChecked(True)
    win.viewport.set_angles(STAND_STATE)
    win.viewport.set_face_preview(win._resolve_face_pixels("thinking"))
    app.processEvents()

    orig_l1_pivot = list(win.viewport.rig.config["joints"]["L1"]["pivot"])

    for f in range(187, 274):
        sf = f - 187
        t = sf / 86.0
        energy = float(rms[f])

        if sf < 48:
            # Mode 1: Pivot / Rotation Center with 3D XYZ gizmo arrows on L1
            win.calibration_panel.pivot_radio.setChecked(True)
            win.viewport._hover_axis = "Y"
            # Gently oscillate pivot Y to demonstrate live gizmo dragging updating the spinboxes
            nudge = 6.0 * math.sin(sf / 47.0 * math.pi * 2.0)
            new_piv = [orig_l1_pivot[0], orig_l1_pivot[1] + nudge, orig_l1_pivot[2]]
            win.viewport.rig.config["joints"]["L1"]["pivot"] = new_piv
            win.calibration_panel._on_gizmo_edited()
        else:
            # Mode 2: Position / Rotation Offset with Rotate Rings on gizmo tips!
            if sf == 48:
                win.viewport.rig.config["joints"]["L1"]["pivot"] = list(orig_l1_pivot)
                win.calibration_panel.offset_radio.setChecked(True)
                win.calibration_panel.gizmo_rotate_radio.setChecked(True)
            win.viewport._hover_axis = "Z"

        win.viewport._yaw = 150.0 + 22.0 * math.sin(t * math.pi)
        win.viewport._pitch = 22.0
        win.viewport._distance = 142.0
        win.viewport.update()
        app.processEvents()

        # Project 3D gizmo Y/Z tip into logical window coordinates so cursor grabs the real 3D arrow!
        anchor = win.viewport._gizmo_visual_anchor()
        vp_tl = win.viewport.mapTo(win, QPoint(0, 0))
        dpr = win.devicePixelRatio() or 1.5
        if anchor is not None:
            axis_vec = (0.0, 18.0, 0.0) if sf < 48 else (0.0, 0.0, 18.0)
            proj = win.viewport._project_point(
                anchor[0] + axis_vec[0],
                anchor[1] + axis_vec[1],
                anchor[2] + axis_vec[2],
            )
            if proj is not None:
                gz_lx = vp_tl.x() + proj[0] / dpr
                gz_ly = vp_tl.y() + proj[1] / dpr
            else:
                gz_lx, gz_ly = 480.0, 280.0
        else:
            gz_lx, gz_ly = 480.0, 280.0

        # Smooth zoom (1.28x) showing both the 3D gizmo and the Joint Calibration spinboxes
        zoom = 1.26
        cam_cx, cam_cy = 720.0, 310.0
        raw_im = qpixmap_to_pil_native(win.grab())
        frame_im, vx0, vy0, cur_z = apply_camera(raw_im, cam_cx, cam_cy, zoom)

        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        cx, cy = map_to_cam(gz_lx, gz_ly, vx0, vy0, cur_z)
        draw_cursor(draw, cx, cy, is_dragging=True)

        draw_callout_badge(
            draw,
            "3D JOINT & PIVOT CALIBRATION",
            "Drag 3D XYZ Gizmos to Align Hinges, Offsets & STL Meshes",
            tag="HARDWARE CALIBRATION",
            pos="bottom-left",
            beat_energy=energy,
        )

        flash = 0.4 if sf == 0 else 0.0
        frame_im = add_top_progress_and_frame_polish(frame_im, overlay, energy, flash_alpha=flash)
        write_frame(frame_im)

    # Restore L1 pivot and turn off calibration gizmo
    win.viewport.rig.config["joints"]["L1"]["pivot"] = list(orig_l1_pivot)
    win.calibration_panel.calib_check.setChecked(False)
    win.timeline_dock.setVisible(True)
    win.inspector_dock.raise_()
    app.processEvents()

    # =========================================================================
    # ACT IV: MAIN BRASS DROP — KEYFRAME TIMELINE & INTERPOLATED PLAYBACK
    #         (Frames 274 - 369 | 11.42s - 15.38s)
    # =========================================================================
    print("[Act 4/8] Main Drop: Keyframe Timeline & Smooth Playback (Frames 274-369)...")
    idx_dance = win.timeline.seq_combo.findData("dance")
    if idx_dance >= 0:
        win.timeline.seq_combo.setCurrentIndex(idx_dance)
        win.timeline._on_sequence_selected(idx_dance)
    win.timeline.play_btn.setText("Stop")  # Active playback state
    app.processEvents()

    play_btn_lx, play_btn_ly = widget_center_in_win(win.timeline.play_btn, win)
    tl_view_tl = win.timeline.view.mapTo(win, QPoint(0, 0))

    for f in range(274, 370):
        sf = f - 274
        t = sf / 95.0
        energy = float(rms[f])

        # Switch from 'dance' to 'wave' at the second brass hit (Frame 331 -> sf == 57)
        if sf == 57:
            idx_wave = win.timeline.seq_combo.findData("wave")
            if idx_wave >= 0:
                win.timeline.seq_combo.setCurrentIndex(idx_wave)
                win.timeline._on_sequence_selected(idx_wave)

        active_seq = DANCE if sf < 57 else WAVE
        seq_time_ms = (sf * 65.0) % max(1.0, win.timeline._total_time_ms)
        scrub_x = win.timeline._time_to_x(seq_time_ms)
        win.timeline._scrub_to_x(scrub_x)

        # Also smoothly interpolate the robot pose and animate the 2-frame 'dance' face on the beat!
        pose, _ = pose_sequence_smooth(active_seq, sf * 65.0)
        win.viewport.set_angles(pose)
        win.inspector.load_angles(pose)

        if sf < 57:
            dance_face = BUILTIN_FACES["dance"]
            beat_frame_idx = (f // 6) % len(dance_face.frames)
            from app.project import frame_to_pixels
            win.viewport.set_face_preview(frame_to_pixels(bytes(dance_face.frames[beat_frame_idx])))
        else:
            win.viewport.set_face_preview(win._resolve_face_pixels("wave"))

        # Dynamic front-facing camera that bobs subtly with the 120 BPM brass groove
        win.viewport._yaw = 165.0 + 24.0 * math.sin(sf * 0.12)
        win.viewport._pitch = 17.0 + 3.0 * math.sin(sf * (math.pi / 6.0))
        win.viewport._distance = 152.0 - 6.0 * energy
        win.viewport._target = np.array([26.0, 26.0, 2.0], dtype=np.float32)
        win.viewport.update()
        app.processEvents()

        # Frame both the full 3D Robot head AND the Timeline sequencer cleanly
        zoom = 1.08
        cam_cx = 560.0
        cam_cy = 365.0
        raw_im = qpixmap_to_pil_native(win.grab())
        frame_im, vx0, vy0, cur_z = apply_camera(raw_im, cam_cx, cam_cy, zoom)

        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Cursor clicks Play at start, then tracks the scrubbing playhead along the timeline ruler
        if sf < 12:
            cx, cy = map_to_cam(play_btn_lx, play_btn_ly, vx0, vy0, cur_z)
            click_p = sf / 10.0 if sf < 10 else 0.0
            draw_cursor(draw, cx, cy, click_progress=click_p)
        else:
            ph_scene_pt = win.timeline.view.mapFromScene(scrub_x, 12.0)
            ph_lx = tl_view_tl.x() + ph_scene_pt.x()
            ph_ly = tl_view_tl.y() + ph_scene_pt.y() + 18.0
            cx, cy = map_to_cam(ph_lx, ph_ly, vx0, vy0, cur_z)
            draw_cursor(draw, cx, cy, is_dragging=True)

        draw_callout_badge(
            draw,
            "KEYFRAME TIMELINE SEQUENCER",
            "Smooth Servo Interpolation • Drag-to-Retime • Face Sub-Lane",
            tag="MOTION SEQUENCING",
            pos="bottom-left",
            beat_energy=energy,
        )

        flash = 0.55 if sf in (0, 9, 57) else 0.0
        frame_im = add_top_progress_and_frame_polish(frame_im, overlay, energy, flash_alpha=flash)
        write_frame(frame_im)

    win.timeline.play_btn.setText("Play")

    # =========================================================================
    # ACT V: 128x64 OLED FACE PAINTER (Frames 370 - 444 | 15.42s - 18.50s)
    #   Multi-frame expression ('star_wink': F0, F1, F2) + Onion-Skinning + Live C Hex!
    # =========================================================================
    print("[Act 5/8] 128x64 OLED Face Painter: Onion-Skin & Live Hex (Frames 370-444)...")
    happy_face = BUILTIN_FACES["happy"]
    wink_qimages = build_star_wink_frames(happy_face)
    wink_face = Face(
        name="star_wink",
        category="custom",
        fps=6,
        frames=[pixels_to_frame(image_to_pixels(im)) for im in wink_qimages],
    )

    dlg = FaceEditorDialog(wink_face, set(), parent=win, is_header_mode=True)
    dlg.resize(1240, 680)
    dlg.show()
    # Select Frame 1 (F1) with Onion-Skin checked so F0's open eye is visibly ghosted underneath!
    dlg.onion_check.setChecked(True)
    dlg._select_frame(1)
    app.processEvents()

    # Dimmed studio backdrop behind the dialog
    studio_backdrop = qpixmap_to_pil_native(win.grab()).resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    studio_backdrop = Image.blend(studio_backdrop, Image.new("RGB", (WIDTH, HEIGHT), (8, 9, 14)), 0.68)

    # Precompute pixel stroke points for the winking '>' eye (x: 82..98, y: 21..37) + star
    wink_points: list[tuple[int, int]] = []
    # Top diagonal of '>'
    for step in range(17):
        px = 82 + step
        py = 21 + int(round(step * 8.0 / 16.0))
        wink_points.append((px, py))
    # Bottom diagonal of '>'
    for step in range(17):
        px = 98 - step
        py = 29 + int(round(step * 8.0 / 16.0))
        wink_points.append((px, py))
    # Sparkle star next to wink
    for dx in range(-5, 6):
        wink_points.append((112 + dx, 16))
    for dy in range(-5, 6):
        wink_points.append((112, 16 + dy))

    for f in range(370, 445):
        sf = f - 370
        energy = float(rms[f])

        if sf < 50:
            # Progressively draw the wink + sparkle on F1 over the onion-skinned F0 eye!
            dlg._select_frame(1)
            n_pts = int((sf / 49.0) * len(wink_points))
            work_img = wink_qimages[1].copy()
            painter = QPainter(work_img)
            painter.setPen(QPen(QColor("white"), 3))
            for idx_pt in range(max(1, n_pts)):
                px, py = wink_points[min(idx_pt, len(wink_points) - 1)]
                painter.drawPoint(px, py)
            painter.end()
            dlg.canvas.image = work_img
            dlg.frames[1] = work_img.copy()
            dlg.canvas.update()
            dlg._refresh_frame_list()
            dlg._refresh_output()
            # Scroll the C hex output box to the middle rows (40% down) so non-zero eye/mouth hex bytes are visible!
            sb = dlg.output_view.verticalScrollBar()
            sb.setValue(int(sb.maximum() * 0.40))

            cur_px, cur_py = wink_points[min(max(0, n_pts - 1), len(wink_points) - 1)]
            canvas_tl = dlg.canvas.mapTo(dlg, QPoint(0, 0))
            cur_dlg_x = canvas_tl.x() + cur_px * (dlg.canvas.width() / 128.0)
            cur_dlg_y = canvas_tl.y() + cur_py * (dlg.canvas.height() / 64.0)
        else:
            # Preview the 3 animation frames F0 -> F1 -> F2 in the frame strip!
            cycle_f = ((sf - 50) // 6) % 3
            dlg._select_frame(cycle_f)
            sb = dlg.output_view.verticalScrollBar()
            sb.setValue(int(sb.maximum() * 0.40))
            fl_tl = dlg.frame_list.mapTo(dlg, QPoint(0, 0))
            cur_dlg_x = fl_tl.x() + 28 + cycle_f * 54
            cur_dlg_y = fl_tl.y() + 24

        app.processEvents()

        # Grab dialog at native DPI and fit crisply into 1220x660 centered on studio backdrop
        dlg_raw = qpixmap_to_pil_native(dlg.grab())
        target_w, target_h = 1216, 656
        dlg_im = dlg_raw.resize((target_w, target_h), Image.Resampling.LANCZOS)

        frame_im = studio_backdrop.copy()
        dx = (WIDTH - target_w) // 2
        dy = (HEIGHT - target_h) // 2
        frame_im.paste(dlg_im, (dx, dy))

        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rounded_rectangle(
            [dx - 2, dy - 2, dx + target_w + 2, dy + target_h + 2],
            radius=10,
            outline=(255, 112, 67, 210),
            width=2,
        )

        # Map dialog-relative cursor to 1280x720 frame
        cx = dx + cur_dlg_x * (target_w / max(1, dlg.width()))
        cy = dy + cur_dlg_y * (target_h / max(1, dlg.height()))
        click_p = 0.6 if (sf >= 50 and (sf - 50) % 6 == 0) else 0.0
        draw_cursor(draw, cx, cy, click_progress=click_p, is_dragging=(sf < 50))

        draw_callout_badge(
            draw,
            "128×64 OLED PIXEL & ANIMATION PAINTER",
            "Multi-Frame Onion-Skinning • Live PROGMEM C Hex Generation",
            tag="EXPRESSION STUDIO",
            pos="bottom-left",
            beat_energy=energy,
        )

        flash = 0.45 if sf == 0 else 0.0
        frame_im = add_top_progress_and_frame_polish(frame_im, overlay, energy, flash_alpha=flash)
        write_frame(frame_im)

    dlg.close()
    app.processEvents()

    # =========================================================================
    # ACT VI: DIRECT face-bitmaps.h FIRMWARE EDITING & C++ CODE EXPORT
    #         (Frames 445 - 512 | 18.54s - 21.33s)
    # =========================================================================
    print("[Act 6/8] Direct face-bitmaps.h Firmware Sync & C++ Export (Frames 445-512)...")
    header_path = str(FIRMWARE_DIR / "face-bitmaps.h")
    win.open_face_bitmaps_path(header_path)
    win.face_dock.raise_()
    win.viewport._yaw = 165.0
    win.viewport._pitch = 16.0
    win.viewport._distance = 135.0
    app.processEvents()

    save_h_lx, save_h_ly = widget_center_in_win(win.face_library.save_header_btn, win)
    list_tl = win.face_library.list_widget.mapTo(win, QPoint(0, 0))

    code_sample = """// Generated by Sesame Studio v2.0 — Ready for Arduino / ESP32
#include "face-bitmaps.h"

void runDanceSequence() {
    // Frame 1: Center stance + animated dance expression
    setServoAngle(L1, 90);  setServoAngle(R1, 90);
    setServoAngle(L2, 90);  setServoAngle(R2, 90);
    setServoAngle(L3, 15);  setServoAngle(R3, 165);
    setServoAngle(L4, 15);  setServoAngle(R4, 165);
    setFaceAnim(epd_bitmap_dance_frames, 2, 4, FACE_LOOP);
    delay(300);

    // Frame 2: Rhythm step + arm wave
    setServoAngle(R4, 115); setServoAngle(R3, 115);
    setServoAngle(L1, 65);  setServoAngle(R1, 115);
    delay(300);
}"""
    code_dlg = CodeOutputDialog("Export Arduino C++ Sequence — dance", code_sample, "movement-sequences.h", parent=win)
    code_dlg.resize(820, 480)

    showcase_faces = ["happy", "love", "freaky", "excited", "cute"]

    for f in range(445, 513):
        sf = f - 445
        energy = float(rms[f])

        if sf < 38:
            # Part A: Clicking through faces in 'Editing: face-bitmaps.h' mode and clicking 'Save .h'
            f_idx = min(len(showcase_faces) - 1, sf // 7)
            chosen_face = showcase_faces[f_idx]
            win.viewport.set_face_preview(win._resolve_face_pixels(chosen_face))

            # Select matching item in list_widget for visual highlight
            for row_i in range(win.face_library.list_widget.count()):
                it = win.face_library.list_widget.item(row_i)
                if it and it.data(Qt.UserRole) == chosen_face:
                    win.face_library.list_widget.setCurrentItem(it)
                    break

            # Open-armed expressive pose so the 3D robot's OLED face is 100% unobstructed!
            pose = dict(STAND_STATE)
            pose["L3"] = int(45 + 15 * math.sin(sf * 0.3))
            pose["R3"] = int(135 - 15 * math.sin(sf * 0.3))
            pose["L1"] = int(80 + 10 * math.cos(sf * 0.25))
            pose["R1"] = int(100 - 10 * math.cos(sf * 0.25))
            win.viewport.set_angles(pose)
            win.viewport._yaw = 158.0 + sf * 0.45
            win.viewport._pitch = 18.0
            win.viewport._distance = 142.0
            win.viewport.update()

            if sf == 22:
                from app.widgets.toast import Toast
                active_toast = Toast(win.viewport, "✓ Saved face-bitmaps.h (37 faces updated)", "success")
                active_toast._fade_in.stop()
                active_toast._effect.setOpacity(1.0)

            app.processEvents()

            # Zoom in (1.26x) on the 3D Robot head + Face Library ('Editing: face-bitmaps.h' banner)
            zoom = 1.26
            cam_cx, cam_cy = 740.0, 270.0
            raw_im = qpixmap_to_pil_native(win.grab())
            frame_im, vx0, vy0, cur_z = apply_camera(raw_im, cam_cx, cam_cy, zoom)

            overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            if sf < 20:
                # Cursor clicking face thumbnails
                col = f_idx % 3
                row = f_idx // 3
                thumb_lx = list_tl.x() + 55 + col * 108
                thumb_ly = list_tl.y() + 45 + row * 72
                cx, cy = map_to_cam(thumb_lx, thumb_ly, vx0, vy0, cur_z)
                click_p = ((sf % 7) / 5.0) if (sf % 7 < 5) else 0.0
            else:
                # Cursor moves to 'Save .h' button and clicks it!
                cx, cy = map_to_cam(save_h_lx, save_h_ly, vx0, vy0, cur_z)
                click_p = (sf - 20) / 8.0 if 20 <= sf <= 28 else 0.0

            draw_cursor(draw, cx, cy, click_progress=click_p)
            draw_callout_badge(
                draw,
                "DIRECT ARDUINO face-bitmaps.h EDITING",
                "Open, Edit & Save Firmware Bitmaps In-Place — Zero Copy-Paste",
                tag="FIRMWARE INTEGRATION",
                pos="bottom-left",
                beat_energy=energy,
            )
            frame_im = add_top_progress_and_frame_polish(frame_im, overlay, energy)
        else:
            # Part B: One-Click Arduino C++ Sequence Exporter Dialog
            if sf == 38 and "active_toast" in locals() and active_toast is not None:
                active_toast.hide()
            if not code_dlg.isVisible():
                code_dlg.show()
                app.processEvents()

            dlg_raw = qpixmap_to_pil_native(code_dlg.grab())
            tw, th = 860, 500
            dlg_im = dlg_raw.resize((tw, th), Image.Resampling.LANCZOS)

            frame_im = studio_backdrop.copy()
            dx = (WIDTH - tw) // 2
            dy = (HEIGHT - th) // 2 - 20
            frame_im.paste(dlg_im, (dx, dy))

            overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            draw.rounded_rectangle(
                [dx - 2, dy - 2, dx + tw + 2, dy + th + 2],
                radius=10,
                outline=(255, 112, 67, 220),
                width=2,
            )

            # Cursor clicks 'Copy to Clipboard' button at bottom-left of CodeOutputDialog
            cx = dx + 95
            cy = dy + th - 28
            click_p = (sf - 48) / 8.0 if 48 <= sf <= 56 else 0.0
            draw_cursor(draw, cx, cy, click_progress=click_p)

            draw_callout_badge(
                draw,
                "ONE-CLICK ARDUINO C++ CODE EXPORT",
                "Generates Clean ESP32 / Arduino Servo & Face Firmware Functions",
                tag="PRODUCTION READY",
                pos="bottom-left",
                beat_energy=energy,
            )
            frame_im = add_top_progress_and_frame_polish(frame_im, overlay, energy)

        write_frame(frame_im)

    code_dlg.close()

    # =========================================================================
    # ACT VII: CLIMAX RAPID-FIRE FULL-SCREEN 3D ROBOT MONTAGE
    #          (Frames 513 - 569 | 21.38s - 23.71s)
    #   Full-bleed 1280x720 3D Viewport! 4 High-Energy Cuts on the Climax Roll:
    #   1. PUSHUP (513-526) | 2. CUTE (527-540) | 3. WAVE (541-554) | 4. WORM (555-569)
    # =========================================================================
    print("[Act 7/8] Climax Rapid-Fire Full-Screen 3D Showcase (Frames 513-569)...")
    win._set_editor_docks_visible(False)
    win.menuBar().setVisible(False)
    win.statusBar().setVisible(False)
    app.processEvents()

    for f in range(513, 570):
        sf = f - 513
        energy = float(rms[f])

        if sf < 14:
            # Cut 1: PUSHUPS
            local_f = sf
            pose, _ = pose_sequence_smooth(PUSHUP, local_f * 95.0)
            face_name = "pushup"
            win.viewport._yaw = 148.0 + local_f * 2.2
            win.viewport._pitch = 15.0
            win.viewport._distance = 132.0
            k_word = "POSE"
            k_sub = "8-Servo Real-Time Kinematics"
        elif sf < 28:
            # Cut 2: CUTE WIGGLE
            local_f = sf - 14
            pose, _ = pose_sequence_smooth(CUTE, local_f * 95.0)
            face_name = "cute"
            win.viewport._yaw = 195.0 - local_f * 2.2
            win.viewport._pitch = 20.0
            win.viewport._distance = 128.0
            k_word = "EXPRESS"
            k_sub = "37+ Built-In & Custom OLED Faces"
        elif sf < 42:
            # Cut 3: WAVE
            local_f = sf - 28
            pose, _ = pose_sequence_smooth(WAVE, local_f * 95.0)
            face_name = "excited"
            win.viewport._yaw = 160.0 + local_f * 2.0
            win.viewport._pitch = 16.0
            win.viewport._distance = 126.0
            k_word = "ANIMATE"
            k_sub = "Interpolated Keyframe Timeline"
        else:
            # Cut 4: WORM DANCE
            local_f = sf - 42
            pose, _ = pose_sequence_smooth(WORM, local_f * 95.0)
            face_name = "freaky"
            win.viewport._yaw = 152.0 + math.sin(local_f * 0.45) * 28.0
            win.viewport._pitch = 22.0 + math.cos(local_f * 0.45) * 6.0
            win.viewport._distance = 130.0
            k_word = "DEPLOY"
            k_sub = "Direct C++ & face-bitmaps.h Sync"

        win.viewport._target = np.array([26.0, 26.0, 2.0], dtype=np.float32)
        win.viewport.set_angles(pose)
        win.viewport.set_face_preview(win._resolve_face_pixels(face_name))
        win.viewport.update()
        app.processEvents()

        raw_im = qpixmap_to_pil_native(win.viewport.grab())
        frame_im = raw_im.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Kinetic typography card in the top-left corner so the center 3D robot is 100% clear
        pop = max(0.0, 1.0 - (local_f / 5.0))
        bx, by, bw, bh = 42, 42, 430, 108
        draw.rounded_rectangle(
            [bx, by, bx + bw, by + bh],
            radius=14,
            fill=(14, 15, 22, 230),
            outline=(255, 112, 67, int(200 + 55 * pop)),
            width=3,
        )
        draw.rounded_rectangle([bx + 8, by + 12, bx + 14, by + bh - 12], radius=3, fill=(255, 112, 67, 255))
        draw.text((bx + 28, by + 12), k_word, font=FONT_PUNCH, fill=(255, 255, 255, 255))
        draw.text((bx + 30, by + 68), k_sub, font=FONT_PUNCH_SUB, fill=(255, 150, 105, 245))

        flash = 0.55 if local_f == 0 else 0.0
        frame_im = add_top_progress_and_frame_polish(frame_im, overlay, energy, flash_alpha=flash)
        write_frame(frame_im)

    # =========================================================================
    # ACT VIII: FINAL TRIUMPHANT CHORD HIT — LIVE 3D + HERO END CARD
    #           (Frames 570 - 605 | 23.75s - 25.25s)
    # =========================================================================
    print("[Act 8/8] Final Chord Hit: Live 3D Hero End Card (Frames 570-605)...")

    for f in range(570, TOTAL_FRAMES):
        sf = f - 570
        t = sf / max(1, TOTAL_FRAMES - 570 - 1)
        energy = float(rms[f])

        # Live 3D robot on the left side of the screen doing a friendly open-armed wave + happy/love face!
        pose = dict(STAND_STATE)
        pose["R3"] = int(135 + 25 * math.sin(sf * 0.45))
        pose["R4"] = int(130 + 25 * math.sin(sf * 0.45))
        pose["L3"] = 45
        pose["L4"] = 50
        win.viewport.set_angles(pose)
        win.viewport.set_face_preview(win._resolve_face_pixels("love" if sf > 16 else "happy"))
        win.viewport._yaw = 158.0 + 12.0 * math.sin(t * math.pi)
        win.viewport._pitch = 17.0
        win.viewport._distance = 165.0
        win.viewport._target = np.array([15.0, 24.0, 48.0], dtype=np.float32)
        win.viewport.update()
        app.processEvents()

        raw_im = qpixmap_to_pil_native(win.viewport.grab())
        frame_im = raw_im.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

        # True RGBA Gaussian-blurred neon studio glow behind the right hero panel
        glow_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow_layer)
        gdraw.ellipse([540, 60, 1220, 660], fill=(255, 112, 67, 75))
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(65))

        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        overlay = Image.alpha_composite(overlay, glow_layer)
        draw = ImageDraw.Draw(overlay)

        # Right-side Hero Finale Card (x: 520..1230)
        cx0, cy0, cw, ch = 510, 64, 725, 592
        draw.rounded_rectangle(
            [cx0, cy0, cx0 + cw, cy0 + ch],
            radius=20,
            fill=(14, 15, 22, 238),
            outline=(255, 112, 67, 235),
            width=2,
        )

        # Logo + Title Header
        if logo_img:
            lw = 115
            r_logo = logo_img.resize((lw, lw), Image.Resampling.LANCZOS)
            overlay.paste(r_logo, (cx0 + 40, cy0 + 36), r_logo)

        draw.rounded_rectangle(
            [cx0 + 175, cy0 + 42, cx0 + 435, cy0 + 68],
            radius=6,
            fill=(255, 112, 67, 50),
            outline=(255, 112, 67, 200),
        )
        draw.text((cx0 + 188, cy0 + 47), "PORTABLE WINDOWS RELEASE", font=FONT_TAG, fill=(255, 155, 110, 255))
        draw.text((cx0 + 175, cy0 + 74), "SESAME STUDIO v2.0", font=FONT_TITLE_BIG, fill=(255, 255, 255, 255))
        draw.text(
            (cx0 + 175, cy0 + 124),
            "Animation & Expression Composer for Sesame Robot",
            font=FONT_TITLE_SUB,
            fill=(195, 205, 220, 255),
        )

        # 2x2 Feature Grid Pills
        features = [
            ("3D STL Viewport", "Live 8-Servo Forward Kinematics"),
            ("Keyframe Sequencer", "Smooth Interpolated Playback"),
            ("128×64 OLED Painter", "Multi-Frame & Onion-Skinning"),
            ("Direct .h Firmware Sync", "Edit & Save face-bitmaps.h Live"),
        ]
        for idx_f, (ftitle, fsub) in enumerate(features):
            col = idx_f % 2
            row = idx_f // 2
            px = cx0 + 40 + col * 330
            py = cy0 + 185 + row * 92
            draw.rounded_rectangle(
                [px, py, px + 312, py + 76],
                radius=10,
                fill=(24, 26, 36, 240),
                outline=(65, 70, 88, 220),
                width=1,
            )
            draw.rounded_rectangle([px + 6, py + 12, px + 10, py + 64], radius=2, fill=(255, 112, 67, 255))
            draw.text((px + 20, py + 14), ftitle, font=FONT_PILL, fill=(255, 255, 255, 255))
            draw.text((px + 20, py + 38), fsub, font=FONT_SUB, fill=(175, 185, 205, 240))

        # Call-To-Action Box
        cta_x, cta_y, cta_w, cta_h = cx0 + 40, cy0 + 395, cw - 80, 150
        draw.rounded_rectangle(
            [cta_x, cta_y, cta_x + cta_w, cta_y + cta_h],
            radius=14,
            fill=(22, 20, 28, 250),
            outline=(255, 112, 67, 255),
            width=2,
        )
        cta_h1 = "FREE & OPEN SOURCE — DOWNLOAD v2.0 NOW"
        w1 = draw.textlength(cta_h1, font=FONT_BOLD)
        draw.text((cta_x + (cta_w - w1) / 2, cta_y + 22), cta_h1, font=FONT_BOLD, fill=(255, 135, 90, 255))

        url_str = "github.com/dorianborian/sesame-studio"
        w2 = draw.textlength(url_str, font=FONT_MONO)
        draw.rounded_rectangle(
            [cta_x + (cta_w - w2) / 2 - 18, cta_y + 62, cta_x + (cta_w + w2) / 2 + 18, cta_y + 98],
            radius=8,
            fill=(12, 13, 18, 255),
            outline=(80, 85, 105, 200),
        )
        draw.text((cta_x + (cta_w - w2) / 2, cta_y + 71), url_str, font=FONT_MONO, fill=(255, 255, 255, 255))

        rel_str = "Includes Portable Windows Executable (SesameStudio.exe) & Full Python Source"
        w3 = draw.textlength(rel_str, font=FONT_SUB)
        draw.text((cta_x + (cta_w - w3) / 2, cta_y + 112), rel_str, font=FONT_SUB, fill=(185, 195, 210, 235))

        flash = 0.75 if sf == 0 else (0.35 if sf == 1 else 0.0)
        frame_im = add_top_progress_and_frame_polish(frame_im, overlay, energy, flash_alpha=flash)

        # Smooth fade to black on the final 8 frames as the chord finishes decaying
        remaining = TOTAL_FRAMES - 1 - f
        if remaining < 8:
            fade_t = 1.0 - (remaining / 8.0)
            frame_im = Image.blend(frame_im, Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0)), smoothstep(fade_t))

        write_frame(frame_im)

    pipe.stdin.close()
    pipe.wait()
    win.close()

    print("\n" + "=" * 68)
    print("TRAILER v2 GENERATED SUCCESSFULLY!")
    print(f"Output: {OUTPUT_MP4}")
    if os.path.exists(OUTPUT_MP4):
        print(f"Size:   {os.path.getsize(OUTPUT_MP4) / (1024 * 1024):.2f} MB")
    print("=" * 68)


if __name__ == "__main__":
    build_trailer()
