"""dg-router routing core — single/greedy net router (pure Python + pcbnew).

Milestone R1: route the DRC gaps of a net on a single copper layer with a grid
A*, string-pull the path to an any-angle polyline, and write tracks back to a
COPY of the board (never the original). kicad-cli DRC is the ground-truth check.

Design choices that matter:
- We route each *gap* reported by the DRC oracle (shim.drc_unconnected), not
  pad-to-pad from scratch. Existing same-net copper is NOT an obstacle, so a
  partially-routed net gets completed, never restarted.
- Obstacles = other nets' pads/tracks/vias (clearance-inflated) + a board-edge
  margin, stamped onto a per-layer boolean grid.
- No vias yet (single layer). Nets whose gap endpoints don't share a copper
  layer are reported as skipped, not silently dropped.
"""

import os
import math
import heapq

import pcbnew

try:
    from . import shim            # imported as part of the package (GUI)
except ImportError:
    import shim                   # imported flat (headless, package dir on path)

_NM = 1e6


class RouteParams:
    def __init__(self, board, pitch_mm=0.15, clearance_mm=None, width_mm=None,
                 edge_margin_mm=None):
        ds = board.GetDesignSettings()
        self.pitch = pitch_mm
        self.clearance = clearance_mm if clearance_mm is not None \
            else _safe(lambda: ds.m_MinClearance / _NM, 0.2)
        self.width = width_mm if width_mm is not None else _dominant_width(board, ds)
        self.edge_margin = edge_margin_mm if edge_margin_mm is not None \
            else _safe(lambda: ds.m_CopperEdgeClearance / _NM, 0.3) or 0.3
        # obstacle inflation: keep the trace CENTER at least this far from other
        # copper so a full-width track clears it, plus a grid-discretization margin.
        self.inflate = self.clearance + self.width / 2.0 + self.pitch


def _via_width_mm(via, layer):
    """Via width in mm. The no-arg PCB_VIA.GetWidth() raises SystemError under a
    running wx.App (i.e. inside KiCad) in KiCad 10 — the layer-arg form works."""
    for L in (layer, pcbnew.F_Cu, pcbnew.B_Cu):
        try:
            w = via.GetWidth(L)
            if w:
                return w / _NM
        except Exception:
            continue
    return 0.6


def _dominant_width(board, ds):
    """Most common existing track width (matches the board's real netclass),
    falling back to the design minimum."""
    from collections import Counter
    ws = Counter()
    for t in board.GetTracks():
        if t.Type() == pcbnew.PCB_TRACE_T:
            ws[t.GetWidth()] += 1
    if ws:
        return ws.most_common(1)[0][0] / _NM
    return _safe(lambda: ds.m_TrackMinWidth / _NM, 0.25) or 0.25


def _safe(fn, default):
    try:
        v = fn()
        return v if v and v > 0 else default
    except Exception:
        return default


# --- costmap ---------------------------------------------------------------

