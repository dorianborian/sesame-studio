"""First-run coach-mark overlay (spotlight + callout bubbles over real
widgets) plus a persistent Help dialog with the same guidance as static
articles, reachable any time from the Help menu."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, QRect, QSettings, Signal
from PySide6.QtGui import QPainter, QColor, QPainterPath, QFont
from PySide6.QtWidgets import (
    QWidget, QPushButton, QLabel, QVBoxLayout, QHBoxLayout, QDialog,
    QListWidget, QTextBrowser, QSplitter, QListWidgetItem,
)

from .theme import ACCENT

SETTINGS_KEY_TUTORIAL_SEEN = "tutorial/seen_v1"


@dataclass
class CoachStep:
    target: QWidget | None
    title: str
    text: str


class TutorialOverlay(QWidget):
    finished = Signal()

    def __init__(self, steps: list[CoachStep], parent: QWidget):
        super().__init__(parent)
        self.steps = steps
        self.index = 0
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setGeometry(parent.rect())

        self.bubble = QWidget(self)
        self.bubble.setStyleSheet(
            f"background-color:#1e1e1e; border:1px solid {ACCENT}; border-radius:8px;"
        )
        layout = QVBoxLayout(self.bubble)
        self.title_label = QLabel()
        self.title_label.setStyleSheet("color:#e6e6e6; font-size:14px; font-weight:700; border:none;")
        self.text_label = QLabel()
        self.text_label.setWordWrap(True)
        self.text_label.setStyleSheet("color:#c8c8c8; font-size:11px; border:none;")
        layout.addWidget(self.title_label)
        layout.addWidget(self.text_label)

        btn_row = QHBoxLayout()
        self.progress_label = QLabel()
        self.progress_label.setStyleSheet("color:#777; border:none;")
        btn_row.addWidget(self.progress_label)
        btn_row.addStretch(1)
        self.skip_btn = QPushButton("Skip")
        self.skip_btn.setProperty("variant", "flat")
        self.skip_btn.clicked.connect(self._finish)
        self.next_btn = QPushButton("Next")
        self.next_btn.clicked.connect(self._advance)
        btn_row.addWidget(self.skip_btn)
        btn_row.addWidget(self.next_btn)
        layout.addLayout(btn_row)
        self.bubble.setFixedWidth(320)

        self._show_step()
        self.show()
        self.raise_()

    def _current_target_rect(self) -> QRect | None:
        step = self.steps[self.index]
        if step.target is None or not step.target.isVisible():
            return None
        top_left = step.target.mapTo(self.parentWidget(), step.target.rect().topLeft())
        return QRect(top_left, step.target.size())

    def _show_step(self) -> None:
        step = self.steps[self.index]
        self.title_label.setText(step.title)
        self.text_label.setText(step.text)
        self.progress_label.setText(f"{self.index + 1} / {len(self.steps)}")
        self.next_btn.setText("Done" if self.index == len(self.steps) - 1 else "Next")

        rect = self._current_target_rect()
        self.bubble.adjustSize()
        bw, bh = self.bubble.width(), self.bubble.height()
        if rect:
            x = min(max(8, rect.center().x() - bw // 2), self.width() - bw - 8)
            y = rect.bottom() + 16
            if y + bh > self.height():
                y = max(8, rect.top() - bh - 16)
        else:
            x, y = (self.width() - bw) // 2, (self.height() - bh) // 2
        self.bubble.move(x, y)
        self.update()

    def _advance(self) -> None:
        if self.index < len(self.steps) - 1:
            self.index += 1
            self._show_step()
        else:
            self._finish()

    def _finish(self) -> None:
        QSettings("SesameRobot", "SesameStudio").setValue(SETTINGS_KEY_TUTORIAL_SEEN, True)
        self.finished.emit()
        self.deleteLater()

    def resizeEvent(self, event) -> None:
        self.setGeometry(self.parentWidget().rect())
        self._show_step()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRect(self.rect())
        rect = self._current_target_rect()
        if rect:
            inner = QPainterPath()
            inner.addRoundedRect(rect.adjusted(-6, -6, 6, 6), 8, 8)
            path = path.subtracted(inner)
            painter.setPen(QColor(ACCENT))
            painter.drawRoundedRect(rect.adjusted(-6, -6, 6, 6), 8, 8)
        painter.fillPath(path, QColor(0, 0, 0, 160))


def should_show_first_run_tutorial() -> bool:
    return not QSettings("SesameRobot", "SesameStudio").value(SETTINGS_KEY_TUTORIAL_SEEN, False, type=bool)


HELP_ARTICLES: list[tuple[str, str]] = [
    ("Getting Started", """
