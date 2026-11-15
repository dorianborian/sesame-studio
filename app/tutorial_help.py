"""Help center: a two-pane dialog (searchable article list + styled article).

Articles are written in a small HTML dialect rendered by ``QTextBrowser``:

* ``[[Ctrl+Z]]`` becomes key-cap chips (one per key; mouse gestures such as
  ``[[Right-drag]]`` use the same chips). The chips are painted on demand as
  images, so they look like the key hints used everywhere else in the app.
* ``<a href="help:Timeline Guide">`` jumps to another article and
  ``<a href="tour:start">`` starts the interactive tour.
* ``{version}`` is the application version.

Every statement in the articles describes the app as it is; when a shortcut,
label or behaviour changes in the code, change it here too. The shortcut
tables were collected from ``MainWindow._build_actions``,
``TimelineWidget._key_action``, ``Viewport3D`` (mouse + ``keyPressEvent``),
``FaceLibraryPanel.eventFilter`` and ``FaceEditorDialog._build_shortcuts``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QFontMetrics, QImage, QPainter, QPen, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QTextBrowser, QVBoxLayout,
)

from . import __version__, theme
from .icons import icon

__all__ = ["HelpDialog", "HelpArticle", "HELP_ARTICLES", "article_titles", "find_article"]


@dataclass(frozen=True)
class HelpArticle:
    title: str
    icon: str
    source: str          # the article in the dialect described above

    def plain_text(self) -> str:
        """Lower-cased text used by the search box (key chips count as text)."""
        text = _KEY_RE.sub(lambda m: f" {m.group(1)} ", self.source)
        text = re.sub(r"<[^>]+>", " ", text)
        text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("{version}", __version__))
        return re.sub(r"\s+", " ", f"{self.title} {text}").strip().lower()


_KEY_RE = re.compile(r"\[\[(.+?)\]\]")


def _rows(*rows: tuple[str, str]) -> str:
    """A two-column "keys | what they do" table."""
    cells = "".join(
        f'<tr><td class="keys" width="42%">{keys}</td><td class="what">{what}</td></tr>'
        for keys, what in rows)
    return f'<table class="ref" width="100%" cellspacing="0" cellpadding="0">{cells}</table>'


def _note(text: str) -> str:
    return (f'<table class="note" width="100%" cellspacing="0" cellpadding="0">'
            f"<tr><td>{text}</td></tr></table>")


# ---------------------------------------------------------------------------
# articles
# ---------------------------------------------------------------------------
_GETTING_STARTED = f"""
<h1>Getting Started</h1>
<p class="lead">Sesame Studio is where you design how your Sesame robot moves and what its
face shows: pose it in 3D, string poses into a sequence, paint faces for its screen, then
export code for the firmware.</p>

{_note('New here? The <a href="tour:start">interactive tour</a> walks you through all of this '
       'hands-on in about two minutes. Replay it any time with [[F1]] or the Tour button.')}

<h2>The workspace</h2>
<ul>
<li><b>3D view</b> (centre): the robot. Drag its legs to pose it. The bar floating in its
top-left corner has Simulate, camera views and display options.</li>
<li><b>Timeline</b> (bottom): the frames of the current sequence, with Play, Add Frame and
the sequence list.</li>
<li><b>Pose</b> tab (right): a slider and an exact angle for each of the eight servos, plus
the selected frame's duration and face.</li>
<li><b>Faces</b> tab (right, beside Pose): the face library, and the way into the face
painter.</li>
</ul>
<p>Panels can be hidden and shown from the View menu. <b>View ▸ Reset Panel Layout</b> puts
everything back.</p>

<h2>Your first animation</h2>
<ol>
<li><b>Pose the robot.</b> Drag a leg in the 3D view, or use the sliders in the Pose tab.
See <a href="help:Posing in 3D">Posing in 3D</a>.</li>
<li><b>Add a frame.</b> Click <b>Add Frame</b> in the timeline ([[Ctrl+Enter]]). It copies
the selected frame, so pose the copy to make the next step of the move.</li>
<li><b>Set the timing.</b> Drag the right edge of a frame block, or type a Duration in the
Pose tab. A frame's duration is the time the robot takes to reach that pose.</li>
<li><b>Play.</b> Press [[Space]]. With Simulate on, the preview turns the joints at real
servo speed with the robot standing on the floor.</li>
<li><b>Add a face.</b> In the Faces tab, select a face and click <b>Assign to Frame</b>.</li>
<li><b>Save and export.</b> [[Ctrl+S]] saves a <code>.sesproj</code> project.
<b>Export ▸ Export Current Sequence…</b> ([[Ctrl+E]]) generates the firmware code; the
<a href="help:Export Guide">Export Guide</a> shows where it goes.</li>
</ol>
<p>Every edit can be undone with [[Ctrl+Z]].</p>

<h2>Built-in moves</h2>
<p>The sequence list in the timeline also holds every move that ships in the firmware:
rest, stand, wave, dance, walk_forward and more. They are read-only, so you can play and
study them but not change them. <b>Duplicate to edit</b> copies one into your project.</p>

