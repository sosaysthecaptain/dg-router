"""wx dialog for dg-router — CONTROLS ONLY. Preview happens in KiCad's canvas.

No in-dialog board render. Route draws the proposed traces straight into the
open board so you review them in KiCad's real editor (native zoom/pan/layers).
The window is non-modal so KiCad stays interactive. Commit keeps the traces
(you Save), Revert removes them, Try again re-routes differently.

Net rows are colored by routing status — green routed / amber partial / grey
unrouted (no emoji, no icons).
"""

import os
import traceback

import pcbnew
import wx

from . import shim
from . import router

# net-row status colors (readable on the white list background)
_COL_ROUTED = wx.Colour(30, 160, 70)
_COL_PARTIAL = wx.Colour(200, 130, 0)
_COL_UNROUTED = wx.Colour(140, 140, 140)
_COL_UNKNOWN = wx.Colour(20, 20, 20)
_STATUS_COL = {"routed": _COL_ROUTED, "partial": _COL_PARTIAL,
               "unrouted": _COL_UNROUTED}

# keep non-modal dialogs alive (prevent GC after Run() returns)
_OPEN = []


class RouterDialog(wx.Dialog):
    def __init__(self, board):
        super().__init__(None, title="dg-router", size=(380, 700),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self.nets = shim.list_nets(board)
        self.layers = shim.copper_layer_names(board)
        self.status_map = {}
        self.unconn = {}
        self._added = []            # uncommitted preview items in the board
        self._proposed_nets = []
        self._brightened = []       # copper items brightened for selected net
        self._rat_shapes = []       # bright ratsnest overlay shapes
        self._name2code = {n["name"]: n["code"] for n in self.nets}
        self._try_seed = 0

        panel = wx.Panel(self)
        v = wx.BoxSizer(wx.VERTICAL)

        v.Add(wx.StaticText(panel, label="Nets (%d) — check to route:"
                            % len(self.nets)), 0, wx.LEFT | wx.TOP, 8)
        self.net_list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.net_list.EnableCheckBoxes(True)
        self.net_list.InsertColumn(0, "Net", width=320)
        for n in self.nets:
            row = self.net_list.InsertItem(self.net_list.GetItemCount(),
                                           "%s   (%d pads)" % (n["name"], n["pads"]))
            self.net_list.SetItemTextColour(row, _COL_UNKNOWN)
        v.Add(self.net_list, 1, wx.EXPAND | wx.ALL, 8)

        lrow = wx.BoxSizer(wx.HORIZONTAL)
        lrow.Add(wx.StaticText(panel, label="Route on:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.layer_checks = {}
        for name in self.layers:
            cb = wx.CheckBox(panel, label=name)
            cb.SetValue(name in ("F.Cu", "B.Cu"))
            self.layer_checks[name] = cb
            lrow.Add(cb, 0, wx.RIGHT, 6)
        v.Add(lrow, 0, wx.LEFT | wx.BOTTOM, 8)

        grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=8)
        grid.AddGrowableCol(1, 1)
        grid.Add(wx.StaticText(panel, label="Via cost:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.via_cost = wx.Slider(panel, value=10, minValue=0, maxValue=100,
                                  style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        grid.Add(self.via_cost, 0, wx.EXPAND)
        grid.Add(wx.StaticText(panel, label="Edge hug (0-100):"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.edge_hug = wx.Slider(panel, value=0, minValue=0, maxValue=100,
                                  style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        grid.Add(self.edge_hug, 0, wx.EXPAND)
        v.Add(grid, 0, wx.EXPAND | wx.ALL, 8)

        self.follow = wx.CheckBox(panel, label="Follow existing tracks")
        self.follow.SetValue(True)
        v.Add(self.follow, 0, wx.LEFT | wx.BOTTOM, 8)

        self.btn_route = wx.Button(panel, label="Route")
        self.btn_route.SetDefault()
        v.Add(self.btn_route, 0, wx.EXPAND | wx.ALL, 8)

        self.act_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_commit = wx.Button(panel, label="Accept")
        self.btn_revert = wx.Button(panel, label="Reject")
        self.btn_try = wx.Button(panel, label="Try again")
        for b in (self.btn_commit, self.btn_revert, self.btn_try):
            self.act_row.Add(b, 1, wx.RIGHT, 6)
        v.Add(self.act_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.status = wx.StaticText(panel, label="Loading…")
        v.Add(self.status, 0, wx.ALL, 8)

        panel.SetSizer(v)

        self.btn_route.Bind(wx.EVT_BUTTON, self.on_route)
        self.btn_try.Bind(wx.EVT_BUTTON, self.on_try_again)
        self.btn_commit.Bind(wx.EVT_BUTTON, self.on_commit)
        self.btn_revert.Bind(wx.EVT_BUTTON, self.on_revert)
        self.net_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select)
        self.Bind(wx.EVT_CLOSE, self.on_close)

        self._show_actions(False)
        # instant window: show a loading state, disable Route until status loads
        self.btn_route.Disable()
        if self.board.GetFileName():
            wx.CallAfter(self._load_status)
        else:
            self.status.SetLabel("Save the board to begin.")

    # --- helpers ------------------------------------------------------------

    def _show_actions(self, show):
        self.act_row.ShowItems(show)
        self.btn_route.Show(not show)
        self.Layout()

    def _apply_status_colors(self):
        for i, n in enumerate(self.nets):
            self.net_list.SetItemTextColour(
                i, _STATUS_COL.get(self.status_map.get(n["name"]), _COL_UNKNOWN))

    def _load_status(self):
        try:
            status, unconn = shim.net_status_map(self.board,
                                                 self.board.GetFileName())
        except Exception as e:  # noqa: BLE001
            self.status.SetLabel("Routing status unavailable: %s" % e)
            return
        self.status_map = status
        self.unconn = unconn
        self._apply_status_colors()
        self.btn_route.Enable()
        c = {"routed": 0, "partial": 0, "unrouted": 0}
        for x in status.values():
            c[x] += 1
        self.status.SetLabel("%d routed  %d partial  %d unrouted"
                             % (c["routed"], c["partial"], c["unrouted"]))

    def _reset_pass(self):
        """After Accept/Reject: clear selection + highlight, re-cock for the
        next pass."""
        for i in range(self.net_list.GetItemCount()):
            if self.net_list.IsItemChecked(i):
                self.net_list.CheckItem(i, False)
        self._clear_highlight()
        self._proposed_nets = []
        self._refresh_canvas()
        self._show_actions(False)

    def _checked_names(self):
        return [self.nets[i]["name"] for i in range(self.net_list.GetItemCount())
                if self.net_list.IsItemChecked(i)]

    def selected_layers(self):
        chosen = [n for n, cb in self.layer_checks.items() if cb.GetValue()]
        return chosen or ["F.Cu", "B.Cu"]

    def _params(self, jitter=0.0):
        return router.RouteParams(
            self.board, via_cost=float(self.via_cost.GetValue()),
            layer_names=self.selected_layers(), seed=self._try_seed, jitter=jitter)

    def _remove_added(self):
        for it in self._added:
            try:
                self.board.Remove(it)
            except Exception:
                pass
        if self._added:
            router.refill_zones(self.board)
        self._added = []

    def _refresh_canvas(self):
        try:
            self.board.BuildConnectivity()
            pcbnew.Refresh()
            pcbnew.UpdateUserInterface()
        except Exception:
            pass

    def _clear_highlight(self):
        for it in self._brightened:
            try:
                it.ClearBrightened()
            except Exception:
                pass
        for s in self._rat_shapes:
            try:
                self.board.Remove(s)
            except Exception:
                pass
        self._brightened = []
        self._rat_shapes = []

    def _draw_ratsnest(self, name):
        """Draw the selected net's remaining connections as BRIGHT lines on
        Dwgs.User, so they stand out far more than KiCad's default airwires."""
        gaps = self.unconn.get(name, [])
        if not gaps:
            return
        layer = self.board.GetLayerID("Dwgs.User")
        seg = getattr(pcbnew, "SHAPE_T_SEGMENT", None)
        try:
            col = pcbnew.COLOR4D(1.0, 0.24, 0.80, 1.0)   # bright magenta
        except Exception:
            col = None
        w = int(0.15 * 1e6)
        for (x1, y1, x2, y2) in gaps:
            s = pcbnew.PCB_SHAPE(self.board)
            if seg is not None:
                s.SetShape(seg)
            s.SetStart(pcbnew.VECTOR2I(int(x1 * 1e6), int(y1 * 1e6)))
            s.SetEnd(pcbnew.VECTOR2I(int(x2 * 1e6), int(y2 * 1e6)))
            s.SetWidth(w)
            s.SetLayer(layer)
            if col is not None:
                try:
                    s.SetLineColor(col)
                except Exception:
                    pass
            s.SetBrightened()
            self.board.Add(s)
            self._rat_shapes.append(s)

    def on_select(self, evt):
        """Highlight the clicked net in KiCad: brighten its copper AND draw its
        remaining ratsnest bright."""
        idx = evt.GetIndex()
        self._clear_highlight()
        name = self.nets[idx]["name"]
        code = self._name2code.get(name)
        if code is not None:
            for pad in self.board.GetPads():
                if pad.GetNetCode() == code:
                    pad.SetBrightened()
                    self._brightened.append(pad)
            for t in self.board.GetTracks():
                if t.GetNetCode() == code:
                    t.SetBrightened()
                    self._brightened.append(t)
        self._draw_ratsnest(name)
        try:
            pcbnew.Refresh()
        except Exception:
            pass

    # --- routing (drawn into KiCad) ----------------------------------------

    def _compute(self, jitter):
        names = self._checked_names()
        if not names:
            wx.MessageBox("Check one or more nets to route first.", "dg-router")
            return
        bp = self.board.GetFileName()
        if not bp or not os.path.exists(bp):
            wx.MessageBox("Save the board first.", "dg-router")
            return

        self._remove_added()     # replace any previous uncommitted preview
        self._clear_highlight()  # stale ratsnest for routed nets would mislead
        self.status.SetLabel("Routing…")
        wx.BeginBusyCursor()
        try:
            params = self._params(jitter=jitter)
            unconn = self.unconn or shim.drc_unconnected(bp)
            results = router.route_batch(self.board, names, unconn, params)
            added = []
            for r in results:
                if r.get("segments") or r.get("vias"):
                    added += router.write_result(self.board, r["net_code"], r)
            router.refill_zones(self.board)
            self._added = added
            self._proposed_nets = [r["net"] for r in results]
        except Exception as e:  # noqa: BLE001
            wx.EndBusyCursor()
            wx.MessageBox("Routing error:\n%s\n\n%s" % (e, traceback.format_exc()),
                          "dg-router")
            return
        wx.EndBusyCursor()
        self._refresh_canvas()

        ok = sum(1 for r in results if r.get("ok"))
        self.status.SetLabel(
            "Proposed %d/%d nets (+%d items). Accept / Reject / Try again"
            % (ok, len(results), len(self._added)))
        self._show_actions(True)

    def on_route(self, _evt):
        self._try_seed = 0
        self._compute(jitter=0.0)

    def on_try_again(self, _evt):
        self._try_seed += 1
        self._compute(jitter=0.35)   # keeps selection; different solution

    def on_revert(self, _evt):       # Reject
        self._remove_added()
        self.status.SetLabel("Rejected — nothing kept. Pick nets and Route again.")
        self._reset_pass()

    def on_commit(self, _evt):       # Accept
        for name in self._proposed_nets:
            self.status_map[name] = "routed"
        self._apply_status_colors()
        n = len(self._added)
        self._added = []             # keep them in the board; stop tracking
        self.status.SetLabel("Accepted %d items (Cmd+S to save). "
                             "Pick nets and Route again." % n)
        self._reset_pass()

    # --- misc ---------------------------------------------------------------

    def on_close(self, _evt):
        # discard any uncommitted preview + highlight so we leave a clean board
        self._remove_added()
        self._clear_highlight()
        self._refresh_canvas()
        if self in _OPEN:
            _OPEN.remove(self)
        self.Destroy()


def show_dialog(board):
    dlg = RouterDialog(board)
    _OPEN.append(dlg)
    dlg.Show()   # non-modal: KiCad canvas stays interactive for review
