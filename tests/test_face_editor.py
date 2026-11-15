"""Tests for the face editor suite: numpy frame helpers (checked against the
original pure-Python implementations), drawing primitives, mirror mode, undo,
selection, image import, the editor dialog and the library panel."""

import os
import sys
import unittest
from pathlib import Path

STUDIO_DIR = Path(__file__).resolve().parent.parent
if str(STUDIO_DIR) not in sys.path:
    sys.path.insert(0, str(STUDIO_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from app import face_core as fc
from app import face_render as fr
from app.builtin_faces import BUILTIN_FACES
from app.face_bitmaps import FaceBitmapsFile
from app.face_canvas import FaceCanvas
from app.face_editor import FaceEditorDialog, frame_bytes_to_image, image_to_pixels
from app.face_import import convert_image
from app.face_library import FaceLibraryPanel
from app.project import (
    BYTES_PER_FRAME, Face, array_to_frame, blank_frame, frame_to_array,
    frame_to_pixels, pixels_to_frame,
)
from app.servo_model import FACE_HEIGHT, FACE_MAX_FRAMES, FACE_WIDTH

W, H = FACE_WIDTH, FACE_HEIGHT


# -- the original implementations, kept verbatim as the reference ------------

def ref_pixels_to_frame(pixels):
    buf = bytearray(BYTES_PER_FRAME)
    for y in range(H):
        row = pixels[y]
        for bx in range(W // 8):
            byte = 0
            for bit in range(8):
                if row[bx * 8 + bit]:
                    byte |= 0x80 >> bit
            buf[y * (W // 8) + bx] = byte
    return buf


def ref_frame_to_pixels(frame):
    pixels = [[False] * W for _ in range(H)]
    stride = W // 8
    for y in range(H):
        for bx in range(stride):
            byte = frame[y * stride + bx]
            for bit in range(8):
                if byte & (0x80 >> bit):
                    pixels[y][bx * 8 + bit] = True
    return pixels


def ref_frame_bytes_to_image(frame):
    img = QImage(W, H, QImage.Format_RGB32)
    img.fill(QColor("black"))
    for y in range(H):
        for bx in range(W // 8):
            byte = frame[y * (W // 8) + bx]
            for bit in range(8):
                if byte & (0x80 >> bit):
                    img.setPixel(bx * 8 + bit, y, 0xFFFFFFFF)
    return img


def ref_image_to_pixels(image):
    return [[QColor(image.pixel(x, y)).lightness() > 127 for x in range(W)] for y in range(H)]


def ref_flood(arr, x, y):
    target = bool(arr[y, x])
    mask = np.zeros_like(arr)
    stack = [(x, y)]
    while stack:
        cx, cy = stack.pop()
        if not (0 <= cx < W and 0 <= cy < H) or mask[cy, cx] or bool(arr[cy, cx]) != target:
            continue
        mask[cy, cx] = True
        stack.extend([(cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)])
    return mask


def random_frame(seed):
    return bytes(np.random.default_rng(seed).integers(0, 256, BYTES_PER_FRAME, dtype=np.uint8))


class TestFrameHelpers(unittest.TestCase):
    def test_round_trip_matches_reference(self):
        for seed in range(5):
            frame = random_frame(seed)
            arr = frame_to_array(frame)
            self.assertEqual(arr.shape, (H, W))
            self.assertEqual(arr.dtype, np.bool_)
            self.assertEqual(arr.tolist(), ref_frame_to_pixels(frame))
            self.assertEqual(frame_to_pixels(frame), ref_frame_to_pixels(frame))
            self.assertEqual(bytes(array_to_frame(arr)), frame)
            self.assertEqual(bytes(pixels_to_frame(arr.tolist())), bytes(ref_pixels_to_frame(arr.tolist())))
            self.assertEqual(bytes(pixels_to_frame(arr)), frame)

    def test_types_and_edge_cases(self):
        self.assertIsInstance(pixels_to_frame(frame_to_pixels(blank_frame())), bytearray)
        self.assertIsInstance(array_to_frame(fc.blank()), bytearray)
        px = frame_to_pixels(random_frame(9))
        self.assertIsInstance(px, list)
        self.assertIsInstance(px[0][0], bool)
        self.assertEqual((len(px), len(px[0])), (H, W))
        # msb-first, horizontal scan
        arr = fc.blank()
        arr[0, 0] = True
        arr[1, 15] = True
        frame = array_to_frame(arr)
        self.assertEqual(frame[0], 0x80)
        self.assertEqual(frame[16 + 1], 0x01)
        # damaged frames are padded rather than crashing
        self.assertEqual(frame_to_array(b"\xff").sum(), 8)
        with self.assertRaises(ValueError):
            array_to_frame(np.zeros((10, 10), dtype=bool))

    def test_qimage_conversions_match_reference(self):
        frame = bytes(BUILTIN_FACES["happy"].frames[0]) if "happy" in BUILTIN_FACES else random_frame(3)
        img = frame_bytes_to_image(frame)
        self.assertEqual(img.format(), QImage.Format_RGB32)
        self.assertTrue(img == ref_frame_bytes_to_image(frame))
        self.assertEqual(image_to_pixels(img), ref_image_to_pixels(img))
        self.assertEqual(bytes(pixels_to_frame(image_to_pixels(img))), frame)
        # arbitrary colours use the same lightness rule as QColor.lightness()
        raw = np.random.default_rng(1).integers(0, 256, (H, W, 4), dtype=np.uint8)
        raw[..., 3] = 255
        colour = QImage(raw.tobytes(), W, H, W * 4, QImage.Format_RGB32).copy()
        self.assertEqual(image_to_pixels(colour), ref_image_to_pixels(colour))

    def test_thumbnail_cache(self):
        frame = random_frame(4)
        a = fr.oled_pixmap(frame, 96, 48)
        b = fr.oled_pixmap(bytearray(frame), 96, 48)
        self.assertEqual(a.cacheKey(), b.cacheKey())
        self.assertEqual((a.width(), a.height()), (96, 48))
        self.assertNotEqual(a.cacheKey(), fr.oled_pixmap(random_frame(5), 96, 48).cacheKey())


class TestCore(unittest.TestCase):
    def test_line_has_no_gaps(self):
        rng = np.random.default_rng(7)
        for _ in range(200):
            x0, x1 = (int(v) for v in rng.integers(0, W, 2))
            y0, y1 = (int(v) for v in rng.integers(0, H, 2))
            pts = fc.line_points(x0, y0, x1, y1)
            self.assertEqual(pts[0], (x0, y0))
            self.assertEqual(pts[-1], (x1, y1))
            self.assertEqual(len(pts), max(abs(x1 - x0), abs(y1 - y0)) + 1)
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                self.assertLessEqual(max(abs(ax - bx), abs(ay - by)), 1)
        self.assertEqual(int(fc.line_mask(0, 0, 127, 63).sum()), 128)
        self.assertEqual(int(fc.line_mask(5, 5, 5, 5).sum()), 1)
        # clipped, not crashing, when dragged off-canvas
        self.assertEqual(int(fc.line_mask(-10, 3, 10, 3).sum()), 11)

    def test_brush_sizes(self):
        for size in range(1, fc.MAX_BRUSH + 1):
            self.assertEqual(int(fc.dot_mask(40, 30, size).sum()), size * size)
        self.assertEqual(int(fc.line_mask(10, 10, 19, 10, 3).sum()), 12 * 3)

    def test_constrain(self):
        self.assertEqual(fc.constrain_line(0, 0, 10, 2), (10, 0))
        self.assertEqual(fc.constrain_line(0, 0, 2, 10), (0, 10))
        self.assertEqual(fc.constrain_line(0, 0, 10, 7), (10, 10))
        self.assertEqual(fc.constrain_line(5, 5, -3, 12), (-3, 13))
        self.assertEqual(fc.constrain_square(0, 0, 10, 4), (10, 10))
        self.assertEqual(fc.constrain_square(10, 10, 4, 7), (4, 4))

    def test_rect_and_ellipse(self):
        r = fc.rect_mask(10, 5, 20, 12)
        self.assertEqual(int(r.sum()), 2 * 11 + 2 * 6)
        self.assertEqual(int(fc.rect_mask(20, 12, 10, 5, filled=True).sum()), 11 * 8)
        self.assertEqual(int(fc.rect_mask(10, 5, 20, 12, size=2).sum()), 11 * 8 - 7 * 4)
        for w in range(1, 40):
            for h in range(1, 30):
                m = fc.ellipse_mask(3, 3, 2 + w, 2 + h)
                self.assertEqual(fc.content_bounds(m), (3, 3, 2 + w, 2 + h))
                box = m[3:3 + h, 3:3 + w]
                self.assertTrue(np.array_equal(box, box[::-1]) and np.array_equal(box, box[:, ::-1]))
                filled = fc.ellipse_mask(3, 3, 2 + w, 2 + h, filled=True)
                self.assertTrue((filled | m).sum() == filled.sum())  # outline lies inside the fill

    def test_flood_fill_matches_reference(self):
        blank = fc.blank()
        self.assertTrue(fc.flood_mask(blank, 10, 10).all())
        rng = np.random.default_rng(11)
        for density in (0.2, 0.4, 0.55):
            arr = rng.random((H, W)) < density
            for _ in range(6):
                x, y = int(rng.integers(0, W)), int(rng.integers(0, H))
                self.assertTrue(np.array_equal(fc.flood_mask(arr, x, y), ref_flood(arr, x, y)))
        # enclosed region stays enclosed
        arr = fc.rect_mask(10, 10, 30, 30)
        inside = fc.flood_mask(arr, 20, 20)
        self.assertEqual(int(inside.sum()), 19 * 19)
        self.assertFalse(fc.flood_mask(arr, 0, 0)[20, 20])

    def test_symmetry_and_image_ops(self):
        m = fc.dot_mask(3, 4)
        s = fc.symmetrize(m, True, True)
        self.assertEqual(sorted(zip(*np.nonzero(s))), [(4, 3), (4, 124), (59, 3), (59, 124)])
        self.assertEqual(fc.mirror_points(3, 4, True, False), [(3, 4), (124, 4)])
        arr = fc.blank()
        arr[0, 0] = True
        self.assertTrue(fc.shift(arr, -1, -1)[H - 1, W - 1])          # wraps
        self.assertFalse(fc.shift(arr, -1, -1, wrap=False).any())     # or not
        self.assertTrue(fc.flip_h(arr)[0, W - 1])
        self.assertTrue(fc.flip_v(arr)[H - 1, 0])
        self.assertEqual(int(fc.invert(arr).sum()), W * H - 1)
        box = fc.rect_mask(0, 0, 9, 3, filled=True)
        self.assertEqual(fc.content_bounds(fc.center_content(box)), (59, 30, 68, 33))
        self.assertFalse(fc.center_content(fc.blank()).any())

    def test_threshold_and_dither(self):
        gray = np.tile(np.arange(W, dtype=np.uint8) * 2, (H, 1))
        hard = fc.threshold_gray(gray, 128)
        self.assertTrue(np.array_equal(hard, gray >= 128))
        self.assertTrue(np.array_equal(fc.threshold_gray(gray, 128, invert_result=True), gray < 128))
        self.assertGreater(fc.threshold_gray(gray, 60).sum(), hard.sum())
        flat = np.full((H, W), 64, dtype=np.uint8)
        self.assertFalse(fc.threshold_gray(flat, 128).any())
        dithered = fc.threshold_gray(flat, 128, dither=True)
        self.assertAlmostEqual(float(dithered.mean()), 64 / 255, delta=0.03)   # keeps the tone
        self.assertEqual(dithered.shape, (H, W))
        self.assertTrue(fc.threshold_gray(np.full((H, W), 255, np.uint8), 128, dither=True).all())

    def test_image_import(self):
        img = QImage(400, 100, QImage.Format_RGB32)
        img.fill(QColor("black"))
        p = QPainter(img)
        p.fillRect(0, 0, 200, 100, QColor("white"))
        p.end()
        fit = convert_image(img, fr.FIT_CONTAIN)          # 128x32 letterboxed
        self.assertEqual(fit.shape, (H, W))
        self.assertFalse(fit[:14].any())
        self.assertFalse(fit[-14:].any())
        self.assertTrue(fit[32, 10] and not fit[32, 120])
        cover = convert_image(img, fr.FIT_COVER)          # scaled to 256x64, centre crop
        self.assertTrue(cover[2, 10] and cover[61, 10] and not cover[32, 120])
        stretch = convert_image(img, fr.FIT_STRETCH)
        self.assertTrue(stretch[0, 5] and stretch[63, 5] and not stretch[0, 122])
        inv = convert_image(img, fr.FIT_STRETCH, invert=True)
        self.assertTrue(np.array_equal(inv, ~stretch))

    def test_history(self):
        h = fc.History(limit=5)
        states = [fc.dot_mask(i, 0) for i in range(8)]
        for s in states[:-1]:
            h.push(s)
        cur = states[-1]
        seen = 0
        while h.can_undo():
            cur = h.undo(cur)
            seen += 1
        self.assertEqual(seen, 5)                      # depth-limited
        self.assertTrue(np.array_equal(cur, states[2]))
        cur = h.redo(cur)
        self.assertTrue(np.array_equal(cur, states[3]))
        h.push(cur)
        self.assertFalse(h.can_redo())                 # new edit clears redo

    def test_fonts_exclude_comic_sans(self):
        self.assertTrue(fr.is_banned_font("Comic Sans MS"))
        self.assertTrue(fr.is_banned_font("comic sans"))
        self.assertFalse(fr.is_banned_font("Consolas"))
        self.assertFalse(any("comic sans" in f.lower() for f in fr.text_font_families()))
        self.assertFalse(fr.is_banned_font(fr.safe_font_family("Comic Sans MS")))


class TestCanvas(unittest.TestCase):
    def setUp(self):
        self.canvas = FaceCanvas()
        self.commits = []
        self.canvas.committed.connect(lambda: self.commits.append(1))

    def test_pencil_stroke_is_connected_and_one_undo_step(self):
        c = self.canvas
        c.simulate_stroke([(2, 2), (60, 40), (120, 5)])     # two long jumps
        expected = fc.line_mask(2, 2, 60, 40) | fc.line_mask(60, 40, 120, 5)
        self.assertTrue(np.array_equal(c.pixels, expected))
        self.assertEqual(len(self.commits), 1)
        c.undo()
        self.assertFalse(c.pixels.any())
        self.assertFalse(c.can_undo())
        c.redo()
        self.assertTrue(np.array_equal(c.pixels, expected))

    def test_right_button_erases_and_eraser_tool(self):
        c = self.canvas
        c.set_tool("rect_fill")
        c.simulate_stroke([(0, 0), (20, 20)])
        c.set_tool("pencil")
        c.simulate_stroke([(5, 5), (15, 5)], erase=True)
        self.assertFalse(c.pixels[5, 5:16].any())
        c.set_tool("eraser")
        c.set_brush_size(3)
        c.simulate_stroke([(10, 10)])
        self.assertFalse(c.pixels[9:12, 9:12].any())
        self.assertTrue(c.pixels[12, 12])

    def test_shape_preview_is_live_and_constrained(self):
        c = self.canvas
        c.set_tool("rect")
        c.begin_at(10, 10)
        c.drag_to(40, 30)
        self.assertTrue(np.array_equal(c.pixels, fc.rect_mask(10, 10, 40, 30)))   # drawn while dragging
        c.drag_to(20, 15)
        self.assertTrue(np.array_equal(c.pixels, fc.rect_mask(10, 10, 20, 15)))   # rubber band, no trail
        c.drag_to(30, 14, constrain=True)
        self.assertTrue(np.array_equal(c.pixels, fc.rect_mask(10, 10, 30, 30)))   # shift -> square
        self.assertFalse(c.can_undo())
        c.end_stroke()
        self.assertTrue(c.can_undo())
        c.set_tool("line")
        c.simulate_stroke([(0, 50), (20, 53)], constrain=True)
        self.assertTrue(c.pixels[50, 0:21].all())

    def test_cancel_stroke_restores(self):
        c = self.canvas
        c.set_tool("ellipse_fill")
        c.begin_at(5, 5)
        c.drag_to(50, 40)
        self.assertTrue(c.pixels.any())
        self.assertTrue(c.cancel_stroke())
        self.assertFalse(c.pixels.any())
        self.assertFalse(c.can_undo())

    def test_mirror_mode(self):
        c = self.canvas
        c.set_mirror(mirror_x=True)
        c.simulate_stroke([(10, 10), (30, 25)])
        self.assertTrue(c.pixels.any())
        self.assertTrue(np.array_equal(c.pixels, c.pixels[:, ::-1]))
        self.assertTrue(c.pixels[25, 30] and c.pixels[25, W - 1 - 30])
        self.assertFalse(np.array_equal(c.pixels, c.pixels[::-1, :]))
        c.set_mirror(mirror_y=True)
        c.set_tool("ellipse")
        c.simulate_stroke([(5, 3), (20, 12)])
        self.assertTrue(np.array_equal(c.pixels, c.pixels[:, ::-1]))
        c.clear_all()
        c.set_tool("rect_fill")
        c.simulate_stroke([(4, 4), (8, 8)])
        self.assertEqual(int(c.pixels.sum()), 4 * 25)
        c.undo()
        self.assertFalse(c.pixels.any())           # the mirrored stroke is one step

    def test_fill(self):
        c = self.canvas
        c.set_tool("fill")
        c.begin_at(64, 32)
        self.assertTrue(c.pixels.all())
        c.undo()
        self.assertFalse(c.pixels.any())
        c.set_tool("rect")
        c.simulate_stroke([(10, 10), (30, 30)])
        c.set_tool("fill")
        c.begin_at(20, 20)
        self.assertEqual(int(c.pixels.sum()), 21 * 21)
        c.begin_at(20, 20, erase=True)             # right button fills with black
        self.assertFalse(c.pixels.any())

    def test_whole_image_ops_and_undo_depth(self):
        c = self.canvas
        c.simulate_stroke([(0, 0)])
        c.shift(-1, 0)
        self.assertTrue(c.pixels[0, W - 1])
        c.flip_horizontal()
        self.assertTrue(c.pixels[0, 0])
        c.flip_vertical()
        self.assertTrue(c.pixels[H - 1, 0])
        c.invert()
        self.assertEqual(int(c.pixels.sum()), W * H - 1)
        c.invert()
        c.center_content()
        self.assertTrue(c.pixels[31, 63] or c.pixels[32, 64] or c.pixels[31, 64] or c.pixels[32, 63])
        for i in range(100):
            c.simulate_stroke([(i, 10)])
        steps = 0
        while c.can_undo():
            c.undo()
            steps += 1
        self.assertEqual(steps, 107)               # deep history, one step per op
        self.assertFalse(c.pixels.any())

    def test_selection_move_copy_paste_delete(self):
        c = self.canvas
        c.set_tool("rect_fill")
        c.simulate_stroke([(10, 10), (14, 14)])
        before = c.pixels.copy()
        c.set_tool("select")
        c.select_rect(10, 10, 14, 14)
        c.begin_at(12, 12)
        c.drag_to(42, 22)
        c.end_stroke()
        self.assertFalse(c.pixels[10:15, 10:15].any())
        self.assertTrue(c.pixels[20:25, 40:45].all())
        self.assertEqual(int(c.pixels.sum()), 25)
        c.nudge_selection(1, 0)
        self.assertTrue(c.pixels[20:25, 41:46].all())
        c.undo()                                    # the whole move is one step
        self.assertTrue(np.array_equal(c.pixels, before))
        self.assertFalse(c.has_selection())

        c.select_rect(10, 10, 14, 14)
        c.copy_selection()
        c.paste_array(c.selection_array(), pos=(100, 50))
        self.assertTrue(c.is_floating())
        self.assertTrue(c.pixels[50:55, 100:105].all() and c.pixels[10:15, 10:15].all())
        c.deselect()
        self.assertEqual(int(c.pixels.sum()), 50)
        c.select_rect(100, 50, 104, 54)
        self.assertTrue(c.delete_selection())
        self.assertEqual(int(c.pixels.sum()), 25)
        c.select_rect(8, 8, 16, 16)
        c.invert()                                  # ops act on the selection only
        self.assertEqual(int(c.pixels.sum()), 81 - 25)

    def test_legacy_image_property(self):
        c = self.canvas
        img = frame_bytes_to_image(random_frame(21))
        c.image = img
        self.assertEqual(bytes(array_to_frame(c.pixels)), random_frame(21))
        self.assertTrue(c.image == img)

    def test_zoom_and_coordinates(self):
        c = self.canvas
        c.resize(128 * 5 + 40, 64 * 5 + 40)
        c.set_zoom(5)
        self.assertEqual(c.widget_to_pixel(c.pixel_to_widget(17, 9)), (17, 9))
        c.set_zoom(9, anchor=c.pixel_to_widget(100, 50))
        self.assertEqual(c.widget_to_pixel(c.pixel_to_widget(100, 50)), (100, 50))
        self.assertEqual(c.zoom, 9)
        c.zoom_fit()
        self.assertEqual(c.zoom, c.fit_zoom())


class TestEditorDialog(unittest.TestCase):
    def make(self, face=None, names=(), header=False):
        dlg = FaceEditorDialog(face, set(names), None, is_header_mode=header)
        self.addCleanup(dlg.force_close)
        return dlg

    def test_save_round_trip(self):
        src = Face(name="blinky", category="special", fps=7,
                   frames=[bytearray(random_frame(1)), bytearray(random_frame(2))])
        dlg = self.make(src)
        self.assertFalse(dlg.is_dirty())
        saved = []
        dlg.faceSaved.connect(saved.append)
        dlg._on_save()
        face = dlg.result_face
        self.assertIs(saved[0], face)
        self.assertEqual((face.name, face.category, face.fps), ("blinky", "special", 7))
        self.assertEqual([bytes(f) for f in face.frames], [bytes(f) for f in src.frames])
        self.assertIsInstance(face.frames[0], bytearray)
        self.assertFalse(face.is_override)

    def test_painting_marks_dirty_and_emits_preview(self):
        dlg = self.make()
        got = []
        dlg.previewPixels.connect(got.append)
        dlg.canvas.simulate_stroke([(1, 1), (20, 1)])
        self.assertTrue(dlg.is_dirty())
        self.assertTrue(got)
        self.assertEqual(got[-1].shape, (H, W))
        self.assertEqual(got[-1].dtype, np.bool_)
        self.assertTrue(got[0][1, 1])
        dlg._on_save()
        self.assertTrue(frame_to_array(dlg.result_face.frames[0])[1, 1:21].all())

    def test_preview_is_throttled(self):
        dlg = self.make()
        got = []
        dlg.previewPixels.connect(got.append)
        dlg.canvas.begin_at(0, 0)
        for x in range(1, 100):
            dlg.canvas.drag_to(x, 0)       # 99 synchronous moves, well inside one 33 ms slot
        dlg.canvas.end_stroke()
        self.assertLessEqual(len(got), 3)
        dlg._on_live_timer()                # the trailing update carries the final pixels
        self.assertTrue(got[-1][0, 0:100].all())

    def test_frames_add_duplicate_move_delete_limit(self):
        dlg = self.make()
        dlg.canvas.simulate_stroke([(0, 0)])
        dlg._duplicate_frame()
        self.assertEqual((len(dlg.frames), dlg.frame_index), (2, 1))
        self.assertTrue(dlg.frames[1][0, 0])
        dlg._add_frame()
        self.assertEqual((len(dlg.frames), dlg.frame_index), (3, 2))
        self.assertFalse(dlg.frames[2].any())
        dlg.canvas.simulate_stroke([(5, 5)])
        dlg.move_frame(2, 0)
        self.assertEqual(dlg.frame_index, 0)
        self.assertTrue(dlg.frames[0][5, 5] and dlg.frames[1][0, 0])
        dlg.canvas.undo()                   # history travels with its frame
        self.assertFalse(dlg.frames[0].any())
        while len(dlg.frames) < FACE_MAX_FRAMES:
            dlg._add_frame()
        dlg._add_frame()
        dlg._duplicate_frame()
        self.assertEqual(len(dlg.frames), FACE_MAX_FRAMES)
        self.assertFalse(dlg.add_frame_btn.isEnabled())
        self.assertEqual(dlg.frame_list.count(), FACE_MAX_FRAMES)
        while len(dlg.frames) > 1:
            dlg._delete_frame()
        dlg._delete_frame()
        self.assertEqual(len(dlg.frames), 1)
        self.assertFalse(dlg.del_frame_btn.isEnabled())

    def test_undo_is_per_frame(self):
        dlg = self.make()
        dlg.canvas.simulate_stroke([(1, 1)])
        dlg._add_frame()
        dlg.canvas.simulate_stroke([(2, 2)])
        dlg.canvas.undo()
        self.assertFalse(dlg.frames[1].any())
        self.assertFalse(dlg.canvas.can_undo())
        self.assertTrue(dlg.frames[0][1, 1])       # frame 1's stroke untouched
        dlg._select_frame(0)
        self.assertTrue(dlg.canvas.can_undo())

    def test_onion_and_playback(self):
        src = Face(name="anim", frames=[bytearray(random_frame(i)) for i in range(3)])
        dlg = self.make(src)
        dlg._select_frame(1)
        self.assertTrue(np.array_equal(dlg.canvas.onion, dlg.frames[0]))
        dlg.onion_check.setChecked(False)
        self.assertIsNone(dlg.canvas.onion)
        self.assertEqual(dlg._play_sequence(), [0, 1, 2])
        dlg.boomerang_btn.setChecked(True)
        self.assertEqual(dlg._play_sequence(), [0, 1, 2, 1])
        got = []
        dlg.previewPixels.connect(got.append)
        dlg.play_btn.setChecked(True)
        dlg._advance_playback()
        self.assertTrue(np.array_equal(got[-1], dlg.frames[1]))
        self.assertTrue(np.array_equal(dlg.visible_pixels(), dlg.frames[1]))
        dlg.play_btn.setChecked(False)
        self.assertTrue(np.array_equal(got[-1], dlg.frames[dlg.frame_index]))

    def test_validation_rules(self):
        dlg = self.make(None, names={"taken"})
        self.assertTrue(dlg.name_edit.text())                  # sensible default name
        self.assertEqual(dlg.warning_label.text(), "")
        dlg.name_edit.setText("taken")
        self.assertIn("already exists in this project", dlg.warning_label.text())
        self.assertFalse(dlg.save_btn.isEnabled())
        dlg._on_save()
        self.assertIsNone(dlg.result_face)                     # blocked
        dlg.name_edit.setText("walk")                          # reserved firmware name
        self.assertIn("built-in movement face", dlg.warning_label.text())
        self.assertTrue(dlg.save_btn.isEnabled())              # advisory only, as before
        dlg.override_check.setChecked(True)
        self.assertEqual(dlg.warning_label.text(), "")
        dlg.name_edit.setText("")
        self.assertIn("Enter a face name", dlg.warning_label.text())
        self.assertFalse(dlg.save_btn.isEnabled())

        hdr = self.make(Face(name="walk"), names={"walk", "sad"}, header=True)
        self.assertEqual(hdr.warning_label.text(), "")         # own name is fine
        hdr.name_edit.setText("sad")
        self.assertIn("already exists in this file", hdr.warning_label.text())
        hdr.name_edit.setText("no-dashes")
        self.assertIn("valid C identifier", hdr.warning_label.text())
        hdr.name_edit.setText("fine_1")
        hdr._on_save()
        self.assertTrue(hdr.result_face.is_override)           # header faces always override

    def test_code_view_is_lazy(self):
        dlg = self.make(Face(name="lazy"))
        self.assertEqual(dlg.output_view.toPlainText(), "")    # nothing generated up front
        dlg.canvas.simulate_stroke([(0, 0), (7, 0)])
        self.assertEqual(dlg.output_view.toPlainText(), "")    # ...nor while painting
        dlg._refresh_output()
        text = dlg.output_view.toPlainText()
        self.assertIn("epd_bitmap_lazy [] PROGMEM", text)
        self.assertIn("0xFF, 0x00", text)

    def test_legacy_script_access(self):
        """create_trailer.py pokes these directly."""
        src = Face(name="t", frames=[blank_frame(), blank_frame()])
        dlg = self.make(src, header=True)
        dlg.onion_check.setChecked(True)
        dlg._select_frame(1)
        img = frame_bytes_to_image(random_frame(33))
        dlg.canvas.image = img
        dlg.frames[1] = img.copy()
        dlg.canvas.update()
        dlg._refresh_frame_list()
        dlg._refresh_output()
        self.assertIn("epd_bitmap_t_1", dlg.output_view.toPlainText())
        self.assertEqual(bytes(array_to_frame(dlg.frames[1])), random_frame(33))
        self.assertIsNotNone(dlg.output_view.verticalScrollBar())
        self.assertIsNotNone(dlg.frame_list.mapTo(dlg, dlg.frame_list.rect().topLeft()))

    def test_import_and_paste(self):
        dlg = self.make()
        gray = QImage(W, H, QImage.Format_RGB32)
        gray.fill(QColor("white"))
        dlg.canvas.set_pixels(convert_image(gray))
        self.assertTrue(dlg.frames[0].all())
        dlg.canvas.undo()
        self.assertFalse(dlg.frames[0].any())
        dlg.canvas.paste_array(np.ones((4, 6), dtype=bool), pos=(1, 2))
        self.assertEqual(dlg.current_tool(), "select")
        self.assertTrue(dlg.frames[0][2:6, 1:7].all())
        self.assertEqual(int(dlg.frames[0].sum()), 24)


class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.panel = FaceLibraryPanel()
        self.addCleanup(lambda: self.panel.close_editor(force=True))
        self.custom = {"mine": Face(name="mine", frames=[bytearray(random_frame(1))])}
        self.panel.load_faces(self.custom)

    def names(self):
        lw = self.panel.list_widget
        return [lw.item(i).data(0x0100) for i in range(lw.count())]   # Qt.UserRole

    def test_grid_filter_and_selection(self):
        p = self.panel
        self.assertEqual(self.names(), p.all_face_names())
        self.assertIn("mine", self.names())
        self.assertFalse(p.assign_btn.isEnabled())
        p.search_edit.setText("MIN")
        self.assertEqual(self.names(), ["mine"])
        self.assertIn("1 of", p.count_label.text())
        p.search_edit.setText("no such face")
        self.assertEqual(self.names(), [])
        self.assertFalse(p.empty_state.isHidden())
        p.clear_filters()
        self.assertTrue(p.empty_state.isHidden())
        p.set_category_filter("custom")
        self.assertEqual(self.names(), ["mine"])
        p.set_category_filter(None)
        previews, assigned = [], []
        p.facePreviewRequested.connect(previews.append)
        p.faceAssigned.connect(assigned.append)
        self.assertTrue(p.select_face("mine"))
        self.assertEqual(p._selected_name(), "mine")
        self.assertEqual(previews, ["mine"])
        self.assertTrue(p.assign_btn.isEnabled())
        p.assign_btn.click()
        self.assertEqual(assigned, ["mine"])
        p.search_edit.setText("mi")                # selection survives re-filtering
        self.assertEqual(p._selected_name(), "mine")

    def test_duplicate_builtin_makes_editable_copy(self):
        p = self.panel
        if not p.builtin_faces:
            self.skipTest("no bundled firmware faces")
        name = sorted(p.builtin_faces)[0]
        changed = []
        p.facesChanged.connect(lambda: changed.append(1))
        p.select_face(name)
        self.assertFalse(p.delete_btn.isEnabled())            # built-ins are read-only
        p._duplicate_selected()
        copy = f"{name}_copy"
        self.assertIn(copy, p.faces)
        self.assertFalse(p.is_builtin(copy))
        self.assertEqual(p._selected_name(), copy)
        self.assertEqual(bytes(p.faces[copy].frames[0]), bytes(p.builtin_faces[name].frames[0]))
        self.assertIsNot(p.faces[copy].frames[0], p.builtin_faces[name].frames[0])
        self.assertTrue(p.delete_btn.isEnabled())
        self.assertEqual(len(changed), 1)
        blocked = []
        p.editBlocked.connect(blocked.append)
        p.select_face(name)
        p._delete_selected()
        self.assertEqual(len(blocked), 1)
        self.assertIn(name, p.builtin_faces)

    def test_editor_is_modeless_and_mirrors_pixels(self):
        p = self.panel
        live, changed = [], []
        p.facePixelsPreview.connect(live.append)
        p.facesChanged.connect(lambda: changed.append(1))
        p._new_face()                                          # returns immediately
        ed = p.editor()
        self.assertIsNotNone(ed)
        self.assertFalse(ed.isModal())
        ed.canvas.simulate_stroke([(3, 3), (9, 3)])
        self.assertTrue(live and live[-1] is not None and live[-1][3, 3])
        ed.name_edit.setText("mine")                           # collides with the library
        self.assertFalse(ed.save_btn.isEnabled())
        ed.name_edit.setText("fresh")
        ed._on_save()
        self.assertIsNone(live[-1])                            # editor closed -> None
        self.assertFalse(p.has_open_editor())
        self.assertIn("fresh", p.faces)
        self.assertTrue(frame_to_array(p.faces["fresh"].frames[0])[3, 3:10].all())
        self.assertEqual(p._selected_name(), "fresh")
        self.assertEqual(len(changed), 1)

    def test_edit_rename_and_cancel(self):
        p = self.panel
        p.select_face("mine")
        p._edit_selected()
        ed = p.editor()
        self.assertEqual(ed.name_edit.text(), "mine")
        ed.name_edit.setText("renamed")
        ed._on_save()
        self.assertNotIn("mine", p.faces)
        self.assertIn("renamed", p.faces)
        p.select_face("renamed")
        p._edit_selected()
        p.editor().canvas.simulate_stroke([(0, 0)])
        before = bytes(p.faces["renamed"].frames[0])
        p.close_editor(force=True)                             # discard
        self.assertFalse(p.has_open_editor())
        self.assertEqual(bytes(p.faces["renamed"].frames[0]), before)

    def test_edit_builtin_saves_custom_copy(self):
        p = self.panel
        if not p.builtin_faces:
            self.skipTest("no bundled firmware faces")
        name = sorted(p.builtin_faces)[0]
        p.select_face(name)
        p._edit_selected()
        ed = p.editor()
        self.assertEqual(ed.name_edit.text(), f"{name}_custom")
        ed._on_save()
        self.assertIn(f"{name}_custom", p.faces)
        self.assertTrue(p.is_builtin(name))

    def test_header_mode_edit_and_dirty_indicator(self):
        bundled = STUDIO_DIR / "assets" / "firmware" / "face-bitmaps.h"
        if not bundled.exists():
            self.skipTest("no bundled face-bitmaps.h")
        fb = FaceBitmapsFile.load(str(bundled))
        p = self.panel
        p.load_header_file(fb)
        self.assertFalse(p.header_banner.isHidden())
        self.assertEqual(p.save_header_btn.text(), "Save .h")
        self.assertTrue("stand" in fb.undefined_names)
        p.select_face("stand")
        p._edit_selected()
        ed = p.editor()
        self.assertTrue(ed.is_header_mode)
        ed.canvas.simulate_stroke([(0, 0), (7, 0)])
        ed._on_save()
        self.assertNotIn("stand", fb.undefined_names)
        self.assertTrue(fb.dirty)
        self.assertEqual(fb.faces["stand"].frames[0][0], 0xFF)
        p.update_header_dirty(True)
        self.assertEqual(p.save_header_btn.text(), "Save .h*")
        p.select_face("cute")
        p._edit_selected()
        p.editor().name_edit.setText("kawaii")
        p.editor()._on_save()
        self.assertIn("kawaii", fb.order)
        self.assertNotIn("cute", fb.faces)
        p.load_faces({})
        self.assertTrue(p.header_banner.isHidden())

    def test_unsaved_draft_survives_document_switch(self):
        p = self.panel
        p._new_face()
        ed = p.editor()
        ed.canvas.simulate_stroke([(1, 1)])
        ed.name_edit.setText("draft")
        other = {}
        p.load_faces(other)                                    # project switched underneath
        self.assertIs(p.editor(), ed)                          # dirty editor kept as a draft
        ed._on_save()
        self.assertIn("draft", other)
        p._new_face()
        clean = p.editor()
        p.load_faces({})
        self.assertIsNot(p.editor(), clean)                    # clean editor is closed
        self.assertFalse(p.has_open_editor())


if __name__ == "__main__":
    unittest.main()
