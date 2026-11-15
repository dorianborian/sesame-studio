"""Centralized path and asset resolution for development and frozen/bundled runs.

Supports:
- Running from source repo (development)
- Running from PyInstaller bundle (single-file or directory mode, where sys._MEIPASS is set)
- Persistent storage for user configs in %APPDATA%/SesameStudio (or ~/.sesame_studio)
"""
import os
import sys
from pathlib import Path


def get_base_dir() -> Path:
    """Returns the base directory of the application.
    When frozen with PyInstaller, this points to sys._MEIPASS.
    Otherwise, it points to the repository root.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def get_user_data_dir() -> Path:
    """Returns a persistent directory for user configuration."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            p = Path(appdata) / "SesameStudio"
            p.mkdir(parents=True, exist_ok=True)
            return p
    home = Path.home() / ".sesame_studio"
    home.mkdir(parents=True, exist_ok=True)
    return home


BASE_DIR = get_base_dir()
ASSETS_DIR = BASE_DIR / "assets"
MODEL_DIR = ASSETS_DIR / "model"
FIRMWARE_DIR = ASSETS_DIR / "firmware"
LOGO_PATH = str(ASSETS_DIR / "sesame-sticker2.png")
ICON_PATH = str(ASSETS_DIR / "sesame-sticker2.png")
ICON_ICO_PATH = str(ASSETS_DIR / "sesame-icon.ico")
