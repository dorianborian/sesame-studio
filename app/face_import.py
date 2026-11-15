"""Image import for the Face Editor: fit/crop any picture to the 128x64
panel and reduce it to 1 bit with a live threshold / dither preview."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout,
)

from . import face_core as fc
from . import face_render as fr
from .face_widgets import OledPreview, caption, mono_label, suite_stylesheet
from .widgets import SegmentedControl

FIT_MODES = [
    (fr.FIT_CONTAIN, "Fit", "Show the whole image, letterboxed"),
    (fr.FIT_COVER, "Fill", "Fill the screen, cropping the overflow"),
    (fr.FIT_STRETCH, "Stretch", "Ignore the aspect ratio"),
]


def convert_image(image: QImage, mode: str = fr.FIT_CONTAIN, threshold: int = 128,
                  dither: bool = False, invert: bool = False) -> np.ndarray:
    """QImage -> (64,128) bool face frame."""
    return fc.threshold_gray(fr.fit_image_gray(image, mode), threshold, dither, invert)


class ImageImportDialog(QDialog):
    """Modal helper; ``result_array`` holds the converted frame on accept."""

    def __init__(self, image: QImage, parent=None, title: str = "Import Image"):
        super().__init__(parent)
        self.setObjectName("FaceImport")
        self.setWindowTitle(title)
        self.setStyleSheet(suite_stylesheet())
        self.image = image
        self.result_array: np.ndarray | None = None
        self._gray: np.ndarray | None = None
        self._mode = fr.FIT_CONTAIN

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.preview = OledPreview(scale=3)
        root.addWidget(self.preview, 0, Qt.AlignHCenter)

        info = QLabel(f"Source: {image.width()} x {image.height()} px  ->  128 x 64, 1-bit")
        info.setProperty("role", "hint")
        info.setAlignment(Qt.AlignCenter)
        root.addWidget(info)

        fit_row = QHBoxLayout()
        fit_row.setSpacing(6)
        fit_row.addWidget(caption("SCALE"))
        fit_row.addSpacing(6)
        self.fit_control = SegmentedControl(
            [(mode, label, None, tip) for mode, label, tip in FIT_MODES], current=self._mode)
        self.fit_control.currentChanged.connect(self._set_mode)
        fit_row.addWidget(self.fit_control)
        fit_row.addStretch(1)
        root.addLayout(fit_row)

        thr_row = QHBoxLayout()
        thr_row.setSpacing(8)
        thr_row.addWidget(caption("THRESHOLD"))
        self.threshold_slider = QSlider(Qt.Horizontal)
        self.threshold_slider.setRange(1, 255)
        self.threshold_slider.setValue(128)
        self.threshold_slider.setToolTip("Pixels brighter than this light up")
        self.threshold_slider.valueChanged.connect(self._on_threshold)
        thr_row.addWidget(self.threshold_slider, 1)
        self.threshold_label = mono_label("128")
        self.threshold_label.setFixedWidth(28)
        self.threshold_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        thr_row.addWidget(self.threshold_label)
        root.addLayout(thr_row)

        opt_row = QHBoxLayout()
        opt_row.setSpacing(16)
        self.dither_check = QCheckBox("Dither (Floyd-Steinberg)")
        self.dither_check.setToolTip("Simulate grey levels with a dot pattern; good for photos")
        self.dither_check.toggled.connect(self._refresh)
        self.invert_check = QCheckBox("Invert")
        self.invert_check.setToolTip("Swap lit and dark pixels (for dark-on-light artwork)")
        self.invert_check.toggled.connect(self._refresh)
        opt_row.addWidget(self.dither_check)
        opt_row.addWidget(self.invert_check)
        opt_row.addStretch(1)
        root.addLayout(opt_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setProperty("variant", "flat")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        self.ok_btn = QPushButton("Use Image")
        self.ok_btn.setProperty("variant", "primary")
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self._accept)
        btn_row.addWidget(cancel)
        btn_row.addWidget(self.ok_btn)
        root.addLayout(btn_row)

        # Dithering costs a few ms; coalesce slider drags into one update.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(15)
        self._timer.timeout.connect(self._refresh)
        self._refresh()

    # -- state ---------------------------------------------------------
    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._gray = None
        self._refresh()

    def _on_threshold(self, value: int) -> None:
        self.threshold_label.setText(str(value))
        self._timer.start()

    def current_array(self) -> np.ndarray:
        if self._gray is None:
            self._gray = fr.fit_image_gray(self.image, self._mode)
        return fc.threshold_gray(self._gray, self.threshold_slider.value(),
                                 self.dither_check.isChecked(), self.invert_check.isChecked())

    def _refresh(self) -> None:
        self.preview.set_array(self.current_array())

    def _accept(self) -> None:
        self.result_array = self.current_array()
        self.accept()
