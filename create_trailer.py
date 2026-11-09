#!/usr/bin/env python3
"""Sesame Studio Official Video Trailer Generator.

Creates a 720p 24fps ~25-second trailer showcasing all of Sesame Studio's
real features, synchronized to '1-13. Mix Me In, Too.mp3'.
Directly grabs frames from the live PySide6 application and encodes to MP4 via ffmpeg.
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

from PySide6.QtCore import Qt, QPoint, QRect, QSize
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QImage, QPixmap
from PySide6.QtWidgets import QApplication

from app.theme import STYLESHEET, ACCENT, BG, PANEL
from app.project import Project, Face, blank_frame
from app.main_window import MainWindow, CodeOutputDialog
from app.face_editor import FaceEditorDialog, frame_bytes_to_image
from app.widgets.toast import show_toast
from app.builtin_sequences import DANCE, WAVE, PUSHUP, CUTE, WORM, STAND_STATE, REST_STATE
from app.builtin_faces import BUILTIN_FACES
from app.paths import LOGO_PATH, FIRMWARE_DIR, ASSETS_DIR

# Trailer Constants
WIDTH = 1280
HEIGHT = 720
FPS = 24
TOTAL_FRAMES = 606  # 25.25 seconds @ 24fps
AUDIO_PATH = "C:/Users/doria/Downloads/1-13. Mix Me In, Too.mp3"
OUTPUT_MP4 = str(STUDIO_DIR / "dist" / "SesameStudio_Trailer.mp4")

# Fonts
FONT_BOLD = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 18)
FONT_SUB = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 13)
FONT_TAG = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 10)
FONT_TITLE_BIG = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 36)
FONT_TITLE_SUB = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 18)
FONT_PUNCH = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 44)
FONT_PILL = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 12)


def qpixmap_to_pil(pix: QPixmap, target_w: int = WIDTH, target_h: int = HEIGHT) -> Image.Image:
    qimg = pix.toImage().convertToFormat(QImage.Format_RGB888)
    w, h = qimg.width(), qimg.height()
    ptr = qimg.constBits()
    bpl = qimg.bytesPerLine()
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape((h, bpl))
    arr = arr[:, :w * 3].reshape((h, w, 3))
    im = Image.fromarray(arr, "RGB")
    if im.size != (target_w, target_h):
        im = im.resize((target_w, target_h), Image.Resampling.LANCZOS)
    return im


def draw_cursor(overlay: ImageDraw.ImageDraw, x: float, y: float, click_progress: float = 0.0) -> None:
    """Draws a clean OS mouse pointer and optional click ripple."""
    if click_progress > 0.0:
        r = 6 + click_progress * 18
        alpha = int((1.0 - click_progress) * 220)
        overlay.ellipse(
            [x - r, y - r, x + r, y + r],
            outline=(255, 112, 67, alpha),
            width=2
        )
    # Cursor polygon
    pts = [(x, y), (x, y + 20), (x + 5, y + 16), (x + 9, y + 25),
           (x + 13, y + 23), (x + 9, y + 14), (x + 15, y + 14)]
    overlay.polygon(pts, fill=(255, 255, 255, 255), outline=(0, 0, 0, 255))


def draw_callout_badge(overlay: ImageDraw.ImageDraw, title: str, subtitle: str, tag: str = "FEATURE HIGHLIGHT", alpha: float = 1.0) -> None:
    """Draws a sleek glassmorphic lower-third badge on the frame."""
    if alpha <= 0.01:
        return
    bx, by, bw, bh = 40, 618, 580, 68
    a_box = int(alpha * 235)
    a_border = int(alpha * 180)
    a_bar = int(alpha * 255)
    a_title = int(alpha * 255)
    a_sub = int(alpha * 220)

    overlay.rounded_rectangle(
        [bx, by, bx + bw, by + bh],
        radius=10,
        fill=(18, 18, 22, a_box),
        outline=(65, 65, 75, a_border),
        width=1
    )
    # Left accent bar
    overlay.rounded_rectangle(
        [bx + 4, by + 10, bx + 8, by + bh - 10],
        radius=2,
        fill=(255, 112, 67, a_bar)
    )
    # Texts
    overlay.text((bx + 22, by + 9), tag.upper(), font=FONT_TAG, fill=(255, 112, 67, a_bar))
    overlay.text((bx + 22, by + 23), title.upper(), font=FONT_BOLD, fill=(255, 255, 255, a_title))
    overlay.text((bx + 22, by + 46), subtitle, font=FONT_SUB, fill=(180, 190, 205, a_sub))


def draw_punch_card(im: Image.Image, text: str, subtitle: str, beat_t: float) -> Image.Image:
    """Renders a punchy, bold centered action card for high-energy cuts."""
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Scale effect with beat
    scale = 1.0 + max(0.0, 0.15 * (1.0 - beat_t))
    box_w, box_h = int(500 * scale), int(100 * scale)
    cx, cy = WIDTH // 2, HEIGHT - 140

    x0, y0 = cx - box_w // 2, cy - box_h // 2
    x1, y1 = cx + box_w // 2, cy + box_h // 2

    draw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=16,
        fill=(14, 14, 18, 230),
        outline=(255, 112, 67, 240),
        width=2
    )

    t_w = draw.textlength(text, font=FONT_PUNCH)
    draw.text((cx - t_w // 2, cy - 28), text, font=FONT_PUNCH, fill=(255, 255, 255, 255))
    s_w = draw.textlength(subtitle, font=FONT_SUB)
    draw.text((cx - s_w // 2, cy + 22), subtitle, font=FONT_SUB, fill=(255, 150, 100, 240))

    return Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")


def bezier_interp(p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float], t: float) -> tuple[float, float]:
    """Quadratic bezier curve for natural human mouse motion."""
    t = max(0.0, min(1.0, t))
    u = 1.0 - t
    x = u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0]
    y = u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]
    return (x, y)


def build_trailer():
    print("=" * 60)
    print("Sesame Studio Trailer Generator (720p 24fps)")
    print(f"Target duration: {TOTAL_FRAMES / FPS:.2f}s ({TOTAL_FRAMES} frames)")
    print(f"Audio source:    {AUDIO_PATH}")
    print(f"Output target:   {OUTPUT_MP4}")
    print("=" * 60)

    # Initialize PySide6
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)

    win = MainWindow()
    win.resize(1280, 720)
    win.show()
    app.processEvents()

    # Setup ffmpeg pipeline
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{WIDTH}x{HEIGHT}",
        "-pix_fmt", "rgb24",
        "-r", str(FPS),
        "-i", "-",  # video stdin
        "-i", AUDIO_PATH,
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-af", f"afade=t=out:st={TOTAL_FRAMES/FPS - 0.5}:d=0.5",
        "-shortest",
        OUTPUT_MP4
    ]

    pipe = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def write_frame(im: Image.Image):
        if im.size != (WIDTH, HEIGHT):
            im = im.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        pipe.stdin.write(im.tobytes())

    # Preload project sequences and faces into UI
    win.project = Project(name="Trailer Studio Demo")
    win._load_project_into_ui()
    win.stack.setCurrentWidget(win.viewport)
    win.viewport.setGraphicsEffect(None)
    win._set_editor_docks_visible(True)
    win.timeline.load_project_sequences({"dance": DANCE, "wave": WAVE, "pushup": PUSHUP, "cute": CUTE, "worm": WORM})
    win.face_library.load_faces(dict(BUILTIN_FACES))
    app.processEvents()

    # Load logo image for splash & outro
    logo_img = None
    if os.path.exists(LOGO_PATH):
        logo_img = Image.open(LOGO_PATH).convert("RGBA")

    # =========================================================================
    # SCENE 1: INTRO & LAUNCH (Frames 0 - 47 | 0.00s - 1.96s)
    # =========================================================================
    print("[Scene 1/8] Intro & Workspace Launch (Frames 0-47)...")
    win.stack.setCurrentWidget(win.welcome_page)
    win._set_editor_docks_visible(False)
    app.processEvents()
    welcome_pix = win.grab()
    welcome_im = qpixmap_to_pil(welcome_pix)

    for f in range(0, 48):
        t = f / 47.0
        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        if f < 22:
            # Splash screen display with gentle pulse
            splash_bg = Image.new("RGB", (WIDTH, HEIGHT), (16, 16, 18))
            s_draw = ImageDraw.Draw(splash_bg)

            # Radial glow
            for r in range(240, 20, -20):
                alpha = int((1.0 - r / 240) * 35)
                s_draw.ellipse([WIDTH//2 - r, HEIGHT//2 - r - 30, WIDTH//2 + r, HEIGHT//2 + r - 30], fill=(255, 112, 67, alpha))

            # Draw Logo
            if logo_img:
                scale = 0.98 + 0.04 * math.sin(f / 22.0 * math.pi)
                lw, lh = int(160 * scale), int(160 * scale)
                r_logo = logo_img.resize((lw, lh), Image.Resampling.LANCZOS)
                splash_bg.paste(r_logo, (WIDTH//2 - lw//2, HEIGHT//2 - lh//2 - 40), r_logo)

            s_title = "SESAME STUDIO"
            s_sub = "Animation & Expression Composer for Sesame Robot"
            tw = s_draw.textlength(s_title, font=FONT_TITLE_BIG)
            s_draw.text((WIDTH//2 - tw//2, HEIGHT//2 + 70), s_title, font=FONT_TITLE_BIG, fill=(255, 112, 67))
            sw = s_draw.textlength(s_sub, font=FONT_TITLE_SUB)
            s_draw.text((WIDTH//2 - sw//2, HEIGHT//2 + 120), s_sub, font=FONT_TITLE_SUB, fill=(180, 190, 205))

            # Fade in/out to welcome page
            if f >= 18:
                fade_alpha = (f - 18) / 4.0
                frame_im = Image.blend(splash_bg, welcome_im, fade_alpha)
            else:
                frame_im = splash_bg
        else:
            # Welcome page with cursor moving towards "New Project"
            frame_im = welcome_im.copy()
            # Cursor travels from (900, 580) to "New Project" at (310, 260)
            c_t = (f - 22) / 25.0
            cx, cy = bezier_interp((900, 580), (600, 380), (310, 260), c_t)
            click_p = max(0.0, (f - 42) / 5.0) if f >= 42 else 0.0

            draw_cursor(draw, cx, cy, click_p)
            draw_callout_badge(draw, "WELCOME TO SESAME STUDIO", "Visual Animation & Expression Design Suite", tag="GETTING STARTED")
            frame_im = Image.alpha_composite(frame_im.convert("RGBA"), overlay).convert("RGB")

            # Wipe transition at very end into Scene 2
            if f >= 45:
                wipe_t = (f - 44) / 3.0
                wipe_overlay = Image.new("RGB", (WIDTH, HEIGHT), (255, 112, 67))
                frame_im = Image.blend(frame_im, wipe_overlay, wipe_t * 0.4)

        write_frame(frame_im)

    # =========================================================================
    # SCENE 2: 3D POSABLE VIEWPORT (Frames 48 - 143 | 1.96s - 5.96s)
    # =========================================================================
    print("[Scene 2/8] 3D Posable Viewport & Kinematics (Frames 48-143)...")
    win.stack.setCurrentWidget(win.viewport)
    win.viewport.setGraphicsEffect(None)
    win._set_editor_docks_visible(True)
    win.inspector_dock.raise_()
    win.viewport.reset_view()
    app.processEvents()

    for f in range(48, 144):
        sf = f - 48
        t = sf / 95.0

        # Camera orbiting around the robot
        win.viewport._yaw = -42.0 + 70.0 * (0.5 - 0.5 * math.cos(t * math.pi))
        win.viewport._pitch = 18.0 + 8.0 * math.sin(t * math.pi)

        # Servo slider animation
        angles = dict(STAND_STATE)
        if sf < 45:
            # Posing legs (L1 & R1)
            drag_t = sf / 45.0
            l1_val = int(45 + 50 * math.sin(drag_t * math.pi * 2))
            r1_val = int(135 - 50 * math.sin(drag_t * math.pi * 2))
            angles["L1"] = l1_val
            angles["R1"] = r1_val
            c_pos = (1120, 160 + int(drag_t * 30))
        else:
            # Posing arms (L3 & R3)
            drag_t = (sf - 45) / 50.0
            l3_val = int(0 + 130 * math.sin(drag_t * math.pi))
            r3_val = int(180 - 130 * math.sin(drag_t * math.pi))
            angles["L3"] = l3_val
            angles["R3"] = r3_val
            angles["L2"] = int(135 - 35 * math.sin(drag_t * math.pi))
            c_pos = (1120, 240 + int(drag_t * 40))

        win.viewport.set_angles(angles)
        win.inspector.load_angles(angles)
        win.viewport.update()
        app.processEvents()

        frame_pix = win.grab()
        frame_im = qpixmap_to_pil(frame_pix)

        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        draw_cursor(draw, c_pos[0], c_pos[1], click_progress=0.0)
        draw_callout_badge(
            draw,
            "3D POSABLE VIEWPORT",
            "Real Printed STL Geometry • Live Kinematics & Joint Angles",
            tag="REAL-TIME RIGGING"
        )
        frame_im = Image.alpha_composite(frame_im.convert("RGBA"), overlay).convert("RGB")
        write_frame(frame_im)

    # =========================================================================
    # SCENE 3: KEYFRAME TIMELINE & SEQUENCER (Frames 144 - 239 | 5.96s - 9.96s)
    # =========================================================================
    print("[Scene 3/8] Keyframe Timeline & Sequencer (Frames 144-239)...")
    idx = win.timeline.seq_combo.findText("dance")
    if idx >= 0:
        win.timeline.seq_combo.setCurrentIndex(idx)
        win.timeline._on_sequence_selected(idx)
    win.viewport.reset_view()
    win.viewport._yaw = -25.0
    win.viewport._pitch = 20.0
    app.processEvents()

    num_dance_frames = len(DANCE.frames)

    for f in range(144, 240):
        sf = f - 144
        t = sf / 95.0

        # Timeline playhead moves across frames
        frame_idx = int((sf / 95.0) * num_dance_frames) % num_dance_frames
        win.timeline.select_frame(frame_idx)

        # Pose robot according to dance sequence frame
        cur_pose = DANCE.frames[frame_idx].angles
        win.viewport.set_angles(cur_pose)
        face_name = DANCE.frames[frame_idx].face or "dance"
        win.viewport.set_face_preview(win._resolve_face_pixels(face_name))

        # Camera subtle bob in sync with the beat
        win.viewport._pitch = 20.0 + 4.0 * math.sin(sf * 0.4)
        win.viewport._yaw = -25.0 + 15.0 * math.sin(sf * 0.15)
        win.viewport.update()
        app.processEvents()

        frame_pix = win.grab()
        frame_im = qpixmap_to_pil(frame_pix)

        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Cursor hovering near timeline controls
        c_x = 180 + int(sf * 3.5) % 400
        c_y = 660
        draw_cursor(draw, c_x, c_y, click_progress=0.0)

        draw_callout_badge(
            draw,
            "KEYFRAME TIMELINE",
            "Sequence, Retime & Preview Complex Robot Motions Live",
            tag="MOTION SEQUENCER"
        )
        frame_im = Image.alpha_composite(frame_im.convert("RGBA"), overlay).convert("RGB")
        write_frame(frame_im)

    # =========================================================================
    # SCENE 4: 128x64 OLED FACE PAINTER (Frames 240 - 323 | 9.96s - 13.46s)
    # =========================================================================
    print("[Scene 4/8] 128x64 OLED Face Painter (Frames 240-323)...")
    happy_face = BUILTIN_FACES.get("happy") or Face(name="happy", frames=[blank_frame()])
    dlg = FaceEditorDialog(happy_face, set(), parent=win)
    dlg.resize(920, 600)
    dlg.show()
    app.processEvents()

    # Base workspace background (dimmed)
    win_im = qpixmap_to_pil(win.grab())
    dim_win = Image.blend(win_im, Image.new("RGB", (WIDTH, HEIGHT), (10, 10, 14)), 0.65)

    for f in range(240, 324):
        sf = f - 240
        t = sf / 83.0

        # Draw evolving pixels on the canvas
        painter = QPainter(dlg.canvas.image)
        painter.setPen(QPen(QColor("white"), 2))

        # Frames 0-35: Draw sparkling eyes
        if sf < 35:
            e_t = sf / 35.0
            eye_rad = int(e_t * 12)
            if eye_rad > 2:
                painter.drawEllipse(32 - eye_rad, 24 - eye_rad, eye_rad * 2, eye_rad * 2)
                painter.drawEllipse(96 - eye_rad, 24 - eye_rad, eye_rad * 2, eye_rad * 2)
                # Sparkles
                painter.setPen(QPen(QColor("black"), 1))
                painter.drawPoint(34, 22)
                painter.drawPoint(98, 22)
        # Frames 35-60: Draw smiling mouth
        elif sf < 60:
            m_t = (sf - 35) / 25.0
            span_angle = int(m_t * 180 * 16)
            painter.setPen(QPen(QColor("white"), 2))
            painter.drawArc(44, 32, 40, 22, 0, -span_angle)
        # Frames 60+: Blinking eyelid stroke & animation preview
        else:
            painter.setPen(QPen(QColor("white"), 2))
            painter.drawLine(20, 24, 44, 24)
            painter.drawLine(84, 24, 108, 24)

        painter.end()
        dlg.canvas.update()
        dlg._on_canvas_changed()
        app.processEvents()

        dlg_pix = dlg.grab()
        dlg_im = qpixmap_to_pil(dlg_pix, target_w=980, target_h=580)

        # Composite dialog centered on dimmed studio
        frame_im = dim_win.copy()
        dx = (WIDTH - 980) // 2
        dy = (HEIGHT - 580) // 2
        frame_im.paste(dlg_im, (dx, dy))

        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Cursor drawing on the canvas
        cx = dx + 260 + int(math.sin(sf * 0.3) * 60)
        cy = dy + 220 + int(math.cos(sf * 0.3) * 40)
        draw_cursor(draw, cx, cy, click_progress=0.0)

        draw_callout_badge(
            draw,
            "128×64 OLED FACE PAINTER",
            "Pixel Drawing • Onion-Skinning • Real-Time C Hex Output",
            tag="EXPRESSION DESIGN"
        )
        frame_im = Image.alpha_composite(frame_im.convert("RGBA"), overlay).convert("RGB")
        write_frame(frame_im)

    dlg.close()

    # =========================================================================
    # SCENE 5: FACE LIBRARY & DIRECT .H EDITING (Frames 324 - 407 | 13.46s - 16.96s)
    # =========================================================================
    print("[Scene 5/8] Face Library & Direct Firmware Header Editing (Frames 324-407)...")
    header_path = str(FIRMWARE_DIR / "face-bitmaps.h")
    win.open_face_bitmaps_path(header_path)
    win.face_dock.raise_()
    win.viewport.reset_view()
    win.viewport._yaw = -30.0
    win.viewport._pitch = 18.0
    app.processEvents()

    for f in range(324, 408):
        sf = f - 324
        t = sf / 83.0

        # Change faces on robot screen in rhythm
        face_list = ["dance", "wave", "happy", "freaky", "love", "sleepy"]
        f_idx = int(sf / 14) % len(face_list)
        f_name = face_list[f_idx]
        win.viewport.set_face_preview(win._resolve_face_pixels(f_name))

        # Show toast popup near frame 370
        if sf == 45:
            show_toast(win, "Saved face-bitmaps.h (37 faces directly to firmware)", "success")

        win.viewport.update()
        app.processEvents()

        frame_pix = win.grab()
        frame_im = qpixmap_to_pil(frame_pix)

        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Cursor clicking on Face Library items
        cx = 1050 + int(math.sin(sf * 0.2) * 40)
        cy = 140 + int(sf * 2.5) % 200
        click_p = 0.8 if (sf % 20 < 4) else 0.0
        draw_cursor(draw, cx, cy, click_progress=click_p)

        draw_callout_badge(
            draw,
            "DIRECT FIRMWARE .H EDITING",
            "Open, Edit & Save Arduino face-bitmaps.h With Zero Copy-Paste",
            tag="FIRMWARE INTEGRATION"
        )
        frame_im = Image.alpha_composite(frame_im.convert("RGBA"), overlay).convert("RGB")
        write_frame(frame_im)

    # =========================================================================
    # SCENE 6: JOINT CALIBRATION & C++ CODE EXPORT (Frames 408 - 479 | 16.96s - 19.96s)
    # =========================================================================
    print("[Scene 6/8] Joint Calibration & Arduino C++ Export (Frames 408-479)...")
    win.calib_dock.raise_()
    win.viewport.set_calib_target("L1", "pivot")
    win.viewport.reset_view()
    win.viewport._yaw = -35.0
    win.viewport._pitch = 24.0
    app.processEvents()

    code_sample = """// Generated by Sesame Studio