<h2>Editing a firmware face file directly</h2>
<p><b>File ▸ Open Face Bitmaps (.h)…</b> ([[Ctrl+Shift+O]]) opens a firmware
<code>face-bitmaps.h</code>. The Faces tab then shows the faces in that file, and saving
writes them straight back into it. In this mode the timeline is only a scratch pad:
sequences are not saved with the header.</p>
"""

_POSING = f"""
<h1>Posing in 3D</h1>
<p class="lead">The 3D view shows a model built from the robot's CAD, so every pivot and
rotation axis matches the real thing. You pose it directly: grab a leg and drag.</p>

<h2>Moving the camera</h2>
{_rows(
    ("[[Drag]] empty space or the body", "Orbit"),
    ("[[Alt+Drag]]", "Orbit, even when the cursor is over a leg"),
    ("[[Right-drag]] or [[Middle-drag]]", "Pan. [[Shift+Drag]] on empty space pans too"),
    ("[[Scroll]]", "Zoom towards the cursor"),
    ("[[Double-click]] empty space, or [[F]]", "Frame the robot"),
    ("[[Home]]", "Back to the default three-quarter view"),
)}
<p>The camera button in the floating bar (and <b>View ▸ Camera</b>) has Front, Back, Left,
Right and Top views.</p>

<h2>Posing the legs</h2>
<p>Each leg has two joints: a hip (R1, R2, L1, L2) and a knee (R3, R4, L3, L4). Drag the
part of the leg you want to move. What you pose is written into the selected timeline
frame, and each drag is one undo step.</p>
{_rows(
    ("[[Drag]] a leg", "Turn that joint"),
    ("[[Ctrl]] while dragging", "Snap to 5° steps"),
    ("[[Shift]] while dragging", "Slow, fine movement"),
    ("[[Shift+Drag]] a lower leg", "Move the foot; the hip and knee both follow"),
    ("[[Esc]] while dragging", "Cancel and put the pose back"),
    ("[[Click]] a leg", "Select its joint. A ring shows the joint's range, its angle, "
                        "and a handle you can drag as well"),
    ("[[Tab]] / [[Shift+Tab]]", "Select the next / previous joint"),
    ("[[Left]] [[Right]] or [[[]] [[]]]", "Nudge the selected joint by 1°. Hold [[Shift]] for 10°"),
    ("[[Right-click]] a leg", "Reset to 90°, set to the stand angle, or mirror onto its twin"),
    ("[[Esc]]", "Deselect"),
)}
<p><b>Mirror posing</b> (the mirror button in the floating bar) makes every edit drive the
matching joint on the other side as well, which is handy for symmetric poses.</p>
<p>While a sequence is playing, dragging always orbits. Pause to pose.</p>

<h2>The Pose tab</h2>
<ul>
<li>One row per joint, grouped by leg, with a slider and an exact angle (0–180°). Pointing
at a row lights up that leg in 3D, and the other way round.</li>
<li><b>Presets</b> jumps to Stand or Rest (all joints at 90°).</li>
<li><b>Mirror</b> copies the left legs onto the right, the right onto the left, or swaps
the two sides.</li>
<li>The two buttons on the right copy the whole pose and paste it, for example into
another frame.</li>
<li>Right-click a row to reset that joint to 90°, set it to its stand angle, or mirror it
onto its twin.</li>
</ul>

<h2>Physics preview</h2>
<p>With <b>Simulate</b> on (floating bar, or <b>View ▸ Simulate Physics</b>, [[Ctrl+P]]) the
robot is a rigid body under gravity whose shape follows the servo angles. Its feet and
body touch the floor with friction, so planted feet grip and the body moves when the legs
do. Standing, tipping over, push-ups and walking all come out of the servo angles alone.</p>
<ul>
<li><b>While you pose</b>, joints jump straight to the angle you set and the body settles
on the floor.</li>
<li><b>During playback</b>, each frame's angles are sent as servo targets the moment the
playhead enters the frame, exactly as the firmware does, and the simulated servos turn
towards them at servo speed. If a frame is too short for the servos to get there, the pose
falls short, just as it would on the robot.</li>
<li>Playing from the start first puts the robot back at the centre of the floor in the
first frame's pose, so every run is repeatable.</li>
</ul>
{_rows(
    ("<b>Simulate</b>", "Turn the physics preview on or off ([[Ctrl+P]])"),
    ("<b>Reset</b> (home icon)", "Put the robot back at the centre of the floor ([[Ctrl+R]])"),
    ("<b>Follow</b> (target icon)", "Keep the camera on the robot as it walks away"),
)}
<p>With Simulate off the robot stays fixed in place and playback simply blends from one
pose to the next.</p>
<p class="muted">The simulation is a preview, not a guarantee. It treats the robot as
rigid parts on a flat floor; it does not model motor strength or the parts bumping into
each other.</p>
"""

_TIMELINE = f"""
<h1>Timeline Guide</h1>
<p class="lead">A sequence is a list of frames. Each frame stores all eight servo angles,
a duration in milliseconds and, optionally, a face.</p>

