"""dg-router routing core — octilinear, netclass-aware grid router (pcbnew).

Routes each DRC gap of a net on a single copper layer:
- per-net width/clearance come from the net's effective NETCLASS (not a global
  guess), via board connectivity NET_SETTINGS
- A* is turn-penalized on an 8-connected grid and the path is reduced to
  OCTILINEAR segments (0/45/90 only) — no arbitrary-angle diagonals
- the branch into a pad NECKS DOWN when the pad is narrower than the net width
- existing same-net copper is never an obstacle, so partial nets are completed
- writes to a COPY (headless) or the in-memory board (GUI); kicad-cli DRC is the
  ground-truth check

No vias yet (single layer): nets whose endpoints don't share a copper layer are
reported skipped, not silently dropped.
"""

import math
import heapq

import pcbnew

try:
    from . import shim
except ImportError:
    import shim

_NM = 1e6

# 8 directions in ANGULAR order (45deg apart) so turn cost = index distance.
_DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1),
         (-1, 0), (-1, -1), (0, -1), (1, -1)]
_NODIR = 8


def _safe(fn, default):
    try:
        v = fn()
        return v if v and v > 0 else default
    except Exception:
        return default


def _sgn(v):
    return (v > 0) - (v < 0)


def _via_width_mm(via, layer):
    """No-arg PCB_VIA.GetWidth() raises under a running wx.App (KiCad GUI) in
    KiCad 10; the layer-arg form works."""
    for L in (layer, pcbnew.F_Cu, pcbnew.B_Cu):
        try:
            w = via.GetWidth(L)
            if w:
                return w / _NM
        except Exception:
            continue
    return 0.6


class RouteParams:
    """Global routing params. Per-net width/clearance are resolved from the
    net's netclass at route time."""

    def __init__(self, board, pitch_mm=0.2, turn_cost=0.7):
        ds = board.GetDesignSettings()
        self.pitch = pitch_mm
        self.turn_cost = turn_cost
        # board-setup hard floors (enforced regardless of netclass)
        self.edge_clearance = _safe(lambda: ds.m_CopperEdgeClearance / _NM, 0.3)
        self.min_track = _safe(lambda: ds.m_TrackMinWidth / _NM, 0.2)
        try:
            self.netsettings = board.GetConnectivity().GetNetSettings()
        except Exception:
            self.netsettings = None

    def net_class(self, net_name):
        """(track_width_mm, clearance_mm) for a net from its effective netclass."""
        if self.netsettings is not None:
            try:
                nc = self.netsettings.GetEffectiveNetClass(net_name)
                return nc.GetTrackWidth() / _NM, nc.GetClearance() / _NM
            except Exception:
                pass
        return 0.2, 0.2


# --- costmap ---------------------------------------------------------------

class CostMap:
    def __init__(self, board, layer_id, net_code, pitch, edge_margin,
                 clearance, width):
        bb = board.GetBoardEdgesBoundingBox()
        self.x0 = bb.GetX() / _NM
        self.y0 = bb.GetY() / _NM
        self.pitch = pitch
        self.nx = max(1, int(math.ceil(bb.GetWidth() / _NM / pitch)))
        self.ny = max(1, int(math.ceil(bb.GetHeight() / _NM / pitch)))
        self.blocked = bytearray(self.nx * self.ny)
        self.inflate = clearance + width / 2.0 + pitch
        self._stamp_edge(edge_margin)
        self._stamp_obstacles(board, layer_id, net_code)

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

    def _stamp_edge(self, margin):
        m = int(math.ceil(margin / self.pitch))
        nx, ny = self.nx, self.ny
        for j in range(ny):
            border = j < m or j >= ny - m
            base = j * nx
            if border:
                for i in range(nx):
                    self.blocked[base + i] = 1
            else:
                for i in range(m):
                    self.blocked[base + i] = 1
                    self.blocked[base + nx - 1 - i] = 1

    def _disc(self, cx, cy, r):
        p = self.pitch
        i0 = max(0, int((cx - r - self.x0) / p))
        i1 = min(self.nx - 1, int((cx + r - self.x0) / p))
        j0 = max(0, int((cy - r - self.y0) / p))
        j1 = min(self.ny - 1, int((cy + r - self.y0) / p))
        r2 = r * r
        for j in range(j0, j1 + 1):
            wy = self.y0 + (j + 0.5) * p
            row = j * self.nx
            for i in range(i0, i1 + 1):
                wx = self.x0 + (i + 0.5) * p
                if (wx - cx) ** 2 + (wy - cy) ** 2 <= r2:
                    self.blocked[row + i] = 1

    def _segment(self, x1, y1, x2, y2, r):
        length = math.hypot(x2 - x1, y2 - y1)
        steps = max(1, int(length / (self.pitch * 0.5)))
        for s in range(steps + 1):
            t = s / steps
            self._disc(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, r)

    def _stamp_obstacles(self, board, layer_id, net_code):
        clr = self.inflate
        for pad in board.GetPads():
            if pad.GetNetCode() == net_code or not pad.IsOnLayer(layer_id):
                continue
            pos = pad.GetPosition()
            sz = pad.GetSize()
            self._disc(pos.x / _NM, pos.y / _NM,
                       math.hypot(sz.x, sz.y) / 2.0 / _NM + clr)
        for t in board.GetTracks():
            if t.GetNetCode() == net_code:
                continue
            if t.Type() == pcbnew.PCB_VIA_T:
                pos = t.GetPosition()
                self._disc(pos.x / _NM, pos.y / _NM,
                           _via_width_mm(t, layer_id) / 2.0 + clr)
            elif t.IsOnLayer(layer_id):
                s, e = t.GetStart(), t.GetEnd()
                self._segment(s.x / _NM, s.y / _NM, e.x / _NM, e.y / _NM,
                              t.GetWidth() / 2.0 / _NM + clr)