void runDancePose() {
    setServoAngle(L1, 90); setServoAngle(R1, 90);
    setServoAngle(L2, 90); setServoAngle(R2, 90);
    setServoAngle(L3, 10); setServoAngle(R3, 160);
    setServoAngle(L4, 10); setServoAngle(R4, 160);
    delayWithFace(300, epd_bitmap_dance);
    setServoAngle(R4, 115); setServoAngle(R3, 115);
    delayWithFace(300, NULL);
}"""
    code_dlg = CodeOutputDialog("Export Motion Code - Dance", code_sample, "movement-sequences.h", parent=win)
    code_dlg.resize(760, 480)

    for f in range(408, 480):
        sf = f - 408

        if sf < 36:
            # Joint calibration 3D gizmo view
            win.viewport._yaw = -35.0 + sf * 0.8
            win.viewport.update()
            app.processEvents()
            frame_pix = win.grab()
            frame_im = qpixmap_to_pil(frame_pix)

            overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            # Cursor adjusting pivot slider
            draw_cursor(draw, 1140, 280 + int(math.sin(sf * 0.4) * 20), click_progress=0.0)
            draw_callout_badge(
                draw,
                "JOINT CALIBRATION PANEL",
                "Align 3D Pivot Coordinates & Rotation Axes with Real Prints",
                tag="3D CALIBRATION"
            )
            frame_im = Image.alpha_composite(frame_im.convert("RGBA"), overlay).convert("RGB")
        else:
            # Code Exporter Dialog
            if not code_dlg.isVisible():
                code_dlg.show()
                app.processEvents()

            code_pix = code_dlg.grab()
            code_im = qpixmap_to_pil(code_pix, target_w=820, target_h=480)

            frame_im = dim_win.copy()
            dx = (WIDTH - 820) // 2
            dy = (HEIGHT - 480) // 2
            frame_im.paste(code_im, (dx, dy))

            overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            # Cursor clicking Copy to Clipboard
            cx = dx + 650
            cy = dy + 440
            click_p = 0.8 if (sf > 55 and sf < 62) else 0.0
            draw_cursor(draw, cx, cy, click_progress=click_p)

            draw_callout_badge(
                draw,
                "ARDUINO C++ EXPORTER",
                "Generate Production-Ready Motion Sequences & Bitmaps",
                tag="ONE-CLICK CODE EXPORT"
            )
            frame_im = Image.alpha_composite(frame_im.convert("RGBA"), overlay).convert("RGB")

        write_frame(frame_im)

    code_dlg.close()

    # =========================================================================
    # SCENE 7: FAST ACTION CLIMAX (Frames 480 - 570 | 19.96s - 23.75s)
    # =========================================================================
    print("[Scene 7/8] Fast-Paced Action Climax (Frames 480-570)...")
    win.inspector_dock.raise_()
    win.viewport.set_calib_target(None)

    for f in range(480, 571):
        sf = f - 480

        # Beat 1 (480-509): PUSHUPS
        if sf < 30:
            p_t = sf / 30.0
            p_frame = PUSHUP.frames[int(p_t * len(PUSHUP.frames)) % len(PUSHUP.frames)]
            win.viewport.set_angles(p_frame.angles)
            win.viewport.set_face_preview(win._resolve_face_pixels("pushup"))
            win.viewport._yaw = -50.0 + sf * 1.2
            win.viewport._pitch = 14.0
            badge_text = "DESIGN"
            badge_sub = "Full 8-Servo Pose Control"
            beat_phase = (sf % 12) / 12.0

        # Beat 2 (510-539): CUTE & WAVE
        elif sf < 60:
            w_sf = sf - 30
            w_t = w_sf / 30.0
            w_frame = CUTE.frames[int(w_t * len(CUTE.frames)) % len(CUTE.frames)]
            win.viewport.set_angles(w_frame.angles)
            win.viewport.set_face_preview(win._resolve_face_pixels("cute"))
            win.viewport._yaw = 25.0 - w_sf * 1.5
            win.viewport._pitch = 22.0
            badge_text = "ANIMATE"
            badge_sub = "Keyframe Interpolation & Emotion Sync"
            beat_phase = (w_sf % 12) / 12.0

        # Beat 3 (540-570): WORM DANCE
        else:
            wm_sf = sf - 60
            wm_t = wm_sf / 31.0
            wm_frame = WORM.frames[int(wm_t * len(WORM.frames)) % len(WORM.frames)]
            win.viewport.set_angles(wm_frame.angles)
            win.viewport.set_face_preview(win._resolve_face_pixels("worm"))
            win.viewport._yaw = -15.0 + math.sin(wm_sf * 0.5) * 35.0
            win.viewport._pitch = 24.0 + math.cos(wm_sf * 0.5) * 8.0
            badge_text = "BRING TO LIFE"
            badge_sub = "Deploy Straight to Sesame Robot"
            beat_phase = (wm_sf % 12) / 12.0

        win.viewport.update()
        app.processEvents()

        frame_pix = win.grab()
        frame_im = qpixmap_to_pil(frame_pix)
        frame_im = draw_punch_card(frame_im, badge_text, badge_sub, beat_phase)
        write_frame(frame_im)

    # =========================================================================
    # SCENE 8: GRAND FINALE / OUTRO CARD (Frames 571 - 605 | 23.75s - 25.25s)
    # =========================================================================
    print("[Scene 8/8] Grand Finale & Outro Card (Frames 571-605)...")

    for f in range(571, TOTAL_FRAMES):
        sf = f - 571
        t = sf / (TOTAL_FRAMES - 571)

        # Background with subtle gradient & dark obsidian vignette
        outro_im = Image.new("RGB", (WIDTH, HEIGHT), (12, 12, 15))
        draw = ImageDraw.Draw(outro_im)

        # Glowing radial halo behind logo
        halo_rad = 320
        for r in range(halo_rad, 20, -25):
            alpha = int((1.0 - r / halo_rad) * 45)
            draw.ellipse(
                [WIDTH//2 - r, HEIGHT//2 - r - 80, WIDTH//2 + r, HEIGHT//2 + r - 80],
                fill=(255, 112, 67, alpha)
            )

        # Logo with subtle zoom
        if logo_img:
            scale = 1.0 + 0.03 * math.sin(t * math.pi)
            lw, lh = int(170 * scale), int(170 * scale)
            r_logo = logo_img.resize((lw, lh), Image.Resampling.LANCZOS)
            outro_im.paste(r_logo, (WIDTH//2 - lw//2, 70), r_logo)

        # Title
        t_str = "SESAME STUDIO v2.0"
        tw = draw.textlength(t_str, font=FONT_TITLE_BIG)
        draw.text((WIDTH//2 - tw//2, 260), t_str, font=FONT_TITLE_BIG, fill=(255, 112, 67))

        # Subtitle
        sub_str = "Animation & Expression Design Suite for Sesame Robot"
        sw = draw.textlength(sub_str, font=FONT_TITLE_SUB)
        draw.text((WIDTH//2 - sw//2, 310), sub_str, font=FONT_TITLE_SUB, fill=(240, 240, 245))

        # Feature Pills
        pills = ["3D Posable Viewport", "128x64 OLED Painter", "Direct .h Firmware Editing", "Portable Windows Executable"]
        pill_total_w = len(pills) * 190 + (len(pills) - 1) * 12
        px_start = (WIDTH - pill_total_w) // 2
        py = 370
        for i, pill in enumerate(pills):
            px = px_start + i * 202
            draw.rounded_rectangle([px, py, px + 190, py + 36], radius=8, fill=(25, 25, 32), outline=(55, 55, 68), width=1)
            pw = draw.textlength(pill, font=FONT_PILL)
            draw.text((px + (190 - pw) // 2, py + 9), pill, font=FONT_PILL, fill=(220, 225, 235))

        # Call to Action Banner
        cta_box_w = 640
        cta_x = (WIDTH - cta_box_w) // 2
        cta_y = 440
        draw.rounded_rectangle([cta_x, cta_y, cta_x + cta_box_w, cta_y + 80], radius=12, fill=(20, 20, 26), outline=(255, 112, 67), width=2)
        cta_title = "FREE & OPEN SOURCE — DOWNLOAD NOW"
        ctw = draw.textlength(cta_title, font=FONT_BOLD)
        draw.text((WIDTH//2 - ctw//2, cta_y + 14), cta_title, font=FONT_BOLD, fill=(255, 112, 67))

        repo_url = "github.com/dorianborian/sesame-studio"
        rw = draw.textlength(repo_url, font=FONT_SUB)
        draw.text((WIDTH//2 - rw//2, cta_y + 44), repo_url, font=FONT_SUB, fill=(255, 255, 255))

        # Final fade to black in last 8 frames
        if sf > (TOTAL_FRAMES - 571 - 8):
            fade_out = (sf - (TOTAL_FRAMES - 571 - 8)) / 8.0
            outro_im = Image.blend(outro_im, Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0)), fade_out)

        write_frame(outro_im)

    # Close pipe and finalize video
    pipe.stdin.close()
    pipe.wait()

    win.close()
    print("\n" + "=" * 60)
    print("TRAILER GENERATED SUCCESSFULLY!")
    print(f"File: {OUTPUT_MP4}")
    if os.path.exists(OUTPUT_MP4):
        size_mb = os.path.getsize(OUTPUT_MP4) / (1024 * 1024)
        print(f"File Size: {size_mb:.2f} MB")
    print("=" * 60)


if __name__ == "__main__":
    build_trailer()