<h2>How timing works</h2>
<p>A frame's duration is the time the robot takes to move <i>into</i> that pose, so the
pose is reached at the <b>end</b> of its block, where the diamond marker is. Selecting a
frame parks the playhead there. This matches the exported code, which sets the servos and
then waits.</p>

<h2>Working with frames</h2>
{_rows(
    ("<b>Add Frame</b>, [[Ctrl+Enter]], or the + after the last block",
     "Insert a copy of the selected frame right after it"),
    ("[[Click]] a block", "Select it. Its pose loads into the 3D view and the Pose tab"),
    ("[[Ctrl+Click]] / [[Shift+Click]]", "Add to the selection / select a range"),
    ("[[Drag]] a block", "Reorder. Dragging a selected block moves the whole selection"),
    ("[[Drag]] a block's right edge",
     "Retime in 10 ms steps ([[Shift]] for 1 ms). With several frames selected they all change together"),
    ("[[Right-click]]", "Duplicate, cut, copy, paste, insert a blank frame, delete"),
    ("[[Ctrl+C]] [[Ctrl+X]] [[Ctrl+V]]", "Copy, cut, and paste after the selection"),
    ("[[Ctrl+D]]", "Duplicate the selected frames"),
    ("[[Del]]", "Delete the selected frames"),
    ("[[Ctrl+A]]", "Select every frame"),
    ("[[Ctrl+Z]] / [[Ctrl+Y]]", "Undo / redo any edit: poses, timing, faces, frames and sequences"),
)}
<p>The frame shortcuts work while the timeline has focus, so click it first. Copied frames
also go to the system clipboard as text, so you can paste them into another project. A
sequence always keeps at least one frame.</p>

<h2>Scrubbing and playback</h2>
{_rows(
    ("[[Click]] or [[Drag]] in the ruler", "Scrub: see the pose at any moment"),
    ("<b>Play</b> / [[Space]]", "Play from the playhead, or pause"),
    ("<b>Stop</b>", "Pause and go back to the start"),
    ("[[Left]] / [[Right]]", "Previous / next frame. Hold [[Shift]] to grow the selection"),
    ("[[Home]] / [[End]]", "First / last frame"),
    ("<b>Loop</b> / [[L]]", "Repeat when the end is reached"),
    ("Speed (0.25× to 2×)", "Preview speed only. It does not change the exported timing"),
)}
<p>With Simulate on, playback shows what the robot would really do. See
<a href="help:Posing in 3D">Posing in 3D</a>.</p>

<h2>Zoom and tracks</h2>
{_rows(
    ("[[+]] / [[-]]", "Zoom in / out"),
    ("[[Ctrl+Scroll]]", "Zoom at the cursor"),
    ("[[F]] or [[Ctrl+0]]", "Zoom to fit the whole sequence"),
    ("[[Scroll]] or [[Middle-drag]]", "Scroll sideways"),
    ("[[T]]", "Show or hide the servo tracks"),
)}
<p>The servo tracks draw each joint's angle over time, one lane per servo. The FACE lane
shows which face is on screen: a face stays up until a later frame sets another one.</p>

<h2>Timing and face of a frame</h2>
<p>Below the sliders, the Pose tab shows the selected frame's <b>Duration</b>, its
<b>Face</b> and the <b>Face mode</b> for animated faces (loop, once or boomerang).</p>

<h2>Sequences</h2>
<p>The list at the top-left of the timeline switches between sequences. The buttons beside
it create, duplicate, rename and delete them (also in the Sequence menu). Your project
always keeps at least one sequence of its own.</p>
<p>The firmware's built-in moves are listed below yours with a lock. They are read-only:
you can play them and pose the robot to try things, but nothing is stored.
<b>Duplicate to edit</b> makes a copy you can change.</p>
"""

_FACES = f"""
<h1>Face Painter Guide</h1>
<p class="lead">Faces are 128 × 64 pixel, one-colour bitmaps: exactly what the robot's
OLED screen shows. The Faces tab is the library. Double-click a face, or click
<b>New</b>, to open the painter.</p>

<h2>The face library</h2>
<ul>
<li>Click a face to preview it on the robot's screen in the 3D view. Point at an animated
face to play it.</li>
<li><b>Assign to Frame</b> puts the selected face on the selected timeline frame.</li>
<li>The search box and the category chips filter the grid.</li>
<li>Faces with a lock ship in the firmware and cannot be changed. Editing one opens a copy
under a new name, and Duplicate makes a copy at once.</li>
<li>An orange dot marks faces used in the current sequence. OVERRIDE marks a face of yours
that replaces a firmware face of the same name.</li>
</ul>
{_rows(
    ("[[Double-click]] or [[Enter]]", "Edit the selected face"),
    ("[[Ctrl+D]]", "Duplicate it"),
    ("[[Del]]", "Delete it (your own faces only)"),
)}