# --- A* (turn-penalized) + octilinear reduction ----------------------------

def nearest_free(cm, cell, max_rings=14):
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


def astar(cm, start, goal, turn_cost, max_expansions=1_500_000):
    if not (cm.in_bounds(*start) and cm.in_bounds(*goal)):
        return None
    if cm.is_blocked(*start) or cm.is_blocked(*goal):
        return None
    gx, gy = goal

    def h(i, j):
        dx, dy = abs(i - gx), abs(j - gy)
        return (dx + dy) - (2 - math.sqrt(2)) * min(dx, dy)

    start_state = (start[0], start[1], _NODIR)
    open_heap = [(h(*start), 0.0, start_state)]
    g = {start_state: 0.0}
    came = {}
    seen = 0
    while open_heap:
        _, gc, st = heapq.heappop(open_heap)
        ci, cj, cd = st
        if (ci, cj) == goal:
            return _reconstruct(came, st)
        if gc > g.get(st, 1e18):
            continue
        seen += 1
        if seen > max_expansions:
            return None
        for nd, (di, dj) in enumerate(_DIRS):
            ni, nj = ci + di, cj + dj
            if not cm.in_bounds(ni, nj) or cm.is_blocked(ni, nj):
                continue
            if di and dj:  # block diagonal corner-cutting
                if cm.is_blocked(ci + di, cj) or cm.is_blocked(ci, cj + dj):
                    continue
                step = math.sqrt(2)
            else:
                step = 1.0
            turn = 0 if cd == _NODIR else min(abs(cd - nd), 8 - abs(cd - nd))
            ng = gc + step + turn * turn_cost
            nst = (ni, nj, nd)
            if ng < g.get(nst, 1e18):
                g[nst] = ng
                came[nst] = st
                heapq.heappush(open_heap, (ng + h(ni, nj), ng, nst))
    return None


def _reconstruct(came, st):
    cells = [(st[0], st[1])]
    while st in came:
        st = came[st]
        cells.append((st[0], st[1]))
    cells.reverse()
    return cells


def octilinear_polyline(cm, cells):
    """Keep only bend points -> octilinear polyline (segments are all 0/45/90
    because A* moves are 8-directional)."""
    if len(cells) <= 1:
        return [cm.to_world(*c) for c in cells]
    keep = [cells[0]]
    prev = (_sgn(cells[1][0] - cells[0][0]), _sgn(cells[1][1] - cells[0][1]))
    for k in range(1, len(cells) - 1):
        d = (_sgn(cells[k + 1][0] - cells[k][0]),
             _sgn(cells[k + 1][1] - cells[k][1]))
        if d != prev:
            keep.append(cells[k])
            prev = d
    keep.append(cells[-1])
    return [cm.to_world(*c) for c in keep]


# --- neck-down + segment building ------------------------------------------

def _pt_along(a, b, dist):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    if L <= dist:
        return None
    t = dist / L
    return (a[0] + dx * t, a[1] + dy * t)


def _pad_min_dim(board, net_code, x, y):
    best, bd = None, 1e18
    for pad in board.GetPads():
        if pad.GetNetCode() != net_code:
            continue
        p = pad.GetPosition()
        d = math.hypot(p.x / _NM - x, p.y / _NM - y)
        if d < bd:
            bd = d
            sz = pad.GetSize()
            best = min(sz.x, sz.y) / _NM
    return best


