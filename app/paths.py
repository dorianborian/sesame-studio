"""Centralized path and asset resolution for development and frozen/bundled runs.

Supports:
- Running from source repo (development)
- Running from PyInstaller bundle (single-file or directory mode, where sys._MEIPASS is set)
- Persistent storage for user calibrations/configs in %APPDATA%/SesameStudio (or ~/.sesame_studio)
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
    """Returns a persistent directory for user configuration and calibrations."""
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
STL_DIR = ASSETS_DIR / "stl"
FIRMWARE_DIR = ASSETS_DIR / "firmware"
LOGO_PATH = str(ASSETS_DIR / "sesame-sticker2.png")
ICON_PATH = str(ASSETS_DIR / "sesame-sticker2.png")
ICON_ICO_PATH = str(ASSETS_DIR / "sesame-icon.ico")


def get_joints_config_load_path() -> Path:
    """Returns the path to joints_config.json to read.
    Prefers user calibration if saved previously, falling back to bundled default.
    """
    user_cfg = get_user_data_dir() / "joints_config.json"
    if user_cfg.exists():
        return user_cfg
    return ASSETS_DIR / "joints_config.json"


def get_joints_config_save_path() -> Path:
    """Returns the path where joint calibrations should be written.
    When frozen (e.g. portable .exe), saves to persistent user_data_dir.
    When running from source, saves to repo assets/joints_config.json.
    """
    if getattr(sys, "frozen", False):
        return get_user_data_dir() / "joints_config.json"
    return ASSETS_DIR / "joints_config.json"
