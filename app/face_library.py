"""Face grid: thumbnails with category filter chips + search, double-click
to edit, assign to the currently selected timeline frame."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QCheckBox, QLabel, QMessageBox, QFrame,
)

from .project import Face
from .face_editor import FaceEditorDialog, frame_bytes_to_image
from .servo_model import FACE_CATEGORIES, FACE_CATEGORY_LABELS
from .builtin_faces import BUILTIN_FACES
from .face_bitmaps import FaceBitmapsFile
from .theme import PANEL, ACCENT, BORDER


class FaceLibraryPanel(QWidget):
    faceAssigned = Signal(str)     # face name -> assign to selected timeline frame
    facesChanged = Signal()
    facePreviewRequested = Signal(str)  # face name -> show on the model's face plane
    editBlocked = Signal(str)
    headerFileSaveRequested = Signal()
    headerFileSaveAsRequested = Signal()
    openHeaderRequested = Signal()
    closeHeaderRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.faces: dict[str, Face] = {}
        self.builtin_faces: dict[str, Face] = dict(BUILTIN_FACES)
        self.header_file: FaceBitmapsFile | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        # Header file banner (visible when editing a face-bitmaps.h file)
        self.header_banner = QFrame()
        self.header_banner.setStyleSheet(
            f"QFrame {{ background-color:{PANEL}; border:1px solid {ACCENT}; border-radius:6px; }}"
        )
        banner_layout = QHBoxLayout(self.header_banner)
        banner_layout.setContentsMargins(8, 4, 8, 4)
        self.header_label = QLabel("Editing: face-bitmaps.h")
        self.header_label.setStyleSheet("font-weight:700; font-size:12px;")
        banner_layout.addWidget(self.header_label, 1)

        self.save_header_btn = QPushButton("Save .h")
        self.save_header_btn.setProperty("variant", "success")
        self.save_header_btn.clicked.connect(self.headerFileSaveRequested.emit)
        banner_layout.addWidget(self.save_header_btn)

        save_as_header_btn = QPushButton("Save As...")
        save_as_header_btn.setProperty("variant", "flat")
        save_as_header_btn.clicked.connect(self.headerFileSaveAsRequested.emit)
        banner_layout.addWidget(save_as_header_btn)

        close_header_btn = QPushButton("Close")
        close_header_btn.setProperty("variant", "flat")
        close_header_btn.setToolTip("Close header file and return to project mode")
        close_header_btn.clicked.connect(self.closeHeaderRequested.emit)
        banner_layout.addWidget(close_header_btn)

        self.header_banner.setVisible(False)
        layout.addWidget(self.header_banner)

        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search faces...")
        self.search_edit.textChanged.connect(self._refresh)
        search_row.addWidget(self.search_edit)

        open_h_btn = QPushButton("Open .h...")
        open_h_btn.setToolTip("Open a face-bitmaps.h header to edit and save directly")
        open_h_btn.clicked.connect(self.openHeaderRequested.emit)
        search_row.addWidget(open_h_btn)

        new_btn = QPushButton("+ New Face")
        new_btn.clicked.connect(self._new_face)
        search_row.addWidget(new_btn)
        layout.addLayout(search_row)

        chip_row = QHBoxLayout()
        self.category_checks: dict[str, QCheckBox] = {}
        for cat in FACE_CATEGORIES:
            chk = QCheckBox(FACE_CATEGORY_LABELS[cat])
            chk.setChecked(True)
            chk.toggled.connect(self._refresh)
            chip_row.addWidget(chk)
            self.category_checks[cat] = chk
        chip_row.addStretch(1)
        layout.addLayout(chip_row)

        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.IconMode)
        self.list_widget.setIconSize(QSize(96, 48))
        self.list_widget.setResizeMode(QListWidget.Adjust)
        self.list_widget.setSpacing(8)
        self.list_widget.itemDoubleClicked.connect(self._edit_selected)
        self.list_widget.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self.list_widget, 1)

        btn_row = QHBoxLayout()
        assign_btn = QPushButton("Assign to Selected Frame")
        assign_btn.setProperty("variant", "success")
        assign_btn.clicked.connect(self._assign_selected)
        edit_btn = QPushButton("Edit"); edit_btn.setProperty("variant", "flat")
        edit_btn.clicked.connect(self._edit_selected)
        del_btn = QPushButton("Delete"); del_btn.setProperty("variant", "danger")
        del_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(assign_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(del_btn)
        layout.addLayout(btn_row)

    # -- data -------------------------------------------------------
    def load_faces(self, faces: dict[str, Face]) -> None:
        self.header_file = None
        self.builtin_faces = dict(BUILTIN_FACES)
        self.faces = faces
        self.header_banner.setVisible(False)
        self._refresh()

    def load_header_file(self, header_file: FaceBitmapsFile) -> None:
        self.header_file = header_file
        self.builtin_faces = {}
        self.faces = header_file.faces
        self.header_label.setText(f"Editing: {header_file.filename}")
        self.header_label.setToolTip(header_file.path)
        self.header_banner.setVisible(True)
        self.update_header_dirty(header_file.dirty)
        self._refresh()

    def update_header_dirty(self, dirty: bool) -> None:
        if self.header_file:
            self.save_header_btn.setText("Save .h*" if dirty else "Save .h")

    def get_face(self, name: str) -> Face | None:
        """Custom faces shadow built-ins of the same name, same priority
        rule as Timeline sequences."""
        return self.faces.get(name) or self.builtin_faces.get(name)

    def is_builtin(self, name: str) -> bool:
        return name in self.builtin_faces and name not in self.faces

    def all_face_names(self) -> list[str]:
        return sorted(set(self.faces.keys()) | set(self.builtin_faces.keys()))

    def _refresh(self) -> None:
        query = self.search_edit.text().strip().lower()
        active_categories = {c for c, chk in self.category_checks.items() if chk.isChecked()}
        self.list_widget.clear()
        for name in self.all_face_names():
            face = self.get_face(name)
            if face.category not in active_categories:
                continue
            if query and query not in name.lower():
                continue
            is_builtin = self.is_builtin(name)
            item = QListWidgetItem(f"★ {name}" if is_builtin else name)
            img = frame_bytes_to_image(bytes(face.frames[0]))
            item.setIcon(QIcon(QPixmap.fromImage(img)))
            item.setData(Qt.UserRole, name)
            label = FACE_CATEGORY_LABELS.get(face.category, face.category)
            tooltip = f"{name} ({label})"
            if is_builtin:
                tooltip += " -- built-in, read-only. Edit to save a custom copy."
            elif self.header_file and name in self.header_file.undefined_names:
                tooltip += " -- in FACE_LIST (no bitmap defined yet). Double-click to paint."
            item.setToolTip(tooltip)
            self.list_widget.addItem(item)

    def _selected_name(self) -> str | None:
        item = self.list_widget.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _on_selection_changed(self, current, previous) -> None:
        name = current.data(Qt.UserRole) if current else None
        if name:
            self.facePreviewRequested.emit(name)

    def _notify_blocked(self) -> None:
        self.editBlocked.emit(
            "Built-in faces are read-only. Edit one to save a custom copy under a new name."
        )

    # -- actions -------------------------------------------------------
    def _new_face(self) -> None:
        if self.header_file:
            dlg = FaceEditorDialog(None, set(self.faces.keys()), self, is_header_mode=True)
            if dlg.exec() and dlg.result_face:
                self.faces[dlg.result_face.name] = dlg.result_face
                self.header_file.add_face(dlg.result_face)
                self._refresh()
                self.facesChanged.emit()
                self.facePreviewRequested.emit(dlg.result_face.name)
            return

        dlg = FaceEditorDialog(None, set(self.faces.keys()), self)
        if dlg.exec() and dlg.result_face:
            self.faces[dlg.result_face.name] = dlg.result_face
            self._refresh()
            self.facesChanged.emit()

    def _edit_selected(self) -> None:
        name = self._selected_name()
        if not name:
            return
        face = self.get_face(name)

        if self.header_file:
            others = set(self.faces.keys())
            dlg = FaceEditorDialog(face, others, self, is_header_mode=True)
            if dlg.exec() and dlg.result_face:
                new_face = dlg.result_face
                if new_face.name != name:
                    del self.faces[name]
                    self.header_file.rename_face(name, new_face.name)
                self.faces[new_face.name] = new_face
                self.header_file.undefined_names.discard(new_face.name)
                self.header_file.mark_dirty()
                self._refresh()
                self.facesChanged.emit()
                self.facePreviewRequested.emit(new_face.name)
            return

        if self.is_builtin(name):
            # Duplicate-to-custom: pre-fill the built-in's bitmap but start
            # the name already renamed, so Save just works without hitting
            # the "name already exists" guard for the original.
            base = name
            new_name, i = f"{base}_custom", 2
            while new_name in self.faces or new_name in self.builtin_faces:
                new_name = f"{base}_custom{i}"
                i += 1
            seed = Face(
                name=new_name, category=face.category, fps=face.fps,
                frames=[bytearray(f) for f in face.frames],
            )
            dlg = FaceEditorDialog(seed, set(self.faces.keys()) | set(self.builtin_faces.keys()), self)
            if dlg.exec() and dlg.result_face:
                self.faces[dlg.result_face.name] = dlg.result_face
                self._refresh()
                self.facesChanged.emit()
            return

        others = set(self.faces.keys())
        dlg = FaceEditorDialog(face, others, self)
        if dlg.exec() and dlg.result_face:
            if dlg.result_face.name != name:
                del self.faces[name]
            self.faces[dlg.result_face.name] = dlg.result_face
            self._refresh()
            self.facesChanged.emit()

    def _delete_selected(self) -> None:
        name = self._selected_name()
        if not name:
            return

        if self.header_file:
            if QMessageBox.question(
                self, "Delete Face", f"Delete face '{name}' from {self.header_file.filename}?"
            ) != QMessageBox.Yes:
                return
            del self.faces[name]
            self.header_file.remove_face(name)
            self._refresh()
            self.facesChanged.emit()
            return

        if self.is_builtin(name):
            self._notify_blocked()
            return
        if QMessageBox.question(self, "Delete face", f"Delete '{name}'?") != QMessageBox.Yes:
            return
        del self.faces[name]
        self._refresh()
        self.facesChanged.emit()

    def _assign_selected(self) -> None:
        name = self._selected_name()
        if name:
            self.faceAssigned.emit(name)
