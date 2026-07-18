"""wx dialog for dg-router — choices on the left, live board preview on the right.

Net list is a DataViewListCtrl: a checkbox (route?), the net name, and a
right-aligned color-coded status icon (green check = routed, amber = partial,
grey ring = unrouted). Click a row (or check it) to highlight that net on the
render: existing copper green, remaining DRC gaps magenta dashed.

Isolated from action_plugin.py so importing the package headless never touches
wx. All board work delegates to shim.py / router.py.
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

# highlight colors
_C_PAD = wx.Colour(255, 235, 59)     # bright yellow — net membership
_C_KEPT = wx.Colour(75, 222, 128)    # green — existing copper (already routed)
_C_TODO = wx.Colour(255, 62, 165)    # magenta — remaining connections (to route)
_BG = wx.Colour(24, 24, 24)

_BG_PPM = 10.0  # background raster resolution (px per mm)

# status icon colors
_ICO_ROUTED = wx.Colour(46, 204, 113)
_ICO_PARTIAL = wx.Colour(241, 196, 15)
_ICO_UNROUTED = wx.Colour(120, 120, 120)
_ICON_SZ = 16


def _status_icon(kind):
    """A small transparent bitmap: green check-circle (routed), amber
    dash-circle (partial), grey ring (unrouted), or blank (unknown)."""
    sz = _ICON_SZ
    bmp = wx.Bitmap.FromRGBA(sz, sz, 0, 0, 0, 0)
    dc = wx.MemoryDC(bmp)
    gc = wx.GraphicsContext.Create(dc)
    if kind == "routed":
        gc.SetBrush(wx.Brush(_ICO_ROUTED))
        gc.SetPen(wx.Pen(_ICO_ROUTED, 1))
        gc.DrawEllipse(1, 1, sz - 2, sz - 2)
        gc.SetPen(wx.Pen(wx.Colour(255, 255, 255), 2))
        p = gc.CreatePath()
        p.MoveToPoint(4.5, 8.5)
        p.AddLineToPoint(7.0, 11.0)
        p.AddLineToPoint(11.5, 5.0)
        gc.StrokePath(p)
    elif kind == "partial":
        gc.SetBrush(wx.Brush(_ICO_PARTIAL))
        gc.SetPen(wx.Pen(_ICO_PARTIAL, 1))
        gc.DrawEllipse(1, 1, sz - 2, sz - 2)
        gc.SetPen(wx.Pen(wx.Colour(255, 255, 255), 2))
        gc.StrokeLine(4.5, 8, 11.5, 8)
    elif kind == "unrouted":
        gc.SetBrush(wx.Brush(wx.Colour(0, 0, 0, 0)))
        gc.SetPen(wx.Pen(_ICO_UNROUTED, 1.5))
        gc.DrawEllipse(2, 2, sz - 4, sz - 4)
    dc.SelectObject(wx.NullBitmap)
    return bmp


class PreviewPanel(wx.Panel):
    """Renders the board once (kicad-cli SVG -> bitmap) and draws a live
    highlight overlay for the selected nets on top of it."""

    def __init__(self, parent, board):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.board = board
        self.bg_bmp = None
        self.origin = None
        self.err = None
        self.selected = []
        self._geom_cache = {}
        self.unconn = {}
        self.status_loaded = False
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda e: None)  # kill flicker
        self._last_size = (0, 0)
        self.Bind(wx.EVT_SIZE, self.on_size)

    def on_size(self, evt):
        # Only repaint on a real size change (activation resends size events,
        # which otherwise cause a visible blink).
        s = tuple(self.GetClientSize())
        if s != self._last_size:
            self._last_size = s
            self.Refresh()
        evt.Skip()

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

    def mark_routed(self, names):
        """After routing: drop cached geometry (so new tracks show) and clear
        the remaining gaps for these nets."""
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
            vbw, vbh = shim.parse_svg_viewbox(svg)
            self.origin = shim.plot_origin(self.board, vbw, vbh)
            wpx, hpx = max(1, int(vbw * _BG_PPM)), max(1, int(vbh * _BG_PPM))
            img = wx.svg.SVGimage.CreateFromFile(svg)
            self.bg_bmp = img.ConvertToScaledBitmap(wx.Size(wpx, hpx))
        except Exception as e:  # noqa: BLE001
            self.err = "Preview failed:\n%s" % e

    def _draw_center_text(self, dc, text):
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
            self._draw_center_text(dc, self.err)
            return
        if self.bg_bmp is None:
            self._draw_center_text(dc, "Rendering board…")
            return

        cs = self.GetClientSize()
        bw, bh = self.bg_bmp.GetWidth(), self.bg_bmp.GetHeight()
        pad = 8
        scale = max(0.01, min((cs.width - 2 * pad) / bw, (cs.height - 2 * pad) / bh))
        dw, dh = int(bw * scale), int(bh * scale)
        ox, oy = (cs.width - dw) // 2, (cs.height - dh) // 2

        gc = wx.GraphicsContext.Create(dc)
        gc.DrawBitmap(self.bg_bmp, ox, oy, dw, dh)

        eff = _BG_PPM * scale
        orx, ory = self.origin

        def T(xmm, ymm):
            return (ox + (xmm - orx) * eff, oy + (ymm - ory) * eff)

        for name in self.selected:
            g = self._geom_cache.get(name)
            if not g:
                continue

            gc.SetPen(wx.Pen(_C_TODO, 1, wx.PENSTYLE_SHORT_DASH))
            for (x1, y1, x2, y2) in self._ratsnest_for(name, g):
                a, b = T(x1, y1), T(x2, y2)
                gc.StrokeLine(a[0], a[1], b[0], b[1])

            for (x1, y1, x2, y2, w) in g["tracks"]:
                gc.SetPen(wx.Pen(_C_KEPT, int(max(1, round(w * eff)))))
                a, b = T(x1, y1), T(x2, y2)
                gc.StrokeLine(a[0], a[1], b[0], b[1])
            gc.SetBrush(wx.Brush(_C_KEPT))
            gc.SetPen(wx.Pen(_C_KEPT, 1))
            for (x, y, r) in g["vias"]:
                cx, cy = T(x, y)
                rr = max(r * eff, 2.5)
                gc.DrawEllipse(cx - rr, cy - rr, 2 * rr, 2 * rr)

            gc.SetBrush(wx.Brush(wx.Colour(255, 235, 59, 150)))
            gc.SetPen(wx.Pen(_C_PAD, 1.5))
            for (x, y, r) in g["pads"]:
                cx, cy = T(x, y)
                rr = max(r * eff, 3.5)
                gc.DrawEllipse(cx - rr, cy - rr, 2 * rr, 2 * rr)


class RouterDialog(wx.Dialog):
    # DataViewListCtrl column indices
    COL_CHECK, COL_NET, COL_STATUS = 0, 1, 2

    def __init__(self, board):
        super().__init__(None, title="dg-router",
                         size=(940, 720),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self.nets = shim.list_nets(board)
        self.layers = shim.copper_layer_names(board)
        self.status_map = {}
        self._icons = {k: _status_icon(k)
                       for k in (None, "routed", "partial", "unrouted")}

        panel = wx.Panel(self)
        panel.SetDoubleBuffered(True)  # kill dialog-wide blink on re-activation
        main = wx.BoxSizer(wx.HORIZONTAL)

        # --- left column: choices -----------------------------------------
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(wx.StaticText(panel, label="Nets (%d) — click to preview, "
                               "check to route:" % len(self.nets)),
                 0, wx.LEFT | wx.TOP, 8)

        self.net_list = dv.DataViewListCtrl(
            panel, style=dv.DV_ROW_LINES | dv.DV_SINGLE)
        self.net_list.AppendToggleColumn("", width=28,
                                         mode=dv.DATAVIEW_CELL_ACTIVATABLE)
        self.net_list.AppendTextColumn("Net", width=232)
        # DataViewListCtrl.AppendBitmapColumn wants positional args (label,
        # model_column, mode, width, align) — keywords aren't accepted.
        self.net_list.AppendBitmapColumn("", self.COL_STATUS,
                                         dv.DATAVIEW_CELL_INERT, 40,
                                         wx.ALIGN_CENTER)
        for n in self.nets:
            self.net_list.AppendItem(
                [False, "%s  (%d pads)" % (n["name"], n["pads"]),
                 self._icons[None]])
        left.Add(self.net_list, 1, wx.EXPAND | wx.ALL, 8)

        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=8)
        grid.AddGrowableCol(1, 1)
        grid.Add(wx.StaticText(panel, label="Prefer layer:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.layer_choice = wx.Choice(panel, choices=self.layers or ["F.Cu", "B.Cu"])
        self.layer_choice.SetSelection(0)
        grid.Add(self.layer_choice, 0, wx.EXPAND)
        grid.Add(wx.StaticText(panel, label="Via cost:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.via_cost = wx.SpinCtrl(panel, min=0, max=1000, initial=80)
        grid.Add(self.via_cost, 0)
        grid.Add(wx.StaticText(panel, label="Edge hug (0-100):"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.edge_hug = wx.Slider(panel, value=0, minValue=0, maxValue=100,
                                  style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        grid.Add(self.edge_hug, 0, wx.EXPAND)
        left.Add(grid, 0, wx.EXPAND | wx.ALL, 8)

        self.follow = wx.CheckBox(panel, label="Follow existing tracks")
        self.follow.SetValue(True)
        left.Add(self.follow, 0, wx.LEFT | wx.BOTTOM, 8)

        self.btn_route = wx.Button(panel, label="Route checked ▶")
        self.btn_route.SetToolTip("Route the checked nets into the board "
                                  "(in memory — Save to keep, or close without "
                                  "saving to discard)")
        left.Add(self.btn_route, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_open = wx.Button(panel, label="Open full SVG")
        self.btn_emit = wx.Button(panel, label="Emit job.json")
        btns.Add(self.btn_open, 0, wx.RIGHT, 6)
        btns.Add(self.btn_emit, 0, wx.RIGHT, 6)
        btns.AddStretchSpacer(1)
        btns.Add(wx.Button(panel, wx.ID_CANCEL, label="Close"), 0)
        left.Add(btns, 0, wx.EXPAND | wx.ALL, 8)

        self.status = wx.StaticText(panel, label="Ready.")
        left.Add(self.status, 0, wx.ALL, 8)

        # --- right column: live preview -----------------------------------
        self.preview = PreviewPanel(panel, board)

        main.Add(left, 0, wx.EXPAND)
        main.SetItemMinSize(left, 340, -1)
        main.Add(self.preview, 1, wx.EXPAND | wx.ALL, 6)
        panel.SetSizer(main)

        self.net_list.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self.on_selection)
        self.net_list.Bind(dv.EVT_DATAVIEW_ITEM_VALUE_CHANGED, self.on_selection)
        self.btn_open.Bind(wx.EVT_BUTTON, self.on_open)
        self.btn_emit.Bind(wx.EVT_BUTTON, self.on_emit)
        self.btn_route.Bind(wx.EVT_BUTTON, self.on_route)

        if self.board.GetFileName():
            self.status.SetLabel("Computing routing status (DRC)…")
            wx.CallAfter(self._load_status)

    # --- routing status ---------------------------------------------------

    def _apply_status_icons(self):
        for i, n in enumerate(self.nets):
            self.net_list.SetValue(self._icons[self.status_map.get(n["name"])],
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
        counts = {"routed": 0, "partial": 0, "unrouted": 0}
        for v in status.values():
            counts[v] += 1
        self.status.SetLabel(
            "%d routed   %d partial   %d unrouted   (green=keep, magenta=to route)"
            % (counts["routed"], counts["partial"], counts["unrouted"]))

    # --- selection / preview ----------------------------------------------

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

    def current_prefer(self):
        return {
            "layer": self.layer_choice.GetStringSelection() or "B.Cu",
            "viaCost": self.via_cost.GetValue(),
            "edgeHug": round(self.edge_hug.GetValue() / 100.0, 2),
        }

    def out_dir(self):
        bp = self.board.GetFileName()
        base = os.path.dirname(bp) if bp else tempfile.gettempdir()
        d = os.path.join(base, "dg-router-out")
        os.makedirs(d, exist_ok=True)
        return d

    # --- actions ----------------------------------------------------------

    def on_open(self, _evt):
        bp = self.board.GetFileName()
        if not bp or not os.path.exists(bp):
            wx.MessageBox("Save the board first so the shim can render it.",
                          "dg-router")
            return
        out = os.path.join(self.out_dir(), "preview.svg")
        try:
            shim.render_board_svg(bp, out)
        except Exception as e:  # noqa: BLE001
            wx.MessageBox("Render failed:\n%s" % e, "dg-router")
            return
        webbrowser.open("file://" + out)
        self.status.SetLabel("Rendered: %s" % out)

    def on_route(self, _evt):
        names = self.selected_net_names()
        if not names:
            wx.MessageBox("Tick the checkbox on one or more nets first "
                          "(the leftmost column).", "dg-router")
            return
        bp = self.board.GetFileName()
        if not bp or not os.path.exists(bp):
            wx.MessageBox("Save the board first so the router can read DRC gaps.",
                          "dg-router")
            return
        try:
            wx.BeginBusyCursor()
            try:
                done, failed, added = self._do_route(names, bp)
            finally:
                wx.EndBusyCursor()
        except Exception as e:  # noqa: BLE001
            wx.MessageBox("Routing error:\n%s\n\n%s"
                          % (e, traceback.format_exc()), "dg-router")
            return

        for name, _ in done:
            self.status_map[name] = "routed"
        self._apply_status_icons()
        self.preview.mark_routed([n for n, _ in done])
        self.on_selection(None)

        self.status.SetLabel(
            "Routed %d, failed %d, +%d tracks — IN BOARD (unsaved)."
            % (len(done), len(failed), added))
        lines = ["Added %d track segments to the board (in memory)." % added,
                 "Save (Cmd+S) to keep, or close KiCad without saving to discard.",
                 ""]
        if done:
            lines.append("Routed: " + ", ".join(n for n, _ in done))
        if failed:
            lines.append("")
            lines.append("Failed (single-layer only, no vias yet):")
            lines += ["  %s — %s" % (n, why) for n, why in failed]
        wx.MessageBox("\n".join(lines), "dg-router — routing done")

    def _do_route(self, names, bp):
        params = router.RouteParams(self.board)
        unconn = self.preview.unconn if self.preview.status_loaded \
            else shim.drc_unconnected(bp)
        prefer = self.layer_choice.GetStringSelection()
        added, done, failed = 0, [], []
        for name in names:
            gaps = unconn.get(name, [])
            if not gaps:
                done.append((name, "already routed"))
                continue
            r = router.route_net(self.board, name, gaps, params, prefer)
            polys = r.get("polylines")
            if polys:
                added += router.write_polylines(
                    self.board, r["net_code"], r["layer_id"], polys, params.width)
            if r.get("ok"):
                done.append((name, "layer %s" % r.get("layer")))
            else:
                reason = r.get("reason") or \
                    ("routed %d/%d gaps" % (r.get("routed", 0), r.get("gaps", 0)))
                failed.append((name, reason))

        try:
            pcbnew.ZONE_FILLER(self.board).Fill(self.board.Zones())
        except Exception:
            pass
        try:
            self.board.BuildConnectivity()
            pcbnew.Refresh()
            pcbnew.UpdateUserInterface()
        except Exception:
            pass
        return done, failed, added

    def on_emit(self, _evt):
        job = shim.build_job(
            self.selected_net_names(),
            self.current_prefer(),
            follow_existing=self.follow.GetValue(),
        )
        out = os.path.join(self.out_dir(), "job.json")
        shim.write_job(job, out)
        self.status.SetLabel("Wrote job spec: %s" % out)
        wx.MessageBox(json.dumps(job, indent=2), "job.json  →  " + out)


def show_dialog(board):
    dlg = RouterDialog(board)
    dlg.ShowModal()
    dlg.Destroy()
