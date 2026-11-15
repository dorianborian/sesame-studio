"""Shared pytest setup.

Redirects QSettings to a throwaway INI directory so tests that construct the
main window (recent files, window layout, tutorial-seen flag, viewport
options) never read or write the user's real Sesame Studio settings.
"""

import os
import sys
import tempfile
from pathlib import Path

STUDIO_DIR = Path(__file__).resolve().parent.parent
if str(STUDIO_DIR) not in sys.path:
    sys.path.insert(0, str(STUDIO_DIR))

from PySide6.QtCore import QSettings

_SETTINGS_DIR = tempfile.mkdtemp(prefix="sesame-studio-tests-")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _SETTINGS_DIR)
os.environ.setdefault("SESAME_STUDIO_TESTING", "1")
