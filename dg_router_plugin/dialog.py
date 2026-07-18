"""wx dialog for the dg-router shim — the "see choices" surface.

Now with a live in-window preview: pick/check nets on the left and the board
render on the right highlights that net's pads, existing copper, and an
approximate ratsnest. Isolated from action_plugin.py so importing the package
headless never touches wx. All board work delegates to shim.py.
"""

import os
import json
import tempfile
import webbrowser

import wx
import wx.svg

from . import shim

# highlight colors
_C_PAD = wx.Colour(255, 235, 59)     # bright yellow — net membership
_C_KEPT = wx.Colour(75, 222, 128)    # green — existing copper (already routed, keep)
_C_TODO = wx.Colour(255, 62, 165)    # magenta — remaining connections (still to route)
_BG = wx.Colour(24, 24, 24)

_BG_PPM = 10.0  # background raster resolution (px per mm)

# net-list status badges
_BADGE = {"routed": "✓ ", "partial": "◐ ", "unrouted": "· "}


class PreviewPanel(wx.Panel):
    """Renders the board once (kicad-cli SVG -> bitmap) and draws a live
    highlight overlay for the selected nets on top of it."""

    def __init__(self, parent, board):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.board = board
        self.bg_bmp = None          # wx.Bitmap of the board render
        self.origin = None          # (ox, oy) mm -> SVG (0,0)
        self.err = None
        self.selected = []          # net names to highlight
        self._geom_cache = {}
        self.unconn = {}            # net -> [(x1,y1,x2,y2)] remaining connections
        self.status_loaded = False  # True once DRC routing status is in
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))

    def set_selected(self, names):
        # Compute geometry here (selection change), NOT during paint — keeps
        # EVT_PAINT free of pcbnew calls.
        self.selected = list(names)
        for name in names:
            if name not in self._geom_cache:
                self._geom_cache[name] = \
                    shim.net_geometry(self.board, [name]).get(name)
        self.Refresh()

    def set_routing(self, unconn):
        """Provide the DRC missing-connection map; switches the ratsnest from
        the MST fallback to the ground-truth gaps."""
        self.unconn = unconn or {}
        self.status_loaded = True
        self.Refresh()

    def _ratsnest_for(self, name, geom):
        """Remaining connections to draw. After DRC: the true gaps ([] if
        routed). Before DRC: an MST over all pads (best-effort)."""
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
        except Exception as e:  # noqa: BLE001 - surface everything
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

        eff = _BG_PPM * scale        # px per mm on screen
        orx, ory = self.origin

        def T(xmm, ymm):
            return (ox + (xmm - orx) * eff, oy + (ymm - ory) * eff)

        for name in self.selected:
            g = self._geom_cache.get(name)
            if not g:
                continue

            # remaining connections (magenta dashed) — what's still to route.
            # Drawn first, underneath the kept copper.
            gc.SetPen(wx.Pen(_C_TODO, 1, wx.PENSTYLE_SHORT_DASH))
            for (x1, y1, x2, y2) in self._ratsnest_for(name, g):
                a, b = T(x1, y1), T(x2, y2)
                gc.StrokeLine(a[0], a[1], b[0], b[1])

            # existing copper (green) — already routed, kept as-is
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

            # pads (bright yellow, semi-transparent so the pad underneath reads)
            gc.SetBrush(wx.Brush(wx.Colour(255, 235, 59, 150)))
            gc.SetPen(wx.Pen(_C_PAD, 1.5))
            for (x, y, r) in g["pads"]:
                cx, cy = T(x, y)
                rr = max(r * eff, 3.5)
                gc.DrawEllipse(cx - rr, cy - rr, 2 * rr, 2 * rr)


class RouterDialog(wx.Dialog):
    def __init__(self, board):
        super().__init__(None, title="dg-router — Milestone 0 shim",
                         size=(940, 720),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self.nets = shim.list_nets(board)
        self.layers = shim.copper_layer_names(board)

        panel = wx.Panel(self)
        main = wx.BoxSizer(wx.HORIZONTAL)

        # --- left column: choices -----------------------------------------
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(wx.StaticText(panel, label="Nets (%d) — click to preview, "
                               "check to route:" % len(self.nets)),
                 0, wx.LEFT | wx.TOP, 8)
        self.status_map = {}
        labels = [self._net_label(n) for n in self.nets]
        self.net_list = wx.CheckListBox(panel, choices=labels)
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

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_open = wx.Button(panel, label="Open full SVG")
        self.btn_emit = wx.Button(panel, label="Emit job.json")
        btns.Add(self.btn_open, 0, wx.RIGHT, 6)
        btns.Add(self.btn_emit, 0, wx.RIGHT, 6)
        btns.AddStretchSpacer(1)
        btns.Add(wx.Button(panel, wx.ID_CANCEL, label="Close"), 0)
        left.Add(btns, 0, wx.EXPAND | wx.ALL, 8)

        self.status = wx.StaticText(panel, label="Shim mode: no routing performed.")
        left.Add(self.status, 0, wx.ALL, 8)

        # --- right column: live preview -----------------------------------
        self.preview = PreviewPanel(panel, board)

        main.Add(left, 0, wx.EXPAND)
        main.SetItemMinSize(left, 340, -1)
        main.Add(self.preview, 1, wx.EXPAND | wx.ALL, 6)
        panel.SetSizer(main)

        self.net_list.Bind(wx.EVT_LISTBOX, self.on_selection)
        self.net_list.Bind(wx.EVT_CHECKLISTBOX, self.on_selection)
        self.btn_open.Bind(wx.EVT_BUTTON, self.on_open)
        self.btn_emit.Bind(wx.EVT_BUTTON, self.on_emit)

        # DRC routing-status is slow-ish; load it after the dialog is visible.
        if self.board.GetFileName():
            self.status.SetLabel("Computing routing status (DRC)…")
            wx.CallAfter(self._load_status)

    # --- routing status ---------------------------------------------------

    def _net_label(self, net):
        badge = _BADGE.get(self.status_map.get(net["name"]), "")
        return "%s%s  (%d pads)" % (badge, net["name"], net["pads"])

    def _load_status(self):
        try:
            status, unconn = shim.net_status_map(self.board,
                                                 self.board.GetFileName())
        except Exception as e:  # noqa: BLE001
            self.status.SetLabel("Routing status unavailable: %s" % e)
            return
        self.status_map = status
        for i, n in enumerate(self.nets):
            self.net_list.SetString(i, self._net_label(n))
        self.preview.set_routing(unconn)
        counts = {"routed": 0, "partial": 0, "unrouted": 0}
        for v in status.values():
            counts[v] += 1
        self.status.SetLabel(
            "✓ %d routed   ◐ %d partial   · %d unrouted   "
            "(green=keep, magenta=to route)"
            % (counts["routed"], counts["partial"], counts["unrouted"]))

    # --- selection / preview ----------------------------------------------

    def _highlighted_names(self):
        idxs = set(self.net_list.GetCheckedItems())
        sel = self.net_list.GetSelection()
        if sel != wx.NOT_FOUND:
            idxs.add(sel)
        return [self.nets[i]["name"] for i in sorted(idxs)]

    def on_selection(self, _evt):
        self.preview.set_selected(self._highlighted_names())

    # --- choice extraction ------------------------------------------------

    def selected_net_names(self):
        return [self.nets[i]["name"] for i in self.net_list.GetCheckedItems()]

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
