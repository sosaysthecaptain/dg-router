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

from . import shim
from . import router

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

_OPEN = []   # keep non-modal dialogs alive


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
        self.on_pad_pick = None          # callback(net_name) on a pad click
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
        # a click (no meaningful drag) selects the net under the cursor
        if self._down is not None:
            dx = evt.GetX() - self._down[0]
            dy = evt.GetY() - self._down[1]
            if dx * dx + dy * dy <= 16:      # <=4px movement == a click
                self._pick_net_at(evt.GetX(), evt.GetY())
        self._down = None

    def _pick_net_at(self, mx, my):
        if self.bg_bmp is None or self.origin is None or not self.on_pad_pick:
            return
        ppm = self._ppm()
        bx0, by0 = self._origin_screen(ppm)
        orx, ory = self.origin
        wx_mm = orx + (mx - bx0) / ppm
        wy_mm = ory + (my - by0) / ppm
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
        if self.selected or self.proposed or self.accepted:
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

        for name in self.selected:
            g = self._geom_cache.get(name)
            if not g:
                continue
            is_active = (name == self.active)
            rat_col = _C_ACTIVE if is_active else _C_TODO
            rat_w = 3.0 if is_active else 2.0
            for (x1, y1, x2, y2) in self._ratsnest_for(name, g):
                glow_line(S(x1, y1), S(x2, y2), rat_col, rat_w, True)
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
        self._loaded = False
        self._try_seed = 0

        panel = wx.Panel(self)
        main = wx.BoxSizer(wx.HORIZONTAL)
        left = wx.BoxSizer(wx.VERTICAL)

        hdr = wx.BoxSizer(wx.HORIZONTAL)
        hdr.Add(wx.StaticText(panel, label="Nets (%d)" % len(self.nets)),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.btn_all = wx.Button(panel, label="All", size=(52, -1))
        self.btn_none = wx.Button(panel, label="None", size=(60, -1))
        hdr.AddStretchSpacer(1)
        hdr.Add(self.btn_all, 0, wx.RIGHT, 4)
        hdr.Add(self.btn_none, 0)
        left.Add(hdr, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self.net_list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.net_list.EnableCheckBoxes(True)
        self.net_list.InsertColumn(0, "Net", width=210)
        self.net_list.InsertColumn(1, "Status", width=84)
        for n in self.nets:
            row = self.net_list.InsertItem(self.net_list.GetItemCount(),
                                           "%s  (%d pads)" % (n["name"], n["pads"]))
            self.net_list.SetItem(row, 1, "")
            self.net_list.SetItemTextColour(row, _COL_UNKNOWN)
        left.Add(self.net_list, 1, wx.EXPAND | wx.ALL, 8)

        lrow = wx.BoxSizer(wx.HORIZONTAL)
        lrow.Add(wx.StaticText(panel, label="Route on:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.layer_checks = {}
        for name in self.layers:
            cb = wx.CheckBox(panel, label=name)
            cb.SetValue(name in ("F.Cu", "B.Cu"))
            self.layer_checks[name] = cb
            lrow.Add(cb, 0, wx.RIGHT, 6)
        left.Add(lrow, 0, wx.LEFT | wx.BOTTOM, 8)

        prow = wx.BoxSizer(wx.HORIZONTAL)
        prow.Add(wx.StaticText(panel, label="Prefer:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.prefer = wx.Choice(panel, choices=["(any)"] + self.layers)
        self.prefer.SetSelection(0)
        prow.Add(self.prefer, 0)
        left.Add(prow, 0, wx.LEFT | wx.BOTTOM, 8)

        self._obj_keys = ["least_obtrusive", "direct", "follow", "hug"]
        self.objective = wx.RadioBox(
            panel, label="Objective",
            choices=["Least obtrusive", "Direct", "Follow existing", "Hug edges"],
            majorDimension=1, style=wx.RA_SPECIFY_COLS)   # vertical stack
        self.objective.SetSelection(0)   # least obtrusive default
        left.Add(self.objective, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=8)
        grid.AddGrowableCol(1, 1)
        grid.Add(wx.StaticText(panel, label="Via cost:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.via_cost = wx.Slider(panel, value=10, minValue=0, maxValue=100,
                                  style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        grid.Add(self.via_cost, 0, wx.EXPAND)
        left.Add(grid, 0, wx.EXPAND | wx.ALL, 8)

        self.chk_debug = wx.CheckBox(panel, label="Debug: show A* search heatmap")
        left.Add(self.chk_debug, 0, wx.LEFT | wx.BOTTOM, 8)

        self.btn_route = wx.Button(panel, label="Route")
        self.btn_route.SetDefault()
        left.Add(self.btn_route, 0, wx.EXPAND | wx.ALL, 8)

        self.act_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_commit = wx.Button(panel, label="Accept")
        self.btn_revert = wx.Button(panel, label="Reject")
        self.btn_try = wx.Button(panel, label="Try again")
        for b in (self.btn_commit, self.btn_revert, self.btn_try):
            self.act_row.Add(b, 1, wx.RIGHT, 6)
        left.Add(self.act_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.gauge = wx.Gauge(panel, range=100)
        left.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.gauge.Hide()

        self.status = wx.StaticText(panel, label="Loading…")
        left.Add(self.status, 0, wx.ALL, 8)

        self.btn_claude = wx.Button(panel, label="Agent instructions (markdown)")
        left.Add(self.btn_claude, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.preview = PreviewPanel(panel, board)
        self.preview.on_pad_pick = self._on_pad_pick
        main.Add(left, 0, wx.EXPAND)
        main.SetItemMinSize(left, 360, -1)
        main.Add(self.preview, 1, wx.EXPAND | wx.ALL, 6)
        panel.SetSizer(main)
        self.panel = panel
        droot = wx.BoxSizer(wx.VERTICAL)
        droot.Add(panel, 1, wx.EXPAND)
        self.SetSizer(droot)

        self.btn_route.Bind(wx.EVT_BUTTON, self.on_route)
        self.btn_try.Bind(wx.EVT_BUTTON, self.on_try_again)
        self.btn_commit.Bind(wx.EVT_BUTTON, self.on_commit)
        self.btn_revert.Bind(wx.EVT_BUTTON, self.on_revert)
        self.btn_all.Bind(wx.EVT_BUTTON, lambda e: self._check_all(True))
        self.btn_none.Bind(wx.EVT_BUTTON, lambda e: self._check_all(False))
        self.btn_claude.Bind(wx.EVT_BUTTON, self.on_driving)
        self.net_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select)
        self.net_list.Bind(wx.EVT_LIST_ITEM_CHECKED, self.on_check)
        self.net_list.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self.on_check)
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
        self.btn_route.Show(not show)
        if show:
            self.btn_commit.SetDefault()      # Accept is the primary action now
        else:
            self.btn_route.SetDefault()
        self.panel.Layout()

    def _on_pad_pick(self, name):
        """A pad was clicked in the preview: check + select that net."""
        for i, n in enumerate(self.nets):
            if n["name"] == name:
                self.net_list.CheckItem(i, True)
                self.net_list.Select(i)
                self.net_list.Focus(i)
                self.net_list.EnsureVisible(i)
                self._update_route_enabled()
                self._refresh_highlight()
                return

    def _update_route_enabled(self):
        self.btn_route.Enable(self._loaded and bool(self._checked_names()))

    def _checked_names(self):
        return [self.nets[i]["name"] for i in range(self.net_list.GetItemCount())
                if self.net_list.IsItemChecked(i)]

    def _active_name(self):
        sel = self.net_list.GetFirstSelected()
        return self.nets[sel]["name"] if sel != -1 else None

    def _check_all(self, on):
        for i in range(self.net_list.GetItemCount()):
            self.net_list.CheckItem(i, on)
        self._update_route_enabled()
        self._refresh_highlight()

    def on_check(self, _evt):
        self._update_route_enabled()
        self._refresh_highlight()

    def on_select(self, _evt):
        self._refresh_highlight()

    def _refresh_highlight(self):
        active = self._active_name()
        names = set(self._checked_names())
        if active:
            names.add(active)
        self.preview.set_selected(sorted(names), active)

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
        try:
            self.board.BuildConnectivity()
            pcbnew.Refresh()
        except Exception:
            pass

    # --- routing (preview-first, ON A THREAD so status/progress stay live) ---
    def _compute(self, jitter):
        names = self._checked_names()
        if not names:
            wx.MessageBox("Check one or more nets to route first.", "dg-router")
            return
        bp = self.board.GetFileName()
        if not bp or not os.path.exists(bp):
            wx.MessageBox("Save the board first.", "dg-router")
            return
        self._shown = []
        self.preview.set_proposed([])
        self.preview.set_explored(None, None)   # clear any prior heatmap
        self.status.SetLabel("Routing…")
        self.gauge.SetRange(max(1, len(names)))
        self.gauge.SetValue(0)
        self.gauge.Show()
        self.panel.Layout()
        # lock interaction while the worker reads the board (avoid concurrent
        # pcbnew access + double-route)
        self.btn_route.Disable()
        self.net_list.Disable()

        params = self._params(jitter=jitter)
        cached = self.unconn
        debug = self.chk_debug.GetValue()

        def on_net(done, total, result):
            wx.CallAfter(self._route_net_ui, done, total, result)

        dbg = {"cells": set(), "grid": None}

        def on_prog(cm, new_cells):
            if dbg["grid"] is None:
                dbg["grid"] = (cm.nx, cm.ny, cm.x0, cm.y0, cm.pitch)
            for (i, j, _cl) in new_cells:
                dbg["cells"].add((i, j))

        def worker():
            try:
                unconn = cached or shim.drc_unconnected(bp)
                results = router.route_batch(
                    self.board, names, unconn, params, on_net=on_net,
                    on_progress=(on_prog if debug else None))
                wx.CallAfter(self._route_done, results,
                             dbg if debug else None)
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

    def _route_net_ui(self, done, total, result):
        self._shown.append(result)
        self.preview.set_proposed(list(self._shown))
        self.gauge.SetRange(max(1, total))
        self.gauge.SetValue(done)
        self.status.SetLabel("Routing %d/%d — %s" % (done, total, result["net"]))

    def _finish_route(self):
        self.gauge.Hide()
        self.panel.Layout()
        self.net_list.Enable()

    def _route_error(self, text):
        self._finish_route()
        self.btn_route.Enable()
        self.status.SetLabel("Routing error — see details")
        self._text_dialog("Routing error", text)

    def _route_done(self, results, dbg=None):
        self._finish_route()
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
        if seg == 0 and via == 0:
            self.btn_route.Enable()
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
        self.proposed = []
        self.preview.set_proposed([])
        self.status.SetLabel("Rejected. Pick nets and Route again.")
        self._reset_pass()

    def on_commit(self, _evt):
        added = 0
        done = []
        committed = []
        for r in self.proposed:
            if r.get("segments") or r.get("vias"):
                added += len(router.write_result(self.board, r["net_code"], r))
                committed.append(r)
            done.append(r["net"])
        router.refill_zones(self.board)
        self._refresh_canvas()
        for name in done:
            self.status_map[name] = "routed"
            self.unconn[name] = []
        self._apply_status()
        self.proposed = []
        self.preview.add_accepted(committed)   # keep them visible next pass (h)
        self.preview.set_proposed([])
        self.preview.set_routing(self.unconn)
        self.status.SetLabel("Accepted %d items (Cmd+S to save). Route again."
                             % added)
        self._reset_pass()

    def _reset_pass(self):
        for i in range(self.net_list.GetItemCount()):
            if self.net_list.IsItemChecked(i):
                self.net_list.CheckItem(i, False)
        self._refresh_highlight()
        self._show_actions(False)
        self._update_route_enabled()

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
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.realpath(__file__))), "docs", "DRIVING.md")
        try:
            with open(path) as f:
                text = f.read()
        except Exception:
            text = ("See docs/DRIVING.md in the dg-router repo — the headless "
                    "CLI (headless.py) is the API for driving from an agent.")
        self._text_dialog("Agent instructions", text)

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
