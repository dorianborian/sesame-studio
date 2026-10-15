#!/usr/bin/env python3
"""Sesame Studio launcher.

Thin entry point: sets up the Qt application, dark theme, OpenGL surface
format, splash screen, then hands off to app.main_window.MainWindow. The
actual application lives in the app/ package.
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Guard against None stdout/stderr in windowed/frozen environments
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

from app.paths import LOGO_PATH, ICON_ICO_PATH
from app.theme import STYLESHEET
from app.viewport3d import apply_desktop_gl_format
from app.splash import SplashScreen
from app.main_window import MainWindow


def main() -> int:
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("sesame.robot.studio.2.0")
        except Exception:
            pass

    apply_desktop_gl_format()

    app = QApplication(sys.argv)
    app.setApplicationName("Sesame Studio")
    app.setOrganizationName("SesameRobot")
    if os.path.exists(ICON_ICO_PATH):
        app.setWindowIcon(QIcon(ICON_ICO_PATH))
    elif os.path.exists(LOGO_PATH):
        app.setWindowIcon(QIcon(LOGO_PATH))

    window = MainWindow()

    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        arg_path = os.path.abspath(sys.argv[1])
        if arg_path.endswith(".h") or arg_path.endswith(".hpp"):
            window.open_face_bitmaps_path(arg_path)
        elif arg_path.endswith(".sesproj"):
            window.open_project_path(arg_path)

    splash = SplashScreen(LOGO_PATH)

    def show_main():
        window.show()

    splash.finished.connect(show_main)
    splash.start()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