<h2>Getting Started</h2>
<p>Sesame Studio is organized around three panels: the <b>3D Viewport</b> (posable
robot preview), the <b>Timeline</b> (sequences of poses), and the <b>Inspector</b>
(per-servo angles for whatever frame is selected).</p>
<ol>
<li>Drag sliders in the Inspector to pose the robot; the 3D Viewport updates
live. Drag empty space in the Viewport to orbit, scroll to zoom, Shift+drag
to pan.</li>
<li>Click <b>+ Add Frame</b> in the Timeline to capture that pose.</li>
<li>Repeat to build a sequence, then hit <b>Play</b> to preview it -- servos
animate smoothly between frames rather than snapping, so it previews the
way the physical servos actually move.</li>
<li>The Sequence dropdown also lists every built-in firmware pose
(wave, dance, walk, ...) marked with a star -- select one to play it
immediately, or "Duplicate to Custom" to edit your own copy.</li>
<li>Use the Faces panel to draw expressions and assign them to frames; the
assigned face shows live on the small screen mounted on the model.</li>
<li>Export finished sequences and faces from the File menu and paste them
into your firmware checkout -- see the Export Guide.</li>
</ol>
"""),
    ("Timeline Guide", """
<h2>Timeline Guide</h2>
<p>Each sequence is a named list of frames. A frame stores all 8 servo angles,
a delay in milliseconds, and an optional face.</p>
<ul>
<li><b>Ruler/grid:</b> click or drag anywhere in the ruler strip at the top
to scrub the red playhead and preview the pose at that exact moment,
Premiere-style.</li>
<li><b>Reorder:</b> drag a frame block left/right past its neighbor.</li>
<li><b>Retime:</b> drag the right edge of a block to change its delay.</li>
<li><b>Right-click</b> a block for duplicate/insert/delete.</li>
<li>Use the sequence dropdown and +/Rename/Delete to manage multiple
custom sequences (e.g. one per pose/animation). Built-in sequences (marked
with a star) are read-only -- use "Duplicate to Custom" to edit one.</li>
<li><b>Play</b> animates the playhead across the sequence with the servos
interpolating smoothly between frames, scaled by the speed slider for fast
iteration.</li>
</ul>
"""),
    ("Face Painter Guide", """
<h2>Face Painter Guide</h2>
<p>The Face Editor is a 128x64 1-bit bitmap canvas matching the robot's OLED
exactly, with pencil/eraser/line/rect/ellipse/fill tools plus a text tool
for typing expressions directly onto the display.</p>
<ul>
<li>The right-hand panel always shows the exact byte array Studio will
export -- no separate conversion step needed.</li>
<li>Add up to 6 animation frames per face (root + 5 numbered frames) with
onion-skinning to help line up motion.</li>
<li>Category flags (Movement/Conversational/Talk/Special/Custom) organize
the Face Library and warn you before overwriting a built-in firmware face
name.</li>
<li>Whatever face is selected in the library, or assigned to the current
timeline frame, renders on the small screen mounted on the 3D model
(0.96in across, matching the real OLED). Its position, rotation, and size
are all adjustable in Joint Calibration under "Face Display (Screen)" if it
doesn't line up with your hardware.</li>
</ul>
"""),
    ("Export Guide", """
<h2>Exporting to Firmware</h2>
<p>Studio generates code in the same style the firmware already uses, meant
to be pasted in rather than written automatically:</p>
<ol>
<li><b>Sequences</b> export as a function like <code>runWavePose()</code> in
<code>firmware/movement-sequences.h</code>: add the prototype near the top,
paste the function body, and add a dispatch line in <code>loop()</code> in
<code>firmware/sesame-firmware-main.ino</code> (e.g.
<code>else if (cmd == "mypose") runMyPose();</code>).</li>
<li><b>Faces</b> export as: (a) a line to add to the <code>FACE_LIST</code>
macro in <code>firmware/face-bitmaps.h</code>, (b) the raw
<code>epd_bitmap_*</code> byte arrays to paste into that same file, and (c)
an entry for <code>faceFpsEntries[]</code> in the main .ino if you want a
custom animation speed.</li>
<li>Re-flash the firmware (Arduino IDE) once your paste is in place -- see
<code>firmware/README.md</code> for board setup.</li>
</ol>
"""),
    ("About", """
<h2>About Sesame Studio</h2>
<p>Sesame Studio is the animation and expression design tool for the Sesame
Robot project: pose the robot in 3D, sequence poses on a timeline, paint
OLED faces, and export code ready to paste into the firmware.</p>
"""),
]


class HelpDialog(QDialog):
    def __init__(self, parent=None, open_article: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Sesame Studio Help")
        self.resize(760, 520)
        layout = QHBoxLayout(self)
        splitter = QSplitter()
        layout.addWidget(splitter)

        self.list_widget = QListWidget()
        for title, _ in HELP_ARTICLES:
            self.list_widget.addItem(QListWidgetItem(title))
        splitter.addWidget(self.list_widget)

        self.browser = QTextBrowser()
        splitter.addWidget(self.browser)
        splitter.setSizes([200, 560])

        self.list_widget.currentRowChanged.connect(self._show_article)
        start_row = 0
        if open_article:
            for i, (title, _) in enumerate(HELP_ARTICLES):
                if title == open_article:
                    start_row = i
                    break
        self.list_widget.setCurrentRow(start_row)

    def _show_article(self, row: int) -> None:
        if 0 <= row < len(HELP_ARTICLES):
            self.browser.setHtml(HELP_ARTICLES[row][1])