class CostMap:
    """Per-layer boolean obstacle grid over the board edges bbox."""

    def __init__(self, board, layer_id, net_code, params):
        self.p = params
        bb = board.GetBoardEdgesBoundingBox()
        self.x0 = bb.GetX() / _NM
        self.y0 = bb.GetY() / _NM
        w = bb.GetWidth() / _NM
        h = bb.GetHeight() / _NM
        self.pitch = params.pitch
        self.nx = max(1, int(math.ceil(w / self.pitch)))
        self.ny = max(1, int(math.ceil(h / self.pitch)))
        self.blocked = bytearray(self.nx * self.ny)
        self._stamp_edge_margin()
        self._stamp_obstacles(board, layer_id, net_code)

    # index / coordinate helpers
    def idx(self, i, j):
        return j * self.nx + i

    def to_cell(self, x, y):
        return (int((x - self.x0) / self.pitch), int((y - self.y0) / self.pitch))

    def to_world(self, i, j):
        return (self.x0 + (i + 0.5) * self.pitch, self.y0 + (j + 0.5) * self.pitch)

    def in_bounds(self, i, j):
        return 0 <= i < self.nx and 0 <= j < self.ny

    def is_blocked(self, i, j):
        return self.blocked[self.idx(i, j)]

    # --- rasterization ---
    def _stamp_edge_margin(self):
        m = int(math.ceil(self.p.edge_margin / self.pitch))
        for j in range(self.ny):
            for i in range(self.nx):
                if i < m or j < m or i >= self.nx - m or j >= self.ny - m:
                    self.blocked[self.idx(i, j)] = 1

    def _stamp_disc(self, cx, cy, r):
        p = self.pitch
        i0 = max(0, int((cx - r - self.x0) / p))
        i1 = min(self.nx - 1, int((cx + r - self.x0) / p))
        j0 = max(0, int((cy - r - self.y0) / p))
        j1 = min(self.ny - 1, int((cy + r - self.y0) / p))
        r2 = r * r
        for j in range(j0, j1 + 1):
            wy = self.y0 + (j + 0.5) * p
            for i in range(i0, i1 + 1):
                wx = self.x0 + (i + 0.5) * p
                if (wx - cx) ** 2 + (wy - cy) ** 2 <= r2:
                    self.blocked[self.idx(i, j)] = 1

    def _stamp_segment(self, x1, y1, x2, y2, r):
        length = math.hypot(x2 - x1, y2 - y1)
        steps = max(1, int(length / (self.pitch * 0.5)))
        for s in range(steps + 1):
            t = s / steps
            self._stamp_disc(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, r)

    def carve_disc(self, cx, cy, r):
        """Clear a disc (used around our own gap endpoints)."""
        p = self.pitch
        i0 = max(0, int((cx - r - self.x0) / p))
        i1 = min(self.nx - 1, int((cx + r - self.x0) / p))
        j0 = max(0, int((cy - r - self.y0) / p))
        j1 = min(self.ny - 1, int((cy + r - self.y0) / p))
        r2 = r * r
        for j in range(j0, j1 + 1):
            wy = self.y0 + (j + 0.5) * p
            for i in range(i0, i1 + 1):
                wx = self.x0 + (i + 0.5) * p
                if (wx - cx) ** 2 + (wy - cy) ** 2 <= r2:
                    self.blocked[self.idx(i, j)] = 0

    def _stamp_obstacles(self, board, layer_id, net_code):
        clr = self.p.inflate
        # pads (other nets) present on this layer, or PTH (all layers)
        for pad in board.GetPads():
            if pad.GetNetCode() == net_code:
                continue
            if not pad.IsOnLayer(layer_id):
                continue
            pos = pad.GetPosition()
            sz = pad.GetSize()
            # circumscribe the (possibly rectangular) pad — half-DIAGONAL, so
            # corners can't poke past the disc — then inflate for clearance.
            r = math.hypot(sz.x, sz.y) / 2.0 / _NM + clr
            self._stamp_disc(pos.x / _NM, pos.y / _NM, r)
        # tracks / vias (other nets)
        for t in board.GetTracks():
            if t.GetNetCode() == net_code:
                continue
            if t.Type() == pcbnew.PCB_VIA_T:
                pos = t.GetPosition()
                r = _via_width_mm(t, layer_id) / 2.0 + clr
                self._stamp_disc(pos.x / _NM, pos.y / _NM, r)
            elif t.IsOnLayer(layer_id):
                s, e = t.GetStart(), t.GetEnd()
                r = t.GetWidth() / 2.0 / _NM + clr
                self._stamp_segment(s.x / _NM, s.y / _NM, e.x / _NM, e.y / _NM, r)


# --- A* + string pull ------------------------------------------------------

_DIRS = [(-1, 0), (1, 0), (0, -1), (0, 1),
         (-1, -1), (-1, 1), (1, -1), (1, 1)]


