#!/usr/bin/env python3
"""Build script for packaging Sesame Studio into a portable Windows executable (.exe).

Usage:
    python build_windows_exe.py
"""

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def compute_sha256(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def ensure_icon(assets_dir: Path) -> Path:
    ico_path = assets_dir / "sesame-icon.ico"
    png_path = assets_dir / "sesame-sticker2.png"
    if not ico_path.exists() and png_path.exists():
        from PIL import Image

        print(f"Generating {ico_path.name} from {png_path.name}...")
        img = Image.open(png_path)
        img.save(
            ico_path,
            format="ICO",
            sizes=[
                (16, 16),
                (24, 24),
                (32, 32),
                (48, 48),
                (64, 64),
                (128, 128),
                (256, 256),
            ],
        )
    return ico_path


def main():
    repo_root = Path(__file__).resolve().parent
    assets_dir = repo_root / "assets"
    dist_dir = repo_root / "dist"
    spec_path = repo_root / "SesameStudio.spec"

    print("=" * 60)
    print("Building Sesame Studio Standalone Portable Windows Executable")
    print("=" * 60)

    # 1. Ensure icon exists
    ensure_icon(assets_dir)

    # 2. Invoke PyInstaller
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        str(spec_path),
    ]
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(repo_root))
    if result.returncode != 0:
        print(f"ERROR: PyInstaller failed with exit code {result.returncode}")
        sys.exit(result.returncode)

    exe_path = dist_dir / "SesameStudio.exe"
    if not exe_path.exists():
        print(f"ERROR: Expected executable not found at {exe_path}")
        sys.exit(1)

    exe_size_mb = exe_path.stat().st_size / (1024 * 1024)
    exe_sha = compute_sha256(exe_path)
    print("\n" + "=" * 60)
    print("Build Succeeded!")
    print(f"Executable: {exe_path}")
    print(f"Size:       {exe_size_mb:.2f} MB")
    print(f"SHA-256:    {exe_sha}")

    # 3. Create zip package
    zip_path = dist_dir / "SesameStudio-v2.0.0-windows-x64.zip"
    print(f"\nCreating release archive: {zip_path.name}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(exe_path, arcname="SesameStudio.exe")

    zip_size_mb = zip_path.stat().st_size / (1024 * 1024)
    zip_sha = compute_sha256(zip_path)
    print(f"Archive:    {zip_path}")
    print(f"Size:       {zip_size_mb:.2f} MB")
    print(f"SHA-256:    {zip_sha}")
    print("=" * 60)


if __name__ == "__main__":
    main()