<h2>Drawing tools</h2>
{_rows(
    ("[[B]]", "Pencil"),
    ("[[E]]", "Eraser"),
    ("[[L]]", "Line"),
    ("[[R]] / [[Shift+R]]", "Rectangle / filled rectangle"),
    ("[[O]] / [[Shift+O]]", "Ellipse / filled ellipse"),
    ("[[G]]", "Flood fill"),
    ("[[T]]", "Text: type in the bar above the canvas, then click to stamp it"),
    ("[[M]]", "Select / move"),
    ("[[1]] to [[4]], or [[[]] [[]]]", "Brush size"),
    ("[[Right-drag]]", "Erase instead of draw, with the pencil, line and shape tools"),
    ("[[Shift]] while drawing", "Straight line, 45° line, square or circle"),
)}

<h2>Mirror and transform</h2>
{_rows(
    ("[[S]] / [[Shift+S]]", "Mirror drawing left-right / top-bottom, to draw both eyes at once"),
    ("[[H]] / [[V]]", "Flip the frame horizontally / vertically"),
    ("[[Ctrl+I]]", "Invert the pixels"),
    ("[[Alt+Left]] [[Alt+Right]] [[Alt+Up]] [[Alt+Down]]", "Shift the whole frame by 1 px (it wraps around)"),
    ("[[Ctrl+Del]]", "Clear the frame, or the selection"),
    ("[[Ctrl+Z]] / [[Ctrl+Y]]", "Undo / redo"),
)}
<p>The toolbar also has a button that centres the drawing on the screen.</p>

<h2>Selection</h2>
{_rows(
    ("[[Drag]] with the Select tool", "Select a region"),
    ("[[Drag]] inside it", "Move it. Hold [[Ctrl]] to move a copy"),
    ("Arrow keys", "Nudge by 1 px. Hold [[Shift]] for 8 px"),
    ("[[Ctrl+A]]", "Select the whole frame"),
    ("[[Ctrl+C]] [[Ctrl+X]] [[Ctrl+V]]", "Copy, cut, paste. A copied region can be pasted into another face"),
    ("[[Del]]", "Clear the selection"),
    ("[[Esc]] or [[Enter]]", "Deselect"),
)}

<h2>Frames, onion skin and preview</h2>
<p>A face can have up to 6 frames. The strip under the canvas shows them: click to
switch, drag to reorder, right-click for more.</p>
{_rows(
    ("[[Ctrl+N]]", "New blank frame"),
    ("[[Ctrl+D]]", "Duplicate the current frame"),
    ("[[,]] / [[.]]", "Previous / next frame"),
    ("[[K]]", "Onion skin: ghost the previous frame under the one you are drawing"),
    ("[[P]]", "Play or pause the animated preview"),
)}
<p>The <b>FPS</b> next to the preview is the animation speed and is saved with the face.
Boomerang there is a preview only: whether a face loops, plays once or boomerangs is
chosen per timeline frame (Face mode in the Pose tab). While the painter is open, the
robot's screen in the 3D view mirrors your drawing live.</p>

<h2>Import and export pictures</h2>
<ul>
<li>[[Ctrl+O]] imports a picture (PNG, JPG, BMP, GIF or WebP). You can also drop an image
file onto the painter, or paste one with [[Ctrl+V]].</li>
<li>The import window fits the picture to the screen (Fit, Fill or Stretch) and turns it
into on/off pixels with a threshold, optional dithering and Invert.</li>
<li>[[Ctrl+E]] saves the current frame as a PNG.</li>
</ul>

<h2>View</h2>
{_rows(
    ("[[Ctrl+Scroll]], [[Ctrl++]] / [[Ctrl+-]]", "Zoom"),
    ("[[Ctrl+0]]", "Fit to the window"),
    ("[[Space+Drag]] or [[Middle-drag]]", "Pan"),
    ("[[#]]", "Show or hide the pixel grid"),
)}

<h2>Name, category and saving</h2>
<ul>
<li><b>Name</b> becomes part of the code, so use letters, digits and underscores.</li>
<li><b>Category</b> is the group the face is filed under in the library.</li>
<li><b>Override existing firmware face</b>: tick this when your face is meant to replace a
built-in face of the same name.</li>
<li>The <b>C Array</b> tab shows the byte array the firmware needs, with a Copy button.</li>
<li><b>Save Face</b> ([[Ctrl+S]]) saves and closes. [[Esc]] closes, asking first if there is
unsaved work.</li>
</ul>
"""

_EXPORT = f"""
<h1>Export Guide</h1>
<p class="lead">Studio generates code in the same style the firmware already uses. You
paste it into your firmware checkout; Studio does not change the firmware for you.</p>

<h2>Sequences</h2>
<ol>
<li>Select the sequence in the timeline, then choose <b>Export ▸ Export Current
Sequence…</b> ([[Ctrl+E]]). Use <b>Copy to Clipboard</b> or <b>Save to File…</b>.</li>
<li>You get a function named after the sequence, such as <code>runWavePose()</code>.
Paste it into <code>firmware/movement-sequences.h</code> and add its prototype near the
top of that file.</li>
<li>Add a dispatch line in <code>loop()</code> in
<code>firmware/sesame-firmware-main.ino</code>, for example
<code>else if (cmd == "mypose") runMyPose();</code></li>
</ol>
<p>In the function, each frame sets all eight servos with <code>setServoAngle()</code> and
then waits with <code>delayWithFace()</code> for the frame's duration. A frame that changes
the face calls <code>setFaceWithMode()</code> first. Export stops with a message if any
angle is outside 0–180°.</p>

