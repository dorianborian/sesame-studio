"""Main application shell: menus, toolbar, dockable panels, welcome/editor
pages, project lifecycle, export dialogs -- and the wiring that keeps the 3D
viewport, inspector, timeline and face library in sync.

Data flow (one direction per edit, no feedback loops):

* The **selected timeline frame** is the source of truth for the pose being
  edited. Inspector sliders and dragging a limb in 3D both write into it via
  ``timeline.update_selected_frame_angles`` and mirror the new pose to the
  other view with *non-emitting* setters (``inspector.load_angles`` /
  ``viewport.set_angles``).
* Continuous edits (a slider drag, a limb drag) are bracketed with
  ``timeline.begin_edit`` / ``end_edit`` so each is a single undo step.
* During playback the timeline emits interpolated poses (``poseAtIndex``) and
  per-frame servo targets (``frameTargetChanged``). With physics on, the
  viewport follows the *targets* through its servo + ground-contact sim (what
  the real robot does); with physics off it shows the interpolated pose.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QObject, QSettings, QTimer, QElapsedTimer
from PySide6.QtGui import QIcon, QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow, QStackedWidget, QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QFileDialog, QMessageBox, QComboBox, QLabel, QDialog, QPlainTextEdit,
    QPushButton, QApplication, QToolBar, QToolButton, QMenu, QSizePolicy, QTabWidget,
)

from .project import Project
from .servo_model import BOARD_VARIANTS, SERVO_ORDER
from .viewport3d import Viewport3D
from .inspector import InspectorPanel
from .timeline import TimelineWidget
from .face_library import FaceLibraryPanel
from .face_bitmaps import FaceBitmapsFile
from .builtin_faces import reload_builtin_faces
from .builtin_sequences import STAND_STATE
from .welcome import WelcomePage
from .tutorial import start_tour, HelpDialog, should_show_first_run_tutorial
from .code_export import export_sequence, export_face_bundle, ValidationError
from .widgets.toast import show_toast
from .icons import icon
from . import theme

from .paths import ASSETS_DIR, LOGO_PATH, ICON_PATH, FIRMWARE_DIR, BASE_DIR

RECENT_KEY = "project/recent"
MAX_RECENT = 8
# Bumped whenever the dock/toolbar set changes so a stale saved layout from
# an older version can't scramble the new one.
LAYOUT_VERSION = 3
DEFAULT_PROJECT_NAME = "Untitled"


class CodeOutputDialog(QDialog):
    def __init__(self, title: str, code: str, suggested_filename: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        hint = QLabel("Paste this into your firmware checkout — see Help ▸ Export Guide for where each piece goes.")
        hint.setProperty("role", "subheading")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setPlainText(code)
        self.text_edit.setFont(theme.mono_font(10))
        self.text_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self.text_edit, 1)

        btn_row = QHBoxLayout()
        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.setProperty("variant", "primary")
        copy_btn.setIcon(icon("copy", theme.ACCENT_FG))
        copy_btn.clicked.connect(self._copy)
        save_btn = QPushButton("Save to File…")
        save_btn.setIcon(icon("save"))
        save_btn.clicked.connect(lambda: self._save(suggested_filename))
        close_btn = QPushButton("Close")
        close_btn.setProperty("variant", "flat")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(save_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.text_edit.toPlainText())
        show_toast(self, "Copied to clipboard", "success")

    def _save(self, suggested: str) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Export", suggested, "Header (*.h);;All Files (*)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.text_edit.toPlainText())
            show_toast(self, f"Saved {os.path.basename(path)}", "success")


class FaceAnimator(QObject):
    """Plays a face on the robot's OLED the way the firmware does: animated
    faces cycle their frames at the face's fps in loop / once / boomerang
    mode. Single-frame faces are just shown."""

    FALLBACK_FACES = ("idle", "default")

    def __init__(self, viewport: Viewport3D, resolve_face, parent=None):
        super().__init__(parent)
        self._viewport = viewport
        self._resolve = resolve_face
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._name: str | None = None
        self._mode = "loop"
        self._frames: list[bytes] = []
        self._index = 0
        self._direction = 1
        self._live = False

    def show(self, name: str | None, mode: str = "loop", restart: bool = False) -> None:
        if self._live:
            return  # a face editor is streaming live pixels; don't fight it
        if not name:
            # Nothing assigned: show the firmware's resting face rather than
            # a dead screen, as the real robot does when it is idle.
            name = next((n for n in self.FALLBACK_FACES if self._resolve(n)), None)
            mode = "loop"
        if not name:
            self.clear()
            return
        if name == self._name and mode == self._mode and not restart:
            return
        face = self._resolve(name)
        if not face or not face.frames:
            self.clear()
            return
        self._name, self._mode = name, mode
        self._frames = [bytes(f) for f in face.frames]
        self._index, self._direction = 0, 1
        self._viewport.set_face_preview(self._frames[0])
        if len(self._frames) > 1:
            self._timer.start(max(16, int(1000 / max(1, face.fps))))
        else:
            self._timer.stop()

    def refresh(self) -> None:
        """Re-read the current face (it may have been edited or deleted)."""
        name, mode = self._name, self._mode
        self._name = None
        self.show(name, mode)

    def clear(self) -> None:
        self._timer.stop()
        self._name = None
        self._frames = []
        if not self._live:
            self._viewport.set_face_preview(None)

    def show_live(self, pixels) -> None:
        """Mirror the face editor's canvas; ``None`` ends live mode and
        restores whatever face was showing."""
        if pixels is None:
            self._live = False
            self.refresh()
            if self._name is None:
                self._viewport.set_face_preview(None)
            return
        self._live = True
        self._timer.stop()
        self._viewport.set_face_preview(pixels)

    def _tick(self) -> None:
        n = len(self._frames)
        if n <= 1:
            self._timer.stop()
            return
        if self._mode == "once":
            if self._index >= n - 1:
                self._timer.stop()
                return
            self._index += 1
        elif self._mode == "boomerang":
            nxt = self._index + self._direction
            if nxt < 0 or nxt >= n:
                self._direction = -self._direction
                nxt = self._index + self._direction
            self._index = nxt
        else:
            self._index = (self._index + 1) % n
        self._viewport.set_face_preview(self._frames[self._index])


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sesame Studio")
        self.resize(1480, 920)
        self.setMinimumSize(1000, 640)
        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))

        self.settings = QSettings("SesameRobot", "SesameStudio")
        self.project: Project | None = None
        self.header_file: FaceBitmapsFile | None = None
        self._inspector_sync = QElapsedTimer()
        self._inspector_sync.start()
        self._tour = None

        self._build_central()
        self._build_docks()
        self.face_animator = FaceAnimator(self.viewport, self.face_library.get_face, self)
        self._build_actions()
        self._build_menu()
        self._build_toolbar()
        self._build_statusbar()
        self._wire_signals()
        self._default_state = self.saveState(LAYOUT_VERSION)
        self._restore_geometry()

        self.stack.setCurrentWidget(self.welcome_page)
        self._set_editor_chrome_visible(False)
        self._refresh_recent()
        self._update_title()

    # -- shell layout ---------------------------------------------------
    def _build_central(self) -> None:
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.welcome_page = WelcomePage(LOGO_PATH)
        self.welcome_page.newProjectRequested.connect(self.new_project)
        self.welcome_page.openProjectRequested.connect(self.open_project)
        self.welcome_page.openFaceBitmapsRequested.connect(self.open_face_bitmaps_dialog)
        self.welcome_page.recentProjectRequested.connect(self.open_project_path)
        self.welcome_page.tutorialRequested.connect(lambda: self.new_project(start_tutorial=True))
        self.stack.addWidget(self.welcome_page)

        self.viewport = Viewport3D()
        self.stack.addWidget(self.viewport)

    def _make_dock(self, title: str, name: str, widget: QWidget, area) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(name)
        dock.setWidget(widget)
        dock.setFeatures(QDockWidget.DockWidgetClosable)
        # No title bars: the tab strip already names the right-hand panels and
        # the timeline is self-evident. Panels are shown/hidden from the View
        # menu; this keeps ~60px of chrome out of the workspace.
        dock.setTitleBarWidget(QWidget(dock))
        self.addDockWidget(area, dock)
        return dock

    def _build_docks(self) -> None:
        # The right-hand column runs the full window height; the timeline
        # sits under the viewport only, so it never squeezes the inspector.
        self.setCorner(Qt.BottomRightCorner, Qt.RightDockWidgetArea)
        self.setCorner(Qt.TopRightCorner, Qt.RightDockWidgetArea)
        self.setDockOptions(QMainWindow.AnimatedDocks | QMainWindow.AllowTabbedDocks)
        self.setTabPosition(Qt.AllDockWidgetAreas, QTabWidget.North)

        self.inspector = InspectorPanel()
        self.inspector_dock = self._make_dock("Pose", "dock_inspector", self.inspector, Qt.RightDockWidgetArea)

        self.face_library = FaceLibraryPanel()
        self.face_dock = self._make_dock("Faces", "dock_faces", self.face_library, Qt.RightDockWidgetArea)
        self.tabifyDockWidget(self.inspector_dock, self.face_dock)

        self.timeline = TimelineWidget()
        self.timeline_dock = self._make_dock("Timeline", "dock_timeline", self.timeline, Qt.BottomDockWidgetArea)

        self.inspector_dock.raise_()
        self.resizeDocks([self.inspector_dock], [360], Qt.Horizontal)
        self.resizeDocks([self.timeline_dock], [300], Qt.Vertical)

    def _editor_docks(self):
        return (self.inspector_dock, self.face_dock, self.timeline_dock)

    def _set_editor_chrome_visible(self, visible: bool) -> None:
        for dock in self._editor_docks():
            dock.setVisible(visible)
        self.main_toolbar.setVisible(visible)
        for act in self._editor_only_actions:
            act.setEnabled(visible)
        if visible:
            self.inspector_dock.raise_()

    # -- actions ----------------------------------------------------------
    def _action(self, text: str, shortcut=None, slot=None, icon_name: str | None = None,
                tip: str | None = None, checkable: bool = False) -> QAction:
        act = QAction(text, self)
        if icon_name:
            act.setIcon(icon(icon_name))
        if shortcut:
            if isinstance(shortcut, (list, tuple)):
                act.setShortcuts([QKeySequence(s) for s in shortcut])
            else:
                act.setShortcut(QKeySequence(shortcut))
        if tip:
            act.setStatusTip(tip)
            act.setToolTip(tip)
        act.setCheckable(checkable)
        if slot is not None:
            act.triggered.connect(slot)
        return act

    def _timeline_action(self, text: str, shortcut, slot, icon_name=None) -> QAction:
        """An Edit-menu action whose shortcut only fires while the timeline
        has focus, so Ctrl+C / Delete keep their normal meaning in text
        fields and other panels."""
        act = self._action(text, shortcut, slot, icon_name)
        act.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.timeline.addAction(act)
        return act

    def _build_actions(self) -> None:
        a = self._action
        self.act_new = a("New Project", "Ctrl+N", lambda: self.new_project(), "file-plus", "Start a new project")
        self.act_open = a("Open…", "Ctrl+O", self.open_project, "folder-open",
                          "Open a .sesproj project or a face-bitmaps.h header")
        self.act_open_header = a("Open Face Bitmaps (.h)…", "Ctrl+Shift+O", self.open_face_bitmaps_dialog,
                                 "file-code", "Edit the faces in a firmware face-bitmaps.h directly")
        self.act_save = a("Save", "Ctrl+S", self.save_project, "save", "Save the project")
        self.act_save_as = a("Save As…", "Ctrl+Shift+S", self.save_project_as)
        self.act_export_seq = a("Export Current Sequence…", "Ctrl+E", self.export_current_sequence, "code",
                                "Generate the firmware function for this sequence")
        self.act_export_face = a("Export Selected Face…", None, self.export_selected_face, "face")
        self.act_export_faces = a("Export All Faces to face-bitmaps.h…", None, self.save_face_bitmaps_as, "file-code")
        self.act_exit = a("Exit", "Ctrl+Q", self.close)

        stack = self.timeline.undo_stack
        self.act_undo = stack.createUndoAction(self, "Undo")
        self.act_undo.setShortcut(QKeySequence("Ctrl+Z"))
        self.act_undo.setIcon(icon("undo"))
        self.act_redo = stack.createRedoAction(self, "Redo")
        self.act_redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.act_redo.setIcon(icon("redo"))

        t = self._timeline_action
        self.act_cut = t("Cut Frames", "Ctrl+X", self.timeline.cut, "cut")
        self.act_copy = t("Copy Frames", "Ctrl+C", self.timeline.copy, "copy")
        self.act_paste = t("Paste Frames", "Ctrl+V", self.timeline.paste, "paste")
        self.act_duplicate = t("Duplicate Frames", "Ctrl+D", self.timeline.duplicate_selected, "duplicate")
        self.act_delete = t("Delete Frames", "Del", self.timeline.delete_selected, "trash")
        self.act_select_all = t("Select All Frames", "Ctrl+A", self.timeline.select_all)

        self.act_add_frame = a("Add Frame", "Ctrl+Return", self.timeline.add_frame, "plus",
                               "Insert a copy of the selected frame after it, ready to pose")
        self.act_play = a("Play / Pause", "Space", self.timeline.toggle_playback, "play",
                          "Play or pause the current sequence")
        self.act_stop = a("Stop", None, self.timeline.stop, "stop", "Stop and return to the start")
        self.act_new_seq = a("New Sequence…", None, lambda: self.timeline.new_sequence(), "plus")
        self.act_dup_seq = a("Duplicate Sequence", None, lambda: self.timeline.duplicate_sequence(), "duplicate")
        self.act_rename_seq = a("Rename Sequence…", None, lambda: self.timeline.rename_sequence(), "rename")
        self.act_delete_seq = a("Delete Sequence", None, lambda: self.timeline.delete_sequence(), "trash")

        self.act_physics = a("Simulate Physics", "Ctrl+P", None, "physics",
                             "Show the robot standing, walking and tipping on the ground", checkable=True)
        self.act_physics.setChecked(self.viewport.physics_enabled)
        self.act_physics.toggled.connect(self.viewport.set_physics_enabled)
        self.act_reset_robot = a("Reset Robot Position", "Ctrl+R", self.viewport.reset_physics, "home",
                                 "Put the robot back at the centre of the floor")
        self.act_frame_robot = a("Frame Robot", None, self.viewport.frame_robot, "fit")
        self.act_reset_view = a("Reset Camera", None, self.viewport.reset_view, "camera")
        self.act_reset_layout = a("Reset Panel Layout", None, self.reset_layout)

        self.act_tour = a("Interactive Tour", "F1", self.start_tutorial, "sparkle",
                          "A two-minute hands-on walkthrough")
        self.act_shortcuts = a("Keyboard Shortcuts", "Ctrl+/", lambda: self.show_help("Keyboard Shortcuts"), "keyboard")

        # Things that make no sense on the welcome page.
        self._editor_only_actions = [
            self.act_save, self.act_save_as, self.act_export_seq, self.act_export_face,
            self.act_export_faces, self.act_undo, self.act_redo, self.act_cut, self.act_copy,
            self.act_paste, self.act_duplicate, self.act_delete, self.act_select_all,
            self.act_add_frame, self.act_play, self.act_stop, self.act_new_seq, self.act_dup_seq,
            self.act_rename_seq, self.act_delete_seq, self.act_physics, self.act_reset_robot,
            self.act_frame_robot, self.act_reset_view,
        ]

    def _build_menu(self) -> None:
        mb = self.menuBar()

        file_menu = mb.addMenu("&File")
        file_menu.addAction(self.act_new)
        file_menu.addAction(self.act_open)
        file_menu.addAction(self.act_open_header)
        self.recent_menu = file_menu.addMenu("Open Recent")
        file_menu.addSeparator()
        file_menu.addAction(self.act_save)
        file_menu.addAction(self.act_save_as)
        file_menu.addSeparator()
        export_menu = file_menu.addMenu("Export")
        export_menu.setIcon(icon("export"))
        export_menu.addAction(self.act_export_seq)
        export_menu.addAction(self.act_export_face)
        export_menu.addAction(self.act_export_faces)
        self.export_menu = export_menu
        file_menu.addSeparator()
        file_menu.addAction(self.act_exit)

        edit_menu = mb.addMenu("&Edit")
        edit_menu.addAction(self.act_undo)
        edit_menu.addAction(self.act_redo)
        edit_menu.addSeparator()
        for act in (self.act_cut, self.act_copy, self.act_paste, self.act_duplicate, self.act_delete):
            edit_menu.addAction(act)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_select_all)

        seq_menu = mb.addMenu("&Sequence")
        seq_menu.addAction(self.act_add_frame)
        seq_menu.addSeparator()
        seq_menu.addAction(self.act_play)
        seq_menu.addAction(self.act_stop)
        seq_menu.addSeparator()
        for act in (self.act_new_seq, self.act_dup_seq, self.act_rename_seq, self.act_delete_seq):
            seq_menu.addAction(act)
        seq_menu.addSeparator()
        board_menu = seq_menu.addMenu("Target Board")
        board_menu.setIcon(icon("chip"))
        self.board_group = QActionGroup(self)
        self.board_group.setExclusive(True)
        self._board_actions: dict[str, QAction] = {}
        for key, variant in BOARD_VARIANTS.items():
            act = QAction(variant.label, self, checkable=True)
            act.setData(key)
            self.board_group.addAction(act)
            board_menu.addAction(act)
            self._board_actions[key] = act
        self.board_group.triggered.connect(self._on_board_variant_changed)

        faces_menu = mb.addMenu("F&aces")
        faces_menu.addAction(self._action("New Face…", None, self.face_library._new_face, "plus"))
        faces_menu.addAction(self._action("Show Face Library", None, self._show_faces, "face"))
        faces_menu.addSeparator()
        faces_menu.addAction(self.act_open_header)
        faces_menu.addAction(self._action("Save face-bitmaps.h", None, self.save_face_bitmaps, "save"))
        faces_menu.addAction(self._action("Save face-bitmaps.h As…", None, self.save_face_bitmaps_as))

        view_menu = mb.addMenu("&View")
        for dock in self._editor_docks():
            view_menu.addAction(dock.toggleViewAction())
        view_menu.addAction(self.act_reset_layout)
        view_menu.addSeparator()
        view_menu.addAction(self.act_physics)
        view_menu.addAction(self.act_reset_robot)
        view_menu.addSeparator()
        camera_menu = view_menu.addMenu("Camera")
        camera_menu.setIcon(icon("camera"))
        for label, name in (("Three-quarter", "iso"), ("Front", "front"), ("Back", "back"),
                            ("Left", "left"), ("Right", "right"), ("Top", "top")):
            camera_menu.addAction(self._action(label, None, lambda _=False, n=name: self.viewport.set_view(n)))
        camera_menu.addSeparator()
        camera_menu.addAction(self.act_frame_robot)
        camera_menu.addAction(self.act_reset_view)

        help_menu = mb.addMenu("&Help")
        help_menu.addAction(self.act_tour)
        help_menu.addAction(self.act_shortcuts)
        help_menu.addSeparator()
        for title in ("Getting Started", "Posing in 3D", "Timeline Guide", "Face Painter Guide", "Export Guide"):
            help_menu.addAction(self._action(title, None, lambda _=False, t=title: self.show_help(t)))
        help_menu.addSeparator()
        help_menu.addAction(self._action("About Sesame Studio", None, lambda: self.show_help("About"), "info"))

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setObjectName("toolbar_main")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        tb.toggleViewAction().setVisible(False)
        self.addToolBar(Qt.TopToolBarArea, tb)
        self.main_toolbar = tb

        tb.addAction(self.act_new)
        tb.addAction(self.act_open)
        tb.addAction(self.act_save)
        tb.addSeparator()
        tb.addAction(self.act_undo)
        tb.addAction(self.act_redo)
        tb.addSeparator()

        export_btn = QToolButton()
        export_btn.setText("Export")
        export_btn.setIcon(icon("export"))
        export_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        export_btn.setPopupMode(QToolButton.InstantPopup)
        export_btn.setMenu(self.export_menu)
        export_btn.setToolTip("Generate firmware code for the current sequence or faces")
        tb.addWidget(export_btn)
        self.export_btn = export_btn

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.doc_label = QLabel("")
        self.doc_label.setProperty("role", "subheading")
        tb.addWidget(self.doc_label)

        spacer2 = QWidget()
        spacer2.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer2)

        tour_btn = QToolButton()
        tour_btn.setDefaultAction(self.act_tour)
        tour_btn.setText("Tour")
        tour_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.addWidget(tour_btn)
        self.tour_btn = tour_btn

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.status_info = QLabel("")
        self.status_info.setProperty("role", "subheading")
        sb.addPermanentWidget(self.status_info)

    # -- signal wiring ---------------------------------------------------
    def _wire_signals(self) -> None:
        insp, tl, vp, faces = self.inspector, self.timeline, self.viewport, self.face_library

        # pose editing: inspector <-> viewport <-> selected frame
        insp.anglesChanged.connect(self._on_inspector_angles_changed)
        insp.frameFieldsChanged.connect(self._on_inspector_frame_fields_changed)
        insp.editStarted.connect(lambda: tl.begin_edit("Edit Pose"))
        insp.editFinished.connect(tl.end_edit)
        insp.jointHovered.connect(vp.set_hovered_joint)
        insp.jointSelected.connect(vp.set_selected_joint)

        vp.anglesEdited.connect(self._on_viewport_angles_edited)
        vp.dragStarted.connect(self._on_viewport_drag_started)
        vp.dragFinished.connect(lambda _joint: tl.end_edit())
        vp.jointSelected.connect(self._on_viewport_joint_selected)
        vp.jointHovered.connect(insp.set_hovered_joint)
        vp.physicsToggled.connect(self._on_physics_toggled)

        # timeline
        tl.frameSelected.connect(self._on_frame_selected)
        tl.poseAtIndex.connect(self._on_playback_pose)
        tl.frameTargetChanged.connect(self._on_frame_target)
        tl.playbackStarted.connect(self._on_playback_started)
        tl.playbackStateChanged.connect(self._on_playback_state)
        tl.currentSequenceChanged.connect(self._on_sequence_changed)
        tl.sequencesChanged.connect(self._on_sequences_changed)
        tl.editBlocked.connect(lambda msg: show_toast(self, msg, "warning"))

        # faces
        faces.editBlocked.connect(lambda msg: show_toast(self, msg, "warning"))
        faces.faceAssigned.connect(self._on_face_assigned)
        faces.facesChanged.connect(self._on_faces_changed)
        faces.facePreviewRequested.connect(self._on_face_preview_requested)
        faces.facePixelsPreview.connect(self.face_animator.show_live)
        faces.headerFileSaveRequested.connect(self.save_face_bitmaps)
        faces.headerFileSaveAsRequested.connect(self.save_face_bitmaps_as)
        faces.openHeaderRequested.connect(self.open_face_bitmaps_dialog)
        faces.closeHeaderRequested.connect(self.close_header_file)

    # -- pose editing -------------------------------------------------------
    def _on_inspector_angles_changed(self, angles: dict) -> None:
        self.viewport.set_angles(angles)
        self.timeline.update_selected_frame_angles(angles)

    def _on_inspector_frame_fields_changed(self, delay_ms: int, face, face_mode: str) -> None:
        self.timeline.update_selected_frame_fields(delay_ms, face, face_mode)
        self.face_animator.show(face, face_mode)

    def _on_viewport_angles_edited(self, angles: dict) -> None:
        self.inspector.load_angles(angles)
        self.timeline.update_selected_frame_angles(self.inspector.current_angles())

    def _on_viewport_drag_started(self, joint: str) -> None:
        if self.timeline.is_playing():
            self.timeline.pause()
        self.timeline.begin_edit(f"Pose {joint}")

    def _on_viewport_joint_selected(self, joint) -> None:
        self.inspector.set_selected_joint(joint)
        if joint:
            self.inspector_dock.raise_()

    # -- timeline -> views ---------------------------------------------------
    def _current_frame(self):
        seq = self.timeline.current_sequence()
        idx = self.timeline.selected_index
        if not seq or idx is None or not (0 <= idx < len(seq.frames)):
            return None
        return seq.frames[idx]

    def _on_frame_selected(self, index: int) -> None:
        seq = self.timeline.current_sequence()
        if not seq or not (0 <= index < len(seq.frames)):
            return
        frame = seq.frames[index]
        self.inspector.load_angles(frame.angles)
        self.inspector.load_frame_fields(frame.delay_ms, frame.face, frame.face_mode)
        self.inspector.set_frame_caption(f"Frame {index + 1} of {len(seq.frames)}")
        if not self.timeline.is_playing():
            self.viewport.set_angles(frame.angles)
            self.face_animator.show(frame.face, frame.face_mode)
        self._update_status()

    def _on_playback_pose(self, angles: dict, face, face_mode: str) -> None:
        playing = self.timeline.is_playing()
        if playing and self.viewport.physics_enabled:
            # The sim is following the frame targets through its servo model;
            # just keep the sliders roughly in step with what's on screen.
            if self._inspector_sync.elapsed() > 60:
                self._inspector_sync.restart()
                self.inspector.load_angles(self.viewport.angles)
        else:
            self.viewport.set_angles(angles)
            if self._inspector_sync.elapsed() > 30 or not playing:
                self._inspector_sync.restart()
                self.inspector.load_angles(angles)
        if face:
            self.face_animator.show(face, face_mode)

    def _on_frame_target(self, angles: dict) -> None:
        if self.timeline.is_playing() and self.viewport.physics_enabled:
            self.viewport.set_servo_targets(angles)

    def _on_playback_started(self, start_ms: float) -> None:
        if start_ms <= 0.5 and self.viewport.physics_enabled:
            # Every run from the top starts from the same spot, in the first
            # frame's pose, so the motion is repeatable.
            seq = self.timeline.current_sequence()
            if seq and seq.frames:
                self.viewport.set_angles(seq.frames[0].angles)
            self.viewport.reset_physics()

    def _on_playback_state(self, playing: bool) -> None:
        self.viewport.set_playing(playing)
        self.act_play.setIcon(icon("pause" if playing else "play"))
        if not playing:
            if self.timeline.current_time_ms() <= 0.5:
                # Stop (as opposed to Pause) rewinds: show the selected frame
                # again and walk the robot back to the middle of the floor.
                frame = self._current_frame()
                if frame is not None:
                    self.viewport.set_angles(frame.angles)
                self.viewport.reset_physics()
            self.inspector.load_angles(self.viewport.angles)

    def _on_sequence_changed(self, _name: str = "") -> None:
        self.inspector.set_read_only(self.timeline.current_is_builtin())
        editable = not self.timeline.current_is_builtin()
        for act in (self.act_rename_seq, self.act_delete_seq, self.act_add_frame):
            act.setEnabled(editable)
        self._update_used_faces()
        self._update_status()
        # A different sequence starts from the centre of the floor, once its
        # first frame's pose has been applied.
        QTimer.singleShot(0, self.viewport.reset_physics)

    def _on_sequences_changed(self) -> None:
        # Sequences live in the project; in face-bitmaps.h mode they are a
        # scratch pad and must not flag the header as modified.
        if self.project and not self.header_file:
            self.project.dirty = True
            self._update_title()
        self._update_used_faces()
        self._update_status()

    def _update_used_faces(self) -> None:
        seq = self.timeline.current_sequence()
        self.face_library.set_used_faces({f.face for f in seq.frames if f.face} if seq else set())

    def _update_status(self) -> None:
        seq = self.timeline.current_sequence()
        if not seq:
            self.status_info.setText("")
            return
        total = self.timeline.total_time_ms() / 1000.0
        kind = "built-in" if self.timeline.current_is_builtin() else "custom"
        self.status_info.setText(f"{seq.name}  ·  {len(seq.frames)} frames  ·  {total:.2f} s  ·  {kind}")

    # -- physics ------------------------------------------------------------
    def _on_physics_toggled(self, on: bool) -> None:
        self.act_physics.blockSignals(True)
        self.act_physics.setChecked(on)
        self.act_physics.blockSignals(False)
        self.act_reset_robot.setEnabled(on)

    # -- faces --------------------------------------------------------------
    def _on_face_preview_requested(self, name: str) -> None:
        self.face_animator.show(name, "loop", restart=True)

    def _resolve_face_pixels(self, face_name):
        """First frame of a face in a form ``viewport.set_face_preview`` takes
        (kept for scripts such as create_trailer.py)."""
        if not face_name:
            return None
        face = self.face_library.get_face(face_name)
        if not face or not face.frames:
            return None
        return bytes(face.frames[0])

    def _on_face_assigned(self, name: str) -> None:
        frame = self._current_frame()
        if frame is None:
            show_toast(self, "Select a timeline frame first", "warning")
            return
        if self.timeline.current_is_builtin():
            show_toast(self, "Built-in sequences are read-only — duplicate it to assign faces", "warning")
            return
        self.timeline.update_selected_frame_fields(frame.delay_ms, name, frame.face_mode)
        self.inspector.load_frame_fields(frame.delay_ms, name, frame.face_mode)
        self.face_animator.show(name, frame.face_mode, restart=True)
        show_toast(self, f"Assigned “{name}” to frame {self.timeline.selected_index + 1}", "success")

    def _on_faces_changed(self) -> None:
        self.inspector.set_face_options(self.face_library.all_face_names())
        self.face_animator.refresh()
        if self.header_file:
            self.header_file.dirty = True
            self.face_library.update_header_dirty(True)
        elif self.project:
            self.project.dirty = True
        self._update_title()

    def _show_faces(self) -> None:
        self.face_dock.setVisible(True)
        self.face_dock.raise_()

    def _on_board_variant_changed(self, action: QAction) -> None:
        if self.project and self.project.settings.board_variant != action.data():
            self.project.settings.board_variant = action.data()
            if not self.header_file:
                self.project.dirty = True
                self._update_title()

    def _update_title(self) -> None:
        if self.header_file:
            name, dirty, kind = self.header_file.filename, self.header_file.dirty, "face bitmaps"
            self.face_library.update_header_dirty(dirty)
        elif self.project:
            name, dirty, kind = self.project.name, self.project.dirty, ""
        else:
            self.setWindowTitle("Sesame Studio")
            self.doc_label.setText("")
            return
        mark = "*" if dirty else ""
        tag = " [Face Bitmaps]" if self.header_file else ""
        self.setWindowTitle(f"{name}{mark}{tag} — Sesame Studio")
        self.doc_label.setText(f"{name}{mark}" + (f"  ·  {kind}" if kind else ""))

    # -- project lifecycle ------------------------------------------------
    def new_project(self, start_tutorial: bool = False) -> None:
        if not self._confirm_discard_unsaved():
            return
        self.header_file = None
        self.project = Project(DEFAULT_PROJECT_NAME)
        # Start standing: a posed robot on its feet is a far better blank
        # canvas than the all-90 "rest" sprawl.
        for seq in self.project.sequences.values():
            for frame in seq.frames:
                frame.angles = dict(STAND_STATE)
        self._load_project_into_ui()
        self._goto_editor()
        if start_tutorial or should_show_first_run_tutorial():
            QTimer.singleShot(250, self.start_tutorial)

    def open_project(self) -> None:
        if not self._confirm_discard_unsaved():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open", self._last_dir(),
            "Sesame files (*.sesproj *.h *.hpp);;Sesame Project (*.sesproj);;Face Bitmaps (*.h *.hpp);;All Files (*)"
        )
        if path:
            self.open_project_path(path, confirmed=True)

    def open_project_path(self, path: str, confirmed: bool = False) -> None:
        if not os.path.exists(path):
            show_toast(self, "That file no longer exists", "error")
            self._remove_recent(path)
            return
        if not confirmed and not self._confirm_discard_unsaved():
            return
        if path.lower().endswith((".h", ".hpp")):
            self.open_face_bitmaps_path(path)
            return
        try:
            project = Project.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Couldn't open project", f"{os.path.basename(path)} could not be read.\n\n{exc}")
            return
        self.project = project
        self.header_file = None
        self._load_project_into_ui()
        self._goto_editor()
        self._add_recent(path)
        show_toast(self, f"Opened {os.path.basename(path)}", "success")

    def save_project(self) -> bool:
        if self.header_file:
            return self.save_face_bitmaps()
        if not self.project:
            return False
        if not self.project.path:
            return self.save_project_as()
        return self._write_project(self.project.path)

    def save_project_as(self) -> bool:
        if self.header_file:
            return self.save_face_bitmaps_as()
        if not self.project:
            return False
        default = self.project.path or os.path.join(self._last_dir(), f"{self.project.name}.sesproj")
        path, _ = QFileDialog.getSaveFileName(self, "Save Project As", default, "Sesame Project (*.sesproj)")
        if not path:
            return False
        if not path.lower().endswith(".sesproj"):
            path += ".sesproj"
        self.project.name = os.path.splitext(os.path.basename(path))[0]
        return self._write_project(path)

    def _write_project(self, path: str) -> bool:
        self._sync_project_from_ui()
        try:
            self.project.save(path)
        except OSError as exc:
            QMessageBox.critical(self, "Couldn't save project", str(exc))
            return False
        self._add_recent(path)
        self._update_title()
        show_toast(self, f"Saved {os.path.basename(path)}", "success")
        return True

    def _confirm_discard_unsaved(self) -> bool:
        """True if it's OK to replace/close what's open (saved, discarded,
        or nothing to lose); False if the user cancelled or a save failed."""
        # An open face editor may hold unsaved paint of its own.
        if not self.face_library.close_editor():
            return False
        if self.header_file and self.header_file.dirty:
            name, save = self.header_file.filename, self.save_face_bitmaps
        elif self.project and self.project.dirty and not self.header_file:
            name, save = self.project.name, self.save_project
        else:
            return True
        if os.environ.get("SESAME_STUDIO_TESTING"):
            return True  # automated runs must never block on a modal prompt
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"Save changes to “{name}”?")
        box.setInformativeText("Your changes will be lost if you don't save them.")
        box.setStandardButtons(QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Save)
        res = box.exec()
        if res == QMessageBox.Save:
            return bool(save())
        return res == QMessageBox.Discard

    # -- face-bitmaps.h lifecycle -----------------------------------------
    def open_face_bitmaps_dialog(self) -> None:
        if not self._confirm_discard_unsaved():
            return
        candidates = [
            str(FIRMWARE_DIR / "face-bitmaps.h"),
            str(ASSETS_DIR / "face-bitmaps.h"),
            str(BASE_DIR.parent / "sesame-robot" / "firmware" / "face-bitmaps.h"),
            str(BASE_DIR.parent.parent / "firmware" / "face-bitmaps.h"),
        ]
        default_path = next((c for c in candidates if os.path.exists(c)), "")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Face Bitmaps Header", default_path,
            "Face Bitmaps (*.h);;Header Files (*.h *.hpp);;All Files (*)"
        )
        if path:
            self.open_face_bitmaps_path(path)

    def open_face_bitmaps_path(self, path: str) -> None:
        if not os.path.exists(path):
            show_toast(self, "Header file not found", "error")
            return
        try:
            header_file = FaceBitmapsFile.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Couldn't open face bitmaps", str(exc))
            return
        self.header_file = header_file
        self.project = Project(f"Face Bitmaps ({header_file.filename})")
        self.timeline.load_project_sequences(self.project.sequences)
        self.face_library.load_header_file(header_file)
        self.inspector.set_face_options(self.face_library.all_face_names())
        self.project.dirty = False
        self._goto_editor()
        self._show_faces()
        self._add_recent(path)
        self._update_title()
        show_toast(self, f"Opened {header_file.filename} · {len(header_file.faces)} faces", "success")

    def save_face_bitmaps(self) -> bool:
        if not self.header_file:
            return self.save_face_bitmaps_as()
        try:
            self.header_file.save()
        except Exception as exc:
            QMessageBox.critical(self, "Couldn't save face bitmaps", str(exc))
            return False
        self.face_library.update_header_dirty(False)
        self._update_title()
        reload_builtin_faces()
        show_toast(self, f"Saved {self.header_file.filename}", "success")
        return True

    def save_face_bitmaps_as(self) -> bool:
        default = self.header_file.path if self.header_file else "face-bitmaps.h"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Face Bitmaps As", default,
            "Face Bitmaps (*.h);;Header Files (*.h *.hpp);;All Files (*)"
        )
        if not path:
            return False
        try:
            if self.header_file:
                self.header_file.save(path)
            else:
                # Export the project's faces as a standalone header without
                # leaving project mode.
                fb = FaceBitmapsFile(
                    path=path,
                    faces=dict(self.face_library.faces),
                    order=list(self.face_library.faces.keys()),
                    undefined_names=set(),
                )
                fb.save(path)
        except Exception as exc:
            QMessageBox.critical(self, "Couldn't save face bitmaps", str(exc))
            return False
        if self.header_file:
            self.face_library.update_header_dirty(False)
            self._add_recent(path)
            reload_builtin_faces()
        self._update_title()
        show_toast(self, f"Saved {os.path.basename(path)}", "success")
        return True

    def close_header_file(self) -> None:
        if not self._confirm_discard_unsaved():
            return
        self._goto_welcome()

    def _goto_welcome(self) -> None:
        self.timeline.stop()
        self.header_file = None
        self.project = None
        self.face_library.load_faces({})
        self.stack.setCurrentWidget(self.welcome_page)
        self._set_editor_chrome_visible(False)
        self._update_title()

    def _load_project_into_ui(self) -> None:
        self.timeline.stop()
        self.face_library.load_faces(self.project.faces)
        self.inspector.set_face_options(self.face_library.all_face_names())
        self.timeline.load_project_sequences(self.project.sequences)
        act = self._board_actions.get(self.project.settings.board_variant)
        if act is None:
            act = next(iter(self._board_actions.values()))
        act.setChecked(True)
        self.project.dirty = False
        self._on_sequence_changed()
        self._update_title()

    def _sync_project_from_ui(self) -> None:
        # sequences/faces dicts are mutated in place by Timeline/FaceLibrary,
        # so the project already reflects the latest edits.
        self.project.sequences = self.timeline.sequences
        self.project.faces = self.face_library.faces

    def _goto_editor(self) -> None:
        self.stack.setCurrentWidget(self.viewport)
        self._set_editor_chrome_visible(True)
        self.viewport.reset_physics()
        self.viewport.setFocus()

    # -- recent files -------------------------------------------------------
    def _recent_list(self) -> list[str]:
        value = self.settings.value(RECENT_KEY, [])
        if isinstance(value, str):
            value = [value]
        return [p for p in (value or []) if isinstance(p, str)]

    def _last_dir(self) -> str:
        for p in self._recent_list():
            d = os.path.dirname(p)
            if os.path.isdir(d):
                return d
        return ""

    def _add_recent(self, path: str) -> None:
        recents = [p for p in self._recent_list() if p != path]
        recents.insert(0, path)
        self.settings.setValue(RECENT_KEY, recents[:MAX_RECENT])
        self._refresh_recent()

    def _remove_recent(self, path: str) -> None:
        self.settings.setValue(RECENT_KEY, [p for p in self._recent_list() if p != path])
        self._refresh_recent()

    def _refresh_recent(self) -> None:
        recents = self._recent_list()
        self.welcome_page.set_recent(recents)
        self.recent_menu.clear()
        if not recents:
            act = self.recent_menu.addAction("No recent files")
            act.setEnabled(False)
            return
        for path in recents:
            act = self.recent_menu.addAction(os.path.basename(path))
            act.setToolTip(path)
            act.setStatusTip(path)
            act.triggered.connect(lambda _=False, p=path: self.open_project_path(p))

    # -- export -----------------------------------------------------------
    def export_current_sequence(self) -> None:
        seq = self.timeline.current_sequence()
        if not seq:
            show_toast(self, "No sequence to export", "warning")
            return
        try:
            code = export_sequence(seq)
        except ValidationError as exc:
            QMessageBox.warning(self, "Can't export this sequence yet", str(exc))
            return
        CodeOutputDialog(f"Export Sequence — {seq.name}", code, f"{seq.name}_pose.h", self).exec()

    def export_selected_face(self) -> None:
        name = self.face_library._selected_name()
        if not name:
            self._show_faces()
            show_toast(self, "Select a face in the Faces panel first", "warning")
            return
        if self.face_library.is_builtin(name):
            show_toast(self, f"“{name}” already ships in the firmware — nothing to export", "warning")
            return
        face = self.face_library.faces[name]
        code = export_face_bundle(face)
        CodeOutputDialog(f"Export Face — {face.name}", code, f"face_{face.name}.h", self).exec()

    # -- help / tutorial ---------------------------------------------------
    def start_tutorial(self) -> None:
        if self.stack.currentWidget() is not self.viewport:
            # The tour needs the editor; open a scratch project for it.
            self.new_project(start_tutorial=True)
            return
        self._tour = start_tour(self)

    def show_help(self, article: str | None = None) -> None:
        HelpDialog(self, open_article=article).exec()

    # -- window state -------------------------------------------------
    def reset_layout(self) -> None:
        self.restoreState(self._default_state, LAYOUT_VERSION)
        editing = self.stack.currentWidget() is self.viewport
        self._set_editor_chrome_visible(editing)

    def _restore_geometry(self) -> None:
        geo = self.settings.value("window/geometry")
        state = self.settings.value(f"window/state_v{LAYOUT_VERSION}")
        if geo is not None:
            self.restoreGeometry(geo)
        if state is not None:
            self.restoreState(state, LAYOUT_VERSION)

    def closeEvent(self, event) -> None:
        if not self._confirm_discard_unsaved():
            event.ignore()
            return
        self.timeline.stop()
        self.settings.setValue("window/geometry", self.saveGeometry())
        if self.stack.currentWidget() is self.viewport:
            # Only remember the layout while the editor is showing; on the
            # welcome page every dock is hidden and that isn't worth saving.
            self.settings.setValue(f"window/state_v{LAYOUT_VERSION}", self.saveState(LAYOUT_VERSION))
        super().closeEvent(event)
