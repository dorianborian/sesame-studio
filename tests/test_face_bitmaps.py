import os
import sys
import unittest
from pathlib import Path

# Add app to path
STUDIO_DIR = Path(__file__).resolve().parent.parent
if str(STUDIO_DIR) not in sys.path:
    sys.path.insert(0, str(STUDIO_DIR))

from app.face_bitmaps import FaceBitmapsFile, format_byte_array
from app.project import Face, blank_frame, pixels_to_frame


class TestFaceBitmaps(unittest.TestCase):
    def setUp(self):
        bundled = STUDIO_DIR / "assets" / "firmware" / "face-bitmaps.h"
        sibling = STUDIO_DIR.parent / "sesame-robot" / "firmware" / "face-bitmaps.h"
        if bundled.exists():
            self.firmware_header = bundled
        elif sibling.exists():
            self.firmware_header = sibling
        else:
            self.firmware_header = bundled

    def test_load_firmware_header(self):
        self.assertTrue(self.firmware_header.exists(), "firmware/face-bitmaps.h must exist")
        fb = FaceBitmapsFile.load(str(self.firmware_header))
        self.assertEqual(len(fb.order), 37)
        self.assertIn("walk", fb.faces)
        self.assertIn("rest", fb.faces)
        self.assertIn("stand", fb.faces)
        self.assertIn("defualt", fb.faces)
        self.assertIn("idle_blink", fb.faces)

        # Check multi-frame faces
        self.assertEqual(len(fb.faces["rest"].frames), 3)
        self.assertEqual(len(fb.faces["point"].frames), 3)
        self.assertEqual(len(fb.faces["idle_blink"].frames), 4)

        # Check undefined names
        self.assertIn("stand", fb.undefined_names)
        self.assertIn("defualt", fb.undefined_names)
        self.assertFalse(fb.dirty)

    def test_save_and_reload_roundtrip(self):
        fb = FaceBitmapsFile.load(str(self.firmware_header))
        tmp_path = STUDIO_DIR / "tests" / "tmp_test_bitmaps.h"
        try:
            fb.save(str(tmp_path))
            self.assertTrue(tmp_path.exists())

            # Reload saved file
            fb2 = FaceBitmapsFile.load(str(tmp_path))
            self.assertEqual(len(fb2.order), len(fb.order))
            self.assertEqual(fb2.order, fb.order)
            self.assertEqual(fb2.undefined_names, fb.undefined_names)

            for name, face in fb.faces.items():
                self.assertIn(name, fb2.faces)
                face2 = fb2.faces[name]
                self.assertEqual(len(face2.frames), len(face.frames), f"Frame count mismatch for {name}")
                for idx in range(len(face.frames)):
                    self.assertEqual(face2.frames[idx], face.frames[idx], f"Byte mismatch in {name} frame {idx}")
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def test_add_edit_delete_faces(self):
        fb = FaceBitmapsFile.load(str(self.firmware_header))
        tmp_path = STUDIO_DIR / "tests" / "tmp_test_edit.h"
        try:
            # 1. Edit an existing face (draw pixel on walk)
            walk_frame = fb.faces["walk"].frames[0]
            walk_frame[0] = 0xAA
            walk_frame[1] = 0x55
            fb.mark_dirty()
            self.assertTrue(fb.dirty)

            # 2. Paint an undefined face (stand)
            stand_frame = fb.faces["stand"].frames[0]
            stand_frame[10] = 0xFF
            fb.undefined_names.discard("stand")

            # 3. Add a new face
            new_face = Face(name="cool_face", frames=[blank_frame(), blank_frame()])
            new_face.frames[0][0] = 0x12
            new_face.frames[1][0] = 0x34
            fb.add_face(new_face)
            self.assertIn("cool_face", fb.faces)
            self.assertIn("cool_face", fb.order)

            # 4. Rename a face
            fb.rename_face("cute", "kawaii")
            self.assertNotIn("cute", fb.faces)
            self.assertIn("kawaii", fb.faces)
            self.assertNotIn("cute", fb.order)
            self.assertIn("kawaii", fb.order)

            # 5. Delete a face
            fb.remove_face("freaky")
            self.assertNotIn("freaky", fb.faces)
            self.assertNotIn("freaky", fb.order)

            # Save and verify
            fb.save(str(tmp_path))
            fb_reloaded = FaceBitmapsFile.load(str(tmp_path))

            # Verify edits
            self.assertEqual(fb_reloaded.faces["walk"].frames[0][0], 0xAA)
            self.assertEqual(fb_reloaded.faces["walk"].frames[0][1], 0x55)
            self.assertNotIn("stand", fb_reloaded.undefined_names)
            self.assertEqual(fb_reloaded.faces["stand"].frames[0][10], 0xFF)

            # Verify added face
            self.assertIn("cool_face", fb_reloaded.faces)
            self.assertEqual(len(fb_reloaded.faces["cool_face"].frames), 2)
            self.assertEqual(fb_reloaded.faces["cool_face"].frames[0][0], 0x12)
            self.assertEqual(fb_reloaded.faces["cool_face"].frames[1][0], 0x34)

            # Verify renamed face
            self.assertNotIn("cute", fb_reloaded.faces)
            self.assertIn("kawaii", fb_reloaded.faces)

            # Verify deleted face
            self.assertNotIn("freaky", fb_reloaded.faces)
            self.assertNotIn("freaky", fb_reloaded.order)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()


if __name__ == "__main__":
    unittest.main()
