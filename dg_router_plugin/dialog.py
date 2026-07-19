"""wx dialog for dg-router — controls + a fluid full-res board preview.

The preview is the working surface: a full-resolution board render, cached once
and zoomed/panned within (never regenerated), with overlays drawn ON TOP (never
on the board itself):
- selected/checked nets' ratlines (checked bright, the active one brighter),
- proposed routes after Route (preview-first: the board is not touched until
  Accept),
- (later) live routing playback.

Accept applies the proposed routes to the board; Reject discards them (the board
was never modified); Try again re-routes differently. Non-modal so KiCad stays
interactive.
"""

import os
import tempfile
import threading
import traceback

import pcbnew
import wx
import wx.svg
import wx.grid

from . import shim
from . import router
from . import placement

# overlay colors
_C_PAD = wx.Colour(255, 235, 59)
_C_KEPT = wx.Colour(75, 222, 128)         # existing same-net copper
_C_TODO = wx.Colour(255, 62, 165)         # ratlines of a checked net (magenta)
_C_ACTIVE = wx.Colour(120, 245, 255)      # ratlines of the ACTIVE net (brighter)
# proposed routes drawn in the board's own layer colors (F.Cu red, B.Cu blue)
# so you can read at a glance which layer a track lands on
_C_PROP = {pcbnew.F_Cu: wx.Colour(235, 70, 70),
           pcbnew.B_Cu: wx.Colour(70, 135, 245)}
_C_PROP_DEFAULT = wx.Colour(230, 230, 230)
_C_PROP_VIA = wx.Colour(255, 235, 59)
_C_DEBUG = wx.Colour(120, 245, 255)       # A* explored-cell heatmap
_BG = wx.Colour(24, 24, 24)
_C_EDGE = wx.Colour(210, 210, 70)         # board outline (KiCad edge-cuts yellow)
_BG_PPM = 30.0                            # base render resolution (px per mm)
_BG_PPM_MAX = 110.0                       # cap for re-raster on zoom-in

# net-list status (word column stays readable under the blue selection)
_COL_ROUTED = wx.Colour(30, 160, 70)
_COL_PARTIAL = wx.Colour(200, 130, 0)
_COL_UNROUTED = wx.Colour(140, 140, 140)
_COL_UNKNOWN = wx.Colour(20, 20, 20)
_STATUS_COL = {"routed": _COL_ROUTED, "partial": _COL_PARTIAL,
               "unrouted": _COL_UNROUTED}
_STATUS_WORD = {"routed": "routed", "partial": "partial", "unrouted": "unrouted"}

_C_CONN = wx.Colour(120, 245, 255)        # a connection IN the job (bright)
_C_CONN_OFF = wx.Colour(150, 120, 170)    # shown but not in the job (dim)
_C_PLACE = wx.Colour(120, 245, 160)       # proposed component placement (ghost)

_OPEN = []   # keep non-modal dialogs alive


def _pt_seg_dist(px, py, gap):
    x1, y1, x2, y2 = gap
    vx, vy = x2 - x1, y2 - y1
    L = vx * vx + vy * vy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / L))
    import math
    return math.hypot(px - (x1 + t * vx), py - (y1 + t * vy))


