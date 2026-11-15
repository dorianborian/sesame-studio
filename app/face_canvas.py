"""The paint surface of the Face Editor: a zoomable, pannable 128x64 1-bit
canvas with pencil/shape/fill/text/select tools, mirror drawing, selection
move/copy/paste and per-frame undo.

All editing goes through pixel-coordinate methods (``begin_at`` /
``drag_to`` / ``end_stroke`` and friends), the mouse handlers only translate
widget positions, so tests and scripts can drive the canvas without
synthesising Qt events.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QPoint, QRect, QRectF, QTimer, Signal, QMimeData
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QGuiApplication
from PySide6.QtWidgets import QWidget, QSizePolicy

from . import face_core as fc
from . import face_render as fr
from .servo_model import FACE_WIDTH, FACE_HEIGHT
from .theme import ACCENT, BG_SUNKEN, BORDER_STRONG, SELECTION

W, H = FACE_WIDTH, FACE_HEIGHT

TOOLS = ["pencil", "eraser", "line", "rect", "rect_fill", "ellipse",
         "ellipse_fill", "fill", "text", "select"]
SHAPE_TOOLS = ("line", "rect", "rect_fill", "ellipse", "ellipse_fill")
BRUSH_TOOLS = ("pencil", "eraser", "line", "rect", "ellipse")

MIN_ZOOM, MAX_ZOOM = 1, 32
CLIP_MIME = "application/x-sesame-face-clip"
_MARGIN = 8

# Shared between editor windows, so a region copied in one face can be
# pasted into another.
_clipboard: np.ndarray | None = None


def clipboard_array() -> np.ndarray | None:
    return None if _clipboard is None else _clipboard.copy()


class FaceCanvas(QWidget):
    changed = Signal()               # pixels changed (fires live while dragging)
    committed = Signal()             # one undoable edit finished
    hoverChanged = Signal(int, int)  # pixel under the cursor, (-1, -1) outside
    zoomChanged = Signal(int)
    selectionChanged = Signal()
    historyChanged = Signal()
    toolRequested = Signal(str)      # canvas wants the UI to switch tool (paste)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FaceCanvas")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(W * 2 + 2 * _MARGIN, H * 2 + 2 * _MARGIN)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

        self.pixels: np.ndarray = fc.blank()
        self.history = fc.History()
        self.onion: np.ndarray | None = None

        self.tool = "pencil"
        self.brush_size = 1
        self.mirror_x = False   # left <-> right symmetry (vertical axis)
        self.mirror_y = False   # top <-> bottom symmetry (horizontal axis)
        self.show_grid = True

        self.zoom = 4
        self._auto_fit = True
        self._pan = QPoint(0, 0)

        self._display: QImage | None = None
        self._ghost: np.ndarray | None = None
        self._hover: tuple[int, int] | None = None

        # stroke state
        self._stroke_base: np.ndarray | None = None
        self._stroke_value = True
        self._stroke_start: tuple[int, int] = (0, 0)
        self._stroke_last: tuple[int, int] = (0, 0)

        # selection state
        self.selection: QRect | None = None
        self._float: np.ndarray | None = None
        self._float_base: np.ndarray | None = None
        self._sel_mode: str | None = None       # "marquee" | "move"
        self._sel_anchor: tuple[int, int] = (0, 0)
        self._sel_moved = False

        # text tool
        self._text = ""
        self._text_family = ""
        self._text_size = 16
        self._text_bold = False
        self._text_mask: np.ndarray | None = None
        self._text_baseline = 0

        # pan state
        self._space_down = False
        self._panning = False
        self._pan_origin = QPoint()
        self._pan_start = QPoint()

        self._ants_phase = 0
        self._ants_timer = QTimer(self)
        self._ants_timer.setInterval(140)
        self._ants_timer.timeout.connect(self._advance_ants)

        self._update_cursor()

    # ------------------------------------------------------------------
    # Geometry / view
    # ------------------------------------------------------------------
    def fit_zoom(self) -> int:
        zw = (self.width() - 2 * _MARGIN) // W
        zh = (self.height() - 2 * _MARGIN) // H
        return max(MIN_ZOOM, min(MAX_ZOOM, zw, zh))

    def panel_origin(self) -> QPoint:
        return QPoint((self.width() - W * self.zoom) // 2 + self._pan.x(),
                      (self.height() - H * self.zoom) // 2 + self._pan.y())

    def panel_rect(self) -> QRect:
        o = self.panel_origin()
        return QRect(o.x(), o.y(), W * self.zoom, H * self.zoom)

    def widget_to_pixel(self, pos) -> tuple[int, int]:
        """Widget position -> (x, y) pixel, not clamped to the panel."""
        o = self.panel_origin()
        return (int((pos.x() - o.x()) // self.zoom), int((pos.y() - o.y()) // self.zoom))

    def pixel_to_widget(self, x: int, y: int) -> QPoint:
        """Centre of pixel (x, y) in widget coordinates."""
        o = self.panel_origin()
        return QPoint(o.x() + x * self.zoom + self.zoom // 2,
                      o.y() + y * self.zoom + self.zoom // 2)

    def _clamp_pan(self) -> None:
        # The panel may be dragged anywhere as long as a strip of it stays
        # in view, so it can never be lost off-screen.
        keep = 48
        mx = max(0, (self.width() + W * self.zoom) // 2 - keep)
        my = max(0, (self.height() + H * self.zoom) // 2 - keep)
        self._pan = QPoint(max(-mx, min(mx, self._pan.x())), max(-my, min(my, self._pan.y())))

    def set_zoom(self, zoom: int, anchor: QPoint | None = None, auto: bool = False) -> None:
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, int(zoom)))
        self._auto_fit = auto
        if zoom == self.zoom and not auto:
            return
        if anchor is not None and zoom != self.zoom:
            # keep the image point under the cursor fixed
            o = self.panel_origin()
            fx = (anchor.x() - o.x()) / self.zoom
            fy = (anchor.y() - o.y()) / self.zoom
            self.zoom = zoom
            base = QPoint((self.width() - W * zoom) // 2, (self.height() - H * zoom) // 2)
            self._pan = QPoint(round(anchor.x() - fx * zoom) - base.x(),
                               round(anchor.y() - fy * zoom) - base.y())
        else:
            self.zoom = zoom
            if auto:
                self._pan = QPoint(0, 0)
        self._clamp_pan()
        self.zoomChanged.emit(self.zoom)
        self.update()

    def zoom_in(self) -> None:
        self.set_zoom(self.zoom + 1)

    def zoom_out(self) -> None:
        self.set_zoom(self.zoom - 1)

    def zoom_fit(self) -> None:
        self.set_zoom(self.fit_zoom(), auto=True)

    def is_auto_fit(self) -> bool:
        return self._auto_fit

    def resizeEvent(self, event) -> None:
        if self._auto_fit:
            z = self.fit_zoom()
            if z != self.zoom:
                self.zoom = z
                self.zoomChanged.emit(z)
            self._pan = QPoint(0, 0)
        else:
            self._clamp_pan()
        super().resizeEvent(event)

    # ------------------------------------------------------------------
    # Frame / option setters
    # ------------------------------------------------------------------
    def set_frame(self, pixels: np.ndarray, history: fc.History | None = None,
                  onion: np.ndarray | None = None) -> None:
        """Show another frame (no undo entry, no ``changed`` signal). Call
        ``finish_interaction`` first if a drag might be in progress."""
        self._stroke_base = None
        self._sel_mode = None
        self._clear_selection(emit=True)
        self.pixels = np.asarray(pixels, dtype=bool)
        self.history = history if history is not None else fc.History()
        self.onion = onion
        self._invalidate()
        self.historyChanged.emit()

    def set_onion(self, onion: np.ndarray | None) -> None:
        self.onion = onion
        self._invalidate()

    def set_tool(self, tool: str) -> None:
        if tool not in TOOLS or tool == self.tool:
            return
        self._abort_interaction()
        if self.tool == "select":
            self._clear_selection(emit=True)
        self.tool = tool
        self._refresh_ghost()
        self._update_cursor()
        self.update()

    def set_brush_size(self, size: int) -> None:
        self.brush_size = max(1, min(fc.MAX_BRUSH, int(size)))
        self.update()

    def set_mirror(self, mirror_x: bool | None = None, mirror_y: bool | None = None) -> None:
        if mirror_x is not None:
            self.mirror_x = bool(mirror_x)
        if mirror_y is not None:
            self.mirror_y = bool(mirror_y)
        self.update()

    def set_grid_visible(self, visible: bool) -> None:
        self.show_grid = bool(visible)
        self.update()

    def set_text_options(self, text: str, family: str, size: int, bold: bool = False) -> None:
        self._text, self._text_family = text, family
        self._text_size, self._text_bold = int(size), bool(bold)
        if text:
            self._text_mask, self._text_baseline = fr.render_text_mask(text, family, size, bold)
        else:
            self._text_mask = None
        self._refresh_ghost()

    # -- legacy QImage view of the frame (trailer generator pokes this) ----
    @property
    def image(self) -> QImage:
        return fr.array_to_image(self.pixels)

    @image.setter
    def image(self, img: QImage) -> None:
        self.pixels = fr.image_to_array(img)
        self._invalidate()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _invalidate(self) -> None:
        self._display = None
        self.update()

    def _emit_changed(self) -> None:
        self._invalidate()
        self.changed.emit()

    def _sym(self, mask: np.ndarray) -> np.ndarray:
        return fc.symmetrize(mask, self.mirror_x, self.mirror_y)

    def _push_undo(self, before: np.ndarray | None = None) -> None:
        self.history.push(self.pixels if before is None else before)
        self.historyChanged.emit()

    def _commit(self) -> None:
        self.committed.emit()

    def _abort_interaction(self) -> None:
        if self._stroke_base is not None:
            self.pixels = self._stroke_base
            self._stroke_base = None
            self._emit_changed()
        self._sel_mode = None
        self._panning = False

    def in_stroke(self) -> bool:
        return self._stroke_base is not None

    def finish_interaction(self) -> None:
        """Commit whatever drag is in progress (called before the frame or
        document underneath the canvas changes)."""
        if self._stroke_base is not None or self._sel_mode is not None:
            self.end_stroke()
        self._panning = False

    # ------------------------------------------------------------------
    # Drawing (pixel coordinates)
    # ------------------------------------------------------------------
    def _shape_mask(self, x0: int, y0: int, x1: int, y1: int, constrain: bool) -> np.ndarray:
        t = self.tool
        if constrain:
            x1, y1 = (fc.constrain_line if t == "line" else fc.constrain_square)(x0, y0, x1, y1)
        if t == "line":
            return fc.line_mask(x0, y0, x1, y1, self.brush_size)
        if t == "rect":
            return fc.rect_mask(x0, y0, x1, y1, False, self.brush_size)
        if t == "rect_fill":
            return fc.rect_mask(x0, y0, x1, y1, True)
        if t == "ellipse":
            return fc.ellipse_mask(x0, y0, x1, y1, False, self.brush_size)
        return fc.ellipse_mask(x0, y0, x1, y1, True)

    def begin_at(self, x: int, y: int, erase: bool = False, constrain: bool = False,
                 duplicate: bool = False) -> None:
        """Start an edit at pixel (x, y). ``erase`` is the right button,
        ``constrain`` is Shift, ``duplicate`` is Ctrl (selection drag)."""
        tool = self.tool
        if tool == "select":
            self._select_begin(x, y, duplicate)
            return
        value = not erase
        if tool == "eraser":
            value = False
        if tool == "text":
            self.stamp_text(x, y, value)
            return
        if tool == "fill":
            self.fill_at(x, y, value)
            return
        self._stroke_base = self.pixels
        self._stroke_value = value
        self.pixels = self.pixels.copy()
        if tool in ("pencil", "eraser"):
            x, y = self._clamp(x, y)
            self.pixels[self._sym(fc.dot_mask(x, y, self.brush_size))] = value
        else:
            self.pixels[self._sym(self._shape_mask(x, y, x, y, False))] = value
        self._stroke_start = self._stroke_last = (x, y)
        self._emit_changed()

    def drag_to(self, x: int, y: int, constrain: bool = False) -> None:
        if self.tool == "select":
            self._select_drag(x, y)
            return
        if self._stroke_base is None:
            return
        if self.tool in ("pencil", "eraser"):
            x, y = self._clamp(x, y)
            lx, ly = self._stroke_last
            if (x, y) == (lx, ly):
                return
            if constrain:
                # Shift on a freehand stroke: straight line from the press point.
                sx, sy = self._stroke_start
                x, y = self._clamp(*fc.constrain_line(sx, sy, x, y))
                self.pixels = self._stroke_base.copy()
                mask = fc.line_mask(sx, sy, x, y, self.brush_size)
            else:
                mask = fc.line_mask(lx, ly, x, y, self.brush_size)
            self.pixels[self._sym(mask)] = self._stroke_value
            self._stroke_last = (x, y)
        else:
            sx, sy = self._stroke_start
            mask = self._sym(self._shape_mask(sx, sy, x, y, constrain))
            self.pixels = fc.apply_mask(self._stroke_base, mask, self._stroke_value)
            self._stroke_last = (x, y)
        self._emit_changed()

    def end_stroke(self) -> None:
        if self.tool == "select":
            self._select_end()
            return
        base = self._stroke_base
        if base is None:
            return
        self._stroke_base = None
        if not np.array_equal(base, self.pixels):
            self._push_undo(base)
            self._commit()

    def cancel_stroke(self) -> bool:
        if self._stroke_base is None:
            return False
        self._abort_interaction()
        return True

    def simulate_stroke(self, points, erase: bool = False, constrain: bool = False) -> None:
        """Programmatic press-drag-release through pixel points."""
        pts = list(points)
        if not pts:
            return
        self.begin_at(*pts[0], erase=erase, constrain=constrain)
        for p in pts[1:]:
            self.drag_to(*p, constrain=constrain)
        self.end_stroke()

    @staticmethod
    def _clamp(x: int, y: int) -> tuple[int, int]:
        return max(0, min(W - 1, x)), max(0, min(H - 1, y))

    def fill_at(self, x: int, y: int, value: bool = True) -> None:
        if not (0 <= x < W and 0 <= y < H):
            return
        mask = fc.empty_mask()
        for px, py in fc.mirror_points(x, y, self.mirror_x, self.mirror_y):
            if bool(self.pixels[py, px]) != value and not mask[py, px]:
                mask |= fc.flood_mask(self.pixels, px, py)
        if not mask.any():
            return
        self._push_undo()
        self.pixels = fc.apply_mask(self.pixels, mask, value)
        self._emit_changed()
        self._commit()

    def stamp_text(self, x: int, y: int, value: bool = True) -> None:
        if self._text_mask is None or self._text_mask.size == 0:
            return
        mask = fr.text_stamp_mask(self._text_mask, self._text_baseline, x, y)
        if not mask.any():
            return
        self._push_undo()
        self.pixels = fc.apply_mask(self.pixels, mask, value)
        self._emit_changed()
        self._commit()

    # ------------------------------------------------------------------
    # Whole-image / selection-aware operations
    # ------------------------------------------------------------------
    def set_pixels(self, arr: np.ndarray, undoable: bool = True) -> None:
        """Replace the whole frame (import, paste-as-frame)."""
        self._abort_interaction()
        self._clear_selection(emit=True)
        arr = np.asarray(arr, dtype=bool)
        if np.array_equal(arr, self.pixels):
            return
        if undoable:
            self._push_undo()
        self.pixels = arr.copy()
        self._emit_changed()
        self._commit()

    def _apply(self, fn) -> None:
        """Run ``fn(array) -> array`` on the selection if there is one,
        otherwise on the whole frame, as a single undo step."""
        self._abort_interaction()
        if self._float is not None:
            self._float = fn(self._float)
            self._recomposite()
            self._commit()
            return
        if self.selection is not None:
            r = self._sel_clipped()
            if r is None:
                return
            x0, y0, x1, y1 = r
            new = self.pixels.copy()
            new[y0:y1, x0:x1] = fn(self.pixels[y0:y1, x0:x1])
        else:
            new = fn(self.pixels)
        if np.array_equal(new, self.pixels):
            return
        self._push_undo()
        self.pixels = new
        self._emit_changed()
        self._commit()

    def clear_all(self) -> None:
        self._apply(lambda a: np.zeros_like(a))

    def invert(self) -> None:
        self._apply(lambda a: ~a)

    def flip_horizontal(self) -> None:
        self._apply(lambda a: a[:, ::-1].copy())

    def flip_vertical(self) -> None:
        self._apply(lambda a: a[::-1, :].copy())

    def shift(self, dx: int, dy: int) -> None:
        """Nudge by whole pixels: moves the selection if there is one,
        otherwise rolls the entire frame with wrap-around."""
        if self.selection is not None:
            self.nudge_selection(dx, dy)
            return
        self._abort_interaction()
        self._push_undo()
        self.pixels = fc.shift(self.pixels, dx, dy, wrap=True)
        self._emit_changed()
        self._commit()

    def center_content(self) -> None:
        self._abort_interaction()
        self._clear_selection(emit=True)
        new = fc.center_content(self.pixels)
        if np.array_equal(new, self.pixels):
            return
        self._push_undo()
        self.pixels = new
        self._emit_changed()
        self._commit()

    # legacy names
    clear = clear_all

    def mirror(self, horizontal: bool) -> None:
        self.flip_horizontal() if horizontal else self.flip_vertical()

    # ------------------------------------------------------------------
    # Undo / redo
    # ------------------------------------------------------------------
    def can_undo(self) -> bool:
        return self.history.can_undo()

    def can_redo(self) -> bool:
        return self.history.can_redo()

    def undo(self) -> None:
        if self.cancel_stroke():
            return
        self._clear_selection(emit=True)
        prev = self.history.undo(self.pixels)
        if prev is None:
            return
        self.pixels = prev
        self.historyChanged.emit()
        self._emit_changed()
        self._commit()

    def redo(self) -> None:
        self._abort_interaction()
        self._clear_selection(emit=True)
        nxt = self.history.redo(self.pixels)
        if nxt is None:
            return
        self.pixels = nxt
        self.historyChanged.emit()
        self._emit_changed()
        self._commit()

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def has_selection(self) -> bool:
        return self.selection is not None

    def is_floating(self) -> bool:
        return self._float is not None

    def _sel_clipped(self) -> tuple[int, int, int, int] | None:
        """Selection as half-open (x0, y0, x1, y1) clipped to the canvas."""
        if self.selection is None:
            return None
        r = self.selection.intersected(QRect(0, 0, W, H))
        if r.isEmpty():
            return None
        return r.left(), r.top(), r.left() + r.width(), r.top() + r.height()

    def _set_selection(self, rect: QRect | None) -> None:
        self.selection = rect
        if rect is not None and not self._ants_timer.isActive():
            self._ants_timer.start()
        elif rect is None:
            self._ants_timer.stop()
        self.selectionChanged.emit()
        self._update_cursor()
        self.update()

    def _clear_selection(self, emit: bool = False) -> None:
        """Drop the marquee; floating pixels stay where they were put."""
        had = self.selection is not None
        self._float = None
        self._float_base = None
        self._sel_mode = None
        self.selection = None
        self._ants_timer.stop()
        if had:
            if emit:
                self.selectionChanged.emit()
            self._update_cursor()
            self.update()

    def deselect(self) -> bool:
        if self.selection is None:
            return False
        self._clear_selection(emit=True)
        return True

    def select_all(self) -> None:
        self._abort_interaction()
        self._float = None
        self._float_base = None
        self._set_selection(QRect(0, 0, W, H))

    def select_rect(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """Select the box spanning both corners inclusive."""
        self._float = None
        self._float_base = None
        r = QRect(QPoint(min(x0, x1), min(y0, y1)), QPoint(max(x0, x1), max(y0, y1)))
        self._set_selection(r.intersected(QRect(0, 0, W, H)))

    def _lift(self, duplicate: bool = False) -> bool:
        """Turn the selected region into a floating layer."""
        if self._float is not None:
            return True
        r = self._sel_clipped()
        if r is None:
            return False
        x0, y0, x1, y1 = r
        self._push_undo()
        self._float = self.pixels[y0:y1, x0:x1].copy()
        base = self.pixels.copy()
        if not duplicate:
            base[y0:y1, x0:x1] = False
        self._float_base = base
        self.selection = QRect(x0, y0, x1 - x0, y1 - y0)
        return True

    def _recomposite(self) -> None:
        if self._float is None or self._float_base is None or self.selection is None:
            return
        self.pixels = fc.paste(self._float_base, self._float,
                               self.selection.left(), self.selection.top())
        self._emit_changed()

    def _select_begin(self, x: int, y: int, duplicate: bool) -> None:
        if self.selection is not None and self.selection.contains(x, y):
            if duplicate and self._float is not None:
                # Ctrl-drag an already floating piece: stamp it, keep a copy.
                self._float_base = self.pixels.copy()
            elif not self._lift(duplicate):
                return
            self._sel_mode = "move"
            self._sel_anchor = (x - self.selection.left(), y - self.selection.top())
            self._sel_moved = False
            self._update_cursor()
            return
        self._float = None
        self._float_base = None
        x, y = self._clamp(x, y)
        self._sel_mode = "marquee"
        self._sel_anchor = (x, y)
        self._sel_moved = False
        self._set_selection(None)

    def _select_drag(self, x: int, y: int) -> None:
        if self._sel_mode == "marquee":
            x, y = self._clamp(x, y)
            ax, ay = self._sel_anchor
            if (x, y) != (ax, ay):
                self._sel_moved = True
            if self._sel_moved:
                self._set_selection(QRect(QPoint(min(ax, x), min(ay, y)),
                                          QPoint(max(ax, x), max(ay, y))))
        elif self._sel_mode == "move" and self.selection is not None:
            ax, ay = self._sel_anchor
            nx, ny = x - ax, y - ay
            if (nx, ny) != (self.selection.left(), self.selection.top()):
                self._sel_moved = True
                self.selection.moveTo(nx, ny)
                self._recomposite()

    def _select_end(self) -> None:
        mode, self._sel_mode = self._sel_mode, None
        if mode == "move":
            self._commit()
        self._update_cursor()

    def nudge_selection(self, dx: int, dy: int) -> None:
        if self.selection is None or not self._lift():
            return
        self.selection.translate(dx, dy)
        self._recomposite()
        self._commit()

    def selection_array(self) -> np.ndarray | None:
        if self._float is not None:
            return self._float.copy()
        r = self._sel_clipped()
        if r is None:
            return None
        x0, y0, x1, y1 = r
        return self.pixels[y0:y1, x0:x1].copy()

    def copy_selection(self) -> bool:
        """Copy the selection (or the whole frame if nothing is selected)."""
        global _clipboard
        arr = self.selection_array()
        if arr is None:
            arr = self.pixels.copy()
        _clipboard = arr
        try:
            h, w = arr.shape
            mime = QMimeData()
            mime.setImageData(fr.indexed_image(arr.astype(np.uint8),
                                               [0xFF000000, 0xFFFFFFFF]))
            mime.setData(CLIP_MIME, bytes([w, h]) + np.packbits(arr, axis=None).tobytes())
            QGuiApplication.clipboard().setMimeData(mime)
        except Exception:  # clipboard can be locked by another app
            pass
        return True

    def cut_selection(self) -> bool:
        if self.selection is None:
            return False
        self.copy_selection()
        return self.delete_selection()

    def delete_selection(self) -> bool:
        if self.selection is None:
            return False
        if self._float is not None:
            self.pixels = self._float_base
            self._clear_selection(emit=True)
            self._emit_changed()
            self._commit()
            return True
        r = self._sel_clipped()
        self._clear_selection(emit=True)
        if r is None:
            return False
        x0, y0, x1, y1 = r
        if self.pixels[y0:y1, x0:x1].any():
            self._push_undo()
            self.pixels = self.pixels.copy()
            self.pixels[y0:y1, x0:x1] = False
            self._emit_changed()
            self._commit()
        return True

    def paste_array(self, arr: np.ndarray, pos: tuple[int, int] | None = None) -> None:
        """Paste as a floating selection that can be dragged into place."""
        arr = np.asarray(arr, dtype=bool)
        if arr.ndim != 2 or arr.size == 0:
            return
        arr = arr[:H, :W]
        self._abort_interaction()
        self._clear_selection()
        ah, aw = arr.shape
        if pos is None:
            if self._hover is not None and (aw, ah) != (W, H):
                pos = (self._hover[0] - aw // 2, self._hover[1] - ah // 2)
            else:
                pos = ((W - aw) // 2, (H - ah) // 2)
        self.toolRequested.emit("select")
        self.tool = "select"
        self._refresh_ghost()
        self._push_undo()
        self._float = arr.copy()
        self._float_base = self.pixels.copy()
        self._set_selection(QRect(pos[0], pos[1], aw, ah))
        self._recomposite()
        self._commit()

    def _advance_ants(self) -> None:
        self._ants_phase = (self._ants_phase + 1) % 8
        if self.selection is not None:
            self.update()

    # ------------------------------------------------------------------
    # Hover / ghost
    # ------------------------------------------------------------------
    def _set_hover(self, hover: tuple[int, int] | None) -> None:
        if hover == self._hover:
            return
        self._hover = hover
        self.hoverChanged.emit(*(hover if hover is not None else (-1, -1)))
        self._refresh_ghost()
        self._update_cursor()
        self.update()

    def _refresh_ghost(self) -> None:
        ghost = None
        if (self.tool == "text" and self._hover is not None
                and self._text_mask is not None and self._text_mask.size):
            ghost = fr.text_stamp_mask(self._text_mask, self._text_baseline, *self._hover)
        if ghost is None and self._ghost is None:
            return
        self._ghost = ghost
        self._invalidate()

    def _update_cursor(self) -> None:
        if self._panning:
            self.setCursor(Qt.ClosedHandCursor)
        elif self._space_down:
            self.setCursor(Qt.OpenHandCursor)
        elif (self.tool == "select" and self.selection is not None and self._hover is not None
              and self.selection.contains(*self._hover)):
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.setCursor(Qt.CrossCursor)

    # ------------------------------------------------------------------
    # Qt events
    # ------------------------------------------------------------------
    def set_space_pan(self, down: bool) -> None:
        self._space_down = down
        self._update_cursor()

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.MouseFocusReason)
        pos = event.position().toPoint()
        if event.button() == Qt.MiddleButton or (event.button() == Qt.LeftButton and self._space_down):
            self._panning = True
            self._pan_origin = pos
            self._pan_start = QPoint(self._pan)
            self._update_cursor()
            return
        if event.button() not in (Qt.LeftButton, Qt.RightButton):
            return
        if self._stroke_base is not None or self._sel_mode is not None:
            # second button during a drag cancels it (classic paint behaviour)
            if self._stroke_base is not None:
                self.cancel_stroke()
            return
        if self.tool == "select" and event.button() == Qt.RightButton:
            return
        x, y = self.widget_to_pixel(pos)
        mods = event.modifiers()
        self.begin_at(x, y, erase=event.button() == Qt.RightButton,
                      constrain=bool(mods & Qt.ShiftModifier),
                      duplicate=bool(mods & Qt.ControlModifier))

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        if self._panning:
            self._pan = self._pan_start + (pos - self._pan_origin)
            self._auto_fit = False
            self._clamp_pan()
            self.update()
            return
        x, y = self.widget_to_pixel(pos)
        inside = 0 <= x < W and 0 <= y < H
        if self._stroke_base is not None or self._sel_mode is not None:
            self.drag_to(x, y, constrain=bool(event.modifiers() & Qt.ShiftModifier))
            self._set_hover(self._clamp(x, y))
        else:
            self._set_hover((x, y) if inside else None)

    def mouseReleaseEvent(self, event) -> None:
        if self._panning and event.button() in (Qt.MiddleButton, Qt.LeftButton):
            self._panning = False
            self._update_cursor()
            return
        if event.button() in (Qt.LeftButton, Qt.RightButton):
            self.end_stroke()

    def leaveEvent(self, event) -> None:
        if self._stroke_base is None and self._sel_mode is None:
            self._set_hover(None)
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta()
        if event.modifiers() & Qt.ControlModifier:
            step = 1 if delta.y() > 0 else -1 if delta.y() < 0 else 0
            if step:
                self.set_zoom(self.zoom + step, anchor=event.position().toPoint())
            event.accept()
            return
        # Plain wheel scrolls, but only along axes where the panel overflows.
        before = QPoint(self._pan)
        can_x = W * self.zoom > self.width() - 2 * _MARGIN
        can_y = H * self.zoom > self.height() - 2 * _MARGIN
        if event.modifiers() & Qt.ShiftModifier:
            dx, dy = delta.y() // 2 or delta.x() // 2, 0
        else:
            dx, dy = delta.x() // 2, delta.y() // 2
        self._pan += QPoint(dx if can_x else 0, dy if can_y else 0)
        self._clamp_pan()
        if self._pan != before:
            self._auto_fit = False
            self.update()
            event.accept()
        else:
            event.ignore()

    def handle_key(self, event) -> bool:
        """Canvas keyboard commands; returns True when the key was used.
        The dialog forwards unhandled keys here so they work wherever the
        focus is (except inside text fields)."""
        key, mods = event.key(), event.modifiers()
        if key == Qt.Key_Space:
            if not event.isAutoRepeat():
                self.set_space_pan(True)
            return True
        if key == Qt.Key_Escape:
            return self.cancel_stroke() or self.deselect()
        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            return self.delete_selection()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            return self.deselect()
        arrows = {Qt.Key_Left: (-1, 0), Qt.Key_Right: (1, 0), Qt.Key_Up: (0, -1), Qt.Key_Down: (0, 1)}
        if key in arrows:
            dx, dy = arrows[key]
            if mods & Qt.AltModifier:
                # Alt+arrows always roll the whole frame.
                self._clear_selection(emit=True)
                self.shift(dx, dy)
                return True
            if self.selection is not None:
                step = 8 if mods & Qt.ShiftModifier else 1
                self.nudge_selection(dx * step, dy * step)
                return True
        return False

    def keyPressEvent(self, event) -> None:
        if not self.handle_key(event):
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self.set_space_pan(False)
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:
        self.set_space_pan(False)
        super().focusOutEvent(event)

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def _ensure_display(self) -> QImage:
        if self._display is None:
            self._display = fr.oled_image(self.pixels, self.onion, self._ghost)
        return self._display

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(BG_SUNKEN))
        z = self.zoom
        panel = self.panel_rect()

        # bezel
        p.setPen(QPen(QColor(BORDER_STRONG), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(panel.adjusted(-1, -1, 0, 0))

        p.drawImage(panel, self._ensure_display())

        p.setClipRect(panel.adjusted(-1, -1, 1, 1))
        ox, oy = panel.left(), panel.top()

        # pixel grid fades in with zoom
        if self.show_grid and z >= 4:
            strength = min(1.0, (z - 3) / 6.0)
            minor = QColor(110, 140, 160, int(46 * strength))
            major = QColor(110, 140, 160, int(92 * strength))
            vis = event.rect().intersected(panel)
            x_lo = max(0, (vis.left() - ox) // z)
            x_hi = min(W, (vis.right() - ox) // z + 1)
            y_lo = max(0, (vis.top() - oy) // z)
            y_hi = min(H, (vis.bottom() - oy) // z + 1)
            top, bottom = oy + y_lo * z, oy + y_hi * z
            left, right = ox + x_lo * z, ox + x_hi * z
            for pen_color, pick in ((minor, lambda i: i % 8 != 0), (major, lambda i: i % 8 == 0)):
                p.setPen(QPen(pen_color, 1))
                for gx in range(x_lo, x_hi + 1):
                    if pick(gx):
                        p.drawLine(ox + gx * z, top, ox + gx * z, bottom)
                for gy in range(y_lo, y_hi + 1):
                    if pick(gy):
                        p.drawLine(left, oy + gy * z, right, oy + gy * z)

        # symmetry axes
        if self.mirror_x or self.mirror_y:
            pen = QPen(QColor(ACCENT), 1, Qt.DashLine)
            pen.setDashPattern([4, 4])
            p.setPen(pen)
            if self.mirror_x:
                p.drawLine(ox + (W // 2) * z, oy, ox + (W // 2) * z, oy + H * z)
            if self.mirror_y:
                p.drawLine(ox, oy + (H // 2) * z, ox + W * z, oy + (H // 2) * z)

        # hover footprint
        if self._hover is not None and not self._panning:
            self._paint_hover(p, ox, oy, z)

        # selection marquee (marching ants)
        if self.selection is not None:
            s = self.selection
            r = QRect(ox + s.left() * z, oy + s.top() * z, s.width() * z, s.height() * z)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(0, 0, 0), 1))
            p.drawRect(r.adjusted(0, 0, -1, -1))
            pen = QPen(QColor(SELECTION), 1, Qt.CustomDashLine)
            pen.setDashPattern([4, 4])
            pen.setDashOffset(self._ants_phase)
            p.setPen(pen)
            p.drawRect(r.adjusted(0, 0, -1, -1))
        p.end()

    def _paint_hover(self, p: QPainter, ox: int, oy: int, z: int) -> None:
        hx, hy = self._hover
        tool = self.tool
        size = self.brush_size if tool in BRUSH_TOOLS else 1
        if tool == "select" and self.selection is not None and self.selection.contains(hx, hy):
            return
        bx0, by0 = fc.brush_origin(hx, hy, size)
        spots = [(bx0, by0, 255)]
        if tool not in ("select", "text"):
            # reflected footprints, so the user sees where the twin lands
            rx, ry = W - bx0 - size, H - by0 - size
            if self.mirror_x:
                spots.append((rx, by0, 110))
            if self.mirror_y:
                spots.append((bx0, ry, 110))
            if self.mirror_x and self.mirror_y:
                spots.append((rx, ry, 110))
        for bx, by, alpha in spots:
            r = QRectF(ox + bx * z + 0.5, oy + by * z + 0.5, size * z - 1, size * z - 1)
            c = QColor(ACCENT)
            c.setAlpha(alpha)
            p.setPen(QPen(c, 1))
            fill = QColor(ACCENT)
            fill.setAlpha(40 if alpha == 255 else 18)
            p.setBrush(fill)
            p.drawRect(r)
