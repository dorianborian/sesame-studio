"""Cached, high-DPI product logo.

The artwork is a 2500x2500 PNG; decoding it costs ~50 ms, so it is decoded
once per process into a 512px master and every requested size is scaled from
that (the splash and the welcome page both use it).
"""

from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication, QImage, QImageReader, QPixmap

__all__ = ["logo_pixmap"]

_MASTER_PX = 512
_masters: dict[str, QImage] = {}
_scaled: dict[tuple[str, int, float], QPixmap] = {}


def _master(path: str) -> QImage:
    img = _masters.get(path)
    if img is None:
        img = QImage()
        if path and os.path.exists(path):
            reader = QImageReader(path)
            size = reader.size()
            if size.isValid() and max(size.width(), size.height()) > _MASTER_PX:
                reader.setScaledSize(size.scaled(QSize(_MASTER_PX, _MASTER_PX), Qt.KeepAspectRatio))
            img = reader.read()
        _masters[path] = img
    return img


def logo_pixmap(path: str, size: int) -> QPixmap:
    """The logo scaled to fit ``size`` logical pixels (null pixmap if the
    file is missing), rendered at the screen's device pixel ratio."""
    app = QGuiApplication.instance()
    ratio = float(app.devicePixelRatio()) if app is not None else 1.0
    key = (path, int(size), ratio)
    pm = _scaled.get(key)
    if pm is None:
        master = _master(path)
        if master.isNull():
            pm = QPixmap()
        else:
            px = max(1, int(round(size * ratio)))
            pm = QPixmap.fromImage(
                master.scaled(QSize(px, px), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            pm.setDevicePixelRatio(ratio)
        _scaled[key] = pm
    return pm
