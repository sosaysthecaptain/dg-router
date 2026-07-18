"""wx dialog for dg-router — choices + a zoomable board preview.

Flow: pick layers + nets, click Route. Routes are computed and shown as a
PROPOSED overlay in the preview WITHOUT touching the board or file (verified on
a throwaway copy via DRC). Then Try again (re-route differently) / Commit (apply
to the board) / Revert (discard).

Isolated from action_plugin.py so importing the package headless never touches
wx. Board work delegates to shim.py / router.py.
"""

import os
import json
import tempfile
import traceback
import webbrowser

import pcbnew
import wx
import wx.svg
import wx.dataview as dv

from . import shim
from . import router

# highlight (selected-net) colors
_C_PAD = wx.Colour(255, 235, 59)
_C_KEPT = wx.Colour(75, 222, 128)
_C_TODO = wx.Colour(255, 62, 165)
# proposed-route colors (per layer)
_C_PROP = {pcbnew.F_Cu: wx.Colour(0, 229, 255), pcbnew.B_Cu: wx.Colour(255, 150, 0)}
_C_PROP_DEFAULT = wx.Colour(230, 230, 230)
_C_PROP_VIA = wx.Colour(255, 235, 59)
_BG = wx.Colour(24, 24, 24)

_BG_PPM = 22.0  # background raster resolution (px per mm) — crisp when zoomed

_STATUS_EMOJI = {None: "", "routed": "✅", "partial": "🟡", "unrouted": "⚪"}


class PreviewPanel(wx.Panel):
    """Zoomable/pannable board render with a live net-highlight overlay and a
    proposed-route overlay."""

    def __init__(self, parent, board):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.board = board
        self.bg_bmp = None
        self.origin = None          # (ox, oy) mm at bitmap (0,0)
        self.vbw = self.vbh = 1.0
        self.err = None
        self.selected = []
        self._geom_cache = {}
        self.unconn = {}
        self.status_loaded = False
        self.proposed = []          # list of route result dicts
        self.zoom = 1.0
        self.panx = self.pany = 0.0
        self._drag = None
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetBackgroundColour(_BG)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda e: None)
        self.Bind(wx.EVT_MOUSEWHEEL, self.on_wheel)
        self.Bind(wx.EVT_LEFT_DOWN, self.on_down)
        self.Bind(wx.EVT_LEFT_UP, self.on_up)
        self.Bind(wx.EVT_MOTION, self.on_motion)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))

    # --- state --------------------------------------------------------------

    def set_selected(self, names):
        self.selected = list(names)
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

    def mark_routed(self, names):
        for name in names:
            self.unconn[name] = []
            self._geom_cache.pop(name, None)
        self.Refresh()

    def _ratsnest_for(self, name, geom):
        if self.status_loaded:
            return self.unconn.get(name, [])
        pts = [(p[0], p[1]) for p in geom["pads"]]
        return [(pts[i][0], pts[i][1], pts[j][0], pts[j][1])
                for i, j in shim.mst_edges(pts)]

    # --- view transform -----------------------------------------------------

    def _fit_ppm(self):
        cw, ch = self.GetClientSize()
        return max(0.01, min((cw - 16) / self.vbw, (ch - 16) / self.vbh))

    def _ppm(self):
        return self._fit_ppm() * self.zoom

    def _origin_screen(self, ppm):
        cw, ch = self.GetClientSize()
        return ((cw - self.vbw * ppm) / 2.0 + self.panx,
                (ch - self.vbh * ppm) / 2.0 + self.pany)

    def on_wheel(self, evt):
        if self.bg_bmp is None:
            return
        mx, my = evt.GetX(), evt.GetY()
        ppm = self._ppm()
        bx0, by0 = self._origin_screen(ppm)
        wxmm = self.origin[0] + (mx - bx0) / ppm
        wymm = self.origin[1] + (my - by0) / ppm
        f = 1.2 if evt.GetWheelRotation() > 0 else 1 / 1.2
        self.zoom = min(60.0, max(0.3, self.zoom * f))
        ppm2 = self._ppm()
        cw, ch = self.GetClientSize()
        self.panx = mx - (wxmm - self.origin[0]) * ppm2 - (cw - self.vbw * ppm2) / 2.0
        self.pany = my - (wymm - self.origin[1]) * ppm2 - (ch - self.vbh * ppm2) / 2.0
        self.Refresh()

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

    # --- rendering ----------------------------------------------------------

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
            self.vbw, self.vbh = shim.parse_svg_viewbox(svg)
            self.origin = shim.plot_origin(self.board, self.vbw, self.vbh)
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

        ppm = self._ppm()
        bx0, by0 = self._origin_screen(ppm)
        s = ppm / _BG_PPM
        orx, ory = self.origin
        gc = wx.GraphicsContext.Create(dc)
        gc.Translate(bx0, by0)
        gc.Scale(s, s)
        gc.DrawBitmap(self.bg_bmp, 0, 0, self.bg_bmp.GetWidth(),
                      self.bg_bmp.GetHeight())

        def B(x, y):  # mm -> bitmap px (the gc scale handles zoom)
            return ((x - orx) * _BG_PPM, (y - ory) * _BG_PPM)

        # selected-net highlight
        for name in self.selected:
            g = self._geom_cache.get(name)
            if not g:
                continue
            gc.SetPen(wx.Pen(_C_TODO, 1.5, wx.PENSTYLE_SHORT_DASH))
            for (x1, y1, x2, y2) in self._ratsnest_for(name, g):
                a, b = B(x1, y1), B(x2, y2)
                gc.StrokeLine(a[0], a[1], b[0], b[1])
            for (x1, y1, x2, y2, w) in g["tracks"]:
                gc.SetPen(wx.Pen(_C_KEPT, max(1.0, w * _BG_PPM)))
                a, b = B(x1, y1), B(x2, y2)
                gc.StrokeLine(a[0], a[1], b[0], b[1])
            gc.SetBrush(wx.Brush(wx.Colour(255, 235, 59, 140)))
            gc.SetPen(wx.Pen(_C_PAD, 1.0))
            for (x, y, r) in g["pads"]:
                cx, cy = B(x, y)
                rr = max(r * _BG_PPM, 4)
                gc.DrawEllipse(cx - rr, cy - rr, 2 * rr, 2 * rr)

        # proposed routes
        for res in self.proposed:
            for (x1, y1, x2, y2, w, lyr) in res.get("segments", []):
                col = _C_PROP.get(lyr, _C_PROP_DEFAULT)
                gc.SetPen(wx.Pen(col, max(1.0, w * _BG_PPM)))
                a, b = B(x1, y1), B(x2, y2)
                gc.StrokeLine(a[0], a[1], b[0], b[1])
            gc.SetBrush(wx.Brush(_C_PROP_VIA))
            gc.SetPen(wx.Pen(wx.Colour(120, 90, 0), 1.0))
            for (x, y, dia, drill) in res.get("vias", []):
                cx, cy = B(x, y)
                rr = dia / 2.0 * _BG_PPM
                gc.DrawEllipse(cx - rr, cy - rr, 2 * rr, 2 * rr)


