import os
import sys
import unittest
from pathlib import Path

# Add app to path
STUDIO_DIR = Path(__file__).resolve().parent.parent
if str(STUDIO_DIR) not in sys.path:
    sys.path.insert(0, str(STUDIO_DIR))

# Ensure offscreen Qt platform for testing
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from app.face_bitmaps import FaceBitmapsFile
from app.face_editor import FaceEditorDialog
from app.face_library import FaceLibraryPanel
from app.main_window import MainWindow
from app.project import Face, blank_frame


class TestFaceBitmapsGUI(unittest.TestCase):
    def setUp(self):
        bundled = STUDIO_DIR / "assets" / "firmware" / "face-bitmaps.h"
        sibling = STUDIO_DIR.parent / "sesame-robot" / "firmware" / "face-bitmaps.h"
        if bundled.exists():
            header_path = bundled
        elif sibling.exists():
            header_path = sibling
        else:
            header_path = bundled

        self.firmware_header = str(header_path)
        self.tmp_header = str(STUDIO_DIR / "tests" / "tmp_gui_test.h")
        # Create a working copy for tests
        fb = FaceBitmapsFile.load(self.firmware_header)
        fb.save(self.tmp_header)
        self.window = MainWindow()
        self.window.show()

    def tearDown(self):
        self.window.close()
        if os.path.exists(self.tmp_header):
            os.remove(self.tmp_header)

    def test_open_face_bitmaps_in_main_window(self):
        self.window.open_face_bitmaps_path(self.tmp_header)

        # 1. header_file is set on MainWindow
        self.assertIsNotNone(self.window.header_file)
        self.assertEqual(self.window.header_file.path, os.path.abspath(self.tmp_header))
        self.assertFalse(self.window.header_file.dirty)

        # 2. Window title reflects header file
        title = self.window.windowTitle()
        self.assertIn("tmp_gui_test.h", title)
        self.assertIn("[Face Bitmaps]", title)
        self.assertNotIn("*", title)

        # 3. FaceLibraryPanel is in header mode
        panel = self.window.face_library
        self.assertIsNotNone(panel.header_file)
        self.assertFalse(panel.header_banner.isHidden())
        self.assertEqual(panel.header_label.text(), "Editing: tmp_gui_test.h")

        # 4. Builtin faces shadow is disabled in header mode (all faces are directly editable)
        self.assertFalse(panel.is_builtin("walk"))
        self.assertFalse(panel.is_builtin("sad"))

        # 5. All faces are loaded
        self.assertEqual(len(panel.faces), 37)
        self.assertIn("walk", panel.faces)
        self.assertIn("stand", panel.faces)

    def test_edit_face_and_save(self):
        self.window.open_face_bitmaps_path(self.tmp_header)
        panel = self.window.face_library

        # Modify a face in memory
        walk_face = panel.faces["walk"]
        walk_face.frames[0][0] = 0xBE
        walk_face.frames[0][1] = 0xEF
        panel.header_file.mark_dirty()
        self.window._update_title()

        # Check dirty title
        self.assertTrue(self.window.header_file.dirty)
        self.assertIn("*", self.window.windowTitle())
        self.assertEqual(panel.save_header_btn.text(), "Save .h*")

        # Save face bitmaps
        self.window.save_face_bitmaps()
        self.assertFalse(self.window.header_file.dirty)
        self.assertNotIn("*", self.window.windowTitle())
        self.assertEqual(panel.save_header_btn.text(), "Save .h")

        # Reload from disk and verify byte changes persisted
        reloaded = FaceBitmapsFile.load(self.tmp_header)
        self.assertEqual(reloaded.faces["walk"].frames[0][0], 0xBE)
        self.assertEqual(reloaded.faces["walk"].frames[0][1], 0xEF)

    def test_face_editor_header_mode(self):
        face = Face(name="walk", frames=[blank_frame()])
        dlg = FaceEditorDialog(face, {"sad", "happy"}, parent=self.window, is_header_mode=True)
        self.assertTrue(dlg.is_header_mode)
        # override checkbox should be hidden in header mode
        self.assertFalse(dlg.override_check.isVisible())
        self.assertEqual(dlg.windowTitle(), "Face Editor - walk")

        # Test valid identifier validation
        dlg.name_edit.setText("invalid-name-with-dashes")
        dlg._refresh_warning()
        self.assertIn("valid C identifier", dlg.warning_label.text())

        # Test valid identifier
        dlg.name_edit.setText("valid_face_123")
        dlg._refresh_warning()
        self.assertEqual(dlg.warning_label.text(), "")
        dlg.close()

    def test_close_header_returns_to_project_mode(self):
        self.window.open_face_bitmaps_path(self.tmp_header)
        self.assertIsNotNone(self.window.header_file)
        self.assertFalse(self.window.face_library.header_banner.isHidden())

        # Close header file
        self.window.close_header_file()
        self.assertIsNone(self.window.header_file)
        self.assertTrue(self.window.face_library.header_banner.isHidden())
        self.assertEqual(self.window.stack.currentWidget(), self.window.welcome_page)
        self.assertNotIn("[Face Bitmaps]", self.window.windowTitle())


if __name__ == "__main__":
    unittest.main()
