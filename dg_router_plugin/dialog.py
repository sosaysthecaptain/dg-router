"""wx dialog for dg-router — CONTROLS ONLY. Preview happens in KiCad's canvas.

No in-dialog board render. Route draws the proposed traces straight into the
open board so you review them in KiCad's real editor (native zoom/pan/layers).
The window is non-modal so KiCad stays interactive. Commit keeps the traces
(you Save), Revert removes them, Try again re-routes differently.

Net rows are colored by routing status — green routed / amber partial / grey
unrouted (no emoji, no icons).
"""

import os
import threading
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
_STATUS_WORD = {"routed": "routed", "partial": "partial", "unrouted": "unrouted"}

# native KiCad net colors for highlighting (no board geometry -> nothing left
# behind). checked nets get a bright color, the active net an even brighter one.
_HL_CHECKED = (0.10, 0.85, 1.00)   # bright cyan
_HL_ACTIVE = (1.00, 0.95, 0.25)    # bright yellow

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
        self._brightened = []       # copper items brightened for highlight
        self._colored = []          # nets given a temporary net-color
        self._name2code = {n["name"]: n["code"] for n in self.nets}
        self._netsettings = None
        try:
            self._netsettings = board.GetConnectivity().GetNetSettings()
        except Exception:
            pass
        self._try_seed = 0

        panel = wx.Panel(self)
        v = wx.BoxSizer(wx.VERTICAL)

        v.Add(wx.StaticText(panel, label="Nets (%d) — check to route:"
                            % len(self.nets)), 0, wx.LEFT | wx.TOP, 8)
        self.net_list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.net_list.EnableCheckBoxes(True)
        self.net_list.InsertColumn(0, "Net", width=230)
        # a WORD column so status is readable even when the row is selected
        # (the blue selection hides the row text color)
        self.net_list.InsertColumn(1, "Status", width=88)
        for n in self.nets:
            row = self.net_list.InsertItem(self.net_list.GetItemCount(),
                                           "%s   (%d pads)" % (n["name"], n["pads"]))
            self.net_list.SetItem(row, 1, "")
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
        self.panel = panel
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(panel, 1, wx.EXPAND)
        self.SetSizer(root)

        self.btn_route.Bind(wx.EVT_BUTTON, self.on_route)
        self.btn_try.Bind(wx.EVT_BUTTON, self.on_try_again)
        self.btn_commit.Bind(wx.EVT_BUTTON, self.on_commit)
        self.btn_revert.Bind(wx.EVT_BUTTON, self.on_revert)
        self.net_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select)
        self.net_list.Bind(wx.EVT_LIST_ITEM_CHECKED, self.on_check)
        self.net_list.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self.on_check)
        self.Bind(wx.EVT_CLOSE, self.on_close)

        self._loaded = False
        self._show_actions(False)
        self._update_route_enabled()
        if self.board.GetFileName():
            self.status.SetLabel("Loading routing status…")
            # run the slow DRC off the UI thread so the window is responsive
            threading.Thread(target=self._status_worker,
                             args=(self.board.GetFileName(),), daemon=True).start()
        else:
            self.status.SetLabel("Save the board to begin.")

    # --- helpers ------------------------------------------------------------

    def _show_actions(self, show):
        self.act_row.ShowItems(show)
        self.btn_route.Show(not show)
        self.panel.Layout()   # the sizer lives on the panel, not the dialog

    def _update_route_enabled(self):
        self.btn_route.Enable(self._loaded and bool(self._checked_names()))

    def on_check(self, _evt):
        self._update_route_enabled()
        self._refresh_highlight()

    def _apply_status_colors(self):
        for i, n in enumerate(self.nets):
            st = self.status_map.get(n["name"])
            self.net_list.SetItemTextColour(i, _STATUS_COL.get(st, _COL_UNKNOWN))
            self.net_list.SetItem(i, 1, _STATUS_WORD.get(st, ""))

    def _status_worker(self, bp):
        """Runs on a background thread: only the slow kicad-cli DRC."""
        try:
            unconn = shim.drc_unconnected(bp)
        except Exception as e:  # noqa: BLE001
            wx.CallAfter(self.status.SetLabel, "Routing status unavailable: %s" % e)
            return
        wx.CallAfter(self._status_ready, unconn)

    def _status_ready(self, unconn):
        """Back on the UI thread: derive per-net status + apply."""
        self.unconn = unconn
        code_name = {n["code"]: n["name"] for n in self.nets}
        has_cu = {}
        for t in self.board.GetTracks():
            nm = code_name.get(t.GetNetCode())
            if nm:
                has_cu[nm] = True
        self.status_map = {}
        for n in self.nets:
            if n["pads"] < 2:
                continue
            name = n["name"]
            if len(unconn.get(name, [])) == 0:
                self.status_map[name] = "routed"
            elif has_cu.get(name):
                self.status_map[name] = "partial"
            else:
                self.status_map[name] = "unrouted"
        self._apply_status_colors()
        self._loaded = True
        self._update_route_enabled()
        c = {"routed": 0, "partial": 0, "unrouted": 0}
        for x in self.status_map.values():
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
        self._update_route_enabled()

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

    def _color_net(self, name, rgb):
        """Assign a native KiCad net color (colors copper + ratsnest). No board
        geometry is added, so nothing is left behind."""
        if self._netsettings is None:
            return
        try:
            self._netsettings.SetNetColorAssignment(
                name, pcbnew.COLOR4D(rgb[0], rgb[1], rgb[2], 1.0))
            self._colored.append(name)
        except Exception:
            pass

    def _brighten_net(self, name):
        code = self._name2code.get(name)
        if code is None:
            return
        for it in list(self.board.GetPads()) + list(self.board.GetTracks()):
            if it.GetNetCode() == code:
                it.SetBrightened()
                self._brightened.append(it)

    def _clear_highlight(self):
        for it in self._brightened:
            try:
                it.ClearBrightened()
            except Exception:
                pass
        self._brightened = []
        if self._netsettings is not None:
            for name in self._colored:
                try:  # reset just OUR nets (don't nuke the user's net colors)
                    self._netsettings.SetNetColorAssignment(
                        name, pcbnew.COLOR4D(0, 0, 0, 0))
                except Exception:
                    pass
        self._colored = []

    def _refresh_highlight(self):
        """Highlight all CHECKED nets brightly and the ACTIVE (selected) net even
        brighter, using native net colors + copper brightening."""
        self._clear_highlight()
        checked = set(self._checked_names())
        active = None
        sel = self.net_list.GetFirstSelected()
        if sel != -1:
            active = self.nets[sel]["name"]
        for name in checked:
            if name == active:
                continue
            self._color_net(name, _HL_CHECKED)
            self._brighten_net(name)
        if active:
            self._color_net(active, _HL_ACTIVE)   # even brighter than checked
            self._brighten_net(active)
        self._refresh_canvas()

    def on_select(self, _evt):
        self._refresh_highlight()

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

        seg = sum(len(r.get("segments", [])) for r in results)
        via = sum(len(r.get("vias", [])) for r in results)
        if seg == 0 and via == 0:
            # nothing routed — say WHY, keep the nets highlighted, stay on Route
            reasons = []
            for r in results:
                if not r.get("ok"):
                    reasons.append("%s: %s" % (r["net"], r.get("reason") or
                                               "no clear path (try more layers / "
                                               "higher via cost)"))
            self.status.SetLabel("Couldn't route — " + "; ".join(reasons[:3]))
            self._refresh_highlight()
            self._show_actions(False)
            return

        incomplete = sum(1 for r in results if not r.get("ok"))
        msg = "Proposed %d tracks, %d vias" % (seg, via)
        if incomplete:
            msg += "  (%d net%s incomplete)" % (incomplete,
                                                "" if incomplete == 1 else "s")
        self.status.SetLabel(msg + " — Accept / Reject / Try again")
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
