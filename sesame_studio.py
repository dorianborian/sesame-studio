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

from app import theme
from app.paths import LOGO_PATH, ICON_ICO_PATH
from app.viewport3d import apply_desktop_gl_format
from app.splash import SplashScreen


def main() -> int:
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("sesame.robot.studio.3.0")
        except Exception:
            pass

    apply_desktop_gl_format()

    app = QApplication(sys.argv)
    app.setApplicationName("Sesame Studio")
    app.setOrganizationName("SesameRobot")
    theme.apply_theme(app)
    if os.path.exists(ICON_ICO_PATH):
        app.setWindowIcon(QIcon(ICON_ICO_PATH))
    elif os.path.exists(LOGO_PATH):
        app.setWindowIcon(QIcon(LOGO_PATH))

    # Show the splash first so it actually covers the time spent building the
    # main window (model load, GL setup, face header parse) instead of being
    # a fixed delay tacked on afterwards.
    splash = SplashScreen(LOGO_PATH)
    state = {"window": None, "splash_done": False}

    def show_main() -> None:
        state["splash_done"] = True
        if state["window"] is not None:
            state["window"].show()
            state["window"].raise_()
            state["window"].activateWindow()

    splash.finished.connect(show_main)
    splash.start()
    app.processEvents()

    from app.main_window import MainWindow  # imported late: it pulls in every panel
    window = MainWindow()
    state["window"] = window

    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        arg_path = os.path.abspath(sys.argv[1])
        if arg_path.lower().endswith((".h", ".hpp")):
            window.open_face_bitmaps_path(arg_path)
        elif arg_path.lower().endswith(".sesproj"):
            window.open_project_path(arg_path)

    if state["splash_done"]:
        show_main()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