<h2>Faces</h2>
<p>Select a face of your own in the Faces tab and choose <b>Export ▸ Export Selected
Face…</b>. The result has three labelled parts:</p>
<ol>
<li>a line to add to the <code>FACE_LIST</code> macro in
<code>firmware/face-bitmaps.h</code>,</li>
<li>the raw <code>epd_bitmap_*</code> byte arrays to paste into that same file,</li>
<li>an entry for <code>faceFpsEntries[]</code> in the main <code>.ino</code>, which sets
the face's animation speed.</li>
</ol>
<p>Built-in faces are already in the firmware, so there is nothing to export for them.
<b>Export ▸ Export All Faces to face-bitmaps.h…</b> writes all of your project's faces
into one header file.</p>

<h2>Skip the pasting for faces</h2>
<p><b>File ▸ Open Face Bitmaps (.h)…</b> ([[Ctrl+Shift+O]]) opens your firmware's
<code>face-bitmaps.h</code> itself. Edit the faces and save: they are written straight
back into the file. See the <a href="help:Face Painter Guide">Face Painter Guide</a>.</p>

<h2>Flash the robot</h2>
<p>Re-flash the firmware (Arduino IDE) once your paste is in place. See
<code>firmware/README.md</code> for board setup.</p>
"""

_SHORTCUTS = f"""
<h1>Keyboard Shortcuts</h1>
<p class="lead">Every shortcut and mouse gesture, grouped by where it works.</p>

<h2>Everywhere</h2>
{_rows(
    ("[[Ctrl+N]]", "New project"),
    ("[[Ctrl+O]]", "Open a project or a face-bitmaps.h"),
    ("[[Ctrl+Shift+O]]", "Open Face Bitmaps (.h)"),
    ("[[Ctrl+S]]", "Save"),
    ("[[Ctrl+Shift+S]]", "Save As"),
    ("[[Ctrl+E]]", "Export the current sequence"),
    ("[[Ctrl+Z]]", "Undo"),
    ("[[Ctrl+Y]] or [[Ctrl+Shift+Z]]", "Redo"),
    ("[[Space]]", "Play / pause"),
    ("[[Ctrl+Enter]]", "Add Frame"),
    ("[[Ctrl+P]]", "Simulate physics on / off"),
    ("[[Ctrl+R]]", "Reset the robot's position"),
    ("[[F1]]", "Interactive tour"),
    ("[[Ctrl+/]]", "This page"),
    ("[[Ctrl+Q]]", "Exit"),
)}

<h2>3D view</h2>
{_rows(
    ("[[Drag]] a leg", "Pose that joint ([[Ctrl]] snaps to 5°, [[Shift]] is fine control)"),
    ("[[Shift+Drag]] a lower leg", "Move the foot; hip and knee follow"),
    ("[[Drag]] empty space", "Orbit ([[Alt+Drag]] orbits anywhere)"),
    ("[[Right-drag]] or [[Middle-drag]]", "Pan"),
    ("[[Scroll]]", "Zoom"),
    ("[[Double-click]] empty space, or [[F]]", "Frame the robot"),
    ("[[Home]]", "Reset the view"),
    ("[[Tab]] / [[Shift+Tab]]", "Select the next / previous joint"),
    ("[[Left]] [[Right]] or [[[]] [[]]]", "Nudge the selected joint 1° ([[Shift]]: 10°)"),
    ("[[Right-click]] a leg", "Joint menu"),
    ("[[Esc]]", "Cancel a drag, or deselect"),
)}

<h2>Timeline</h2>
{_rows(
    ("[[Left]] / [[Right]]", "Previous / next frame ([[Shift]] extends the selection)"),
    ("[[Home]] / [[End]]", "First / last frame"),
    ("[[Insert]]", "Add Frame"),
    ("[[Del]] or [[Backspace]]", "Delete the selected frames"),
    ("[[Ctrl+C]] [[Ctrl+X]] [[Ctrl+V]]", "Copy, cut, paste frames"),
    ("[[Ctrl+D]]", "Duplicate the selected frames"),
    ("[[Ctrl+A]]", "Select all frames"),
    ("[[+]] / [[-]]", "Zoom in / out ([[Ctrl+Scroll]] zooms at the cursor)"),
    ("[[F]] or [[Ctrl+0]]", "Zoom to fit"),
    ("[[L]]", "Loop on / off"),
    ("[[T]]", "Servo tracks on / off"),
    ("[[Ctrl+Click]] / [[Shift+Click]]", "Add to the selection / select a range"),
    ("[[Drag]] a block / its right edge", "Reorder / retime ([[Shift]]: 1 ms steps)"),
    ("[[Esc]]", "Cancel a drag, or keep only the main selected frame"),
)}