def build_segments(pts, main_w, neck_start, neck_end, neck_len=0.5):
    """Polyline -> [(x1,y1,x2,y2,w)], necking a short stub at each end that
    needs it. Splits stay on the (octilinear) segment so angles are preserved."""
    pts = list(pts)
    if len(pts) < 2:
        return []
    if neck_start < main_w:
        q = _pt_along(pts[0], pts[1], neck_len)
        if q is not None:
            pts.insert(1, q)
    if neck_end < main_w:
        q = _pt_along(pts[-1], pts[-2], neck_len)
        if q is not None:
            pts.insert(len(pts) - 1, q)
    n = len(pts) - 1
    segs = []
    for i in range(n):
        w = main_w
        if i == 0 and neck_start < main_w:
            w = neck_start
        if i == n - 1 and neck_end < main_w:
            w = neck_end
        segs.append((pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], w))
    return segs


# --- orchestration ---------------------------------------------------------

def _find_net(board, net_name):
    for c in range(1, board.GetNetCount()):
        n = board.FindNet(c)
        if n and n.GetNetname() == net_name:
            return n
    return None


def choose_layer(board, net_code, prefer_name=None):
    on = {pcbnew.F_Cu: True, pcbnew.B_Cu: True}
    for pad in board.GetPads():
        if pad.GetNetCode() != net_code:
            continue
        for L in (pcbnew.F_Cu, pcbnew.B_Cu):
            if not pad.IsOnLayer(L):
                on[L] = False
    common = [L for L in (pcbnew.F_Cu, pcbnew.B_Cu) if on[L]]
    if not common:
        return None
    if prefer_name:
        pid = board.GetLayerID(prefer_name)
        if pid in common:
            return pid
    return common[0]


def route_net(board, net_name, gaps, params, prefer_name=None):
    net = _find_net(board, net_name)
    if net is None:
        return {"net": net_name, "ok": False, "reason": "net not found"}
    net_code = net.GetNetCode()

    layer_id = choose_layer(board, net_code, prefer_name)
    if layer_id is None:
        return {"net": net_name, "ok": False,
                "reason": "pads not on a common layer (needs a via — not yet)"}

    nc_width, clearance = params.net_class(net_name)
    # the board minimum track width is a hard floor above the netclass width
    width = max(nc_width, params.min_track)
    # edge stamp keeps the track EDGE clear of the board edge (rule + width/2)
    edge_m = params.edge_clearance + width / 2.0 + params.pitch
    cm = CostMap(board, layer_id, net_code, params.pitch, edge_m,
                 clearance, width)

    segments = []
    routed = 0
    for (x1, y1, x2, y2) in gaps:
        start = nearest_free(cm, cm.to_cell(x1, y1))
        goal = nearest_free(cm, cm.to_cell(x2, y2))
        if start is None or goal is None:
            continue
        cells = astar(cm, start, goal, params.turn_cost)
        if not cells:
            continue
        pts = octilinear_polyline(cm, cells)
        if len(pts) < 2:
            continue
        # neck each end down to the pad if the pad is narrower than the net width
        ps = _pad_min_dim(board, net_code, x1, y1)
        pe = _pad_min_dim(board, net_code, x2, y2)
        neck_s = max(params.min_track, min(width, ps)) if ps else width
        neck_e = max(params.min_track, min(width, pe)) if pe else width
        segments += build_segments(pts, width, neck_s, neck_e)
        routed += 1

    return {"net": net_name, "ok": routed == len(gaps) and len(gaps) > 0,
            "layer": board.GetLayerName(layer_id), "gaps": len(gaps),
            "routed": routed, "segments": segments, "net_code": net_code,
            "layer_id": layer_id, "width": width}


def write_segments(board, net_code, layer_id, segments):
    n = 0
    for (x1, y1, x2, y2, w) in segments:
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(int(round(x1 * _NM)), int(round(y1 * _NM))))
        t.SetEnd(pcbnew.VECTOR2I(int(round(x2 * _NM)), int(round(y2 * _NM))))
        t.SetWidth(int(round(w * _NM)))
        t.SetLayer(layer_id)
        t.SetNetCode(net_code)
        board.Add(t)
        n += 1
    return n


def refill_zones(board):
    try:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception:
        pass


def solve(board_path, net_names, out_path, params=None, prefer_name=None):
    import os
    board = pcbnew.LoadBoard(board_path)
    params = params or RouteParams(board)
    unconn = shim.drc_unconnected(board_path)

    results = []
    total = 0
    for name in net_names:
        gaps = unconn.get(name, [])
        if not gaps:
            results.append({"net": name, "ok": True, "reason": "already routed",
                            "gaps": 0, "routed": 0})
            continue
        r = route_net(board, name, gaps, params, prefer_name)
        if r.get("segments"):
            total += write_segments(board, r["net_code"], r["layer_id"],
                                    r["segments"])
        r.pop("segments", None)
        results.append(r)

    refill_zones(board)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    pcbnew.SaveBoard(out_path, board)
    return {"out": out_path, "tracks_added": total, "results": results}