def astar(cm, start, goal, max_expansions=2_000_000):
    if not cm.in_bounds(*start) or not cm.in_bounds(*goal):
        return None
    if cm.is_blocked(*goal) or cm.is_blocked(*start):
        return None
    sx, sy = start
    gx, gy = goal

    def h(i, j):
        dx, dy = abs(i - gx), abs(j - gy)
        return (dx + dy) + (math.sqrt(2) - 2) * min(dx, dy)

    open_heap = [(h(sx, sy), 0.0, start)]
    gscore = {start: 0.0}
    came = {}
    seen = 0
    while open_heap:
        _, g, cur = heapq.heappop(open_heap)
        if cur == goal:
            return _reconstruct(came, cur)
        if g > gscore.get(cur, 1e18):
            continue
        seen += 1
        if seen > max_expansions:
            return None
        ci, cj = cur
        for di, dj in _DIRS:
            ni, nj = ci + di, cj + dj
            if not cm.in_bounds(ni, nj) or cm.is_blocked(ni, nj):
                continue
            if di and dj:  # no diagonal corner-cutting
                if cm.is_blocked(ci + di, cj) or cm.is_blocked(ci, cj + dj):
                    continue
                step = math.sqrt(2)
            else:
                step = 1.0
            ng = g + step
            nxt = (ni, nj)
            if ng < gscore.get(nxt, 1e18):
                gscore[nxt] = ng
                came[nxt] = cur
                heapq.heappush(open_heap, (ng + h(ni, nj), ng, nxt))
    return None


def nearest_free(cm, cell, max_rings=12):
    """Nearest non-blocked cell to `cell` (spiral search), or None."""
    ci, cj = cell
    if cm.in_bounds(ci, cj) and not cm.is_blocked(ci, cj):
        return cell
    for r in range(1, max_rings + 1):
        for di in range(-r, r + 1):
            for dj in (-r, r):
                i, j = ci + di, cj + dj
                if cm.in_bounds(i, j) and not cm.is_blocked(i, j):
                    return (i, j)
        for dj in range(-r + 1, r):
            for di in (-r, r):
                i, j = ci + di, cj + dj
                if cm.in_bounds(i, j) and not cm.is_blocked(i, j):
                    return (i, j)
    return None


def _reconstruct(came, cur):
    path = [cur]
    while cur in came:
        cur = came[cur]
        path.append(cur)
    path.reverse()
    return path


def _los(cm, a, b):
    """Strict line-of-sight: densely sample the segment (sub-cell steps) and
    reject if any sampled cell is blocked or out of bounds."""
    (ai, aj), (bi, bj) = a, b
    dist = math.hypot(bi - ai, bj - aj)
    n = max(1, int(dist / 0.25))
    for s in range(n + 1):
        t = s / n
        i = int(round(ai + (bi - ai) * t))
        j = int(round(aj + (bj - aj) * t))
        if not cm.in_bounds(i, j) or cm.is_blocked(i, j):
            return False
    return True


def string_pull(cm, cells):
    """Reduce a grid path to a minimal any-angle polyline of cells."""
    if len(cells) <= 2:
        return list(cells)
    out = [cells[0]]
    i = 0
    while i < len(cells) - 1:
        j = len(cells) - 1
        while j > i + 1 and not _los(cm, cells[i], cells[j]):
            j -= 1
        out.append(cells[j])
        i = j
    return out


# --- orchestration ---------------------------------------------------------

def _pad_layers(board, net_code):
    layers = {pcbnew.F_Cu: True, pcbnew.B_Cu: True}
    result = {pcbnew.F_Cu: 0, pcbnew.B_Cu: 0}
    total = 0
    for pad in board.GetPads():
        if pad.GetNetCode() != net_code:
            continue
        total += 1
        for l in (pcbnew.F_Cu, pcbnew.B_Cu):
            if not pad.IsOnLayer(l):
                layers[l] = False
    common = [l for l in (pcbnew.F_Cu, pcbnew.B_Cu) if layers[l]]
    return common


