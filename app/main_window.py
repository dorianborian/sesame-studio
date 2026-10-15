"""Main application shell: menu bar, toolbar, dockable panels, welcome/editor
page transition, project lifecycle, and export dialogs."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QSettings, QPropertyAnimation, QEasingCurve, Signal
from PySide6.QtGui import QIcon, QAction, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow, QStackedWidget, QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QFileDialog, QMessageBox, QInputDialog, QComboBox, QLabel, QDialog,
    QPlainTextEdit, QPushButton, QApplication, QGraphicsOpacityEffect, QToolBar,
)

from .project import Project, frame_to_pixels
from .servo_model import BOARD_VARIANTS
from .viewport3d import Viewport3D, JointCalibrationPanel
from .inspector import InspectorPanel
from .timeline import TimelineWidget
from .face_library import FaceLibraryPanel
from .face_bitmaps import FaceBitmapsFile
from .builtin_faces import reload_builtin_faces
from .welcome import WelcomePage
from .tutorial import (
    TutorialOverlay, CoachStep, HelpDialog, should_show_first_run_tutorial,
)
from .code_export import export_sequence, export_face_bundle, ValidationError
from .widgets.toast import show_toast

from .paths import ASSETS_DIR, LOGO_PATH, ICON_PATH, FIRMWARE_DIR, BASE_DIR
RECENT_KEY = "project/recent"
MAX_RECENT = 8


class CodeOutputDialog(QDialog):
    def __init__(self, title: str, code: str, suggested_filename: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setPlainText(code)
        self.text_edit.setStyleSheet("font-family:Consolas; font-size:10pt;")
        layout.addWidget(self.text_edit)

        btn_row = QHBoxLayout()
        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.setProperty("variant", "success")
        copy_btn.clicked.connect(self._copy)
        save_btn = QPushButton("Save to File...")
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sesame Studio")
        self.resize(1440, 900)
        self.setMinimumSize(1000, 640)
        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))

        self.settings = QSettings("SesameRobot", "SesameStudio")
        self.project: Project | None = None
        self.header_file: FaceBitmapsFile | None = None

        self._build_central()
        self._build_docks()
        self._build_menu()
        self._build_statusbar()
        self._wire_signals()
        self._restore_geometry()

        self.stack.setCurrentWidget(self.welcome_page)
        self._set_editor_docks_visible(False)
        self._refresh_recent()

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

    def _build_docks(self) -> None:
        self.inspector = InspectorPanel()
        self.inspector_dock = QDockWidget("Inspector", self)
        self.inspector_dock.setObjectName("dock_inspector")
        self.inspector_dock.setWidget(self.inspector)
        self.addDockWidget(Qt.RightDockWidgetArea, self.inspector_dock)

        self.face_library = FaceLibraryPanel()
        self.face_dock = QDockWidget("Face Library", self)
        self.face_dock.setObjectName("dock_faces")
        self.face_dock.setWidget(self.face_library)
        self.addDockWidget(Qt.RightDockWidgetArea, self.face_dock)
        self.tabifyDockWidget(self.inspector_dock, self.face_dock)

        self.calibration_panel = JointCalibrationPanel(self.viewport)
        self.calib_dock = QDockWidget("Joint Calibration", self)
        self.calib_dock.setObjectName("dock_calibration")
        self.calib_dock.setWidget(self.calibration_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self.calib_dock)
        self.tabifyDockWidget(self.face_dock, self.calib_dock)

        self.timeline = TimelineWidget()
        self.timeline_dock = QDockWidget("Timeline", self)
        self.timeline_dock.setObjectName("dock_timeline")
        self.timeline_dock.setWidget(self.timeline)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.timeline_dock)

        self.inspector_dock.raise_()

    def _set_editor_docks_visible(self, visible: bool) -> None:
        for dock in (self.inspector_dock, self.face_dock, self.calib_dock, self.timeline_dock):
            dock.setVisible(visible)

    def _build_menu(self) -> None:
        mb = self.menuBar()

        file_menu = mb.addMenu("&File")
        file_menu.addAction(self._action("New Project", "Ctrl+N", self.new_project))
        file_menu.addAction(self._action("Open Project...", "Ctrl+O", self.open_project))
        file_menu.addAction(self._action("Open Face Bitmaps (*.h)...", "Ctrl+Shift+O", self.open_face_bitmaps_dialog))
        file_menu.addAction(self._action("Save", "Ctrl+S", self.save_project))
        file_menu.addAction(self._action("Save As...", "Ctrl+Shift+S", self.save_project_as))
        file_menu.addAction(self._action("Save Face Bitmaps As...", None, self.save_face_bitmaps_as))
        file_menu.addSeparator()
        self.recent_menu = file_menu.addMenu("Recent Files")
        file_menu.addSeparator()
        export_menu = file_menu.addMenu("Export")
        export_menu.addAction(self._action("Export Current Sequence...", None, self.export_current_sequence))
        export_menu.addAction(self._action("Export Selected Face...", None, self.export_selected_face))
        export_menu.addAction(self._action("Export All Faces to face-bitmaps.h...", None, self.save_face_bitmaps_as))
        file_menu.addSeparator()
        file_menu.addAction(self._action("Exit", "Ctrl+Q", self.close))

        seq_menu = mb.addMenu("&Sequence")
        seq_menu.addAction(self._action("Add Frame from Current Pose", None,
                                          lambda: self.timeline._add_frame_from_current()))
        seq_menu.addAction(self._action("Play / Stop", "Space", lambda: self.timeline.toggle_playback()))
        seq_menu.addSeparator()
        board_widget_action = QAction("Board Variant", self)
        board_widget_action.setEnabled(False)
        seq_menu.addAction(board_widget_action)
        self.board_combo = QComboBox()
        for key, variant in BOARD_VARIANTS.items():
            self.board_combo.addItem(variant.label, key)
        self.board_combo.currentIndexChanged.connect(self._on_board_variant_changed)

        toolbar = QToolBar("Board")
        toolbar.setObjectName("toolbar_board")
        toolbar.addWidget(QLabel(" Board: "))
        toolbar.addWidget(self.board_combo)
        self.addToolBar(Qt.TopToolBarArea, toolbar)

        faces_menu = mb.addMenu("&Faces")
        faces_menu.addAction(self._action("New Face...", None, self.face_library._new_face))
        faces_menu.addAction(self._action("Open Face Library", None, lambda: self.face_dock.raise_()))
        faces_menu.addSeparator()
        faces_menu.addAction(self._action("Open face-bitmaps.h...", None, self.open_face_bitmaps_dialog))
        faces_menu.addAction(self._action("Save to face-bitmaps.h", None, self.save_face_bitmaps))
        faces_menu.addAction(self._action("Save face-bitmaps.h As...", None, self.save_face_bitmaps_as))

        view_menu = mb.addMenu("&View")
        view_menu.addAction(self.inspector_dock.toggleViewAction())
        view_menu.addAction(self.face_dock.toggleViewAction())
        view_menu.addAction(self.calib_dock.toggleViewAction())
        view_menu.addAction(self.timeline_dock.toggleViewAction())
        view_menu.addSeparator()
        view_menu.addAction(self._action("Reset Camera", None, self.viewport.reset_view))

        help_menu = mb.addMenu("&Help")
        help_menu.addAction(self._action("Getting Started (Tour)", None, self.start_tutorial))
        help_menu.addSeparator()
        help_menu.addAction(self._action("Timeline Guide", None, lambda: self.show_help("Timeline Guide")))
        help_menu.addAction(self._action("Face Painter Guide", None, lambda: self.show_help("Face Painter Guide")))
        help_menu.addAction(self._action("Export Guide", None, lambda: self.show_help("Export Guide")))
        help_menu.addSeparator()
        help_menu.addAction(self._action("About Sesame Studio", None, lambda: self.show_help("About")))

    def _action(self, text: str, shortcut, slot) -> QAction:
        act = QAction(text, self)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        act.triggered.connect(slot)
        return act

    def _build_statusbar(self) -> None:
        self.statusBar().showMessage("Ready")

    # -- signal wiring ---------------------------------------------------
    def _wire_signals(self) -> None:
        self.inspector.anglesChanged.connect(self._on_inspector_angles_changed)
        self.inspector.frameFieldsChanged.connect(self._on_inspector_frame_fields_changed)

        self.timeline.frameSelected.connect(self._on_frame_selected)
        self.timeline.poseAtIndex.connect(self._on_playback_pose)
        self.timeline.sequencesChanged.connect(self._mark_dirty)
        self.timeline.editBlocked.connect(lambda msg: show_toast(self, msg, "warning"))
        self.face_library.editBlocked.connect(lambda msg: show_toast(self, msg, "warning"))

        self.face_library.faceAssigned.connect(self._on_face_assigned)
        self.face_library.facesChanged.connect(self._on_faces_changed)
        self.face_library.facePreviewRequested.connect(self._on_face_preview_requested)
        self.face_library.headerFileSaveRequested.connect(self.save_face_bitmaps)
        self.face_library.headerFileSaveAsRequested.connect(self.save_face_bitmaps_as)
        self.face_library.openHeaderRequested.connect(self.open_face_bitmaps_dialog)
        self.face_library.closeHeaderRequested.connect(self.close_header_file)

    def _on_inspector_angles_changed(self, angles: dict) -> None:
        self.viewport.set_angles(angles)
        self.timeline.update_selected_frame_angles(angles)

    def _on_inspector_frame_fields_changed(self, delay_ms: int, face, face_mode: str) -> None:
        self.timeline.update_selected_frame_fields(delay_ms, face, face_mode)

    def _on_frame_selected(self, index: int) -> None:
        seq = self.timeline.current_sequence()
        if not seq or index >= len(seq.frames):
            return
        frame = seq.frames[index]
        self.inspector.load_angles(frame.angles)
        self.inspector.load_frame_fields(frame.delay_ms, frame.face, frame.face_mode)
        self.viewport.set_angles(frame.angles)
        self.viewport.set_face_preview(self._resolve_face_pixels(frame.face))

    def _on_playback_pose(self, angles: dict, face, face_mode: str) -> None:
        self.viewport.set_angles(angles)
        if face:
            self.viewport.set_face_preview(self._resolve_face_pixels(face))

    def _on_face_preview_requested(self, name: str) -> None:
        self.viewport.set_face_preview(self._resolve_face_pixels(name))

    def _resolve_face_pixels(self, face_name):
        if not face_name:
            return None
        face = self.face_library.get_face(face_name)
        if not face or not face.frames:
            return None
        return frame_to_pixels(bytes(face.frames[0]))

    def _on_face_assigned(self, name: str) -> None:
        if self.timeline.selected_index is None:
            show_toast(self, "Select a timeline frame first", "warning")
            return
        seq = self.timeline.current_sequence()
        frame = seq.frames[self.timeline.selected_index]
        self.timeline.update_selected_frame_fields(frame.delay_ms, name, frame.face_mode)
        self.inspector.load_frame_fields(frame.delay_ms, name, frame.face_mode)
        show_toast(self, f"Assigned face '{name}' to frame {self.timeline.selected_index + 1}", "success")

    def _on_faces_changed(self) -> None:
        self.inspector.set_face_options(self.face_library.all_face_names())
        self._mark_dirty()

    def _on_board_variant_changed(self) -> None:
        if self.project:
            self.project.settings.board_variant = self.board_combo.currentData()
            self._mark_dirty()

    def _mark_dirty(self) -> None:
        if self.header_file:
            self.header_file.dirty = True
            self.face_library.update_header_dirty(True)
            self._update_title()
            return
        if self.project:
            self.project.dirty = True
            self._update_title()

    def _update_title(self) -> None:
        if self.header_file:
            star = "*" if self.header_file.dirty else ""
            self.setWindowTitle(f"Sesame Studio - {self.header_file.filename}{star} [Face Bitmaps]")
            self.face_library.update_header_dirty(self.header_file.dirty)
            return
        if not self.project:
            self.setWindowTitle("Sesame Studio")
            return
        star = "*" if self.project.dirty else ""
        self.setWindowTitle(f"Sesame Studio - {self.project.name}{star}")

    # -- project lifecycle ------------------------------------------------
    def new_project(self, start_tutorial: bool = False) -> None:
        if not self._confirm_discard_unsaved():
            return
        name, ok = QInputDialog.getText(self, "New Project", "Project name:", text="Untitled Sesame Project")
        if not ok:
            return
        self.header_file = None
        self.project = Project(name.strip() or "Untitled Sesame Project")
        self._load_project_into_ui()
        self._goto_editor()
        show_toast(self, "New project created", "success")
        if start_tutorial or should_show_first_run_tutorial():
            self.start_tutorial()

    def open_project(self) -> None:
        if not self._confirm_discard_unsaved():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open File", "", "Sesame Project (*.sesproj);;Face Bitmaps (*.h);;All Files (*)"
        )
        if path:
            self.open_project_path(path)

    def open_project_path(self, path: str) -> None:
        if not os.path.exists(path):
            show_toast(self, "Project file not found", "error")
            return
        if path.endswith(".h") or path.endswith(".hpp"):
            self.open_face_bitmaps_path(path)
            return
        try:
            self.project = Project.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Failed to open project", str(exc))
            return
        self.header_file = None
        self._load_project_into_ui()
        self._goto_editor()
        self._add_recent(path)
        show_toast(self, f"Opened {os.path.basename(path)}", "success")

    def save_project(self) -> None:
        if self.header_file:
            self.save_face_bitmaps()
            return
        if not self.project:
            return
        if not self.project.path:
            self.save_project_as()
            return
        self._sync_project_from_ui()
        self.project.save(self.project.path)
        self._add_recent(self.project.path)
        self._update_title()
        show_toast(self, "Project saved", "success")

    def save_project_as(self) -> None:
        if self.header_file:
            self.save_face_bitmaps_as()
            return
        if not self.project:
            return
        default = f"{self.project.name}.sesproj"
        path, _ = QFileDialog.getSaveFileName(self, "Save Project As", default, "Sesame Project (*.sesproj)")
        if not path:
            return
        self._sync_project_from_ui()
        self.project.save(path)
        self._add_recent(path)
        self._update_title()
        show_toast(self, "Project saved", "success")

    def _confirm_discard_unsaved(self) -> bool:
        if self.header_file and self.header_file.dirty:
            filename = self.header_file.filename
            res = QMessageBox.question(
                self, "Unsaved changes",
                f"'{filename}' has unsaved changes. Save before continuing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            )
            if res == QMessageBox.Save:
                self.save_face_bitmaps()
                return True
            return res == QMessageBox.Discard

        if not self.project or not self.project.dirty:
            return True
        res = QMessageBox.question(
            self, "Unsaved changes",
            f"'{self.project.name}' has unsaved changes. Save before continuing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
        )
        if res == QMessageBox.Save:
            self.save_project()
            return True
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
        default_path = ""
        for c in candidates:
            if os.path.exists(c):
                default_path = c
                break
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
            QMessageBox.critical(self, "Failed to open face bitmaps", str(exc))
            return
        self.header_file = header_file
        self.project = Project(f"Face Bitmaps ({header_file.filename})")
        self.project.dirty = False
        self.face_library.load_header_file(header_file)
        self.inspector.set_face_options(self.face_library.all_face_names())
        self._goto_editor()
        self.face_dock.raise_()
        self._add_recent(path)
        self._update_title()
        show_toast(self, f"Opened {header_file.filename} ({len(header_file.faces)} faces)", "success")

    def save_face_bitmaps(self) -> None:
        if not self.header_file:
            self.save_face_bitmaps_as()
            return
        try:
            self.header_file.save()
            self.face_library.update_header_dirty(False)
            self._update_title()
            reload_builtin_faces()
            show_toast(self, f"Saved {self.header_file.filename}", "success")
        except Exception as exc:
            QMessageBox.critical(self, "Failed to save face bitmaps", str(exc))

    def save_face_bitmaps_as(self) -> None:
        default = self.header_file.path if self.header_file else "face-bitmaps.h"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Face Bitmaps As", default,
            "Face Bitmaps (*.h);;Header Files (*.h *.hpp);;All Files (*)"
        )
        if not path:
            return
        try:
            if self.header_file:
                self.header_file.save(path)
            else:
                fb = FaceBitmapsFile(
                    path=path,
                    faces=dict(self.face_library.faces),
                    order=list(self.face_library.faces.keys()),
                    undefined_names=set(),
                )
                fb.save(path)
                self.header_file = fb
                self.face_library.load_header_file(fb)
            self.face_library.update_header_dirty(False)
            self._add_recent(path)
            self._update_title()
            reload_builtin_faces()
            show_toast(self, f"Saved {os.path.basename(path)}", "success")
        except Exception as exc:
            QMessageBox.critical(self, "Failed to save face bitmaps", str(exc))

    def close_header_file(self) -> None:
        if not self._confirm_discard_unsaved():
            return
        self.header_file = None
        self.project = None
        self.face_library.load_faces({})
        self.stack.setCurrentWidget(self.welcome_page)
        self._set_editor_docks_visible(False)
        self._update_title()

    def _load_project_into_ui(self) -> None:
        self.timeline.load_project_sequences(self.project.sequences)
        self.face_library.load_faces(self.project.faces)
        self.inspector.set_face_options(self.face_library.all_face_names())
        idx = self.board_combo.findData(self.project.settings.board_variant)
        self.board_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.project.dirty = False
        self._update_title()

    def _sync_project_from_ui(self) -> None:
        # sequences/faces dicts are mutated in place by Timeline/FaceLibrary,
        # so the project already reflects the latest edits.
        self.project.sequences = self.timeline.sequences
        self.project.faces = self.face_library.faces

    def _goto_editor(self) -> None:
        self._fade_switch(self.viewport)
        self._set_editor_docks_visible(True)

    def _fade_switch(self, widget: QWidget) -> None:
        self.stack.setCurrentWidget(widget)
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(320)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        self._fade_anim = anim

    # -- recent projects -------------------------------------------------
    def _recent_list(self) -> list[str]:
        return self.settings.value(RECENT_KEY, [], type=list) or []

    def _add_recent(self, path: str) -> None:
        recents = [p for p in self._recent_list() if p != path]
        recents.insert(0, path)
        recents = recents[:MAX_RECENT]
        self.settings.setValue(RECENT_KEY, recents)
        self._refresh_recent()

    def _refresh_recent(self) -> None:
        recents = self._recent_list()
        self.welcome_page.set_recent(recents)
        self.recent_menu.clear()
        if not recents:
            act = self.recent_menu.addAction("(none)")
            act.setEnabled(False)
        for path in recents:
            self.recent_menu.addAction(
                self._action(os.path.basename(path), None, lambda _, p=path: self.open_project_path(p))
            )

    # -- export -----------------------------------------------------------
    def export_current_sequence(self) -> None:
        seq = self.timeline.current_sequence()
        if not seq:
            show_toast(self, "No sequence to export", "warning")
            return
        try:
            code = export_sequence(seq)
        except ValidationError as exc:
            QMessageBox.warning(self, "Cannot export sequence", str(exc))
            return
        CodeOutputDialog(f"Export Sequence: {seq.name}", code, f"{seq.name}_pose.h", self).exec()

    def export_selected_face(self) -> None:
        name = self.face_library._selected_name()
        if not name:
            show_toast(self, "Select a face in the Face Library first", "warning")
            return
        if self.face_library.is_builtin(name):
            show_toast(self, f"'{name}' already ships in the firmware -- nothing to export", "warning")
            return
        face = self.face_library.faces[name]
        code = export_face_bundle(face)
        CodeOutputDialog(f"Export Face: {face.name}", code, f"face_{face.name}.h", self).exec()

    # -- help / tutorial ---------------------------------------------------
    def start_tutorial(self) -> None:
        if self.stack.currentWidget() is not self.viewport:
            self._goto_editor()
        steps = [
            CoachStep(self.viewport, "3D Viewport",
                      "Drag to orbit, scroll to zoom, Shift+drag to pan. This "
                      "mirrors the servo angles set in the Inspector in real time."),
            CoachStep(self.inspector_dock, "Inspector",
                      "Set each servo's angle here. Ranges are clamped to what "
                      "your hardware actually supports."),
            CoachStep(self.timeline_dock, "Timeline",
                      "Click + Add Frame to capture the current pose. Drag "
                      "blocks to reorder or resize their edge to retime, drag "
                      "the ruler to scrub, then hit Play -- servos animate "
                      "smoothly between frames. The Sequence dropdown also "
                      "has every built-in firmware pose ready to play."),
            CoachStep(self.face_dock, "Face Library",
                      "Draw or type OLED faces, then Assign to Selected Frame "
                      "to attach one to a pose."),
        ]
        overlay = TutorialOverlay(steps, self)
        overlay.finished.connect(lambda: None)

    def show_help(self, article: str | None = None) -> None:
        HelpDialog(self, open_article=article).exec()

    # -- window state -------------------------------------------------
    def _restore_geometry(self) -> None:
        geo = self.settings.value("window/geometry")
        state = self.settings.value("window/state")
        if geo is not None:
            self.restoreGeometry(geo)
        if state is not None:
            self.restoreState(state)

    def closeEvent(self, event) -> None:
        if not self._confirm_discard_unsaved():
            event.ignore()
            return
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/state", self.saveState())
        super().closeEvent(event)