<h2>Faces tab</h2>
{_rows(
    ("[[Enter]] or [[Double-click]]", "Edit the selected face"),
    ("[[Ctrl+D]]", "Duplicate it"),
    ("[[Del]]", "Delete it"),
)}

<h2>Face painter: tools</h2>
{_rows(
    ("[[B]]", "Pencil"),
    ("[[E]]", "Eraser"),
    ("[[L]]", "Line"),
    ("[[R]] / [[Shift+R]]", "Rectangle / filled rectangle"),
    ("[[O]] / [[Shift+O]]", "Ellipse / filled ellipse"),
    ("[[G]]", "Flood fill"),
    ("[[T]]", "Text"),
    ("[[M]]", "Select / move"),
    ("[[1]] to [[4]], [[[]] [[]]]", "Brush size"),
    ("[[S]] / [[Shift+S]]", "Mirror drawing left-right / top-bottom"),
)}

<h2>Face painter: editing</h2>
{_rows(
    ("[[Ctrl+Z]]", "Undo"),
    ("[[Ctrl+Y]] or [[Ctrl+Shift+Z]]", "Redo"),
    ("[[Ctrl+C]] [[Ctrl+X]] [[Ctrl+V]]", "Copy, cut, paste"),
    ("[[Ctrl+A]]", "Select the whole frame"),
    ("[[Del]]", "Clear the selection"),
    ("Arrow keys", "Nudge the selection 1 px ([[Shift]]: 8 px)"),
    ("[[Alt]] + arrow keys", "Shift the whole frame 1 px"),
    ("[[H]] / [[V]]", "Flip horizontally / vertically"),
    ("[[Ctrl+I]]", "Invert"),
    ("[[Ctrl+Del]]", "Clear the frame"),
    ("[[Esc]]", "Cancel a stroke, deselect, or close the painter"),
)}