class RouterDialog(wx.Dialog):
    COL_CHECK, COL_NET, COL_STATUS = 0, 1, 2

    def __init__(self, board):
        super().__init__(None, title="dg-router", size=(1000, 760),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self.nets = shim.list_nets(board)
        self.layers = shim.copper_layer_names(board)
        self.status_map = {}
        self.proposed = []
        self._try_seed = 0

        panel = wx.Panel(self)
        panel.SetDoubleBuffered(True)
        main = wx.BoxSizer(wx.HORIZONTAL)

        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(wx.StaticText(panel, label="Nets (%d) — click to preview, "
                               "check to route:" % len(self.nets)),
                 0, wx.LEFT | wx.TOP, 8)
        self.net_list = dv.DataViewListCtrl(
            panel, style=dv.DV_ROW_LINES | dv.DV_SINGLE)
        self.net_list.AppendToggleColumn("", mode=dv.DATAVIEW_CELL_ACTIVATABLE,
                                         width=28)
        self.net_list.AppendTextColumn("Net", width=228)
        self.net_list.AppendTextColumn("", dv.DATAVIEW_CELL_INERT, 48,
                                       wx.ALIGN_RIGHT)
        for n in self.nets:
            self.net_list.AppendItem(
                [False, "%s  (%d pads)" % (n["name"], n["pads"]),
                 _STATUS_EMOJI[None]])
        left.Add(self.net_list, 1, wx.EXPAND | wx.ALL, 8)

        # routable layers
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
        self.via_cost = wx.Slider(panel, value=25, minValue=0, maxValue=200,
                                  style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        grid.Add(self.via_cost, 0, wx.EXPAND)
        grid.Add(wx.StaticText(panel, label="Edge hug (0-100):"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.edge_hug = wx.Slider(panel, value=0, minValue=0, maxValue=100,
                                  style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        grid.Add(self.edge_hug, 0, wx.EXPAND)
        left.Add(grid, 0, wx.EXPAND | wx.ALL, 8)

        self.follow = wx.CheckBox(panel, label="Follow existing tracks")
        self.follow.SetValue(True)
        left.Add(self.follow, 0, wx.LEFT | wx.BOTTOM, 8)

        # primary Route button (big)
        self.btn_route = wx.Button(panel, label="Route checked  ▶",
                                   size=(-1, 40))
        self.btn_route.SetDefault()
        f = self.btn_route.GetFont()
        f.SetPointSize(f.GetPointSize() + 2)
        f.MakeBold()
        self.btn_route.SetFont(f)
        left.Add(self.btn_route, 0, wx.EXPAND | wx.ALL, 8)

        # after-route action buttons (hidden until a preview exists)
        self.act_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_try = wx.Button(panel, label="Try again")
        self.btn_commit = wx.Button(panel, label="Commit")
        self.btn_revert = wx.Button(panel, label="Revert")
        for b in (self.btn_try, self.btn_commit, self.btn_revert):
            self.act_row.Add(b, 1, wx.RIGHT, 6)
        left.Add(self.act_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        util = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_open = wx.Button(panel, label="Open full SVG")
        self.btn_emit = wx.Button(panel, label="Emit job.json")
        util.Add(self.btn_open, 0, wx.RIGHT, 6)
        util.Add(self.btn_emit, 0, wx.RIGHT, 6)
        util.AddStretchSpacer(1)
        util.Add(wx.Button(panel, wx.ID_CANCEL, label="Close"), 0)
        left.Add(util, 0, wx.EXPAND | wx.ALL, 8)

        self.status = wx.StaticText(panel, label="Ready.  Scroll=zoom, drag=pan.")
        left.Add(self.status, 0, wx.ALL, 8)

        self.preview = PreviewPanel(panel, board)
        main.Add(left, 0, wx.EXPAND)
        main.SetItemMinSize(left, 360, -1)
        main.Add(self.preview, 1, wx.EXPAND | wx.ALL, 6)
        panel.SetSizer(main)

        self.net_list.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self.on_selection)
        self.net_list.Bind(dv.EVT_DATAVIEW_ITEM_VALUE_CHANGED, self.on_selection)
        self.btn_open.Bind(wx.EVT_BUTTON, self.on_open)
        self.btn_emit.Bind(wx.EVT_BUTTON, self.on_emit)
        self.btn_route.Bind(wx.EVT_BUTTON, self.on_route)
        self.btn_try.Bind(wx.EVT_BUTTON, self.on_try_again)
        self.btn_commit.Bind(wx.EVT_BUTTON, self.on_commit)
        self.btn_revert.Bind(wx.EVT_BUTTON, self.on_revert)

        self._show_actions(False)

        if self.board.GetFileName():
            self.status.SetLabel("Computing routing status (DRC)…")
            wx.CallAfter(self._load_status)

    # --- helpers ------------------------------------------------------------

    def _show_actions(self, show):
        self.act_row.ShowItems(show)
        self.btn_route.Show(not show)
        self.Layout()

    def _apply_status_icons(self):
        for i, n in enumerate(self.nets):
            self.net_list.SetValue(
                _STATUS_EMOJI.get(self.status_map.get(n["name"]), ""),
                i, self.COL_STATUS)

    def _load_status(self):
        try:
            status, unconn = shim.net_status_map(self.board,
                                                 self.board.GetFileName())
        except Exception as e:  # noqa: BLE001
            self.status.SetLabel("Routing status unavailable: %s" % e)
            return
        self.status_map = status
        self._apply_status_icons()
        self.preview.set_routing(unconn)
        c = {"routed": 0, "partial": 0, "unrouted": 0}
        for v in status.values():
            c[v] += 1
        self.status.SetLabel("%d routed  %d partial  %d unrouted  "
                             "(scroll=zoom, drag=pan)"
                             % (c["routed"], c["partial"], c["unrouted"]))

    def _checked_rows(self):
        return [i for i in range(self.net_list.GetItemCount())
                if self.net_list.GetToggleValue(i, self.COL_CHECK)]

    def _highlighted_names(self):
        idxs = set(self._checked_rows())
        sel = self.net_list.GetSelectedRow()
        if sel is not None and sel != wx.NOT_FOUND and sel >= 0:
            idxs.add(sel)
        return [self.nets[i]["name"] for i in sorted(idxs)]

    def on_selection(self, _evt):
        self.preview.set_selected(self._highlighted_names())

    def selected_net_names(self):
        return [self.nets[i]["name"] for i in self._checked_rows()]

    def selected_layers(self):
        chosen = [n for n, cb in self.layer_checks.items() if cb.GetValue()]
        return chosen or ["F.Cu", "B.Cu"]

    def _params(self, jitter=0.0):
        return router.RouteParams(
            self.board, via_cost=float(self.via_cost.GetValue()),
            layer_names=self.selected_layers(), seed=self._try_seed,
            jitter=jitter)

    def current_prefer(self):
        return {"layer": self.selected_layers()[0],
                "viaCost": self.via_cost.GetValue(),
                "edgeHug": round(self.edge_hug.GetValue() / 100.0, 2)}

    def out_dir(self):
        bp = self.board.GetFileName()
        base = os.path.dirname(bp) if bp else tempfile.gettempdir()
        d = os.path.join(base, "dg-router-out")
        os.makedirs(d, exist_ok=True)
        return d

    # --- routing (preview-first) -------------------------------------------

    def _compute(self, jitter):
        names = self.selected_net_names()
        if not names:
            wx.MessageBox("Tick the checkbox on one or more nets first.",
                          "dg-router")
            return
        bp = self.board.GetFileName()
        if not bp or not os.path.exists(bp):
            wx.MessageBox("Save the board first.", "dg-router")
            return
        self.status.SetLabel("Routing…")
        wx.BeginBusyCursor()
        try:
            params = self._params(jitter=jitter)
            unconn = self.preview.unconn if self.preview.status_loaded \
                else shim.drc_unconnected(bp)
            results = router.route_batch(self.board, names, unconn, params)
        except Exception as e:  # noqa: BLE001
            wx.EndBusyCursor()
            wx.MessageBox("Routing error:\n%s\n\n%s" % (e, traceback.format_exc()),
                          "dg-router")
            return
        wx.EndBusyCursor()

        self.proposed = results
        self.preview.set_proposed(results)
        connected = self._verify(results)   # DRC on a throwaway copy
        ntracks = sum(len(r.get("segments", [])) for r in results)
        nvias = sum(len(r.get("vias", [])) for r in results)
        ok = sum(1 for r in results if connected.get(r["net"]))
        self.status.SetLabel(
            "Proposed: %d/%d nets connected, %d segs, %d vias — "
            "Commit / Try again / Revert" % (ok, len(results), ntracks, nvias))
        self._show_actions(True)

    def _verify(self, results):
        """Apply proposed to a throwaway copy and DRC it — returns
        {net: connected?}. The live board/file are never touched."""
        try:
            tmp = os.path.join(tempfile.gettempdir(), "dg-verify.kicad_pcb")
            pcbnew.SaveBoard(tmp, self.board)
            vb = pcbnew.LoadBoard(tmp)
            for r in results:
                if r.get("segments") or r.get("vias"):
                    router.write_result(vb, r["net_code"], r)
            router.refill_zones(vb)
            pcbnew.SaveBoard(tmp, vb)
            after = shim.drc_unconnected(tmp)
            return {r["net"]: not after.get(r["net"]) for r in results}
        except Exception:  # noqa: BLE001
            return {r["net"]: r.get("ok", False) for r in results}

    def on_route(self, _evt):
        self._try_seed = 0
        self._compute(jitter=0.0)

    def on_try_again(self, _evt):
        self._try_seed += 1
        self._compute(jitter=0.35)   # jiggle so A* finds a different solution

    def on_revert(self, _evt):
        self.proposed = []
        self.preview.set_proposed([])
        self.status.SetLabel("Reverted. Nothing was written.")
        self._show_actions(False)

    def on_commit(self, _evt):
        added = 0
        done = []
        for r in self.proposed:
            if r.get("segments") or r.get("vias"):
                added += router.write_result(self.board, r["net_code"], r)
            done.append(r["net"])
        router.refill_zones(self.board)
        try:
            self.board.BuildConnectivity()
            pcbnew.Refresh()
            pcbnew.UpdateUserInterface()
        except Exception:
            pass
        for name in done:
            self.status_map[name] = "routed"
        self._apply_status_icons()
        self.preview.mark_routed(done)
        self.preview.set_proposed([])
        self.proposed = []
        self.status.SetLabel("Committed %d nets (+%d items) to the board. "
                             "Save (Cmd+S) to keep." % (len(done), added))
        self._show_actions(False)

    # --- misc ---------------------------------------------------------------

    def on_open(self, _evt):
        bp = self.board.GetFileName()
        if not bp or not os.path.exists(bp):
            wx.MessageBox("Save the board first.", "dg-router")
            return
        out = os.path.join(self.out_dir(), "preview.svg")
        try:
            shim.render_board_svg(bp, out)
        except Exception as e:  # noqa: BLE001
            wx.MessageBox("Render failed:\n%s" % e, "dg-router")
            return
        webbrowser.open("file://" + out)

    def on_emit(self, _evt):
        job = shim.build_job(self.selected_net_names(), self.current_prefer(),
                             follow_existing=self.follow.GetValue())
        out = os.path.join(self.out_dir(), "job.json")
        shim.write_job(job, out)
        wx.MessageBox(json.dumps(job, indent=2), "job.json  →  " + out)


def show_dialog(board):
    dlg = RouterDialog(board)
    dlg.ShowModal()
    dlg.Destroy()