class PreviewPanel(wx.Panel):
    def __init__(self, parent, board):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.board = board
        self.bg_bmp = None
        self.origin = None
        self.vbw = self.vbh = 1.0
        self.err = None
        self.selected = []
        self.active = None
        self.unconn = {}
        self.status_loaded = False
        self.proposed = []
        self.accepted = []               # committed this session (persist in view)
        self.conns = []                  # [{id,net,gap}] connections to show
        self.job_ids = set()             # which connection ids are in the job
        self.placements = []             # [{ref,x,y,w,h}] proposed part positions
        self.on_pad_pick = None          # callback(net_name) on a pad click
        self.on_conn_toggle = None       # callback(conn) on a ratline click
        self._down = None                # (x,y) at mouse-down, to tell click vs drag
        self.debug_bmp = None            # A* explored heatmap (built by set_explored)
        self.debug_extent = None         # (x0,y0,x1,y1) world mm of the heatmap
        self._geom_cache = {}
        self.zoom = 1.0
        self.panx = self.pany = 0.0
        self._drag = None
        self._svg = None
        self._bmp_ppm = _BG_PPM          # actual resolution of bg_bmp
        self._pinch_base = 1.0
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetBackgroundColour(_BG)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda e: None)
        self.Bind(wx.EVT_MOUSEWHEEL, self.on_wheel)
        self.Bind(wx.EVT_LEFT_DOWN, self.on_down)
        self.Bind(wx.EVT_LEFT_UP, self.on_up)
        self.Bind(wx.EVT_MOTION, self.on_motion)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, lambda e: None)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        # re-raster from the SVG at the zoom level (crisp like Mac Preview),
        # debounced so it happens after the gesture settles.
        self._rtimer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_rtimer)
        # pinch-to-zoom (trackpad); two-finger scroll pans (see on_wheel)
        try:
            self.EnableTouchEvents(wx.TOUCH_ZOOM_GESTURE)
            self.Bind(wx.EVT_GESTURE_ZOOM, self.on_pinch)
        except Exception:
            pass

    # --- state ---
    def set_selected(self, names, active=None):
        self.selected = list(names)
        self.active = active
        for name in names:
            if name not in self._geom_cache:
                self._geom_cache[name] = \
                    shim.net_geometry(self.board, [name]).get(name)
        self.Refresh()

    def set_routing(self, unconn):
        self.unconn = unconn or {}
        self.status_loaded = True
        self.Refresh()

    def set_proposed(self, results):
        self.proposed = results or []
        self.Refresh()
        self.Update()   # force an immediate repaint (Refresh alone from a
                        # CallAfter after the route thread didn't always paint)

    def set_placements(self, placements):
        """placements=[{ref,x,y,w,h}] mm — proposed part positions, drawn as
        labeled ghost boxes."""
        self.placements = placements or []
        self.Refresh()
        self.Update()

    def set_connections(self, conns, job_ids):
        """conns=[{id,net,gap}] to draw as ratlines; job_ids=set of ids that are
        IN the current job (drawn bright/solid vs dim/dashed for the rest)."""
        self.conns = conns or []
        self.job_ids = set(job_ids or [])
        self.Refresh()

    def add_accepted(self, results):
        """Fold just-accepted routes into the view as committed copper so they
        stay visible on the next pass (the base render is from the saved file,
        which doesn't have them yet)."""
        self.accepted.extend(results or [])
        self.Refresh()

    def clear_accepted(self):
        self.accepted = []
        self.Refresh()

    def set_explored(self, extent, bmp):
        """extent=(x0,y0,x1,y1) world mm; bmp=wx.Bitmap heatmap (or None)."""
        self.debug_extent = extent
        self.debug_bmp = bmp
        self.Refresh()
        self.Update()

    def zoom_to_bbox(self, x0, y0, x1, y1, margin=0.35):
        """Frame a world-mm bbox in the viewport (used to show a proposed route)."""
        if self.bg_bmp is None or self.origin is None:
            return
        cw, ch = self.GetClientSize()
        bw = max(0.5, (x1 - x0) * (1 + margin))
        bh = max(0.5, (y1 - y0) * (1 + margin))
        fit = self._fit_ppm()
        want = min((cw - 16) / bw, (ch - 16) / bh)
        self.zoom = max(1.0, min(80.0, want / fit))
        ppm = self._ppm()
        cxw, cyw = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        orx, ory = self.origin
        self.panx = cw / 2.0 - (cw - self.vbw * ppm) / 2.0 - (cxw - orx) * ppm
        self.pany = ch / 2.0 - (ch - self.vbh * ppm) / 2.0 - (cyw - ory) * ppm
        if self.zoom <= 1.0:
            self.panx = self.pany = 0.0
        self.Refresh()
        self._rtimer.StartOnce(140)   # re-raster crisp once it settles

    def _ratsnest_for(self, name, geom):
        if self.status_loaded:
            return self.unconn.get(name, [])
        pts = [(p[0], p[1]) for p in geom["pads"]]
        return [(pts[i][0], pts[i][1], pts[j][0], pts[j][1])
                for i, j in shim.mst_edges(pts)]

    # --- view transform ---
    def _fit_ppm(self):
        cw, ch = self.GetClientSize()
        return max(0.01, min((cw - 16) / self.vbw, (ch - 16) / self.vbh))

    def _ppm(self):
        return self._fit_ppm() * self.zoom

    def _origin_screen(self, ppm):
        cw, ch = self.GetClientSize()
        return ((cw - self.vbw * ppm) / 2.0 + self.panx,
                (ch - self.vbh * ppm) / 2.0 + self.pany)

    def _zoom_at(self, factor, mx, my):
        if self.bg_bmp is None:
            return
        ppm = self._ppm()
        bx0, by0 = self._origin_screen(ppm)
        wxmm = self.origin[0] + (mx - bx0) / ppm
        wymm = self.origin[1] + (my - by0) / ppm
        # zoom 1.0 == whole board fits the viewport; don't zoom out past that
        self.zoom = min(80.0, max(1.0, self.zoom * factor))
        ppm2 = self._ppm()
        cw, ch = self.GetClientSize()
        self.panx = mx - (wxmm - self.origin[0]) * ppm2 - (cw - self.vbw * ppm2) / 2.0
        self.pany = my - (wymm - self.origin[1]) * ppm2 - (ch - self.vbh * ppm2) / 2.0
        if self.zoom <= 1.0:            # fully zoomed out -> recenter the board
            self.panx = self.pany = 0.0
        self.Refresh()
        self._rtimer.StartOnce(140)

    def on_wheel(self, evt):
        # two-finger / wheel scroll = PAN; pinch or Cmd/Ctrl+scroll = zoom
        if self.bg_bmp is None:
            return
        d = evt.GetWheelRotation()
        if evt.ControlDown() or evt.CmdDown():
            self._zoom_at(1.2 if d > 0 else 1 / 1.2, evt.GetX(), evt.GetY())
            return
        if self.zoom <= 1.0:            # nothing to pan when fully zoomed out
            return
        if evt.GetWheelAxis() == wx.MOUSE_WHEEL_HORIZONTAL:
            self.panx -= d
        else:
            self.pany += d
        self.Refresh()

    def on_pinch(self, evt):
        if self.bg_bmp is None:
            return
        try:
            if evt.IsGestureStart():
                self._pinch_base = self.zoom
            pos = evt.GetPosition()
            target = max(1.0, min(80.0, self._pinch_base * evt.GetZoomFactor()))
            self._zoom_at(target / self.zoom if self.zoom else 1.0, pos.x, pos.y)
        except Exception:
            pass

    def _on_rtimer(self, _evt):
        """After a zoom settles, re-raster from the SVG so it's crisp (not a
        blurry upscaled bitmap) — and drop back to base res when zoomed out."""
        if self.bg_bmp is None or not self._svg:
            return
        target = self._ppm()
        if target > self._bmp_ppm * 1.25 and self._bmp_ppm < _BG_PPM_MAX:
            self._reraster(min(target, _BG_PPM_MAX))
        elif target < self._bmp_ppm * 0.5 and self._bmp_ppm > _BG_PPM:
            self._reraster(_BG_PPM)

    def rerender(self, path):
        """Rebuild the board background from a given .kicad_pcb (used after parts
        move on placement-accept, since the real file isn't saved yet)."""
        try:
            svg = os.path.join(tempfile.gettempdir(), "dg-router-preview.svg")
            shim.render_board_svg(path, svg)
            self._svg = svg
            self.vbw, self.vbh = shim.parse_svg_viewbox(svg)
            self.origin = shim.plot_origin(self.board, self.vbw, self.vbh)
            self._bmp_ppm = _BG_PPM
            img = wx.svg.SVGimage.CreateFromFile(svg)
            self.bg_bmp = img.ConvertToScaledBitmap(
                wx.Size(max(1, int(self.vbw * _BG_PPM)),
                        max(1, int(self.vbh * _BG_PPM))))
            self.Refresh()
        except Exception:
            pass

    def _reraster(self, ppm):
        try:
            img = wx.svg.SVGimage.CreateFromFile(self._svg)
            self.bg_bmp = img.ConvertToScaledBitmap(
                wx.Size(max(1, int(self.vbw * ppm)), max(1, int(self.vbh * ppm))))
            self._bmp_ppm = ppm
            self.Refresh()
        except Exception:
            pass

    def on_down(self, evt):
        self._drag = (evt.GetX(), evt.GetY(), self.panx, self.pany)
        self._down = (evt.GetX(), evt.GetY())
        if not self.HasCapture():          # guard: double-capture crashes wxMac
            try:
                self.CaptureMouse()
            except Exception:
                pass

    def on_up(self, evt):
        while self.HasCapture():
            self.ReleaseMouse()
        self._drag = None
        # a click (no meaningful drag) picks a ratline (toggle a connection) or,
        # failing that, a pad (select its net)
        if self._down is not None:
            dx = evt.GetX() - self._down[0]
            dy = evt.GetY() - self._down[1]
            if dx * dx + dy * dy <= 16:      # <=4px movement == a click
                self._pick_at(evt.GetX(), evt.GetY())
        self._down = None

    def _screen_to_world(self, mx, my):
        ppm = self._ppm()
        bx0, by0 = self._origin_screen(ppm)
        orx, ory = self.origin
        return orx + (mx - bx0) / ppm, ory + (my - by0) / ppm, ppm

    def _pick_at(self, mx, my):
        if self.bg_bmp is None or self.origin is None:
            return
        wx_mm, wy_mm, ppm = self._screen_to_world(mx, my)
        # ratline first: nearest connection segment within ~6px
        tol = 6.0 / max(ppm, 0.01)
        best, bd = None, tol
        for c in self.conns:
            d = _pt_seg_dist(wx_mm, wy_mm, c["gap"])
            if d < bd:
                bd, best = d, c
        if best is not None and self.on_conn_toggle:
            self.on_conn_toggle(best)
            return
        # else a pad -> select its net
        if self.on_pad_pick:
            name = shim.net_at_point(self.board, wx_mm, wy_mm,
                                     tol_mm=3.0 / max(ppm, 0.01))
            if name:
                self.on_pad_pick(name)

    def on_motion(self, evt):
        if self._drag and evt.Dragging() and evt.LeftIsDown():
            x0, y0, px, py = self._drag
            self.panx = px + (evt.GetX() - x0)
            self.pany = py + (evt.GetY() - y0)
            self.Refresh()

    # --- rendering ---
    def _ensure_background(self):
        if self.bg_bmp is not None or self.err is not None:
            return
        bp = self.board.GetFileName()
        if not bp or not os.path.exists(bp):
            self.err = "Save the board to enable the preview."
            return
        try:
            svg = os.path.join(tempfile.gettempdir(), "dg-router-preview.svg")
            shim.render_board_svg(bp, svg)
            self._svg = svg
            self.vbw, self.vbh = shim.parse_svg_viewbox(svg)
            self.origin = shim.plot_origin(self.board, self.vbw, self.vbh)
            self._bmp_ppm = _BG_PPM
            wpx = max(1, int(self.vbw * _BG_PPM))
            hpx = max(1, int(self.vbh * _BG_PPM))
            img = wx.svg.SVGimage.CreateFromFile(svg)
            self.bg_bmp = img.ConvertToScaledBitmap(wx.Size(wpx, hpx))
        except Exception as e:  # noqa: BLE001
            self.err = "Preview failed:\n%s" % e

    def _center_text(self, dc, text):
        dc.SetTextForeground(wx.Colour(180, 180, 180))
        cs = self.GetClientSize()
        for i, line in enumerate(text.split("\n")):
            w, h = dc.GetTextExtent(line)
            dc.DrawText(line, (cs.width - w) // 2, cs.height // 2 + i * (h + 2) - 20)

    def on_paint(self, _evt):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(_BG))
        dc.Clear()
        self._ensure_background()
        if self.err:
            self._center_text(dc, self.err)
            return
        if self.bg_bmp is None:
            self._center_text(dc, "Rendering board…")
            return

        cw, ch = self.GetClientSize()
        ppm = self._ppm()
        bx0, by0 = self._origin_screen(ppm)
        orx, ory = self.origin
        bgW, bgH = self.bg_bmp.GetWidth(), self.bg_bmp.GetHeight()
        k = self._bmp_ppm / ppm          # bg-bitmap px per screen px

        gc = wx.GraphicsContext.Create(dc)
        try:
            gc.SetInterpolationQuality(wx.INTERPOLATION_BEST)
        except Exception:
            pass

        sx0 = max(0, int((0 - bx0) * k))
        sy0 = max(0, int((0 - by0) * k))
        sx1 = min(bgW, int((cw - bx0) * k) + 2)
        sy1 = min(bgH, int((ch - by0) * k) + 2)
        if sx1 > sx0 and sy1 > sy0:
            rect = wx.Rect(sx0, sy0, sx1 - sx0, sy1 - sy0)
            dw = max(1, (sx1 - sx0) / k)
            dh = max(1, (sy1 - sy0) / k)
            dx, dy = bx0 + sx0 / k, by0 + sy0 / k
            # let Core Graphics scale the visible sub-bitmap (smooth + crisp)
            gc.DrawBitmap(self.bg_bmp.GetSubBitmap(rect), dx, dy, dw, dh)

        # board outline (edge cuts can render at the very border / clipped)
        gc.SetBrush(wx.Brush(wx.Colour(0, 0, 0, 0)))
        gc.SetPen(wx.Pen(_C_EDGE, 1.2))
        gc.DrawRectangle(bx0, by0, self.vbw * ppm, self.vbh * ppm)

        def S(x, y):
            return (bx0 + (x - orx) * ppm, by0 + (y - ory) * ppm)

        def glow_line(a, b, col, w, dashed):
            # bright halo under a solid/dashed core so the line is unmissable
            w = max(1, int(round(w)))
            gc.SetPen(wx.Pen(wx.Colour(col.Red(), col.Green(), col.Blue(), 80),
                             w * 3))
            gc.StrokeLine(a[0], a[1], b[0], b[1])
            style = wx.PENSTYLE_SHORT_DASH if dashed else wx.PENSTYLE_SOLID
            gc.SetPen(wx.Pen(col, w, style))
            gc.StrokeLine(a[0], a[1], b[0], b[1])

        # dim the board when anything is highlighted so the overlays pop — but
        # subtly, so the rest of the traces stay faintly visible for context
        if self.selected or self.proposed or self.accepted or self.placements:
            gc.SetPen(wx.Pen(wx.Colour(0, 0, 0, 0), 0))
            gc.SetBrush(wx.Brush(wx.Colour(0, 0, 0, 60)))
            gc.DrawRectangle(bx0, by0, self.vbw * ppm, self.vbh * ppm)

        # debug: A* explored-cell heatmap (one scaled blit) — shows WHERE the
        # search spent its time, right under the routes it produced
        if self.debug_bmp is not None and self.debug_extent is not None:
            ex0, ey0, ex1, ey1 = self.debug_extent
            dsx, dsy = S(ex0, ey0)
            gc.DrawBitmap(self.debug_bmp, dsx, dsy,
                          (ex1 - ex0) * ppm, (ey1 - ey0) * ppm)

        # connections: bright+solid when in the job, dim+dashed when merely shown
        for c in self.conns:
            x1, y1, x2, y2 = c["gap"]
            in_job = c["id"] in self.job_ids
            col = _C_CONN if in_job else _C_CONN_OFF
            glow_line(S(x1, y1), S(x2, y2), col, 3.0 if in_job else 2.0,
                      not in_job)

        for name in self.selected:
            g = self._geom_cache.get(name)
            if not g:
                continue
            is_active = (name == self.active)
            for (x1, y1, x2, y2, w) in g["tracks"]:
                gc.SetPen(wx.Pen(_C_KEPT, max(2, int(round(w * ppm)))))
                a, b = S(x1, y1), S(x2, y2)
                gc.StrokeLine(a[0], a[1], b[0], b[1])
            gc.SetBrush(wx.Brush(wx.Colour(255, 235, 59, 220 if is_active else 150)))
            gc.SetPen(wx.Pen(_C_PAD, 1.5))
            for (x, y, r) in g["pads"]:
                cx, cy = S(x, y)
                rr = max(r * ppm, 5)
                gc.DrawEllipse(cx - rr, cy - rr, 2 * rr, 2 * rr)

        # accepted copper: solid layer-colored lines (committed, no glow)
        for res in self.accepted:
            for (x1, y1, x2, y2, w, lyr) in res.get("segments", []):
                col = _C_PROP.get(lyr, _C_PROP_DEFAULT)
                gc.SetPen(wx.Pen(col, max(2, int(round(w * ppm)))))
                a, b = S(x1, y1), S(x2, y2)
                gc.StrokeLine(a[0], a[1], b[0], b[1])
            gc.SetBrush(wx.Brush(_C_PROP_VIA))
            gc.SetPen(wx.Pen(wx.Colour(255, 255, 255), 1.0))
            for (x, y, dia, drill) in res.get("vias", []):
                cx, cy = S(x, y)
                rr = max(dia / 2.0 * ppm, 3)
                gc.DrawEllipse(cx - rr, cy - rr, 2 * rr, 2 * rr)

        for res in self.proposed:
            for (x1, y1, x2, y2, w, lyr) in res.get("segments", []):
                col = _C_PROP.get(lyr, _C_PROP_DEFAULT)
                glow_line(S(x1, y1), S(x2, y2), col, max(2.2, w * ppm), False)
            gc.SetBrush(wx.Brush(_C_PROP_VIA))
            gc.SetPen(wx.Pen(wx.Colour(255, 255, 255), 1.5))
            for (x, y, dia, drill) in res.get("vias", []):
                cx, cy = S(x, y)
                rr = max(dia / 2.0 * ppm, 4)
                gc.DrawEllipse(cx - rr, cy - rr, 2 * rr, 2 * rr)

        # proposed component placements: labeled ghost boxes
        if self.placements:
            gc.SetPen(wx.Pen(_C_PLACE, 2))
            gc.SetBrush(wx.Brush(wx.Colour(_C_PLACE.Red(), _C_PLACE.Green(),
                                           _C_PLACE.Blue(), 45)))
            for p in self.placements:
                cx, cy = S(p["x"], p["y"])
                w, h = p["w"] * ppm, p["h"] * ppm
                gc.DrawRectangle(cx - w / 2, cy - h / 2, w, h)
                if ppm > 4:
                    gc.SetFont(gc.CreateFont(
                        wx.Font(wx.FontInfo(max(7, min(11, int(h / 3)))),),
                        _C_PLACE))
                    tw, th = gc.GetTextExtent(p["ref"])
                    gc.DrawText(p["ref"], cx - tw / 2, cy - th / 2)


