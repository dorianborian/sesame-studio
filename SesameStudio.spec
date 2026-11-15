# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification file for building Sesame Studio on Windows.
Produces a standalone, portable single-file executable (SesameStudio.exe)
with the bundled 3D robot model, firmware templates, and application icons.
"""

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Collect any dynamic submodules for PyOpenGL and PySide6
hidden_imports = [
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtOpenGL",
    "OpenGL",
    "OpenGL.platform",
    "OpenGL.platform.win32",
    "OpenGL.arrays",
    "OpenGL.arrays.ctypesarrays",
    "OpenGL.arrays.ctypespointers",
    "OpenGL.arrays.numpymodule",
    "OpenGL.arrays.lists",
    "OpenGL.arrays.numbers",
    "OpenGL.arrays.strings",
    "OpenGL.GL",
    "OpenGL.GLU",
]
hidden_imports += collect_submodules("OpenGL")
# numpy.random's compiled bit generators import the stdlib ``secrets`` module
# from C, which PyInstaller's static analysis cannot see.
hidden_imports += ["secrets"]
# app.widgets resolves its exports lazily through importlib, so name every
# module of the app package explicitly rather than rely on import tracing.
hidden_imports += collect_submodules("app")

# Only what the app reads at runtime. assets/stl holds print/source geometry
# (inputs to tools/build_model.py) and is deliberately not bundled.
datas = [
    ("assets/model", "assets/model"),
    ("assets/firmware", "assets/firmware"),
    ("assets/sesame-icon.ico", "assets"),
    ("assets/sesame-icon.png", "assets"),
    ("assets/sesame-sticker.png", "assets"),
    ("assets/sesame-sticker2.png", "assets"),
]

a = Analysis(
    ["sesame_studio.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "IPython",
        "unittest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="SesameStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/sesame-icon.ico",
)
