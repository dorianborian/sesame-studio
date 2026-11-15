"""Floating on-canvas controls for the 3D viewport.

A compact, theme-styled toolbar that sits on top of the GL view:

    [Simulate] [Reset] [Follow] | [Mirror] | [View v] [Display v]

It never takes keyboard focus (so the viewport keeps its shortcuts) and only
talks to the viewport through its public methods; ``sync()`` pulls the
current state back into the buttons / menus.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor
from PySide6.QtWidgets import QColorDialog, QFrame, QHBoxLayout, QMenu, QToolButton, QWidget

from . import theme
from .icons import icon as _icon

__all__ = ["ViewportHud", "COVER_LABELS", "SHELL_COLORS", "VIEW_PRESETS"]

COVER_LABELS = {
    "step": "Standard",
    "enclosed": "Enclosed",
    "cat": "Cat ears",
    "no-ears": "No ears",
}

SHELL_COLORS = [
    ("Original", None),
    ("White", "#e9e6df"),
    ("Graphite", "#34363c"),
    ("Sky blue", "#3f8cff"),
    ("Mint", "#3cc79a"),
    ("Pink", "#ff6fa8"),
    ("Yellow", "#f7c531"),
    ("Red", "#e2463c"),
    ("Purple", "#8b66ea"),
]

VIEW_PRESETS = [
    ("3/4 view", "three_quarter"),
    ("Front", "front"),
    ("Back", "back"),
    ("Left", "left"),
    ("Right", "right"),
    ("Top", "top"),
]


def cover_label(variant: str) -> str:
    return COVER_LABELS.get(variant, variant.replace("-", " ").replace("_", " ").title())


def _tip(text: str, shortcut: str | None = None) -> str:
    if not shortcut:
        return text
    return (f"<span style='white-space:nowrap;'>{text}&nbsp;&nbsp;"
            f"<span style='color:{theme.FG_MUTED};'>{shortcut}</span></span>")


class _HudButton(QToolButton):
    def __init__(self, icon_name: str, tooltip: str, parent=None, *, text: str | None = None,
                 checkable: bool = False, shortcut: str | None = None):
        super().__init__(parent)
        self._text = text
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("variant", "flat")
        self.setCheckable(checkable)
        self.setIcon(_icon(icon_name))
        self.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
        self.setToolTip(_tip(tooltip, shortcut))
        self.setAccessibleName(tooltip)
        self.set_compact(text is None)

    def set_compact(self, compact: bool) -> None:
        compact = compact or self._text is None
        if compact:
            self.setText("")
            self.setProperty("iconOnly", "true")
            self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        else:
            self.setText(self._text)
            self.setProperty("iconOnly", None)
            self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        theme.repolish(self)
        self.updateGeometry()


class ViewportHud(QFrame):
    def __init__(self, viewport):
        super().__init__(viewport)
        self._vp = viewport
        self._compact = False
        self.setObjectName("ViewportHud")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        bg = QColor(theme.BG_ELEVATED)
        self.setStyleSheet(
            f"#ViewportHud {{ background-color: rgba({bg.red()},{bg.green()},{bg.blue()},225);"
            f" border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_LG + 2}px; }}"
            f"#ViewportHud QFrame[role=\"separator\"] {{ background-color: {theme.BORDER}; border: none; }}"
            # menu buttons look like the other icon buttons (no arrow, no extra padding)
            "#ViewportHud QToolButton[iconOnly=\"true\"] { padding: 5px; }"
            "#ViewportHud QToolButton::menu-indicator { image: none; width: 0px; height: 0px; }"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)

        self.btn_sim = _HudButton("physics", "Simulate physics: the robot stands, tilts and walks on the floor",
                                  self, text="Simulate", checkable=True)
        self.btn_sim.toggled.connect(self._on_sim)
        self.btn_reset = _HudButton("home", "Put the robot back at the origin", self)
        self.btn_reset.clicked.connect(lambda: self._vp.reset_physics())
        self.btn_follow = _HudButton("target", "Camera follows the robot", self, checkable=True)
        self.btn_follow.toggled.connect(lambda on: self._vp.set_follow(on))
        self.btn_mirror = _HudButton("flip-h", "Mirror posing: editing a joint also drives its left/right twin",
                                     self, checkable=True)
        self.btn_mirror.toggled.connect(lambda on: self._vp.set_mirror_posing(on))
        self.btn_view = _HudButton("camera", "Camera views", self)
        self.btn_display = _HudButton("layers", "Display options", self)

        for w in (self.btn_sim, self.btn_reset, self.btn_follow):
            lay.addWidget(w)
        lay.addWidget(self._sep())
        lay.addWidget(self.btn_mirror)
        lay.addWidget(self._sep())
        lay.addWidget(self.btn_view)
        lay.addWidget(self.btn_display)

        self._build_view_menu()
        self._build_display_menu()
        self.sync()
        self.adjustSize()

    def _sep(self) -> QFrame:
        s = QFrame(self)
        s.setProperty("role", "separator")
        s.setFixedWidth(1)
        s.setFixedHeight(18)
        return s

    # ---------------------------------------------------------------- menus
    def _menu(self, button: QToolButton) -> QMenu:
        menu = QMenu(self)
        menu.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        try:
            theme.polish_popup(menu)
        except Exception:
            pass
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        return menu

    def _build_view_menu(self) -> None:
        menu = self._menu(self.btn_view)
        for label, key in VIEW_PRESETS:
            act = menu.addAction(label)
            act.triggered.connect(lambda _=False, k=key: self._vp.set_view(k))
        menu.addSeparator()
        act = menu.addAction("Frame robot\tF")
        act.triggered.connect(lambda: self._vp.frame_robot())
        act = menu.addAction("Reset view\tHome")
        act.triggered.connect(lambda: self._vp.reset_view())

    def _build_display_menu(self) -> None:
        vp = self._vp
        menu = self._menu(self.btn_display)

        def check(label, getter, setter) -> QAction:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.triggered.connect(lambda on: setter(bool(on)))
            act.setProperty("getter", getter)
            return act

        self._checks = [
            check("Grid", lambda: vp.show_grid, vp.set_show_grid),
            check("Shadows", lambda: vp.show_shadows, vp.set_show_shadows),
            check("Joint labels", lambda: vp.show_labels, vp.set_show_labels),
            check("Show internals", lambda: vp.show_internals, vp.set_show_internals),
        ]
        menu.addSeparator()

        self._cover_menu = menu.addMenu("Top cover")
        self._cover_group = QActionGroup(self._cover_menu)
        self._cover_group.setExclusive(True)
        self._cover_actions: dict[str, QAction] = {}
        for variant in vp.cover_variants:
            act = self._cover_menu.addAction(cover_label(variant))
            act.setCheckable(True)
            self._cover_group.addAction(act)
            act.triggered.connect(lambda _=False, v=variant: vp.set_cover(v))
            self._cover_actions[variant] = act
        self._cover_menu.setEnabled(bool(self._cover_actions))

        self._color_menu = menu.addMenu("Shell color")
        self._color_group = QActionGroup(self._color_menu)
        self._color_group.setExclusive(True)
        self._color_actions: list[tuple[str | None, QAction]] = []
        for label, value in SHELL_COLORS:
            act = self._color_menu.addAction(label)
            act.setCheckable(True)
            self._color_group.addAction(act)
            act.triggered.connect(lambda _=False, v=value: vp.set_shell_color(v))
            self._color_actions.append((value, act))
        self._color_menu.addSeparator()
        self._custom_color = self._color_menu.addAction("Custom…")
        self._custom_color.setCheckable(True)
        self._color_group.addAction(self._custom_color)
        self._custom_color.triggered.connect(self._pick_custom_color)
        for m in (self._cover_menu, self._color_menu):
            try:
                theme.polish_popup(m)
            except Exception:
                pass

    def _pick_custom_color(self) -> None:
        current = QColor(self._vp.shell_color or "#ff6e21")
        color = QColorDialog.getColor(current, self.window(), "Shell color")
        if color.isValid():
            self._vp.set_shell_color(color.name())
        self.sync()

    def _on_sim(self, on: bool) -> None:
        self._vp.set_physics_enabled(on)

    # ----------------------------------------------------------------- state
    def sync(self) -> None:
        """Pull the viewport's state into the controls (no signals fired)."""
        vp = self._vp
        for btn, value in ((self.btn_sim, vp.physics_enabled), (self.btn_follow, vp.follow),
                           (self.btn_mirror, vp.mirror_posing)):
            if btn.isChecked() != bool(value):
                btn.blockSignals(True)
                btn.setChecked(bool(value))
                btn.blockSignals(False)
        available = vp.physics_available
        self.btn_sim.setEnabled(available)
        self.btn_reset.setEnabled(available and vp.physics_enabled)
        self.btn_follow.setEnabled(available and vp.physics_enabled)
        for act in self._checks:
            act.setChecked(bool(act.property("getter")()))
        for variant, act in self._cover_actions.items():
            act.setChecked(variant == vp.cover)
        color = vp.shell_color
        matched = False
        for value, act in self._color_actions:
            hit = (value is None and color is None) or (
                value is not None and color is not None and QColor(value) == QColor(color))
            act.setChecked(hit)
            matched = matched or hit
        self._custom_color.setChecked(not matched)

    def reflow(self, viewport_width: int) -> None:
        """Keep the bar inside narrow viewports (drops the 'Simulate' caption)."""
        compact = viewport_width < 560
        if compact != self._compact:
            self._compact = compact
            self.btn_sim.set_compact(compact)
        self.adjustSize()
        margin = 10 if viewport_width >= 420 else 4
        self.move(margin, margin)

    def contains_global(self, widget_pos) -> bool:
        return self.isVisible() and self.geometry().contains(widget_pos)