class ComponentTableDialog(wx.Dialog):
    """A wide, resizable window for verifying/editing the classification of every
    part: Ref | Name | Value | Type | Parents | Placed. Name/Type/Parents are
    editable and persist to the sidecar (agents edit the same file)."""

    def __init__(self, parent, board, on_changed=None):
        super().__init__(parent, title="Component table", size=(820, 640),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self.bp = board.GetFileName()
        self.on_changed = on_changed
        g = wx.grid.Grid(self)
        self.grid = g
        g.CreateGrid(0, 7)
        for i, lbl in enumerate(["Ref", "Name", "Value", "Size (mm)", "Type",
                                 "Parents", "Placed"]):
            g.SetColLabelValue(i, lbl)
        for i, w in enumerate((66, 240, 90, 78, 140, 210, 56)):
            g.SetColSize(i, w)
        g.SetRowLabelSize(0)
        tattr = wx.grid.GridCellAttr()
        tattr.SetEditor(wx.grid.GridCellChoiceEditor(
            ["anchor", "subsystem_anchor", "satellite"], allowOthers=False))
        g.SetColAttr(4, tattr)
        self._populate()
        g.Bind(wx.grid.EVT_GRID_CELL_CHANGED, self._on_edit)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(g, 1, wx.EXPAND | wx.ALL, 6)
        b = wx.Button(self, wx.ID_ANY, "Close")
        b.Bind(wx.EVT_BUTTON, self._do_close)
        s.Add(b, 0, wx.ALIGN_RIGHT | wx.ALL, 6)
        self.SetSizer(s)
        self.Bind(wx.EVT_CLOSE, lambda e: self._do_close(e))

    def _do_close(self, _evt):
        try:
            if self.on_changed:
                self.on_changed()
        except Exception:
            pass
        self.Destroy()

    def _populate(self):
        t = placement.effective_table(self.board, self.bp)
        region = placement._board_region(self.board)
        fps = {fp.GetReference(): fp for fp in self.board.GetFootprints()
               if fp.GetReference()}
        rank = {"anchor": 0, "subsystem_anchor": 1, "satellite": 2}
        refs = sorted(t, key=lambda r: (rank.get(t[r]["type"], 9),
                                        (t[r].get("name") or r).lower()))
        g = self.grid
        if g.GetNumberRows():
            g.DeleteRows(0, g.GetNumberRows())
        g.AppendRows(len(refs))
        self._refs = refs
        for i, r in enumerate(refs):
            info = t[r]
            fp = fps.get(r)
            placed = fp is not None and not placement.is_unplaced(fp, region)
            w, h = placement._fp_size(fp) if fp else (0, 0)
            g.SetCellValue(i, 0, r)
            g.SetCellValue(i, 1, info.get("name") or "")
            g.SetCellValue(i, 2, info.get("value", ""))
            g.SetCellValue(i, 3, "%.1f×%.1f" % (w, h))
            g.SetCellValue(i, 4, info["type"])
            g.SetCellValue(i, 5, ", ".join(info.get("parents", [])))
            g.SetCellValue(i, 6, "yes" if placed else "no")
            for c in (0, 2, 3, 6):
                g.SetReadOnly(i, c, True)

    def _on_edit(self, evt):
        r, c = evt.GetRow(), evt.GetCol()
        if c in (1, 4, 5):
            ref = self._refs[r]
            saved = placement.load_table(self.bp)
            cur = saved.setdefault("components", {}).setdefault(ref, {})
            if c == 1:
                cur["name"] = self.grid.GetCellValue(r, 1)
            elif c == 4:
                cur["type"] = self.grid.GetCellValue(r, 4)
            else:
                cur["parents"] = [p.strip() for p in
                                  self.grid.GetCellValue(r, 5).split(",")
                                  if p.strip()]
            placement.save_table(self.bp, saved)
        evt.Skip()

class RouterDialog(wx.Dialog):
    def __init__(self, board):
        super().__init__(None, title="dg-router", size=(1040, 760),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self.nets = [n for n in shim.list_nets(board) if n["pads"] >= 2]
        self.layers = shim.copper_layer_names(board)
        self.status_map = {}
        self.unconn = {}
        self.proposed = []
        self.job = {}          # conn_id -> {'id','net','gap'} : the current job
        self._loaded = False
        self._try_seed = 0
        self._cancel = threading.Event()
        self._proposal = None  # 'route' | 'place' — what Accept will apply
        self._subsys_refs = []      # subsystem refs in list-row order
        self._anchor_refs = []
        self._allsat_refs = []
        self._place_table_cache = {}
        self._undo_positions = {}   # ref -> old (x_nm,y_nm) for Undo
        self._place_proposed = {}   # ref -> (x,y) proposed positions

        panel = wx.Panel(self)
        main = wx.BoxSizer(wx.HORIZONTAL)
        # fixed-width left column so a wide child (the grid) can't squeeze the
        # preview; the preview takes all remaining width
        leftp = wx.Panel(panel)
        leftp.SetMinSize((380, -1))
        leftp.SetMaxSize((400, -1))
        self.leftp = leftp
        left = wx.BoxSizer(wx.VERTICAL)

        # === tabs at the TOP; each tab owns its whole panel ===
        self.tabs = wx.Notebook(leftp)
        route_pg = wx.Panel(self.tabs)
        power_pg = wx.Panel(self.tabs)
        place_pg = wx.Panel(self.tabs)
        self.tabs.AddPage(route_pg, "Route")
        self.tabs.AddPage(power_pg, "Power net")
        self.tabs.AddPage(place_pg, "Place")
        left.Add(self.tabs, 1, wx.EXPAND | wx.ALL, 8)

        self._obj_keys = ["least_obtrusive", "direct", "follow", "hug"]

        # --- Route tab: nets + routing controls ---
        rp = wx.BoxSizer(wx.VERTICAL)
        hdr = wx.BoxSizer(wx.HORIZONTAL)
        hdr.Add(wx.StaticText(route_pg, label="Nets (%d)" % len(self.nets)),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.btn_all = wx.Button(route_pg, label="All", size=(52, -1))
        self.btn_none = wx.Button(route_pg, label="None", size=(60, -1))
        hdr.AddStretchSpacer(1)
        hdr.Add(self.btn_all, 0, wx.RIGHT, 4)
        hdr.Add(self.btn_none, 0)
        rp.Add(hdr, 0, wx.EXPAND | wx.ALL, 6)

        self.net_list = wx.ListCtrl(route_pg,
                                    style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.net_list.EnableCheckBoxes(True)
        self.net_list.InsertColumn(0, "Net", width=200)
        self.net_list.InsertColumn(1, "Status", width=80)
        for n in self.nets:
            row = self.net_list.InsertItem(self.net_list.GetItemCount(),
                                           "%s  (%d pads)" % (n["name"], n["pads"]))
            self.net_list.SetItem(row, 1, "")
            self.net_list.SetItemTextColour(row, _COL_UNKNOWN)
        rp.Add(self.net_list, 1, wx.EXPAND | wx.ALL, 6)

        lrow = wx.BoxSizer(wx.HORIZONTAL)
        lrow.Add(wx.StaticText(route_pg, label="Route on:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.layer_checks = {}
        for name in self.layers:
            cb = wx.CheckBox(route_pg, label=name)
            cb.SetValue(name in ("F.Cu", "B.Cu"))
            self.layer_checks[name] = cb
            lrow.Add(cb, 0, wx.RIGHT, 6)
        rp.Add(lrow, 0, wx.ALL, 6)

        prow = wx.BoxSizer(wx.HORIZONTAL)
        prow.Add(wx.StaticText(route_pg, label="Prefer:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.prefer = wx.Choice(route_pg, choices=["(any)"] + self.layers)
        self.prefer.SetSelection(0)
        prow.Add(self.prefer, 0)
        rp.Add(prow, 0, wx.ALL, 6)

        self.objective = wx.RadioBox(
            route_pg, label="Objective",
            choices=["Least obtrusive", "Direct", "Follow existing", "Hug edges"],
            majorDimension=1, style=wx.RA_SPECIFY_COLS)
        self.objective.SetSelection(0)
        rp.Add(self.objective, 0, wx.EXPAND | wx.ALL, 6)

        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=8)
        grid.AddGrowableCol(1, 1)
        grid.Add(wx.StaticText(route_pg, label="Via cost:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.via_cost = wx.Slider(route_pg, value=10, minValue=0, maxValue=100,
                                  style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        grid.Add(self.via_cost, 0, wx.EXPAND)
        rp.Add(grid, 0, wx.EXPAND | wx.ALL, 6)

        self.chk_debug = wx.CheckBox(route_pg, label="Debug: show A* search heatmap")
        rp.Add(self.chk_debug, 0, wx.ALL, 6)
        self.job_label = wx.StaticText(route_pg, label="Job: empty")
        rp.Add(self.job_label, 0, wx.LEFT | wx.RIGHT, 6)
        self.btn_route = wx.Button(route_pg, label="Route")
        self.btn_route.SetDefault()
        rp.Add(self.btn_route, 0, wx.EXPAND | wx.ALL, 6)
        route_pg.SetSizer(rp)

        # --- Power net tab: trunk a chosen power/multi-pin net ---
        pp = wx.BoxSizer(wx.VERTICAL)
        pp.Add(wx.StaticText(power_pg, label=(
            "Route a power / multi-pin net as a trunk (spine) + branches.")),
            0, wx.ALL, 8)
        pp.Add(wx.StaticText(power_pg, label="Net:"), 0, wx.LEFT | wx.TOP, 8)
        self.trunk_net = wx.Choice(power_pg,
                                   choices=[n["name"] for n in self.nets])
        if self.nets:
            self.trunk_net.SetSelection(0)
        pp.Add(self.trunk_net, 0, wx.EXPAND | wx.ALL, 8)
        self.btn_trunk = wx.Button(power_pg, label="Auto-trunk this net")
        pp.Add(self.btn_trunk, 0, wx.EXPAND | wx.ALL, 8)
        pp.AddStretchSpacer(1)
        power_pg.SetSizer(pp)

        # --- Place tab: Component table + inner tabs by tier ---
        plp = wx.BoxSizer(wx.VERTICAL)
        prow = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_edit_table = wx.Button(place_pg, label="Component table…",
                                        size=(140, -1))
        self.btn_refresh = wx.Button(place_pg, label="Refresh", size=(74, -1))
        self.btn_undo_place = wx.Button(place_pg, label="Undo", size=(60, -1))
        self.btn_reclassify = wx.Button(place_pg, label="Re-infer", size=(78, -1))
        prow.Add(self.btn_edit_table, 0, wx.RIGHT, 6)
        prow.AddStretchSpacer(1)
        prow.Add(self.btn_refresh, 0, wx.RIGHT, 4)
        prow.Add(self.btn_undo_place, 0, wx.RIGHT, 4)
        prow.Add(self.btn_reclassify, 0)
        plp.Add(prow, 0, wx.EXPAND | wx.ALL, 8)
        self.btn_undo_place.Disable()

        self.ptabs = wx.Notebook(place_pg)
        anchors_pg = wx.Panel(self.ptabs)
        subs_pg = wx.Panel(self.ptabs)
        sats_pg = wx.Panel(self.ptabs)
        self.ptabs.AddPage(anchors_pg, "Anchors")
        self.ptabs.AddPage(subs_pg, "Subsystems")
        self.ptabs.AddPage(sats_pg, "Satellites")
        self.ptabs.SetSelection(1)             # default to Subsystems
        plp.Add(self.ptabs, 1, wx.EXPAND | wx.ALL, 6)
        place_pg.SetSizer(plp)

        # Anchors page (usually hand-placed; edge-aware auto-place is a starter)
        ap = wx.BoxSizer(wx.VERTICAL)
        self.anchor_list = wx.ListCtrl(anchors_pg, style=wx.LC_REPORT)
        self.anchor_list.InsertColumn(0, "Anchor", width=210)
        self.anchor_list.InsertColumn(1, "Size(mm)", width=66)
        self.anchor_list.InsertColumn(2, "Placed", width=52)
        ap.Add(self.anchor_list, 1, wx.EXPAND | wx.ALL, 6)
        arow = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_anchor_sel = wx.Button(anchors_pg, label="Place selected")
        self.btn_anchor_all = wx.Button(anchors_pg, label="Place all")
        arow.Add(self.btn_anchor_sel, 1, wx.RIGHT, 6)
        arow.Add(self.btn_anchor_all, 1)
        ap.Add(arow, 0, wx.EXPAND | wx.ALL, 6)
        anchors_pg.SetSizer(ap)

        # Subsystems page
        sp = wx.BoxSizer(wx.VERTICAL)
        self.subsys_list = wx.ListCtrl(subs_pg, style=wx.LC_REPORT)
        self.subsys_list.InsertColumn(0, "Subsystem", width=290)
        self.subsys_list.InsertColumn(1, "Size(mm)", width=62)
        self.subsys_list.InsertColumn(2, "Sats", width=38)
        self.subsys_list.InsertColumn(3, "Placed", width=46)
        sp.Add(self.subsys_list, 1, wx.EXPAND | wx.ALL, 6)
        srow = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_place_anchor = wx.Button(subs_pg, label="Place anchor")
        self.btn_place_sats = wx.Button(subs_pg, label="Place satellites")
        srow.Add(self.btn_place_anchor, 1, wx.RIGHT, 6)
        srow.Add(self.btn_place_sats, 1)
        sp.Add(wx.StaticText(subs_pg, label="Selected subsystem(s):"),
               0, wx.LEFT, 6)
        sp.Add(srow, 0, wx.EXPAND | wx.ALL, 6)
        self.sat_label = wx.StaticText(subs_pg, label="Satellites: —")
        sp.Add(self.sat_label, 0, wx.LEFT | wx.RIGHT, 6)
        self.sat_list = wx.ListCtrl(subs_pg, style=wx.LC_REPORT)
        self.sat_list.InsertColumn(0, "Ref", width=46)
        self.sat_list.InsertColumn(1, "Name", width=210)
        self.sat_list.InsertColumn(2, "Size(mm)", width=62)
        self.sat_list.InsertColumn(3, "Placed", width=46)
        sp.Add(self.sat_list, 1, wx.EXPAND | wx.ALL, 6)
        self.btn_place_one = wx.Button(
            subs_pg, label="Place selected satellite(s)")
        sp.Add(self.btn_place_one, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        subs_pg.SetSizer(sp)

        # Satellites page (flat, grouped by subsystem)
        stp = wx.BoxSizer(wx.VERTICAL)
        self.allsat_list = wx.ListCtrl(sats_pg,
                                       style=wx.LC_REPORT)
        self.allsat_list.InsertColumn(0, "Ref", width=44)
        self.allsat_list.InsertColumn(1, "Name", width=210)
        self.allsat_list.InsertColumn(2, "Subsystem", width=90)
        self.allsat_list.InsertColumn(3, "Size(mm)", width=58)
        self.allsat_list.InsertColumn(4, "Placed", width=44)
        stp.Add(self.allsat_list, 1, wx.EXPAND | wx.ALL, 6)
        strow = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_sat_sel = wx.Button(sats_pg, label="Place selected")
        self.btn_sat_all = wx.Button(sats_pg, label="Place all")
        strow.Add(self.btn_sat_sel, 1, wx.RIGHT, 6)
        strow.Add(self.btn_sat_all, 1)
        stp.Add(strow, 0, wx.EXPAND | wx.ALL, 6)
        sats_pg.SetSizer(stp)

        # === shared action area (applies to whatever the active tab proposed) ===
        self.btn_cancel = wx.Button(leftp, label="Cancel")
        left.Add(self.btn_cancel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.btn_cancel.Hide()

        self.act_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_commit = wx.Button(leftp, label="Accept")
        self.btn_revert = wx.Button(leftp, label="Reject")
        self.btn_try = wx.Button(leftp, label="Try again")
        for b in (self.btn_commit, self.btn_revert, self.btn_try):
            self.act_row.Add(b, 1, wx.RIGHT, 6)
        left.Add(self.act_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.gauge = wx.Gauge(leftp, range=100)
        left.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.gauge.Hide()

        self.status = wx.StaticText(leftp, label="Loading…")
        left.Add(self.status, 0, wx.ALL, 8)

        self.btn_claude = wx.Button(leftp, label="Agent instructions (markdown)")
        left.Add(self.btn_claude, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        leftp.SetSizer(left)

        self.preview = PreviewPanel(panel, board)
        self.preview.on_pad_pick = self._on_pad_pick
        self.preview.on_conn_toggle = self._on_conn_toggle
        main.Add(leftp, 0, wx.EXPAND)
        main.Add(self.preview, 1, wx.EXPAND | wx.ALL, 6)
        panel.SetSizer(main)
        self.panel = panel
        droot = wx.BoxSizer(wx.VERTICAL)
        droot.Add(panel, 1, wx.EXPAND)
        self.SetSizer(droot)

        self.btn_route.Bind(wx.EVT_BUTTON, self.on_route)
        self.btn_trunk.Bind(wx.EVT_BUTTON, self.on_trunk)
        self.btn_cancel.Bind(wx.EVT_BUTTON, self.on_cancel)
        self.btn_place_anchor.Bind(wx.EVT_BUTTON, self.on_place_anchor)
        self.btn_place_sats.Bind(wx.EVT_BUTTON, self.on_place_satellites)
        self.btn_place_one.Bind(wx.EVT_BUTTON, self.on_place_one_sat)
        self.btn_anchor_sel.Bind(wx.EVT_BUTTON, self.on_place_anchors_sel)
        self.btn_anchor_all.Bind(wx.EVT_BUTTON, self.on_place_anchors_all)
        self.btn_sat_sel.Bind(wx.EVT_BUTTON, self.on_place_sat_sel)
        self.btn_sat_all.Bind(wx.EVT_BUTTON, self.on_place_sat_all)
        self.btn_reclassify.Bind(wx.EVT_BUTTON, self.on_reclassify)
        self.btn_refresh.Bind(wx.EVT_BUTTON, self.on_refresh_table)
        self.btn_edit_table.Bind(wx.EVT_BUTTON, self.on_edit_table)
        self.btn_undo_place.Bind(wx.EVT_BUTTON, self.on_undo_place)
        self.subsys_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_subsys_select)
        self.subsys_list.Bind(wx.EVT_LIST_ITEM_DESELECTED, self._on_subsys_select)
        self.ptabs.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_ptab)
        self.tabs.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_tab)
        self.btn_try.Bind(wx.EVT_BUTTON, self.on_try_again)
        self.btn_commit.Bind(wx.EVT_BUTTON, self.on_commit)
        self.btn_revert.Bind(wx.EVT_BUTTON, self.on_revert)
        self.btn_all.Bind(wx.EVT_BUTTON, lambda e: self._check_all(True))
        self.btn_none.Bind(wx.EVT_BUTTON, lambda e: self._check_all(False))
        self.btn_claude.Bind(wx.EVT_BUTTON, self.on_driving)
        self.net_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select)
        self.net_list.Bind(wx.EVT_LIST_ITEM_CHECKED, self.on_net_checked)
        self.net_list.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self.on_net_unchecked)
        self.Bind(wx.EVT_CLOSE, self.on_close)

        self._show_actions(False)
        self._update_route_enabled()
        if self.board.GetFileName():
            self.status.SetLabel("Loading routing status…")
            threading.Thread(target=self._status_worker,
                             args=(self.board.GetFileName(),), daemon=True).start()
        else:
            self.status.SetLabel("Save the board to begin.")

    # --- helpers ---
    def _show_actions(self, show):
        self.act_row.ShowItems(show)
        self.tabs.Enable(not show)            # can't start a new route mid-decision
        if show:
            self.btn_commit.SetDefault()      # Accept is the primary action now
        else:
            self.btn_route.SetDefault()
        self.panel.Layout()

    def on_cancel(self, _evt):
        self._cancel.set()
        self.btn_cancel.Disable()
        self.status.SetLabel("Cancelling…")

    # --- job = a set of connections; net-check and ratline-click both edit it ---
    def _conns_for_net(self, name):
        if not self._loaded:
            return []
        return shim.net_connections(self.board, name, self.unconn)

    def _add_net(self, name):
        for c in self._conns_for_net(name):
            self.job[c["id"]] = {"id": c["id"], "net": name, "gap": c["gap"]}

    def _remove_net(self, name):
        self.job = {k: v for k, v in self.job.items() if v["net"] != name}

    def _on_pad_pick(self, name):
        """Click a pad -> check + select its net (adds all its connections)."""
        for i, n in enumerate(self.nets):
            if n["name"] == name:
                self.net_list.CheckItem(i, True)
                self.net_list.Select(i)
                self.net_list.Focus(i)
                self.net_list.EnsureVisible(i)
                self._add_net(name)
                self._refresh_view()
                return

    def _on_conn_toggle(self, conn):
        """Click a ratline -> toggle that one connection in the job."""
        cid = conn["id"]
        if cid in self.job:
            del self.job[cid]
        else:
            self.job[cid] = {"id": cid, "net": conn["net"], "gap": conn["gap"]}
        self._refresh_view()

    def _update_route_enabled(self):
        self.btn_route.Enable(self._loaded and bool(self.job))
        self.btn_trunk.Enable(self._loaded and bool(self.nets))

    def _checked_names(self):
        return [self.nets[i]["name"] for i in range(self.net_list.GetItemCount())
                if self.net_list.IsItemChecked(i)]

    def _active_name(self):
        sel = self.net_list.GetFirstSelected()
        return self.nets[sel]["name"] if sel != -1 else None

    def _shown_nets(self):
        names = set(self._checked_names())
        a = self._active_name()
        if a:
            names.add(a)
        return sorted(names)

    def _check_all(self, on):
        for i in range(self.net_list.GetItemCount()):
            self.net_list.CheckItem(i, on)
        if on:
            for n in self.nets:
                self._add_net(n["name"])
        else:
            self.job = {}
        self._refresh_view()

    def on_net_checked(self, evt):
        self._add_net(self.nets[evt.GetIndex()]["name"])
        self._refresh_view()

    def on_net_unchecked(self, evt):
        self._remove_net(self.nets[evt.GetIndex()]["name"])
        self._refresh_view()

    def on_select(self, _evt):
        self._refresh_view()

    def _refresh_view(self):
        shown = self._shown_nets()
        self.preview.set_selected(shown, self._active_name())
        conns = []
        for nm in shown:
            for c in self._conns_for_net(nm):
                conns.append({"id": c["id"], "net": nm, "gap": c["gap"]})
        self.preview.set_connections(conns, set(self.job.keys()))
        n = len(self.job)
        nn = len({v["net"] for v in self.job.values()})
        self.job_label.SetLabel(
            "Job: empty" if n == 0 else
            "Job: %d connection%s on %d net%s"
            % (n, "" if n == 1 else "s", nn, "" if nn == 1 else "s"))
        self._update_route_enabled()

    def _refresh_highlight(self):
        self._refresh_view()

    def _apply_status(self):
        for i, n in enumerate(self.nets):
            st = self.status_map.get(n["name"])
            self.net_list.SetItemTextColour(i, _STATUS_COL.get(st, _COL_UNKNOWN))
            self.net_list.SetItem(i, 1, _STATUS_WORD.get(st, ""))

    def _status_worker(self, bp):
        try:
            unconn = shim.drc_unconnected(bp)
        except Exception as e:  # noqa: BLE001
            wx.CallAfter(self.status.SetLabel, "Status unavailable: %s" % e)
            return
        wx.CallAfter(self._status_ready, unconn)

    def _status_ready(self, unconn):
        self.unconn = unconn
        self.preview.set_routing(unconn)
        code_name = {n["code"]: n["name"] for n in self.nets}
        has_cu = {}
        for t in self.board.GetTracks():
            nm = code_name.get(t.GetNetCode())
            if nm:
                has_cu[nm] = True
        self.status_map = {}
        for n in self.nets:
            name = n["name"]
            if len(unconn.get(name, [])) == 0:
                self.status_map[name] = "routed"
            elif has_cu.get(name):
                self.status_map[name] = "partial"
            else:
                self.status_map[name] = "unrouted"
        self._apply_status()
        self._loaded = True
        self._update_route_enabled()
        self._refresh_place_all()    # populate Place once data is ready
        c = {"routed": 0, "partial": 0, "unrouted": 0}
        for x in self.status_map.values():
            c[x] += 1
        self.status.SetLabel("%d routed  %d partial  %d unrouted"
                             % (c["routed"], c["partial"], c["unrouted"]))

    def selected_layers(self):
        chosen = [n for n, cb in self.layer_checks.items() if cb.GetValue()]
        return chosen or ["F.Cu", "B.Cu"]

    def _params(self, jitter=0.0):
        pf = self.prefer.GetStringSelection()
        return router.RouteParams(
            self.board, via_cost=float(self.via_cost.GetValue()),
            layer_names=self.selected_layers(), seed=self._try_seed, jitter=jitter,
            objective=self._obj_keys[self.objective.GetSelection()],
            prefer_layer=(None if pf == "(any)" else pf))

    def _refresh_canvas(self):
        # rebuild connectivity, then force KiCad to repaint. Separate try blocks
        # so a connectivity hiccup can't swallow the repaint; also repaint again
        # deferred, since a mid-handler Refresh doesn't always redraw moved parts.
        try:
            self.board.BuildConnectivity()
        except Exception:
            pass
        try:
            pcbnew.Refresh()
        except Exception:
            pass

        def _again():
            try:
                pcbnew.Refresh()
            except Exception:
                pass
        wx.CallAfter(_again)

    def _job_unconn(self):
        """The current job as a {net: [gaps]} subset for route_batch."""
        sub = {}
        for v in self.job.values():
            sub.setdefault(v["net"], []).append(v["gap"])
        return sub

    def _begin_route(self, label):
        """Shared setup for a routing worker; returns (debug, dbg, on_prog)."""
        self._shown = []
        self._cancel.clear()
        self.preview.set_proposed([])
        self.preview.set_explored(None, None)
        self.status.SetLabel(label)
        self.gauge.SetValue(0)
        self.gauge.Show()
        self.tabs.Disable()
        self.net_list.Disable()
        self.act_row.ShowItems(False)
        self.btn_cancel.Show()
        self.btn_cancel.Enable()
        self.panel.Layout()
        debug = self.chk_debug.GetValue()
        dbg = {"cells": set(), "grid": None}

        def on_prog(cm, new_cells):
            if dbg["grid"] is None:
                dbg["grid"] = (cm.nx, cm.ny, cm.x0, cm.y0, cm.pitch)
            for (i, j, _cl) in new_cells:
                dbg["cells"].add((i, j))
        return debug, dbg, on_prog

    # --- routing (preview-first, ON A THREAD so status/progress stay live) ---
    def _compute(self, jitter):
        sub = self._job_unconn()
        if not sub:
            wx.MessageBox("Build a job first: check nets or click ratlines.",
                          "dg-router")
            return
        bp = self.board.GetFileName()
        if not bp or not os.path.exists(bp):
            wx.MessageBox("Save the board first.", "dg-router")
            return
        names = list(sub.keys())
        debug, dbg, on_prog = self._begin_route("Routing…")
        self.gauge.SetRange(max(1, len(names)))
        params = self._params(jitter=jitter)

        def on_net(done, total, result):
            wx.CallAfter(self._route_net_ui, done, total, result)

        def worker():
            try:
                results = router.route_batch(
                    self.board, names, sub, params, on_net=on_net,
                    on_progress=(on_prog if debug else None),
                    should_cancel=self._cancel.is_set)
                wx.CallAfter(self._route_done, results,
                             dbg if debug else None)
            except Exception as e:  # noqa: BLE001
                wx.CallAfter(self._route_error,
                             "%s\n\n%s" % (e, traceback.format_exc()))

        threading.Thread(target=worker, daemon=True).start()

    def on_trunk(self, _evt):
        name = self.trunk_net.GetStringSelection()
        if not name:
            wx.MessageBox("Pick a net to auto-trunk.", "dg-router")
            return
        bp = self.board.GetFileName()
        if not bp or not os.path.exists(bp):
            wx.MessageBox("Save the board first.", "dg-router")
            return
        debug, dbg, on_prog = self._begin_route("Auto-trunking %s…" % name)
        self.gauge.SetRange(1)
        params = self._params()

        def worker():
            try:
                r = router.auto_trunk(self.board, name, params,
                                      on_progress=(on_prog if debug else None),
                                      should_cancel=self._cancel.is_set)
                wx.CallAfter(self._route_done, [r], dbg if debug else None)
            except Exception as e:  # noqa: BLE001
                wx.CallAfter(self._route_error,
                             "%s\n\n%s" % (e, traceback.format_exc()))

        threading.Thread(target=worker, daemon=True).start()

    def _build_debug_bitmap(self, dbg):
        """Turn the set of explored A* cells into one scaled heatmap bitmap."""
        grid = dbg.get("grid")
        cells = dbg.get("cells")
        if not grid or not cells:
            return None, None
        nx, ny, x0, y0, pitch = grid
        rgb = bytearray(nx * ny * 3)
        alpha = bytearray(nx * ny)
        r, g, b = _C_DEBUG.Red(), _C_DEBUG.Green(), _C_DEBUG.Blue()
        for (i, j) in cells:
            if 0 <= i < nx and 0 <= j < ny:
                idx = j * nx + i
                rgb[idx * 3] = r
                rgb[idx * 3 + 1] = g
                rgb[idx * 3 + 2] = b
                alpha[idx] = 90
        img = wx.Image(nx, ny)
        img.SetData(bytes(rgb))
        img.SetAlpha(bytes(alpha))
        extent = (x0, y0, x0 + nx * pitch, y0 + ny * pitch)
        return extent, img.ConvertToBitmap()

    # --- placement: the component table ------------------------------------
    def _on_tab(self, _evt):
        # On Place, hide our preview and slim to a control strip — placement
        # happens in KiCad's own canvas (where you also nudge the parts). Routing
        # keeps the preview.
        place = self.tabs.GetSelection() == 2
        szr = self.panel.GetSizer()
        szr.Show(self.preview, not place)                 # frees its space
        # on Place: no preview, so the controls fill the width and the window
        # shrinks to just that; on Route: cap the left column, restore full width
        szr.GetItem(self.leftp).SetProportion(1 if place else 0)
        self.leftp.SetMaxSize((-1, -1) if place else (400, -1))
        self.panel.Layout()
        self.SetSize((500 if place else 1040, self.GetSize().height))
        self.panel.Layout()
        if place and self._loaded:
            self._refresh_place_all()

    def _on_ptab(self, _evt):
        if self._loaded:
            self._refresh_place_all()

    def _refresh_place_all(self):
        self._refresh_anchors()
        self._refresh_subsystems()
        self._refresh_satellites()

    def _place_table(self):
        return placement.effective_table(self.board, self.board.GetFileName())

    def _refresh_anchors(self):
        table = self._place_table()
        region = placement._board_region(self.board)
        fps = {fp.GetReference(): fp for fp in self.board.GetFootprints()
               if fp.GetReference()}
        refs = sorted((r for r, i in table.items() if i["type"] == "anchor"),
                      key=lambda r: (table[r].get("name") or r).lower())
        self._anchor_refs = refs
        L = self.anchor_list
        L.DeleteAllItems()
        for r in refs:
            fp = fps.get(r)
            placed = fp is not None and not placement.is_unplaced(fp, region)
            nm = table[r].get("name") or table[r].get("value") or r
            w, h = placement._fp_size(fp) if fp else (0, 0)
            row = L.InsertItem(L.GetItemCount(), "%s (%s)" % (r, nm))
            L.SetItem(row, 1, "%.1f×%.1f" % (w, h))
            L.SetItem(row, 2, "yes" if placed else "no")

    def _refresh_satellites(self):
        table = self._place_table()
        region = placement._board_region(self.board)
        fps = {fp.GetReference(): fp for fp in self.board.GetFootprints()
               if fp.GetReference()}
        refs = sorted((r for r, i in table.items() if i["type"] == "satellite"),
                      key=lambda r: ((table[r]["parents"][0]
                                      if table[r].get("parents") else "~"),
                                     r))
        self._allsat_refs = refs
        L = self.allsat_list
        L.DeleteAllItems()
        for r in refs:
            info = table[r]
            fp = fps.get(r)
            placed = fp is not None and not placement.is_unplaced(fp, region)
            par = info["parents"][0] if info.get("parents") else "-"
            pname = table.get(par, {}).get("value") or par
            w, h = placement._fp_size(fp) if fp else (0, 0)
            row = L.InsertItem(L.GetItemCount(), r)
            L.SetItem(row, 1, info.get("name") or info.get("value") or "")
            L.SetItem(row, 2, pname)
            L.SetItem(row, 3, "%.1f×%.1f" % (w, h))
            L.SetItem(row, 4, "yes" if placed else "no")

    def _selected_rows(self, listctrl, refs):
        out, i = [], listctrl.GetFirstSelected()
        while i != -1:
            out.append(refs[i])
            i = listctrl.GetNextSelected(i)
        return out

    def on_place_anchors_sel(self, _evt):
        refs = set(self._selected_rows(self.anchor_list, self._anchor_refs))
        if not refs:
            wx.MessageBox("Select anchors to place.", "dg-router")
            return
        t = self._place_table()
        self._propose_placements(
            {r: xy for r, xy in placement.place_anchors(
                self.board, t, reposition=refs).items() if r in refs},
            "anchor(s)", requested=len(refs))

    def on_place_anchors_all(self, _evt):
        t = self._place_table()
        self._propose_placements(placement.place_anchors(self.board, t),
                                 "anchor(s)")

    def on_place_sat_sel(self, _evt):
        refs = set(self._selected_rows(self.allsat_list, self._allsat_refs))
        if not refs:
            wx.MessageBox("Select satellites to place.", "dg-router")
            return
        t = self._place_table()
        self._propose_placements(
            {r: xy for r, xy in placement.place_satellites(
                self.board, t, reposition=refs).items() if r in refs},
            "satellite(s)", requested=len(refs))

    def on_place_sat_all(self, _evt):
        t = self._place_table()
        self._propose_placements(placement.place_satellites(self.board, t),
                                 "satellite(s)")

    def _refresh_subsystems(self):
        """Fill the subsystem list (by human name) + the satellite detail."""
        table = self._place_table()
        self._place_table_cache = table
        region = placement._board_region(self.board)
        fps = {fp.GetReference(): fp for fp in self.board.GetFootprints()
               if fp.GetReference()}
        subs = sorted((r for r, i in table.items()
                       if i["type"] == "subsystem_anchor"),
                      key=lambda r: (table[r].get("name") or r).lower())
        self._subsys_refs = subs
        L = self.subsys_list
        L.DeleteAllItems()
        for r in subs:
            info = table[r]
            fp = fps.get(r)
            placed = fp is not None and not placement.is_unplaced(fp, region)
            nm = info.get("name") or info.get("value") or r
            w, h = placement._fp_size(fp) if fp else (0, 0)
            row = L.InsertItem(L.GetItemCount(), "%s (%s)" % (r, nm))
            L.SetItem(row, 1, "%.1f×%.1f" % (w, h))
            L.SetItem(row, 2, str(len(placement.satellites_of(table, r))))
            L.SetItem(row, 3, "yes" if placed else "no")
        self._refresh_sat_detail()

    def _selected_subsys_refs(self):
        out, i = [], self.subsys_list.GetFirstSelected()
        while i != -1:
            out.append(self._subsys_refs[i])
            i = self.subsys_list.GetNextSelected(i)
        return out

    def _refresh_sat_detail(self):
        self.sat_list.DeleteAllItems()
        refs = self._selected_subsys_refs()
        table = getattr(self, "_place_table_cache", {})
        if not refs or not table:
            self.sat_label.SetLabel("Satellites: —")
            return
        region = placement._board_region(self.board)
        fps = {fp.GetReference(): fp for fp in self.board.GetFootprints()
               if fp.GetReference()}
        r0 = refs[0]
        name = table[r0].get("name") or r0
        sats = placement.satellites_of(table, r0)
        self._sat_detail_refs = sats
        self.sat_label.SetLabel("Satellites of %s (%d):" % (name, len(sats)))
        for s in sats:
            fp = fps.get(s)
            placed = fp is not None and not placement.is_unplaced(fp, region)
            w, h = placement._fp_size(fp) if fp else (0, 0)
            row = self.sat_list.InsertItem(self.sat_list.GetItemCount(), s)
            self.sat_list.SetItem(row, 1, table[s].get("name")
                                  or table[s].get("value", ""))
            self.sat_list.SetItem(row, 2, "%.1f×%.1f" % (w, h))
            self.sat_list.SetItem(row, 3, "yes" if placed else "no")

    def _on_subsys_select(self, _evt):
        self._refresh_sat_detail()

    def on_edit_table(self, _evt):
        """Open the full component table (Ref/Name/Value/Type/Parents) in a wide
        window; refresh the subsystem list when it closes."""
        dlg = ComponentTableDialog(self, self.board,
                                   on_changed=self._refresh_subsystems)
        dlg.Show()

    def on_refresh_table(self, _evt):
        # re-read the sidecar JSON (e.g. after an external agent edited it) and
        # repopulate — keeps all edits, unlike Re-infer
        self._refresh_place_all()
        self.status.SetLabel("Reloaded classification from sidecar.")

    def on_reclassify(self, _evt):
        placement.save_table(self.board.GetFileName(), {"components": {}})
        self._refresh_place_all()
        self.status.SetLabel("Re-inferred subsystems (overrides cleared).")

    def _propose_placements(self, proposed, what, requested=None):
        """Place directly into KiCad (no ghost/accept) — you tweak in the canvas,
        then place the next. Remembers prior positions so 'Undo' can restore.
        Parts with no free spot are REJECTED (never stacked); report how many."""
        if not proposed:
            wx.MessageBox("No room to place without overlapping — free up space "
                          "or place fewer at a time.", "dg-router")
            return
        _NM = 1e6
        try:
            fps = {fp.GetReference(): fp for fp in self.board.GetFootprints()
                   if fp.GetReference()}
            self._undo_positions = {}
            n = 0
            for ref, (x, y) in proposed.items():
                fp = fps.get(ref)
                if not fp:
                    continue
                p = fp.GetPosition()
                self._undo_positions[ref] = (p.x, p.y)
                fp.SetPosition(pcbnew.VECTOR2I(int(x * _NM), int(y * _NM)))
                n += 1
            self._refresh_canvas()      # KiCad canvas now shows the moved parts
            self._refresh_place_all()
        except Exception as e:  # noqa: BLE001
            self._text_dialog("Placement error",
                              "%s\n\n%s" % (e, traceback.format_exc()))
            return
        self.btn_undo_place.Enable(bool(self._undo_positions))
        msg = "Placed %d %s in KiCad — nudge there, then place the next." % (n, what)
        if requested and requested > n:
            msg = ("Placed %d of %d %s (%d had no room — freed nothing, never "
                   "stacked). " % (n, requested, what, requested - n)) + \
                "Nudge/free space, then retry."
        self.status.SetLabel(msg)

    def on_undo_place(self, _evt):
        for ref, (x, y) in self._undo_positions.items():
            fp = self.board.FindFootprintByReference(ref)
            if fp:
                fp.SetPosition(pcbnew.VECTOR2I(x, y))
        self._undo_positions = {}
        self._refresh_canvas()
        self._refresh_place_all()
        self.btn_undo_place.Disable()
        self.status.SetLabel("Undid last placement.")

    def on_place_anchor(self, _evt):
        # explicit selection = opt-in to (re)place, even if already placed
        refs = set(self._selected_subsys_refs())
        if not refs:
            wx.MessageBox("Select one or more subsystems first.", "dg-router")
            return
        table = self._place_table()
        proposed = {r: xy for r, xy in placement.place_subsystems(
            self.board, table, reposition=refs).items() if r in refs}
        self._propose_placements(proposed, "anchor(s)", requested=len(refs))

    def on_place_satellites(self, _evt):
        refs = self._selected_subsys_refs()
        if not refs:
            wx.MessageBox("Select one or more subsystems first.", "dg-router")
            return
        table = self._place_table()
        want = set()
        for r in refs:
            want.update(placement.satellites_of(table, r))
        proposed = {r: xy for r, xy in placement.place_satellites(
            self.board, table, reposition=want).items() if r in want}
        self._propose_placements(proposed, "satellite(s)", requested=len(want))

    def on_place_one_sat(self, _evt):
        # place just the satellite(s) selected in the detail list — one at a time
        refs = set(self._selected_rows(self.sat_list,
                                       getattr(self, "_sat_detail_refs", [])))
        if not refs:
            wx.MessageBox("Select satellite(s) in the list below first.",
                          "dg-router")
            return
        t = self._place_table()
        self._propose_placements(
            {r: xy for r, xy in placement.place_satellites(
                self.board, t, reposition=refs).items() if r in refs},
            "satellite(s)", requested=len(refs))

    def on_place_all_anchors(self, _evt):
        table = self._place_table()
        self._propose_placements(
            placement.place_subsystems(self.board, table), "anchor(s)")

    def on_place_all_satellites(self, _evt):
        table = self._place_table()
        self._propose_placements(
            placement.place_satellites(self.board, table), "satellite(s)")

    def _route_net_ui(self, done, total, result):
        self._shown.append(result)
        self.preview.set_proposed(list(self._shown))
        self.gauge.SetRange(max(1, total))
        self.gauge.SetValue(done)
        self.status.SetLabel("Routing %d/%d — %s" % (done, total, result["net"]))

    def _finish_route(self):
        self.gauge.Hide()
        self.btn_cancel.Hide()
        self.net_list.Enable()
        self.tabs.Enable()
        self.panel.Layout()
        self._update_route_enabled()

    def _route_error(self, text):
        self._finish_route()
        self.status.SetLabel("Routing error — see details")
        self._text_dialog("Routing error", text)

    def _route_done(self, results, dbg=None):
        self._finish_route()
        cancelled = self._cancel.is_set()
        self.proposed = results
        self.preview.set_proposed(results)
        if dbg is not None:
            extent, bmp = self._build_debug_bitmap(dbg)
            self.preview.set_explored(extent, bmp)
        # frame what we proposed so it's immediately legible
        xs, ys = [], []
        for r in results:
            for (x1, y1, x2, y2, _w, _l) in r.get("segments", []):
                xs += [x1, x2]
                ys += [y1, y2]
            for (x, y, _d, _dr) in r.get("vias", []):
                xs.append(x)
                ys.append(y)
        if xs:
            self.preview.zoom_to_bbox(min(xs), min(ys), max(xs), max(ys))
        seg = sum(len(r.get("segments", [])) for r in results)
        via = sum(len(r.get("vias", [])) for r in results)
        if cancelled and seg == 0 and via == 0:
            self.status.SetLabel("Cancelled.")
            self._show_actions(False)
            return
        if seg == 0 and via == 0:
            why = "\n".join("  %s: %s" % (r["net"], r.get("reason") or "no path")
                            for r in results if not r.get("ok"))
            self.status.SetLabel("Couldn't route — see details")
            self._text_dialog("Couldn't route",
                              "No tracks were produced for:\n\n" + why +
                              "\n\nTry: more layers, higher via cost, a different "
                              "objective, or route fewer/other nets first.")
            self._show_actions(False)
            return
        bad = sum(1 for r in results if not r.get("ok"))
        msg = "Proposed %d tracks, %d vias" % (seg, via)
        if bad:
            msg += "  (%d incomplete)" % bad
        self.status.SetLabel(msg + " — Accept / Reject / Try again")
        self._show_actions(True)

    def on_route(self, _evt):
        self._try_seed = 0
        self._compute(0.0)

    def on_try_again(self, _evt):
        self._try_seed += 1
        self._compute(0.35)

    def on_revert(self, _evt):
        if self._proposal == "place":
            self._place_proposed = {}
            self._proposal = None
            self.preview.set_placements([])
            self.status.SetLabel("Rejected placements.")
            self._show_actions(False)
            self._refresh_place_all()
            return
        self.proposed = []
        self.preview.set_proposed([])
        self.status.SetLabel("Rejected. Pick nets and Route again.")
        self._reset_pass()

    def _commit_placements(self):
        _NM = 1e6
        fps = {fp.GetReference(): fp for fp in self.board.GetFootprints()
               if fp.GetReference()}
        n = 0
        for ref, (x, y) in self._place_proposed.items():
            fp = fps.get(ref)
            if fp:
                fp.SetPosition(pcbnew.VECTOR2I(int(x * _NM), int(y * _NM)))
                n += 1
        self._place_proposed = {}
        self._proposal = None
        self.preview.set_placements([])
        self._refresh_canvas()
        # parts moved -> re-render the preview from a temp copy (never the real file)
        tmp = os.path.join(tempfile.gettempdir(), "dg-place-preview.kicad_pcb")
        try:
            pcbnew.SaveBoard(tmp, self.board)
            self.preview.rerender(tmp)
        except Exception:
            pass
        self._show_actions(False)
        self._refresh_place_all()
        self.status.SetLabel("Placed %d parts (Cmd+S to save)." % n)

    def on_commit(self, _evt):
        if self._proposal == "place":
            self._commit_placements()
            return
        added = 0
        committed = []
        for r in self.proposed:
            if r.get("segments") or r.get("vias"):
                added += len(router.write_result(self.board, r["net_code"], r))
                committed.append(r)
        router.refill_zones(self.board)
        self._refresh_canvas()
        self.proposed = []
        self.preview.add_accepted(committed)   # keep them visible next pass (h)
        self.preview.set_proposed([])
        self._reset_pass()
        # Re-derive status/ratsnest so partial-net jobs are accurate. DRC reads a
        # file, but we must NOT touch the user's .kicad_pcb — save a TEMP copy and
        # DRC that. (He still saves his own board with Cmd+S.)
        self.status.SetLabel("Accepted %d items (Cmd+S to save) — refreshing…"
                             % added)
        tmp = os.path.join(tempfile.gettempdir(), "dg-accept-drc.kicad_pcb")
        try:
            pcbnew.SaveBoard(tmp, self.board)
            threading.Thread(target=self._status_worker, args=(tmp,),
                             daemon=True).start()
        except Exception:
            pass

    def _reset_pass(self):
        for i in range(self.net_list.GetItemCount()):
            if self.net_list.IsItemChecked(i):
                self.net_list.CheckItem(i, False)
        self.job = {}
        self._refresh_view()
        self._show_actions(False)

    def _text_dialog(self, title, text, size=(720, 640)):
        """A read-only, COPYABLE text window (errors, agent instructions)."""
        d = wx.Dialog(self, title=title, size=size,
                      style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        tc = wx.TextCtrl(d, value=text,
                         style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        tc.SetFont(wx.Font(wx.FontInfo(11).Family(wx.FONTFAMILY_TELETYPE)))
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(tc, 1, wx.EXPAND | wx.ALL, 8)
        d.SetSizer(s)
        d.ShowModal()
        d.Destroy()

    def on_driving(self, _evt):
        # show the doc for the ACTIVE tab: Place -> PLACEMENT.md, else DRIVING.md
        doc = "PLACEMENT.md" if self.tabs.GetSelection() == 2 else "DRIVING.md"
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.realpath(__file__))), "docs", doc)
        try:
            with open(path) as f:
                text = f.read()
        except Exception:
            text = ("See docs/%s in the dg-router repo — headless.py is the CLI "
                    "for driving this from an agent." % doc)
        self._text_dialog("Agent instructions — %s" % doc, text)

    def on_close(self, _evt):
        if self in _OPEN:
            _OPEN.remove(self)
        self.Destroy()


def show_dialog(board):
    for d in _OPEN:                 # one window only — raise the existing one
        try:
            d.Raise()
            d.Iconize(False)
            return
        except Exception:
            pass
    dlg = RouterDialog(board)
    _OPEN.append(dlg)
    dlg.Show()