def choose_layer(board, net_code, prefer_name=None):
    """A single copper layer that every pad on the net can reach, or None."""
    common = _pad_layers(board, net_code)
    if not common:
        return None
    if prefer_name:
        pid = board.GetLayerID(prefer_name)
        if pid in common:
            return pid
    return common[0]


def route_net(board, net_name, gaps, params, prefer_name=None):
    """Route each gap of a net on one layer. Returns dict with polylines (mm)
    and per-gap status."""
    net = None
    for c in range(1, board.GetNetCount()):
        n = board.FindNet(c)
        if n and n.GetNetname() == net_name:
            net = n
            break
    if net is None:
        return {"net": net_name, "ok": False, "reason": "net not found"}

    layer_id = choose_layer(board, net.GetNetCode(), prefer_name)
    if layer_id is None:
        return {"net": net_name, "ok": False,
                "reason": "pads not on a common layer (needs via — not yet)"}

    cm = CostMap(board, layer_id, net.GetNetCode(), params)
    polylines = []
    routed = 0
    for (x1, y1, x2, y2) in gaps:
        # Route from the free cell nearest each pad (the costmap stays honest —
        # no carving away real clearance). The final segment snaps to the exact
        # pad center below.
        start = nearest_free(cm, cm.to_cell(x1, y1))
        goal = nearest_free(cm, cm.to_cell(x2, y2))
        if start is None or goal is None:
            continue
        cells = astar(cm, start, goal)
        if not cells:
            continue
        pulled = string_pull(cm, cells)
        pts = [cm.to_world(i, j) for (i, j) in pulled]
        # snap the true endpoints exactly onto the pads
        pts[0] = (x1, y1)
        pts[-1] = (x2, y2)
        polylines.append(pts)
        routed += 1

    return {
        "net": net_name, "ok": routed == len(gaps) and len(gaps) > 0,
        "layer": board.GetLayerName(layer_id),
        "gaps": len(gaps), "routed": routed, "polylines": polylines,
        "net_code": net.GetNetCode(), "layer_id": layer_id,
    }


def write_polylines(board, net_code, layer_id, polylines, width_mm):
    n = 0
    w = int(round(width_mm * _NM))
    for pts in polylines:
        for k in range(len(pts) - 1):
            (x1, y1), (x2, y2) = pts[k], pts[k + 1]
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(pcbnew.VECTOR2I(int(round(x1 * _NM)), int(round(y1 * _NM))))
            t.SetEnd(pcbnew.VECTOR2I(int(round(x2 * _NM)), int(round(y2 * _NM))))
            t.SetWidth(w)
            t.SetLayer(layer_id)
            t.SetNetCode(net_code)
            board.Add(t)
            n += 1
    return n


def solve(board_path, net_names, out_path, params=None, prefer_name=None):
    """Route the given nets, write tracks to a COPY at out_path, return summary."""
    board = pcbnew.LoadBoard(board_path)
    params = params or RouteParams(board)
    unconn = shim.drc_unconnected(board_path)

    results = []
    total_tracks = 0
    for name in net_names:
        gaps = unconn.get(name, [])
        if not gaps:
            results.append({"net": name, "ok": True, "reason": "already routed",
                            "gaps": 0, "routed": 0})
            continue
        r = route_net(board, name, gaps, params, prefer_name)
        if r.get("polylines"):
            total_tracks += write_polylines(board, r["net_code"], r["layer_id"],
                                            r["polylines"], params.width)
        r.pop("polylines", None)
        results.append(r)

    # Refill copper zones so DRC doesn't flag stale pad-to-zone clearances.
    try:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception:
        pass

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    pcbnew.SaveBoard(out_path, board)
    return {"out": out_path, "tracks_added": total_tracks, "results": results,
            "params": {"pitch": params.pitch, "clearance": params.clearance,
                       "width": params.width}}
