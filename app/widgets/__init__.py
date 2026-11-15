"""Shared, theme-aware building blocks for Sesame Studio panels.

Everything here is styled by ``app.theme.STYLESHEET`` through dynamic
properties, so the widgets carry no hard-coded colours. Import what you need
either from the submodule or straight from the package (names are resolved
lazily, so ``import app.widgets`` itself stays free of Qt imports)::

    from app.widgets import IconButton, SegmentedControl, show_toast

Index
-----
``buttons.py``
    ``IconButton(icon_name, tooltip="", shortcut=None, parent=None, *,
    text=None, checkable=False, variant="flat", icon_size=16, bind_shortcut=False)``
        Tool button with a vector icon, a tooltip that shows the shortcut as a
        hint, and the theme's variants. ``set_icon(name)`` swaps the glyph.
    ``tooltip_with_shortcut(text, shortcut) -> str``
        Rich-text tooltip "Text  [Ctrl+Z]" for any widget / QAction.
    ``KeyHint(sequence, parent=None)``
        Row of key-cap chips for a shortcut such as ``"Ctrl+Shift+S"``.
``segmented.py``
    ``SegmentedControl(options, parent=None, *, current=None, icon_only=False)``
        Exclusive pill buttons. ``options`` items are ``key`` or
        ``(key, label)`` or ``(key, label, icon_name)`` or
        ``(key, label, icon_name, tooltip)``. Signal ``currentChanged(str)``;
        ``current()``, ``set_current(key, emit=False)``, ``add_option(...)``,
        ``button(key)``, ``set_option_enabled(key, bool)``.
``layout.py``
    ``Card(parent=None, *, clickable=False, padding=12, spacing=8)``
        Panel-surface frame with a ready ``QVBoxLayout`` (``card.body``);
        ``clickable=True`` adds hover/focus feedback and a ``clicked`` signal
        (mouse, Enter, Space).
    ``ToolStrip(parent=None, *, spacing=4, margins=(8, 4, 8, 4))``
        The ``role="toolbar"`` strip for the top of a dock:
        ``add(widget, stretch=0)``, ``add_stretch()``, ``add_separator()``,
        ``add_spacing(px)``, ``add_label(text)``.
    ``Separator(orientation=Qt.Horizontal, parent=None)`` hairline.
    ``SectionLabel(text, parent=None)`` caption-styled label.
    ``Badge(text, tone=None, parent=None)`` pill label; ``set_tone(tone)``.
``empty_state.py``
    ``EmptyState(icon_name, title, hint="", action_text=None, parent=None, *,
    action_icon=None, action_variant="primary", compact=False)``
        Centered icon + title + hint + optional button. Signal
        ``actionRequested``; ``set_title``, ``set_hint``, ``set_icon``,
        ``set_action(text_or_None)``; ``action_button`` attribute.
``toast.py``
    ``show_toast(parent, text, level="info", duration_ms=None) -> Toast | None``
        Non-blocking notification stacked at the bottom of ``parent``'s
        window. Levels: ``info | success | warning | error``.
``collapsible.py``
    ``CollapsibleSection(title, content, parent=None, expanded=True)``
        Chevron header + animated body. ``toggled(bool)``,
        ``set_expanded(bool, animate=True)``, ``is_expanded()``,
        ``set_title(str)``, ``add_header_widget(widget)``; attributes
        ``toggle_btn`` and ``content``.
``logo.py``
    ``logo_pixmap(path, size) -> QPixmap`` cached, high-DPI product logo.
"""

from __future__ import annotations

import importlib

_EXPORTS = {
    "IconButton": "buttons",
    "KeyHint": "buttons",
    "tooltip_with_shortcut": "buttons",
    "SegmentedControl": "segmented",
    "Card": "layout",
    "ToolStrip": "layout",
    "Separator": "layout",
    "SectionLabel": "layout",
    "Badge": "layout",
    "EmptyState": "empty_state",
    "Toast": "toast",
    "show_toast": "toast",
    "CollapsibleSection": "collapsible",
    "logo_pixmap": "logo",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(list(globals()) + list(_EXPORTS))
