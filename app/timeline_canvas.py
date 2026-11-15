"""Custom-painted timeline surface: ruler, frame blocks, face lane, optional
per-servo tracks and the playhead, with its own zoom/scroll model.

Why one widget instead of a QGraphicsScene: every edit used to rebuild
hundreds of items. Here the static picture is rendered once into a pixmap
(only the visible frames are drawn) and re-rendered only when data, zoom,
scroll or selection change; playhead movement, hover and drag feedback are
cheap overlays painted on top of that pixmap, so playback touches no Qt
objects at all beyond a partial repaint.

The canvas owns the VIEW (zoom, scroll, hover, drags). The DATA (frames,
selection, playhead time, undo) belongs to TimelineWidget, which this class
calls back into for every edit.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right

from PySide6.QtCore import Qt, QEvent, QPoint, QPointF, QRectF, QSize, QTimer
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
    QStaticText, QTransform,
)
from PySide6.QtWidgets import QToolTip, QWidget

from .servo_model import SERVO_COLORS, SERVO_ORDER, DEFAULT_ANGLE
from .theme import (
    ACCENT, ACCENT_SOFT, BG_ELEVATED, BG_SUNKEN, BORDER, BORDER_STRONG, DANGER,
    FG, FG_DIM, FG_MUTED, FONT_FAMILY, FONT_MONO, PANEL, RADIUS_SM, SELECTION,
)
from .timeline_model import (
    TimeMap, PPM_DEFAULT, changed_mask, clamp_ppm, effective_faces, fit_ppm,
    format_tick, frame_duration, minor_divisions, tick_step,
)

# -- geometry (logical px) -----------------------------------------------
GUTTER_W = 44            # fixed lane-label column on the left
PAD_L = 10               # breathing room before t=0
END_PAD = 96             # room after the last frame for the "+" block
RULER_H = 26
BLOCK_Y = RULER_H + 6
BLOCK_H = 54
FACE_Y = BLOCK_Y + BLOCK_H + 5
FACE_H = 18
BASE_H = FACE_Y + FACE_H + 6      # canvas height with the servo tracks collapsed
TRACK_MIN_H = 11
TRACK_MAX_H = 22
TRACK_PAD = 2.5
ADD_W = 30
ADD_GAP = 8
DRAG_THRESHOLD = 6
SNAP_MS = 10
N_SERVOS = len(SERVO_ORDER)


def _mix(a, b, t: float) -> QColor:
    a, b = QColor(a), QColor(b)
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


def _alpha(c, a: int) -> QColor:
    c = QColor(c)
    c.setAlpha(a)
    return c


class _Drag:
    __slots__ = ("mode", "index", "press_x", "press_cx", "orig", "gap", "collapse",
                 "value", "last_x", "last_y", "shift")

    def __init__(self, mode, index=-1, press_x=0.0, press_cx=0.0, orig=None, collapse=False):
        self.mode = mode          # "scrub" | "retime" | "move_armed" | "move" | "pan"
        self.index = index
        self.press_x = press_x
        self.press_cx = press_cx
        self.orig = orig
        self.gap = -1
        self.collapse = collapse
        self.value = 0
        self.last_x = press_x
        self.last_y = 0.0
        self.shift = False


class TimelineCanvas(QWidget):
    def __init__(self, timeline, hbar, parent=None):
        super().__init__(parent)
        self.tl = timeline
        self.hbar = hbar
        self.tm = TimeMap()
        self.ppm = PPM_DEFAULT
        self.scroll_x = 0.0
        self.show_tracks = True

        self._frames = None            # live list of the shown sequence (None = no sequence)
        self._read_only = False
        self._masks: list[int] = []    # per frame: bitmask of servos that changed
        self._eff: list = []           # per frame: (face, mode) on screen
        self._track_pts: list[list] = [[] for _ in range(N_SERVOS)]
        self._tracks_dirty = True

        self._cache: QPixmap | None = None
        self._cache_dirty = True
        self._dirty_span = None        # (x0, x1) strip of the cache to re-render
        self._num_text: dict = {}      # frame number -> (QStaticText, width)
        self._dur_text: dict = {}      # label -> (QStaticText, width)
        self._hover = (None, -1)
        self._drag: _Drag | None = None
        self._ph_ms = 0.0
        self._ph_drawn_x = -100
        self._cur_idx = -1
        self._auto_fit = True          # refit on resize until the user zooms by hand
        self._tracks_y = BASE_H
        self._track_h = TRACK_MIN_H
        self._elide_cache: dict = {}

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setObjectName("TimelineCanvas")

        self._autoscroll = QTimer(self)
        self._autoscroll.setInterval(30)
        self._autoscroll.timeout.connect(self._autoscroll_tick)

        self._init_style()
        self._apply_height()
        hbar.valueChanged.connect(self._on_hbar)

    # -- style objects (built once; painting allocates nothing per frame) ----
    def _init_style(self) -> None:
        self.c_bg = QColor(BG_SUNKEN)
        self.c_ruler = QColor(BG_ELEVATED)
        self.c_gutter = QColor(BG_ELEVATED)
        self.c_border = QColor(BORDER)
        self.c_border_strong = QColor(BORDER_STRONG)
        self.c_fg = QColor(FG)
        self.c_dim = QColor(FG_DIM)
        self.c_muted = QColor(FG_MUTED)
        self.c_accent = QColor(ACCENT)
        self.c_selection = QColor(SELECTION)
        self.c_playhead = _mix(DANGER, FG, 0.10)
        self.c_grid = _alpha(BORDER_STRONG, 70)
        self.c_lane = _mix(BG_SUNKEN, BG_ELEVATED, 0.55)
        self.c_track_alt = _mix(BG_SUNKEN, BG_ELEVATED, 0.45)
        self.c_block = _mix(PANEL, FG, 0.07)
        self.c_block_ro = _mix(BG_ELEVATED, PANEL, 0.6)
        self.c_sliver = _mix(PANEL, FG, 0.30)
        self.c_sel_fill = _mix(ACCENT_SOFT, ACCENT, 0.16)
        self.c_multi_fill = _mix(PANEL, SELECTION, 0.24)
        self.c_bar_base = _alpha(FG, 26)
        self.c_face = _mix(BG_ELEVATED, SELECTION, 0.34)
        self.c_face_border = _mix(BG_ELEVATED, SELECTION, 0.62)
        self.c_face_line = _mix(BG_SUNKEN, SELECTION, 0.42)
        self.c_face_text = _mix(SELECTION, FG, 0.72)
        self.c_dim_overlay = _alpha(BG_SUNKEN, 165)
        self.c_bubble = QColor(BG_ELEVATED)
        self.c_hover_fill = _alpha(FG, 12)
        self.c_focus = _alpha(ACCENT, 105)
        self.c_add_hover = _alpha(ACCENT, 28)

        self.p_block = QPen(self.c_border_strong, 1)
        self.p_block_ro = QPen(self.c_border, 1)
        self.p_sel = QPen(self.c_accent, 1.6)
        self.p_multi = QPen(self.c_selection, 1.3)
        self.p_hover = QPen(_mix(BORDER_STRONG, FG, 0.45), 1)
        self.p_add = QPen(self.c_border_strong, 1, Qt.DashLine)
        self.p_add_hover = QPen(self.c_accent, 1.2, Qt.DashLine)
        self.p_bubble = QPen(self.c_accent, 1)

        self.c_servo = [QColor(SERVO_COLORS[s]) for s in SERVO_ORDER]
        self.c_servo_dim = [_alpha(SERVO_COLORS[s], 58) for s in SERVO_ORDER]
        self.p_track = [QPen(_mix(BG_SUNKEN, SERVO_COLORS[s], 0.82), 1) for s in SERVO_ORDER]
        for pen in self.p_track + [self.p_block, self.p_block_ro, self.p_hover]:
            pen.setCosmetic(True)

        self.f_num = QFont(FONT_FAMILY)
        self.f_num.setPixelSize(11)
        self.f_num.setBold(True)
        self.f_small = QFont(FONT_MONO)
        self.f_small.setStyleHint(QFont.Monospace)
        self.f_small.setPixelSize(10)
        self.f_label = QFont(FONT_FAMILY)
        self.f_label.setPixelSize(10)
        self.f_gutter = QFont(FONT_FAMILY)
        self.f_gutter.setPixelSize(9)
        self.f_gutter.setBold(True)
        self.f_gutter.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
        self.fm_num = QFontMetrics(self.f_num)
        self.fm_small = QFontMetrics(self.f_small)
        self.fm_label = QFontMetrics(self.f_label)
        self.fm_gutter = QFontMetrics(self.f_gutter)

        d = 3.5
        self._diamond = QPainterPath()
        self._diamond.moveTo(0, -d)
        self._diamond.lineTo(d, 0)
        self._diamond.lineTo(0, d)
        self._diamond.lineTo(-d, 0)
        self._diamond.closeSubpath()
        self._head = QPolygonF([QPointF(-5.5, 0), QPointF(5.5, 0), QPointF(5.5, 6),
                                QPointF(0, 12), QPointF(-5.5, 6)])

    # ======================================================================
    # data hooks (called by TimelineWidget)
    # ======================================================================
    def set_sequence(self, frames, read_only: bool) -> None:
        self.cancel_drag()
        self._frames = frames
        self._read_only = read_only
        self._hover = (None, -1)
        self._cur_idx = -1
        self.relayout()

    def relayout(self, delays_only: bool = False) -> None:
        """Rebuild the time map (and, unless only delays changed, the derived
        per-frame data). O(n) arithmetic -- used for structural/timing/zoom
        changes, never for angle edits."""
        frames = self._frames or ()
        self.tm.rebuild(frames, self.ppm)
        if not delays_only or len(self._masks) != len(frames):
            prev = None
            masks = []
            for f in frames:
                masks.append(changed_mask(prev, f.angles, SERVO_ORDER))
                prev = f.angles
            self._masks = masks
            self._eff = effective_faces(frames)
        self._tracks_dirty = True
        self._sync_scrollbar()
        self.invalidate()

    def frame_angles_changed(self, i: int) -> None:
        """O(1): only frame i's mask/vertices (and the next frame's mask)."""
        frames = self._frames
        if not frames or not (0 <= i < len(frames)) or len(self._masks) != len(frames):
            self.relayout()
            return
        self._masks[i] = changed_mask(frames[i - 1].angles if i else None,
                                      frames[i].angles, SERVO_ORDER)
        if i + 1 < len(frames):
            self._masks[i + 1] = changed_mask(frames[i].angles, frames[i + 1].angles, SERVO_ORDER)
        if not self._tracks_dirty and len(self._track_pts[0]) == len(frames) + 1:
            ang = frames[i].angles
            x = self.tm.xs[i + 1]
            for k, s in enumerate(SERVO_ORDER):
                y = self._track_y(k, ang.get(s, DEFAULT_ANGLE))
                self._track_pts[k][i + 1] = QPointF(x, y)
                if i == 0:
                    self._track_pts[k][0] = QPointF(0.0, y)
        else:
            self._tracks_dirty = True
        # block i, the next block's "changed" highlights, and the track
        # segments on either side of key i
        self.invalidate_frames(i, i + 1)

    def frame_fields_changed(self, i: int, delay_changed: bool, face_changed: bool) -> None:
        if face_changed:
            self._eff = effective_faces(self._frames or ())
        if delay_changed:
            self.relayout(delays_only=True)
        else:
            self.invalidate()

    def selection_changed(self) -> None:
        self.invalidate()

    def invalidate(self) -> None:
        """The whole static layer is stale."""
        self._cache_dirty = True
        self._dirty_span = None
        self.update()

    def invalidate_frames(self, i0: int, i1: int) -> None:
        """Only the columns of frames i0..i1 are stale: the next paint
        re-renders just that strip of the static layer (nothing at all if
        the frames are scrolled out of view)."""
        if self._cache_dirty or self._cache is None:
            self.update()
            return
        n = self.tm.n
        if n == 0:
            self.invalidate()
            return
        i0 = max(0, min(n - 1, i0))
        i1 = max(i0, min(n - 1, i1))
        ox = self._ox()
        x0 = max(GUTTER_W, int(ox + self.tm.xs[i0]) - 8)
        x1 = min(self.width(), int(ox + self.tm.xs[i1 + 1]) + 9)
        if x1 <= x0:
            return
        span = self._dirty_span
        if span is not None:
            x0, x1 = min(x0, span[0]), max(x1, span[1])
        self._dirty_span = (x0, x1)
        self.update(x0, 0, x1 - x0, self.height())

    # ======================================================================
    # view model: geometry, zoom, scroll
    # ======================================================================
    def _ox(self) -> float:
        """Widget x of content x = 0."""
        return GUTTER_W + PAD_L - self.scroll_x

    def time_to_widget_x(self, ms: float) -> float:
        return self._ox() + self.tm.time_to_x(ms)

    def widget_x_to_time(self, x: float) -> float:
        return self.tm.x_to_time(x - self._ox())

    def _apply_height(self) -> None:
        if self.show_tracks:
            self.setMinimumHeight(BASE_H + N_SERVOS * TRACK_MIN_H + 4)
            self.setMaximumHeight(BASE_H + N_SERVOS * TRACK_MAX_H + 4)
        else:
            self.setMinimumHeight(BASE_H)
            self.setMaximumHeight(BASE_H)
        self.updateGeometry()
        self._update_track_geometry()

    def sizeHint(self) -> QSize:
        extra = N_SERVOS * 16 + 4 if self.show_tracks else 0
        return QSize(640, BASE_H + extra)

    def minimumSizeHint(self) -> QSize:
        return QSize(GUTTER_W + 160, self.minimumHeight())

    def _update_track_geometry(self) -> None:
        self._tracks_y = BASE_H
        if self.show_tracks:
            avail = self.height() - BASE_H - 4
            h = max(TRACK_MIN_H, min(TRACK_MAX_H, avail // N_SERVOS))
        else:
            h = TRACK_MIN_H
        if h != self._track_h:
            self._track_h = h
        self._tracks_dirty = True

    def set_tracks_visible(self, on: bool) -> None:
        if on == self.show_tracks:
            return
        self.show_tracks = on
        self._apply_height()
        self.invalidate()

    def _track_y(self, k: int, angle: float) -> float:
        h = self._track_h
        return (self._tracks_y + k * h + TRACK_PAD
                + (180.0 - angle) * (h - 2 * TRACK_PAD) / 180.0)

    def _ensure_tracks(self) -> None:
        if not self._tracks_dirty:
            return
        self._tracks_dirty = False
        frames = self._frames or ()
        xs = self.tm.xs
        for k, s in enumerate(SERVO_ORDER):
            pts = []
            if frames:
                pts.append(QPointF(0.0, self._track_y(k, frames[0].angles.get(s, DEFAULT_ANGLE))))
                for i, f in enumerate(frames):
                    pts.append(QPointF(xs[i + 1], self._track_y(k, f.angles.get(s, DEFAULT_ANGLE))))
            self._track_pts[k] = pts

    def _view_w(self) -> float:
        return max(1.0, self.width() - GUTTER_W)

    def _max_scroll(self) -> float:
        return max(0.0, PAD_L + self.tm.total_w + END_PAD - self._view_w())

    def _sync_scrollbar(self) -> None:
        maxv = self._max_scroll()
        if self.scroll_x > maxv:
            self.scroll_x = maxv
        if self.scroll_x < 0:
            self.scroll_x = 0.0
        hb = self.hbar
        hb.blockSignals(True)
        hb.setRange(0, int(round(maxv)))
        hb.setPageStep(int(self._view_w()))
        hb.setSingleStep(48)
        hb.setValue(int(round(self.scroll_x)))
        hb.blockSignals(False)

    def _on_hbar(self, value: int) -> None:
        if abs(value - self.scroll_x) >= 1.0:
            self.scroll_x = float(value)
            self.invalidate()

    def set_scroll(self, x: float) -> None:
        x = max(0.0, min(self._max_scroll(), x))
        if x != self.scroll_x:
            self.scroll_x = x
            self._sync_scrollbar()
            self.invalidate()

    def set_zoom(self, ppm: float, anchor_x: float | None = None) -> None:
        """Zoom keeping the time under widget-x ``anchor_x`` fixed (defaults
        to the playhead if visible, else the view centre)."""
        ppm = clamp_ppm(ppm)
        self._auto_fit = False
        if abs(ppm - self.ppm) < 1e-12:
            return
        if anchor_x is None:
            px = self.time_to_widget_x(self._ph_ms)
            anchor_x = px if GUTTER_W <= px <= self.width() else GUTTER_W + self._view_w() / 2
        t = self.widget_x_to_time(anchor_x)
        self.ppm = ppm
        self.tm.rebuild(self._frames or (), ppm)
        self._tracks_dirty = True
        self.scroll_x = self.tm.time_to_x(t) + GUTTER_W + PAD_L - anchor_x
        self._sync_scrollbar()
        self.invalidate()

    def zoom_by(self, factor: float, anchor_x: float | None = None) -> None:
        self.set_zoom(self.ppm * factor, anchor_x)

    def zoom_fit(self) -> None:
        """Fit the whole sequence in view; stays fitted across resizes until
        the user zooms manually."""
        self._auto_fit = True
        avail = self._view_w() - PAD_L - END_PAD
        frames = self._frames or ()
        ppm = fit_ppm([frame_duration(f) for f in frames], avail)
        self.ppm = clamp_ppm(ppm)
        self.scroll_x = 0.0
        self.tm.rebuild(frames, self.ppm)
        self._tracks_dirty = True
        self._sync_scrollbar()
        self.invalidate()

    def request_fit(self) -> None:
        """Fit now if we have a real size, and keep fitting as the widget gets
        its real size (a widget that was never shown has a placeholder size)."""
        self._auto_fit = True
        if self.width() > GUTTER_W + 200:
            self.zoom_fit()
        else:
            self.relayout(delays_only=True)

    def view_state(self):
        return (self.ppm, self.scroll_x, self._auto_fit)

    def set_view(self, ppm: float, scroll_x: float, auto_fit: bool = False) -> None:
        if auto_fit:
            self.request_fit()
            return
        self._auto_fit = False
        self.ppm = clamp_ppm(ppm)
        self.tm.rebuild(self._frames or (), self.ppm)
        self._tracks_dirty = True
        self.scroll_x = scroll_x
        self._sync_scrollbar()
        self.invalidate()

    def ensure_frame_visible(self, i: int) -> None:
        if not (0 <= i < self.tm.n):
            return
        ox = self._ox()
        x0 = ox + self.tm.xs[i]
        x1 = ox + self.tm.xs[i + 1]
        right = self.width() - 28
        left = GUTTER_W + PAD_L
        if x1 > right:
            shift = min(x1 - right, x0 - left)
            if shift > 0:
                self.set_scroll(self.scroll_x + shift)
        elif x0 < left:
            self.set_scroll(self.scroll_x - (left - x0))

    # -- playhead -----------------------------------------------------------
    def set_playhead(self, ms: float, follow: bool = False) -> None:
        self._ph_ms = ms
        if follow:
            x = self.time_to_widget_x(ms)
            if x > self.width() - 24 or x < GUTTER_W + 2:
                # page-flip rather than continuous scroll: the static layer is
                # re-rendered once per page instead of once per tick
                self.set_scroll(self.tm.time_to_x(ms) + PAD_L - self._view_w() * 0.12)
        cur = self.tm.index_at_time(ms)
        if cur != self._cur_idx:
            self._cur_idx = cur
            self.update()
            return
        new_x = int(round(self.time_to_widget_x(ms)))
        old_x = self._ph_drawn_x
        if new_x != old_x:
            h = self.height()
            self.update(old_x - 8, 0, 16, h)
            self.update(new_x - 8, 0, 16, h)

    def snap_time(self, x: float, tolerance_px: float = 5.0) -> float:
        """Time under widget x, magnetised to the nearest frame key."""
        cx = x - self._ox()
        tm = self.tm
        if tm.n:
            k = tm.nearest_key(cx)
            if abs(tm.xs[k + 1] - cx) <= tolerance_px:
                return tm.ends[k]
            if abs(cx) <= tolerance_px:
                return 0.0
        return max(0.0, min(tm.total_ms, tm.x_to_time(cx)))

    # ======================================================================
    # painting
    # ======================================================================
    def resizeEvent(self, event) -> None:
        self._update_track_geometry()
        if self._auto_fit and self.width() > GUTTER_W + 200 and self._drag is None:
            self.zoom_fit()
        else:
            self._sync_scrollbar()
        self.invalidate()

    def paintEvent(self, event) -> None:
        if self._cache_dirty or self._cache is None:
            self._render_cache()
        elif self._dirty_span is not None:
            self._render_cache(self._dirty_span)
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache)
        if self._frames is not None:
            self._paint_overlay(p)
        p.end()

    def render_static(self) -> None:
        """Force a re-render of the static layer (used by the perf check)."""
        self._cache_dirty = True
        self._render_cache()

    def _visible_range(self, x0: float = GUTTER_W, x1: float | None = None):
        """Frames intersecting widget x-range [x0, x1] (default: the view)."""
        tm = self.tm
        if tm.n == 0:
            return 0, -1
        ox = self._ox()
        cx0 = x0 - ox
        cx1 = (self.width() if x1 is None else x1) - ox
        i0 = max(0, bisect_right(tm.xs, cx0) - 1)
        i1 = min(tm.n - 1, bisect_left(tm.xs, cx1))
        return i0, i1

    def _render_cache(self, span=None) -> None:
        """Render the static layer; with ``span`` = (x0, x1) only that strip
        (clipped), which is what makes single-frame edits cheap."""
        w, h = max(1, self.width()), max(1, self.height())
        dpr = self.devicePixelRatioF()
        pw, ph = int(round(w * dpr)), int(round(h * dpr))
        if self._frames is not None and (len(self._frames) != self.tm.n
                                         or len(self._masks) != self.tm.n):
            # the frame list was changed behind our back (not through the
            # timeline API): rebuild rather than paint from a stale layout
            self.relayout()
            span = None
        pm = self._cache
        if pm is None or pm.width() != pw or pm.height() != ph:
            pm = QPixmap(pw, ph)
            pm.setDevicePixelRatio(dpr)
            self._cache = pm
            span = None
        self._cache_dirty = False
        self._dirty_span = None
        p = QPainter(pm)
        if span is None:
            left, right = 0, w
        else:
            left, right = span
            p.setClipRect(left, 0, right - left, h)
        p.fillRect(left, 0, right - left, h, self.c_bg)
        ox = self._ox()
        tm = self.tm
        frames = self._frames

        # lane backgrounds
        p.fillRect(0, 0, w, RULER_H, self.c_ruler)
        p.fillRect(0, FACE_Y - 2, w, FACE_H + 4, self.c_lane)
        if self.show_tracks:
            th = self._track_h
            for k in range(N_SERVOS):
                if k % 2 == 0:
                    p.fillRect(0, self._tracks_y + k * th, w, th, self.c_track_alt)
            p.fillRect(0, self._tracks_y - 1, w, 1, self.c_border)
        p.fillRect(0, RULER_H, w, 1, self.c_border)

        cl = max(GUTTER_W, left)
        p.setClipRect(cl, 0, right - cl, h)
        self._paint_ruler(p, ox, w, h)
        if frames is not None:
            i0, i1 = self._visible_range(cl, right)
            if i1 >= i0:
                self._paint_blocks(p, ox, i0, i1)
                self._paint_faces(p, ox, i0, i1)
                if self.show_tracks:
                    self._paint_tracks(p, ox, i0, i1, w)
            if not self._read_only:
                self._paint_add(p, ox, False)
        if left < GUTTER_W:
            p.setClipping(False)
            self._paint_gutter(p, h)
        if frames is None:
            p.setPen(self.c_muted)
            p.setFont(self.f_label)
            p.drawText(QRectF(GUTTER_W, RULER_H, w - GUTTER_W, BLOCK_H + 12),
                       Qt.AlignCenter, "No sequence selected")
        p.end()

    def _paint_ruler(self, p: QPainter, ox: float, w: int, h: int) -> None:
        tm = self.tm
        cx0 = GUTTER_W - ox
        cx1 = w - ox
        t0 = tm.x_to_time(cx0)
        t1 = tm.x_to_time(cx1)
        span_px = cx1 - max(cx0, 0.0)
        if span_px <= 1 or t1 <= t0:
            return
        step = tick_step((t1 - t0) / span_px)
        div = minor_divisions(step)
        sub = step / div
        k = int(t0 // sub)
        k_end = int(t1 // sub) + 1
        p.setFont(self.f_small)
        fm = self.fm_small
        last_label_right = -1e9
        bottom = h
        minor_ok = (sub * span_px / (t1 - t0)) >= 5.0
        while k <= k_end:
            t = k * sub
            x = int(round(ox + tm.time_to_x(t)))
            if k % div == 0:
                p.fillRect(x, RULER_H - 9, 1, 9, self.c_muted)
                p.fillRect(x, RULER_H + 1, 1, bottom - RULER_H, self.c_grid)
                label = format_tick(t, step)
                if x + 4 > last_label_right + 8:
                    p.setPen(self.c_dim)
                    p.drawText(x + 4, 12, label)
                    last_label_right = x + 4 + fm.horizontalAdvance(label)
            elif minor_ok:
                p.fillRect(x, RULER_H - 4, 1, 4, self.c_border_strong)
            k += 1

    def _paint_blocks(self, p: QPainter, ox: float, i0: int, i1: int) -> None:
        frames = self._frames
        xs = self.tm.xs
        masks = self._masks
        sel = self.tl._selection
        prim = self.tl.selected_index
        ro = self._read_only
        y, h = BLOCK_Y, BLOCK_H
        r = float(RADIUS_SM)
        c_text = self.c_dim if ro else self.c_fg
        key_y = RULER_H + 0.5
        p.setRenderHint(QPainter.Antialiasing, True)
        for i in range(i0, i1 + 1):
            xa = int(round(ox + xs[i]))
            xb = int(round(ox + xs[i + 1]))
            bw = xb - xa - 1
            if bw < 1:
                bw = 1
            f = frames[i]
            selected = i in sel
            if selected and i == prim:
                fill, pen = self.c_sel_fill, self.p_sel
            elif selected:
                fill, pen = self.c_multi_fill, self.p_multi
            elif ro:
                fill, pen = self.c_block_ro, self.p_block_ro
            else:
                fill, pen = self.c_block, self.p_block
            if bw < 7:
                p.fillRect(xa, y, bw, h, pen.color() if selected else self.c_sliver)
                continue
            p.setPen(pen)
            p.setBrush(fill)
            p.drawRoundedRect(QRectF(xa + 0.5, y + 0.5, bw - 1.0, h - 1.0), r, r)

            # key marker: the pose is reached at the block's END
            if bw >= 9:
                p.translate(xb - 0.5, key_y)
                p.fillPath(self._diamond, self.c_accent if selected else self.c_muted)
                p.translate(-(xb - 0.5), -key_y)

            mask = masks[i]
            if bw >= 16:
                st, nw = self._number_text(i + 1)
                if bw >= nw + 7:
                    st2, dw = self._duration_text(f"{f.delay_ms} ms")
                    wide = bw >= nw + dw + 20
                    tx = xa + 4
                    if wide and tx < GUTTER_W + 4:
                        # block scrolled partly off the left: keep its number in view
                        tx = min(GUTTER_W + 4, xa + bw - dw - nw - 14)
                    p.setFont(self.f_num)
                    p.setPen(c_text)
                    p.drawStaticText(tx, y + 4, st)
                    if wide:
                        p.setFont(self.f_small)
                        p.setPen(self.c_dim)
                        p.drawStaticText(xa + bw - dw - 5, y + 5, st2)
                    else:
                        st, dw = self._duration_text(str(f.delay_ms))
                        if bw >= dw + 9:
                            p.setFont(self.f_small)
                            p.setPen(self.c_dim)
                            p.drawStaticText(xa + 5, y + 17, st)
            ang = f.angles
            if bw >= 30:
                # 8 mini bars: height = angle, bright = changed vs previous frame
                slot = int(min(7.0, (bw - 9) / 8.0))
                barw = slot - 1 if slot > 2 else 2
                bx = xa + 5
                bh = 18
                by = y + h - 5 - bh
                p.fillRect(bx, by + bh, slot * N_SERVOS - 1, 1, self.c_bar_base)
                for k in range(N_SERVOS):
                    a = ang.get(SERVO_ORDER[k], DEFAULT_ANGLE)
                    hh = 1 + int(a * (bh - 1) / 180)
                    p.fillRect(bx + k * slot, by + bh - hh, barw, hh,
                               self.c_servo[k] if (mask >> k) & 1 else self.c_servo_dim[k])
            elif bw >= 9:
                # too narrow for bars: one coloured tick per servo that changed
                ty = y + h - 5 - 3 * N_SERVOS
                for k in range(N_SERVOS):
                    if (mask >> k) & 1:
                        p.fillRect(xa + 2, ty + 3 * k, bw - 4, 2, self.c_servo[k])
        p.setBrush(Qt.NoBrush)
        p.setRenderHint(QPainter.Antialiasing, False)

    def _number_text(self, number: int):
        got = self._num_text.get(number)
        if got is None:
            if len(self._num_text) > 4000:
                self._num_text.clear()
            st = QStaticText(str(number))
            st.setTextFormat(Qt.PlainText)
            st.prepare(QTransform(), self.f_num)
            got = (st, self.fm_num.horizontalAdvance(str(number)))
            self._num_text[number] = got
        return got

    def _duration_text(self, label: str):
        got = self._dur_text.get(label)
        if got is None:
            if len(self._dur_text) > 4000:
                self._dur_text.clear()
            st = QStaticText(label)
            st.setTextFormat(Qt.PlainText)
            st.prepare(QTransform(), self.f_small)
            got = (st, self.fm_small.horizontalAdvance(label))
            self._dur_text[label] = got
        return got

    def _elided(self, text: str, width: int) -> str:
        key = (text, width)
        got = self._elide_cache.get(key)
        if got is None:
            if len(self._elide_cache) > 600:
                self._elide_cache.clear()
            got = self.fm_label.elidedText(text, Qt.ElideRight, width)
            self._elide_cache[key] = got
        return got

    def _paint_faces(self, p: QPainter, ox: float, i0: int, i1: int) -> None:
        frames = self._frames
        xs = self.tm.xs
        eff = self._eff
        n = len(frames)
        y, h = FACE_Y, FACE_H
        mid = y + h // 2 - 1
        p.setFont(self.f_label)
        i = i0
        # a face stays on the OLED until another frame sets one: draw the chip
        # on the frame that sets it and a thin run-on line while it persists
        if not frames[i].face and eff[i][0]:
            # start from the frame that set the face showing at i0 so its
            # label is drawn whole (also keeps strip re-renders identical)
            j = i
            while j > 0 and not frames[j].face and i - j < 400:
                j -= 1
            if frames[j].face:
                i = j
            else:
                j = i
                while j < n and not frames[j].face:
                    j += 1
                xa = int(round(ox + xs[i]))
                xe = int(round(ox + xs[j]))
                p.fillRect(xa, mid, xe - xa, 2, self.c_face_line)
                i = j
        p.setRenderHint(QPainter.Antialiasing, True)
        while i <= i1:
            f = frames[i]
            if not f.face:
                i += 1
                continue
            j = i + 1
            while j < n and not frames[j].face:
                j += 1
            xa = int(round(ox + xs[i]))
            xb = int(round(ox + xs[i + 1]))
            xe = int(round(ox + xs[j]))
            cw = max(2, xb - xa - 1)
            if xe > xb:
                p.fillRect(xb, mid, xe - xb - 1, 2, self.c_face_line)
            if cw >= 7:
                p.setPen(self.c_face_border)
                p.setBrush(self.c_face)
                p.drawRoundedRect(QRectF(xa + 0.5, y + 0.5, cw - 1.0, h - 1.0), 3.0, 3.0)
            else:
                p.fillRect(xa, y, cw, h, self.c_face_border)
            room = xe - xa - 12
            if room >= 22:
                text = self._elided(f.face, room)
                tw = self.fm_label.horizontalAdvance(text)
                if tw + 8 > cw:
                    # name runs past its own (short) frame: give it a backing
                    p.fillRect(xa + cw, y + 2, tw + 10 - cw, h - 4, self.c_lane)
                p.setPen(self.c_face_text)
                p.drawText(xa + 6, y + 13, text)
            i = j
        p.setBrush(Qt.NoBrush)
        p.setRenderHint(QPainter.Antialiasing, False)

    def _paint_tracks(self, p: QPainter, ox: float, i0: int, i1: int, w: int) -> None:
        self._ensure_tracks()
        frames = self._frames
        xs = self.tm.xs
        masks = self._masks
        ty = self._tracks_y
        th = self._track_h
        p.save()
        p.setClipRect(GUTTER_W, ty, w - GUTTER_W, th * N_SERVOS + 2, Qt.IntersectClip)
        p.setRenderHint(QPainter.Antialiasing, True)
        # cosmetic 1-device-px pens take the raster engine's fast line path
        # (a 1.3 px stroked polyline across the view cost ~1 ms per servo);
        # on HiDPI a second pass one device pixel lower restores the weight
        a, b = max(0, i0 - 1), i1 + 3
        dpr = self.devicePixelRatioF()
        passes = (0.0, 1.0 / dpr) if dpr > 1.2 else (0.0,)
        for dy in passes:
            p.translate(ox, dy)
            for k in range(N_SERVOS):
                pts = self._track_pts[k]
                if len(pts) >= 2:
                    p.setPen(self.p_track[k])
                    p.drawPolyline(pts[a:b])
            p.translate(-ox, -dy)
        p.setRenderHint(QPainter.Antialiasing, False)
        # key dots where a servo actually changes
        for i in range(i0, i1 + 1):
            if xs[i + 1] - xs[i] < 9.0:
                continue
            mask = masks[i]
            if not mask:
                continue
            x = int(round(ox + xs[i + 1])) - 2
            ang = frames[i].angles
            for k in range(N_SERVOS):
                if (mask >> k) & 1:
                    yy = int(round(self._track_y(k, ang.get(SERVO_ORDER[k], DEFAULT_ANGLE)))) - 1
                    p.fillRect(x, yy, 3, 3, self.c_servo[k])
        p.restore()

    def _add_rect(self, ox: float):
        return (int(round(ox + self.tm.total_w)) + ADD_GAP, BLOCK_Y, ADD_W, BLOCK_H)

    def _paint_add(self, p: QPainter, ox: float, hover: bool) -> None:
        x, y, w, h = self._add_rect(ox)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(self.p_add_hover if hover else self.p_add)
        p.setBrush(self.c_add_hover if hover else Qt.NoBrush)
        p.drawRoundedRect(QRectF(x + 0.5, y + 0.5, w - 1.0, h - 1.0), float(RADIUS_SM), float(RADIUS_SM))
        p.setRenderHint(QPainter.Antialiasing, False)
        c = self.c_accent if hover else self.c_dim
        cx, cy = x + w // 2, y + h // 2
        p.fillRect(cx - 5, cy - 1, 11, 2, c)
        p.fillRect(cx - 1, cy - 5, 2, 11, c)
        p.setBrush(Qt.NoBrush)

    def _paint_gutter(self, p: QPainter, h: int) -> None:
        p.fillRect(0, 0, GUTTER_W, h, self.c_gutter)
        p.fillRect(GUTTER_W - 1, 0, 1, h, self.c_border)
        p.fillRect(0, RULER_H, GUTTER_W, 1, self.c_border)
        p.setFont(self.f_gutter)
        p.setPen(self.c_muted)
        p.drawText(QRectF(0, BLOCK_Y, GUTTER_W - 1, BLOCK_H), Qt.AlignCenter, "POSE")
        p.drawText(QRectF(0, FACE_Y, GUTTER_W - 1, FACE_H), Qt.AlignCenter, "FACE")
        if self.show_tracks:
            th = self._track_h
            p.fillRect(0, self._tracks_y - 1, GUTTER_W, 1, self.c_border)
            for k, s in enumerate(SERVO_ORDER):
                ty = self._tracks_y + k * th
                p.fillRect(7, ty + th // 2 - 3, 3, 6, self.c_servo[k])
                p.setPen(self.c_dim)
                p.drawText(QRectF(14, ty, GUTTER_W - 16, th), Qt.AlignVCenter | Qt.AlignLeft, s)

    # -- overlay: everything that changes without touching the data ----------
    def _paint_overlay(self, p: QPainter) -> None:
        w, h = self.width(), self.height()
        tm = self.tm
        xs = tm.xs
        ox = self._ox()
        p.setClipRect(GUTTER_W, 0, w - GUTTER_W, h)
        d = self._drag

        # frame under the playhead
        ci = self._cur_idx
        if 0 <= ci < tm.n:
            xa = int(round(ox + xs[ci]))
            xb = int(round(ox + xs[ci + 1]))
            p.fillRect(xa, BLOCK_Y - 3, max(2, xb - xa - 1), 2, self.c_playhead)

        kind, hi = self._hover
        if d is None or d.mode == "move_armed":
            if kind in ("block", "edge") and 0 <= hi < tm.n:
                xa = int(round(ox + xs[hi]))
                xb = int(round(ox + xs[hi + 1]))
                bw = max(1, xb - xa - 1)
                if hi not in self.tl._selection:
                    p.setRenderHint(QPainter.Antialiasing, True)
                    p.setPen(self.p_hover)
                    p.setBrush(self.c_hover_fill)
                    p.drawRoundedRect(QRectF(xa + 0.5, BLOCK_Y + 0.5, bw - 1.0, BLOCK_H - 1.0),
                                      float(RADIUS_SM), float(RADIUS_SM))
                    p.setBrush(Qt.NoBrush)
                    p.setRenderHint(QPainter.Antialiasing, False)
                if kind == "edge":
                    p.fillRect(xb - 4, BLOCK_Y + 8, 2, BLOCK_H - 16, self.c_accent)
            elif kind == "add" and not self._read_only:
                self._paint_add(p, ox, True)

        if d is not None:
            if d.mode == "move":
                sel = self.tl._selection
                i0, i1 = self._visible_range()
                for i in range(i0, i1 + 1):
                    if i in sel:
                        xa = int(round(ox + xs[i]))
                        xb = int(round(ox + xs[i + 1]))
                        p.fillRect(xa, BLOCK_Y, xb - xa, FACE_Y + FACE_H - BLOCK_Y, self.c_dim_overlay)
                if 0 <= d.gap <= tm.n:
                    gx = int(round(ox + xs[d.gap]))
                    top, bot = BLOCK_Y - 4, FACE_Y + FACE_H + 2
                    p.fillRect(gx - 1, top, 2, bot - top, self.c_accent)
                    p.fillRect(gx - 4, top, 8, 2, self.c_accent)
                    p.fillRect(gx - 4, bot - 2, 8, 2, self.c_accent)
                count = len(sel)
                where = "to the end" if d.gap >= tm.n else f"before {d.gap + 1}"
                self._paint_bubble(p, d.last_x, d.last_y,
                                   f"Move {count} frame{'s' if count != 1 else ''} {where}")
            elif d.mode == "retime":
                xb = int(round(ox + xs[d.index + 1])) if d.index < tm.n else 0
                p.fillRect(xb - 1, BLOCK_Y - 4, 2, BLOCK_H + 8, self.c_accent)
                text = f"{d.value} ms"
                if len(d.orig) > 1:
                    text += f"  ·  {len(d.orig)} frames"
                self._paint_bubble(p, d.last_x, d.last_y, text)

        if self.hasFocus():
            # keyboard shortcuts are live: thin accent line under the ruler
            p.fillRect(GUTTER_W, RULER_H, w - GUTTER_W, 1, self.c_focus)

        # playhead
        px = int(round(ox + tm.time_to_x(self._ph_ms)))
        self._ph_drawn_x = px
        p.fillRect(px, 0, 1, h, self.c_playhead)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(self.c_playhead)
        p.translate(px + 0.5, 0.0)
        p.drawPolygon(self._head)
        p.translate(-(px + 0.5), 0.0)
        p.setBrush(Qt.NoBrush)
        p.setRenderHint(QPainter.Antialiasing, False)

    def _paint_bubble(self, p: QPainter, x: float, y: float, text: str) -> None:
        p.setFont(self.f_small)
        tw = self.fm_small.horizontalAdvance(text)
        bw, bh = tw + 14, 20
        bx = int(min(max(GUTTER_W + 4, x + 14), self.width() - bw - 4))
        by = int(min(max(2, y - 30), self.height() - bh - 2))
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(self.p_bubble)
        p.setBrush(self.c_bubble)
        p.drawRoundedRect(QRectF(bx + 0.5, by + 0.5, bw - 1.0, bh - 1.0), 4.0, 4.0)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setPen(self.c_fg)
        p.drawText(bx + 7, by + 14, text)
        p.setBrush(Qt.NoBrush)

    # ======================================================================
    # interaction
    # ======================================================================
    def _hit(self, x: float, y: float):
        """-> (zone, frame index). Zones: gutter, ruler, block, edge (retime
        handle), add, face, track, lane."""
        if self._frames is None:
            return (None, -1)
        if x < GUTTER_W:
            return ("gutter", -1)
        if y < RULER_H:
            return ("ruler", -1)
        tm = self.tm
        xs = tm.xs
        cx = x - self._ox()
        if BLOCK_Y <= y < BLOCK_Y + BLOCK_H:
            i = tm.index_at_x(cx)
            if i is None:
                if cx >= tm.total_w:
                    if (tm.n and not self._read_only and cx - tm.total_w <= 3.0
                            and xs[tm.n] - xs[tm.n - 1] >= 10.0):
                        return ("edge", tm.n - 1)
                    ax = tm.total_w + ADD_GAP
                    if not self._read_only and ax <= cx <= ax + ADD_W:
                        return ("add", -1)
                return ("lane", -1)
            if not self._read_only:
                bw = xs[i + 1] - xs[i]
                # blocks narrower than 10 px are select/move only -- zoom in to retime
                if bw >= 10.0 and xs[i + 1] - cx <= max(3.0, min(7.0, bw * 0.3)):
                    return ("edge", i)
                if i > 0 and cx - xs[i] < 2.0 and xs[i] - xs[i - 1] >= 10.0:
                    return ("edge", i - 1)
            return ("block", i)
        if FACE_Y - 2 <= y < FACE_Y + FACE_H + 2:
            i = tm.index_at_x(cx)
            return ("face", i) if i is not None else ("lane", -1)
        if self.show_tracks and y >= self._tracks_y and tm.n:
            return ("track", tm.nearest_key(cx))
        return ("lane", -1)

    def _gap_at(self, x: float) -> int:
        tm = self.tm
        cx = x - self._ox()
        if cx <= 0 or tm.n == 0:
            return 0
        i = tm.index_at_x(cx)
        if i is None:
            return tm.n
        return i if cx < (tm.xs[i] + tm.xs[i + 1]) / 2.0 else i + 1

    def _set_hover(self, hit) -> None:
        if hit != self._hover:
            self._hover = hit
            kind = hit[0]
            if kind == "edge":
                self.setCursor(Qt.SizeHorCursor)
            elif kind == "add":
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.unsetCursor()
            self.update()

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.MouseFocusReason)
        pos = event.position()
        x, y = pos.x(), pos.y()
        if event.button() == Qt.MiddleButton:
            self._drag = _Drag("pan", press_x=x, press_cx=self.scroll_x)
            self.setCursor(Qt.ClosedHandCursor)
            return
        if event.button() != Qt.LeftButton or self._frames is None:
            return
        if self._drag is not None:
            self.cancel_drag()
        kind, i = self._hit(x, y)
        mods = event.modifiers()
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)
        tl = self.tl
        if kind == "ruler":
            self._drag = _Drag("scrub", press_x=x)
            tl._user_scrub(self.snap_time(x))
        elif kind == "add":
            tl.add_frame()
        elif kind == "edge":
            orig = tl._retime_begin(i)
            if orig:
                d = _Drag("retime", index=i, press_x=x, press_cx=x - self._ox(), orig=orig)
                d.value = orig[i]
                d.last_x, d.last_y = x, y
                self._drag = d
                self.update()
        elif kind == "block":
            collapse = tl._click_select(i, ctrl, shift, defer=True)
            d = _Drag("move_armed", index=i, press_x=x, collapse=collapse)
            d.last_x, d.last_y = x, y
            self._drag = d
        elif kind == "face":
            tl._click_select(i, ctrl, shift, defer=False)
        elif kind == "track":
            tl._click_select(i, False, False, defer=False)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        x, y = pos.x(), pos.y()
        d = self._drag
        if d is None:
            self._set_hover(self._hit(x, y))
            return
        d.last_x, d.last_y = x, y
        d.shift = bool(event.modifiers() & Qt.ShiftModifier)
        if d.mode == "pan":
            self.set_scroll(d.press_cx - (x - d.press_x))
            return
        if d.mode == "move_armed":
            if abs(x - d.press_x) < DRAG_THRESHOLD:
                return
            if self._read_only:
                self.tl._notify_blocked()
                self._drag = None
                return
            d.mode = "move"
            self.setCursor(Qt.ClosedHandCursor)
        self._drag_update()
        near_edge = x < GUTTER_W + 24 or x > self.width() - 24
        if near_edge and d.mode in ("move", "retime", "scrub"):
            if not self._autoscroll.isActive():
                self._autoscroll.start()
        elif self._autoscroll.isActive():
            self._autoscroll.stop()

    def _drag_update(self) -> None:
        d = self._drag
        if d is None:
            return
        x = d.last_x
        if d.mode == "scrub":
            self.tl._user_scrub(self.snap_time(x))
        elif d.mode == "move":
            d.gap = self._gap_at(x)
            self.update()
        elif d.mode == "retime":
            step = 1 if d.shift else SNAP_MS
            base = d.orig[d.index]
            raw = base + ((x - self._ox()) - d.press_cx) / self.ppm
            value = max(step, int(round(raw / step)) * step)
            if value != d.value:
                d.value = value
                delta = value - base
                self.tl._retime_preview({i: max(1, o + delta) for i, o in d.orig.items()})
            self.update()

    def _autoscroll_tick(self) -> None:
        d = self._drag
        if d is None or d.mode not in ("move", "retime", "scrub"):
            self._autoscroll.stop()
            return
        x = d.last_x
        if x < GUTTER_W + 24:
            delta = -max(4.0, (GUTTER_W + 24 - x) * 0.6)
        elif x > self.width() - 24:
            delta = max(4.0, (x - (self.width() - 24)) * 0.6)
        else:
            self._autoscroll.stop()
            return
        before = self.scroll_x
        self.set_scroll(self.scroll_x + min(40.0, max(-40.0, delta)))
        if self.scroll_x != before:
            self._drag_update()

    def mouseReleaseEvent(self, event) -> None:
        d = self._drag
        if d is None:
            return
        if d.mode == "pan":
            if event.button() != Qt.MiddleButton:
                return
        elif event.button() != Qt.LeftButton:
            return
        self._drag = None
        self._autoscroll.stop()
        self.unsetCursor()
        tl = self.tl
        if d.mode == "retime":
            tl._retime_finish(d.orig, commit=True)
        elif d.mode == "move":
            tl.move_frames(sorted(tl._selection), d.gap)
        elif d.mode == "move_armed" and d.collapse:
            tl._click_select(d.index, False, False, defer=False)
        pos = event.position()
        self._hover = (None, -2)
        self._set_hover(self._hit(pos.x(), pos.y()))
        self.update()

    def cancel_drag(self) -> bool:
        """Abort the drag in progress (Esc). True if there was one."""
        d = self._drag
        if d is None:
            return False
        self._drag = None
        self._autoscroll.stop()
        self.unsetCursor()
        if d.mode == "retime":
            self.tl._retime_finish(d.orig, commit=False)
        self.update()
        return d.mode in ("retime", "move", "scrub", "pan")

    def is_dragging(self) -> bool:
        return self._drag is not None and self._drag.mode != "move_armed"

    def leaveEvent(self, event) -> None:
        if self._drag is None:
            self._set_hover((None, -1))

    def focusInEvent(self, event) -> None:
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.cancel_drag()
        self.update()
        super().focusOutEvent(event)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta()
        dy, dx = delta.y(), delta.x()
        if event.modifiers() & Qt.ControlModifier:
            if dy:
                self.zoom_by(1.0015 ** dy, event.position().x())
        else:
            pix = event.pixelDelta()
            if not pix.isNull():
                move = pix.x() if abs(pix.x()) > abs(pix.y()) else pix.y()
            else:
                move = (dx if abs(dx) > abs(dy) else dy) * 0.7
            self.set_scroll(self.scroll_x - move)
        event.accept()

    def contextMenuEvent(self, event) -> None:
        pos = event.pos()
        kind, i = self._hit(pos.x(), pos.y())
        if kind in ("block", "edge", "face", "track") and i >= 0:
            self.tl._context_menu(i, event.globalPos())
        elif kind is not None and kind != "gutter":
            self.tl._context_menu(None, event.globalPos())

    def keyPressEvent(self, event) -> None:
        if not self.tl.handle_key(event):
            super().keyPressEvent(event)

    def event(self, event) -> bool:
        t = event.type()
        if t == QEvent.ShortcutOverride:
            # claim our keys so a window-level QAction with the same shortcut
            # (e.g. Space = Play) doesn't also fire while the timeline has focus
            if self.tl.handles_key(event):
                event.accept()
                return True
        elif t == QEvent.ToolTip:
            self._show_tooltip(event)
            return True
        return super().event(event)

    def _show_tooltip(self, event) -> None:
        pos = event.pos()
        kind, i = self._hit(pos.x(), pos.y())
        frames = self._frames
        text = ""
        if kind in ("block", "edge", "face", "track") and frames and 0 <= i < len(frames):
            f = frames[i]
            tm = self.tm
            mask = self._masks[i] if i < len(self._masks) else 0
            parts = []
            for k, s in enumerate(SERVO_ORDER):
                a = f.angles.get(s, DEFAULT_ANGLE)
                parts.append(f"<b>{s} {a}°</b>" if (mask >> k) & 1 and i else f"{s} {a}°")
            face = f.face or (f"{self._eff[i][0]} (carried)" if self._eff[i][0] else "none")
            text = (f"<b>Frame {i + 1}</b> &nbsp;·&nbsp; {f.delay_ms} ms"
                    f"<br>Pose reached at {tm.ends[i] / 1000:.3f} s (end of block)"
                    f"<br>Face: {face}"
                    f"<br>{' &nbsp; '.join(parts[:4])}<br>{' &nbsp; '.join(parts[4:])}")
            if kind == "edge":
                text += "<br><i>Drag the edge to retime (Shift = 1 ms steps)</i>"
        elif kind == "add":
            text = "Add a frame (copy of the selected one)"
        elif kind == "ruler":
            text = "Click or drag to scrub"
        if text:
            QToolTip.showText(event.globalPos(), text, self)
        else:
            QToolTip.hideText()

    # -- compatibility with the old QGraphicsView-based API -----------------
    def mapFromScene(self, x: float, y: float):
        return QPoint(int(round(x)), int(round(y)))