<h2>Face painter: frames, view and files</h2>
{_rows(
    ("[[Ctrl+N]]", "New blank frame"),
    ("[[Ctrl+D]]", "Duplicate the frame"),
    ("[[,]] / [[.]]", "Previous / next frame"),
    ("[[K]]", "Onion skin"),
    ("[[P]]", "Play / pause the preview"),
    ("[[Ctrl++]] / [[Ctrl+-]]", "Zoom in / out (also [[Ctrl+Scroll]])"),
    ("[[Ctrl+0]]", "Fit to the window"),
    ("[[Space+Drag]]", "Pan"),
    ("[[#]]", "Pixel grid"),
    ("[[Ctrl+O]]", "Import a picture"),
    ("[[Ctrl+E]]", "Export the frame as PNG"),
    ("[[Ctrl+S]]", "Save the face and close"),
)}
"""

_ABOUT = """
<h1>About Sesame Studio</h1>
<p class="lead">Version {version}</p>
<p>Sesame Studio is the animation and expression design tool for the Sesame Robot
project, a small 3D-printed four-legged robot with eight servos and a 128 × 64 OLED face.
Pose the robot in 3D, sequence poses on a timeline, preview the motion with physics,
paint its faces, and export code ready to paste into the firmware.</p>
<p>New to the app? Start with <a href="help:Getting Started">Getting Started</a> or take
the <a href="tour:start">interactive tour</a>.</p>
"""

HELP_ARTICLES: list[HelpArticle] = [
    HelpArticle("Getting Started", "compass", _GETTING_STARTED),
    HelpArticle("Posing in 3D", "cube", _POSING),
    HelpArticle("Timeline Guide", "timeline", _TIMELINE),
    HelpArticle("Face Painter Guide", "face", _FACES),
    HelpArticle("Export Guide", "export", _EXPORT),
    HelpArticle("Keyboard Shortcuts", "keyboard", _SHORTCUTS),
    HelpArticle("About", "info", _ABOUT),
]


def article_titles() -> list[str]:
    return [a.title for a in HELP_ARTICLES]


def find_article(title: str | None) -> HelpArticle | None:
    if not title:
        return None
    wanted = title.strip().lower()
    for article in HELP_ARTICLES:
        if article.title.lower() == wanted:
            return article
    return None


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
KEY_H = 19               # the cap itself
_KEY_DROP = 3            # transparent strip under it: lifts the cap onto the text's x-height
_KEY_PAD = 6
_KEY_FONT_PX = 11


def split_keys(sequence: str) -> list[str]:
    """``"Ctrl+Shift+Z"`` -> ``["Ctrl", "Shift", "Z"]`` (a lone or trailing
    ``+`` is the plus key)."""
    if sequence == "+":
        return ["+"]
    parts = [p.strip() for p in sequence.replace("++", "+Plus").split("+")]
    return [("+" if p == "Plus" else p) for p in parts if p]


def _key_width(label: str) -> int:
    fm = QFontMetrics(theme.ui_font(_KEY_FONT_PX, 500))
    return max(KEY_H + 1, fm.horizontalAdvance(label) + 2 * _KEY_PAD)


def _key_img(label: str) -> str:
    return (f'<img src="key:{quote(label, safe="")}" width="{_key_width(label)}" '
            f'height="{KEY_H + _KEY_DROP}" style="vertical-align: middle;">')


def render_article(article: HelpArticle) -> str:
    """Article source -> HTML for the browser (needs a QGuiApplication)."""
    def chips(match: re.Match) -> str:
        return "&#8201;".join(_key_img(label) for label in split_keys(match.group(1)))

    html = _KEY_RE.sub(chips, article.source)
    return html.replace("{version}", __version__)


def _stylesheet() -> str:
    t = theme
    return f"""
    body {{ color: {t.FG_DIM}; }}
    h1 {{ color: {t.FG}; font-size: 24px; font-weight: 600; margin-top: 0px; margin-bottom: 10px; }}
    h2 {{ color: {t.FG}; font-size: 15px; font-weight: 600; margin-top: 26px; margin-bottom: 8px; }}
    p {{ margin-top: 0px; margin-bottom: 10px; line-height: 150%; }}
    p.lead {{ color: {t.FG}; font-size: 14px; margin-bottom: 14px; }}
    p.muted {{ color: {t.FG_MUTED}; }}
    li {{ margin-bottom: 6px; line-height: 150%; }}
    b {{ color: {t.FG}; font-weight: 600; }}
    code {{ font-family: "{t.FONT_MONO}", "Consolas", monospace; font-size: 12px;
            color: {t.FG}; background-color: {t.INPUT_BG}; }}
    a {{ color: {t.ACCENT_HOVER}; text-decoration: none; }}
    table.ref {{ margin-top: 2px; margin-bottom: 12px; }}
    table.ref td {{ padding: 6px 10px 6px 0px; border-bottom: 1px solid {t.BORDER}; }}
    td.what {{ color: {t.FG_DIM}; }}
    td.keys {{ color: {t.FG_DIM}; }}
    table.note {{ background-color: {t.ACCENT_SOFT}; margin-top: 4px; margin-bottom: 6px; }}
    table.note td {{ padding: 12px 14px; color: {t.FG}; }}
    """


class _HelpBrowser(QTextBrowser):
    """QTextBrowser that paints ``key:<label>`` images as key-cap chips."""

    def loadResource(self, kind, url):
        if isinstance(url, QUrl) and url.scheme() == "key":
            return self._key_image(url.path(QUrl.ComponentFormattingOption.FullyDecoded))
        return super().loadResource(kind, url)

    def _key_image(self, label: str) -> QImage:
        dpr = max(1.0, float(self.devicePixelRatioF()))
        w, h = _key_width(label), KEY_H
        img = QImage(int(round(w * dpr)), int(round((h + _KEY_DROP) * dpr)),
                     QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # the app's key chip (QLabel[role="key"]): raised cap with a heavier bottom edge
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor(theme.BORDER_CONTROL))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 4, 4)
        p.setBrush(theme.qcolor(theme.BUTTON_BG))
        p.drawRoundedRect(QRectF(1.5, 1.5, w - 3, h - 4), 3, 3)
        p.setPen(QPen(theme.qcolor(theme.FG)))
        p.setFont(theme.ui_font(_KEY_FONT_PX, 500))
        p.drawText(QRectF(0, 0, w, h - 1.5), Qt.AlignmentFlag.AlignCenter, label)
        p.end()
        return img


# ---------------------------------------------------------------------------
# dialog
# ---------------------------------------------------------------------------
class HelpDialog(QDialog):
    """Help center. ``HelpDialog(parent, open_article="Timeline Guide").exec()``."""

    def __init__(self, parent=None, open_article: str | None = None):
        super().__init__(parent)
        self.setObjectName("HelpDialog")
        self.setWindowTitle("Sesame Studio Help")
        self.resize(960, 680)
        self.setMinimumSize(720, 460)
        t = theme
        self.setStyleSheet(f"""
            #HelpSide {{ background-color: {t.BG_ELEVATED}; border: none; border-right: 1px solid {t.BORDER}; }}
            #HelpList {{ background: transparent; border: none; padding: 0px; }}
            #HelpList::item {{ padding: 7px 10px; border-radius: {t.RADIUS}px; color: {t.FG_DIM}; }}
            #HelpList::item:hover {{ background-color: {t.HOVER}; color: {t.FG}; }}
            #HelpList::item:selected, #HelpList::item:selected:!active {{
                background-color: {t.ACCENT_SOFT}; color: {t.FG}; }}
            #HelpBrowser {{ background-color: {t.BG}; border: none; border-radius: 0px; padding: 0px; }}
        """)
        self._current: HelpArticle | None = None

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- left: search + article list + tour button ----------------------
        side = QFrame(self)
        side.setObjectName("HelpSide")
        side.setFixedWidth(252)
        col = QVBoxLayout(side)
        col.setContentsMargins(12, 14, 12, 12)
        col.setSpacing(10)

        heading = QLabel("Help", side)
        heading.setProperty("role", "heading")
        heading.setContentsMargins(4, 0, 0, 2)
        col.addWidget(heading)

        self.search_edit = QLineEdit(side)
        self.search_edit.setPlaceholderText("Search help")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.addAction(icon("search", theme.FG_MUTED), QLineEdit.ActionPosition.LeadingPosition)
        self.search_edit.textChanged.connect(self._apply_filter)
        col.addWidget(self.search_edit)

        self.list_widget = QListWidget(side)
        self.list_widget.setObjectName("HelpList")
        self.list_widget.setIconSize(QSize(16, 16))
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_widget.setFont(theme.ui_font(13))
        for article in HELP_ARTICLES:
            item = QListWidgetItem(icon(article.icon), article.title)
            item.setData(Qt.ItemDataRole.UserRole, article.title)
            self.list_widget.addItem(item)
        self.list_widget.currentItemChanged.connect(self._on_item_changed)
        col.addWidget(self.list_widget, 1)

        self.empty_label = QLabel("No articles match your search.", side)
        self.empty_label.setProperty("role", "subheading")
        self.empty_label.setWordWrap(True)
        self.empty_label.setContentsMargins(4, 0, 4, 0)
        self.empty_label.hide()
        col.addWidget(self.empty_label)

        self.tour_btn = QPushButton("Start interactive tour", side)
        self.tour_btn.setProperty("variant", "primary")
        self.tour_btn.setIcon(icon("sparkle", theme.ACCENT_FG))
        self.tour_btn.setToolTip("A two-minute, hands-on walkthrough of the editor")
        self.tour_btn.setAutoDefault(False)
        self.tour_btn.clicked.connect(self._start_tour)
        self.tour_btn.setVisible(self._tour_host() is not None)
        col.addWidget(self.tour_btn)
        root.addWidget(side)

        # -- right: the article ------------------------------------------------
        self.browser = _HelpBrowser(self)
        self.browser.setObjectName("HelpBrowser")
        self.browser.setFrameShape(QFrame.Shape.NoFrame)
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.anchorClicked.connect(self._on_link)
        doc = self.browser.document()
        doc.setDefaultFont(theme.ui_font(13))
        doc.setDefaultStyleSheet(_stylesheet())
        doc.setDocumentMargin(32)
        root.addWidget(self.browser, 1)

        if not self.open_article(open_article):
            self.list_widget.setCurrentRow(0)
        self.list_widget.setFocus()

    # -- API ----------------------------------------------------------------
    def current_title(self) -> str | None:
        return self._current.title if self._current else None

    def open_article(self, title: str | None) -> bool:
        """Show the article called ``title`` (case-insensitive). False if
        there is no such article."""
        article = find_article(title)
        if article is None:
            return False
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == article.title:
                if item.isHidden():                     # hidden by the search: clear it
                    self.search_edit.clear()
                if self.list_widget.currentItem() is item:
                    self._show(article)
                else:
                    self.list_widget.setCurrentItem(item)
                return True
        return False

    def visible_titles(self) -> list[str]:
        return [self.list_widget.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.list_widget.count()) if not self.list_widget.item(i).isHidden()]

    # -- internals -------------------------------------------------------------
    def _tour_host(self):
        """The window that can run the tour (the main window), if any."""
        parent = self.parent()
        return parent if parent is not None and callable(getattr(parent, "start_tutorial", None)) else None

    def _on_item_changed(self, current, _previous) -> None:
        article = find_article(current.data(Qt.ItemDataRole.UserRole)) if current else None
        if article is not None:
            self._show(article)

    def _show(self, article: HelpArticle) -> None:
        self._current = article
        self.browser.setHtml(render_article(article))
        self.browser.verticalScrollBar().setValue(0)
        self._highlight_query()

    def _highlight_query(self) -> None:
        """Jump to the first place the search text occurs in the article."""
        query = self.search_edit.text().strip()
        cursor = self.browser.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.browser.setTextCursor(cursor)
        if len(query) >= 2 and not self.browser.find(query, QTextDocument.FindFlag(0)):
            self.browser.verticalScrollBar().setValue(0)

    def _apply_filter(self, text: str) -> None:
        query = re.sub(r"\s+", " ", text.strip().lower())
        first_visible = None
        for row, article in enumerate(HELP_ARTICLES):
            item = self.list_widget.item(row)
            hidden = bool(query) and query not in article.plain_text()
            item.setHidden(hidden)
            if not hidden and first_visible is None:
                first_visible = item
        self.empty_label.setVisible(first_visible is None)
        current = self.list_widget.currentItem()
        if first_visible is not None and (current is None or current.isHidden()):
            self.list_widget.setCurrentItem(first_visible)
        elif current is not None and not current.isHidden():
            self._highlight_query()

    def _on_link(self, url: QUrl) -> None:
        scheme = url.scheme()
        if scheme == "help":
            self.open_article(url.path(QUrl.ComponentFormattingOption.FullyDecoded))
        elif scheme == "tour":
            self._start_tour()

    def _start_tour(self) -> None:
        host = self._tour_host()
        if host is None:
            return
        self.accept()
        QTimer.singleShot(0, host.start_tutorial)   # after this modal dialog has gone
