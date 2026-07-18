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
_C_PROP = {pcbnew.F_Cu: wx.Colour(0, 229, 255),
           pcbnew.B_Cu: wx.Colour(255, 150, 0)}
_C_PROP_DEFAULT = wx.Colour(230, 230, 230)
_C_PROP_VIA = wx.Colour(255, 235, 59)
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
        self.zoom = min(80.0, max(0.3, self.zoom * factor))
        ppm2 = self._ppm()
        cw, ch = self.GetClientSize()
        self.panx = mx - (wxmm - self.origin[0]) * ppm2 - (cw - self.vbw * ppm2) / 2.0
        self.pany = my - (wymm - self.origin[1]) * ppm2 - (ch - self.vbh * ppm2) / 2.0
        self.Refresh()
        self._rtimer.StartOnce(140)

    def on_wheel(self, evt):
        if self.bg_bmp is None:
            return
        d = evt.GetWheelRotation()
        if evt.ControlDown() or evt.CmdDown():   # modifier + scroll = zoom
            self._zoom_at(1.2 if d > 0 else 1 / 1.2, evt.GetX(), evt.GetY())
            return
        # two-finger / wheel scroll = PAN (KiCad convention)
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
            target = max(0.3, min(80.0, self._pinch_base * evt.GetZoomFactor()))
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
        self.CaptureMouse()

    def on_up(self, _evt):
        if self.HasCapture():
            self.ReleaseMouse()
        self._drag = None

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

        for name in self.selected:
            g = self._geom_cache.get(name)
            if not g:
                continue
            is_active = (name == self.active)
            rat_col = _C_ACTIVE if is_active else _C_TODO
            rat_w = 2.5 if is_active else 1.5
            gc.SetPen(wx.Pen(rat_col, rat_w, wx.PENSTYLE_SHORT_DASH))
            for (x1, y1, x2, y2) in self._ratsnest_for(name, g):
                a, b = S(x1, y1), S(x2, y2)
                gc.StrokeLine(a[0], a[1], b[0], b[1])
            for (x1, y1, x2, y2, w) in g["tracks"]:
                gc.SetPen(wx.Pen(_C_KEPT, max(1.0, w * ppm)))
                a, b = S(x1, y1), S(x2, y2)
                gc.StrokeLine(a[0], a[1], b[0], b[1])
            gc.SetBrush(wx.Brush(wx.Colour(255, 235, 59, 150 if is_active else 90)))
            gc.SetPen(wx.Pen(_C_PAD, 1.0))
            for (x, y, r) in g["pads"]:
                cx, cy = S(x, y)
                rr = max(r * ppm, 4)
                gc.DrawEllipse(cx - rr, cy - rr, 2 * rr, 2 * rr)

        for res in self.proposed:
            for (x1, y1, x2, y2, w, lyr) in res.get("segments", []):
                gc.SetPen(wx.Pen(_C_PROP.get(lyr, _C_PROP_DEFAULT), max(1.0, w * ppm)))
                a, b = S(x1, y1), S(x2, y2)
                gc.StrokeLine(a[0], a[1], b[0], b[1])
            gc.SetBrush(wx.Brush(_C_PROP_VIA))
            gc.SetPen(wx.Pen(wx.Colour(120, 90, 0), 1.0))
            for (x, y, dia, drill) in res.get("vias", []):
                cx, cy = S(x, y)
                rr = dia / 2.0 * ppm
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

        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=8)
        grid.AddGrowableCol(1, 1)
        grid.Add(wx.StaticText(panel, label="Via cost:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.via_cost = wx.Slider(panel, value=10, minValue=0, maxValue=100,
                                  style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        grid.Add(self.via_cost, 0, wx.EXPAND)
        left.Add(grid, 0, wx.EXPAND | wx.ALL, 8)

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

        self.status = wx.StaticText(panel, label="Loading…")
        left.Add(self.status, 0, wx.ALL, 8)

        self.preview = PreviewPanel(panel, board)
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
        self.panel.Layout()

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
        return router.RouteParams(
            self.board, via_cost=float(self.via_cost.GetValue()),
            layer_names=self.selected_layers(), seed=self._try_seed, jitter=jitter)

    def _refresh_canvas(self):
        try:
            self.board.BuildConnectivity()
            pcbnew.Refresh()
        except Exception:
            pass

    # --- routing (preview-first: proposed shown in the panel, not the board) ---
    def _compute(self, jitter):
        names = self._checked_names()
        if not names:
            wx.MessageBox("Check one or more nets to route first.", "dg-router")
            return
        bp = self.board.GetFileName()
        if not bp or not os.path.exists(bp):
            wx.MessageBox("Save the board first.", "dg-router")
            return
        self.status.SetLabel("Routing…")
        wx.BeginBusyCursor()
        try:
            unconn = self.unconn or shim.drc_unconnected(bp)
            results = router.route_batch(self.board, names, unconn,
                                         self._params(jitter=jitter))
        except Exception as e:  # noqa: BLE001
            wx.EndBusyCursor()
            wx.MessageBox("Routing error:\n%s\n\n%s" % (e, traceback.format_exc()),
                          "dg-router")
            return
        wx.EndBusyCursor()

        self.proposed = results
        self.preview.set_proposed(results)   # shown in preview only
        seg = sum(len(r.get("segments", [])) for r in results)
        via = sum(len(r.get("vias", [])) for r in results)
        if seg == 0 and via == 0:
            why = "; ".join("%s: %s" % (r["net"], r.get("reason") or "no path")
                            for r in results if not r.get("ok"))
            self.status.SetLabel("Couldn't route — " + why[:80])
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
        for r in self.proposed:
            if r.get("segments") or r.get("vias"):
                added += len(router.write_result(self.board, r["net_code"], r))
            done.append(r["net"])
        router.refill_zones(self.board)
        self._refresh_canvas()
        for name in done:
            self.status_map[name] = "routed"
            self.unconn[name] = []
        self._apply_status()
        self.proposed = []
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

    def on_close(self, _evt):
        if self in _OPEN:
            _OPEN.remove(self)
        self.Destroy()


def show_dialog(board):
    dlg = RouterDialog(board)
    _OPEN.append(dlg)
    dlg.Show()
