"""dg-router routing core — octilinear, multi-layer (via-capable) grid router.

Per net, per DRC gap:
- 3D A* over (x, y, layer): 8-connected octilinear moves in-plane + via
  transitions between routable layers (via cost from RouteParams)
- turn-penalized, then reduced to OCTILINEAR segments (0/45/90 only)
- width/clearance/via size from the effective NETCLASS (board-min floors)
- the branch into a pad NECKS DOWN when the pad is narrower than the net width
- an octilinear stub is attached so the trace actually ENTERS the pad
- batch routing bundles nets that run together (attraction to already-routed
  members of the same batch)

Existing same-net copper is never an obstacle, so partial nets are completed.
kicad-cli DRC is the ground-truth check (connectivity + violations).
"""

import math
import heapq
import random

import pcbnew

try:
    from . import shim
except ImportError:
    import shim

_NM = 1e6

# 8 directions in ANGULAR order (45deg apart): turn cost = index distance.
_DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1),
         (-1, 0), (-1, -1), (0, -1), (1, -1)]
_NODIR = 8

DEFAULT_LAYERS = ["F.Cu", "B.Cu"]


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
    def __init__(self, board, pitch_mm=0.2, turn_cost=0.7, via_cost=10.0,
                 layer_names=None, seed=0, jitter=0.0):
        ds = board.GetDesignSettings()
        self.pitch = pitch_mm
        self.turn_cost = turn_cost
        self.via_cost = via_cost           # extra A* cost (grid steps) per via
        # "Try again" perturbs costs so a deterministic A* yields a different
        # valid solution. jitter=0 -> deterministic.
        self.jitter = jitter
        self._rng = random.Random(seed)
        self.edge_clearance = _safe(lambda: ds.m_CopperEdgeClearance / _NM, 0.3)
        self.min_track = _safe(lambda: ds.m_TrackMinWidth / _NM, 0.2)
        self.layer_names = layer_names or list(DEFAULT_LAYERS)
        self.layer_ids = [board.GetLayerID(n) for n in self.layer_names]
        try:
            self.netsettings = board.GetConnectivity().GetNetSettings()
        except Exception:
            self.netsettings = None

    def net_class(self, net_name):
        """(track_width, clearance, via_dia, via_drill) mm, from the netclass,
        with the board minimum track width as a floor."""
        w, clr, vd, vdr = 0.2, 0.2, 0.6, 0.3
        if self.netsettings is not None:
            try:
                nc = self.netsettings.GetEffectiveNetClass(net_name)
                w = nc.GetTrackWidth() / _NM
                clr = nc.GetClearance() / _NM
                vd = nc.GetViaDiameter() / _NM
                vdr = nc.GetViaDrill() / _NM
            except Exception:
                pass
        return max(w, self.min_track), clr, vd, vdr


# --- per-layer obstacle grids + via grid -----------------------------------

class CostMap:
    def __init__(self, board, layer_ids, net_code, pitch, edge_margin,
                 clearance, width, via_dia):
        bb = board.GetBoardEdgesBoundingBox()
        self.x0 = bb.GetX() / _NM
        self.y0 = bb.GetY() / _NM
        self.pitch = pitch
        self.nx = max(1, int(math.ceil(bb.GetWidth() / _NM / pitch)))
        self.ny = max(1, int(math.ceil(bb.GetHeight() / _NM / pitch)))
        self.layers = list(layer_ids)
        n = self.nx * self.ny
        self.blocked = [bytearray(n) for _ in self.layers]
        self.via_blocked = bytearray(n)
        self.attract = [bytearray(n) for _ in self.layers]  # bus bundling bonus
        self.track_inflate = clearance + width / 2.0 + pitch
        self.via_inflate = clearance + via_dia / 2.0 + pitch

        edge_via = edge_margin + (via_dia - width) / 2.0
        for li in range(len(self.layers)):
            self._stamp_edge(self.blocked[li], edge_margin)
        self._stamp_edge(self.via_blocked, edge_via)
        self._stamp_obstacles(board, net_code)

    def idx(self, i, j):
        return j * self.nx + i

    def to_cell(self, x, y):
        return (int((x - self.x0) / self.pitch), int((y - self.y0) / self.pitch))

    def to_world(self, i, j):
        return (self.x0 + (i + 0.5) * self.pitch, self.y0 + (j + 0.5) * self.pitch)

    def in_bounds(self, i, j):
        return 0 <= i < self.nx and 0 <= j < self.ny

    def blocked_at(self, li, i, j):
        return self.blocked[li][self.idx(i, j)]

    def via_ok(self, i, j):
        return not self.via_blocked[self.idx(i, j)]

    def _stamp_edge(self, grid, margin):
        m = int(math.ceil(margin / self.pitch))
        nx, ny = self.nx, self.ny
        for j in range(ny):
            base = j * nx
            if j < m or j >= ny - m:
                for i in range(nx):
                    grid[base + i] = 1
            else:
                for i in range(m):
                    grid[base + i] = 1
                    grid[base + nx - 1 - i] = 1

    def _disc(self, grid, cx, cy, r):
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
                    grid[row + i] = 1

    def _seg(self, grid, x1, y1, x2, y2, r):
        length = math.hypot(x2 - x1, y2 - y1)
        steps = max(1, int(length / (self.pitch * 0.5)))
        for s in range(steps + 1):
            t = s / steps
            self._disc(grid, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, r)

    def _stamp_obstacles(self, board, net_code):
        tclr, vclr = self.track_inflate, self.via_inflate
        lset = set(self.layers)
        lidx = {L: k for k, L in enumerate(self.layers)}
        for pad in board.GetPads():
            if pad.GetNetCode() == net_code:
                continue
            pos = pad.GetPosition()
            sz = pad.GetSize()
            rr = math.hypot(sz.x, sz.y) / 2.0 / _NM
            self._disc(self.via_blocked, pos.x / _NM, pos.y / _NM, rr + vclr)
            for L in self.layers:
                if pad.IsOnLayer(L):
                    self._disc(self.blocked[lidx[L]], pos.x / _NM, pos.y / _NM,
                               rr + tclr)
        for t in board.GetTracks():
            if t.GetNetCode() == net_code:
                continue
            if t.Type() == pcbnew.PCB_VIA_T:
                pos = t.GetPosition()
                rr = _via_width_mm(t, self.layers[0]) / 2.0
                self._disc(self.via_blocked, pos.x / _NM, pos.y / _NM, rr + vclr)
                for li in range(len(self.layers)):
                    self._disc(self.blocked[li], pos.x / _NM, pos.y / _NM,
                               rr + tclr)
            else:
                lyr = t.GetLayer()
                if lyr not in lset:
                    continue
                s, e = t.GetStart(), t.GetEnd()
                rr = t.GetWidth() / 2.0 / _NM
                self._seg(self.blocked[lidx[lyr]], s.x / _NM, s.y / _NM,
                          e.x / _NM, e.y / _NM, rr + tclr)
                self._seg(self.via_blocked, s.x / _NM, s.y / _NM,
                          e.x / _NM, e.y / _NM, rr + vclr)

    def stamp_prior(self, segments, vias, our_width):
        """Stamp already-routed batch nets as obstacles so the next net in the
        batch doesn't cross or short them (no board mutation needed)."""
        lidx = {L: k for k, L in enumerate(self.layers)}
        for (x1, y1, x2, y2, w, lyr) in segments:
            r = w / 2.0 + self.track_inflate
            if lyr in lidx:
                self._seg(self.blocked[lidx[lyr]], x1, y1, x2, y2, r)
            self._seg(self.via_blocked, x1, y1, x2, y2, w / 2.0 + self.via_inflate)
        for (x, y, dia, drill) in vias:
            for li in range(len(self.layers)):
                self._disc(self.blocked[li], x, y, dia / 2.0 + self.track_inflate)
            self._disc(self.via_blocked, x, y, dia / 2.0 + self.via_inflate)

    def add_attraction(self, li, cells, radius_cells=2, bonus=1):
        """Mark cells (and a neighborhood) on a layer as attractive, so later
        nets in the batch prefer to run alongside — bus bundling."""
        g = self.attract[li]
        for (i, j) in cells:
            for dj in range(-radius_cells, radius_cells + 1):
                for di in range(-radius_cells, radius_cells + 1):
                    ii, jj = i + di, j + dj
                    if self.in_bounds(ii, jj):
                        g[self.idx(ii, jj)] = bonus


# --- 3D A* (x, y, layer) + octilinear reduction ----------------------------

def nearest_free(cm, li, cell, max_rings=16):
    ci, cj = cell
    if cm.in_bounds(ci, cj) and not cm.blocked_at(li, ci, cj):
        return cell
    for r in range(1, max_rings + 1):
        for di in range(-r, r + 1):
            for dj in (-r, r):
                i, j = ci + di, cj + dj
                if cm.in_bounds(i, j) and not cm.blocked_at(li, i, j):
                    return (i, j)
        for dj in range(-r + 1, r):
            for di in (-r, r):
                i, j = ci + di, cj + dj
                if cm.in_bounds(i, j) and not cm.blocked_at(li, i, j):
                    return (i, j)
    return None


def astar(cm, starts, goal_cell, goal_layers, params, max_expansions=1_500_000,
          on_progress=None, progress_every=600):
    """starts: list of (i,j,li). goal reached at goal_cell on any goal layer.
    Returns list of (i,j,li) or None. on_progress(cm, new_cells) is called
    periodically with cells popped since the last call (for live animation)."""
    gx, gy = goal_cell
    goalset = set(goal_layers)
    attract_bonus = 0.6
    new_cells = []

    def h(i, j):
        dx, dy = abs(i - gx), abs(j - gy)
        return (dx + dy) - (2 - math.sqrt(2)) * min(dx, dy)

    open_heap = []
    g = {}
    came = {}
    for (si, sj, sl) in starts:
        if cm.blocked_at(sl, si, sj):
            continue
        st = (si, sj, sl, _NODIR)
        g[st] = 0.0
        heapq.heappush(open_heap, (h(si, sj), 0.0, st))

    seen = 0
    nlayers = len(cm.layers)
    while open_heap:
        _, gc, st = heapq.heappop(open_heap)
        ci, cj, cl, cd = st
        if (ci, cj) == goal_cell and cl in goalset:
            if on_progress and new_cells:
                on_progress(cm, new_cells)
            return _reconstruct(came, st)
        if gc > g.get(st, 1e18):
            continue
        seen += 1
        if seen > max_expansions:
            return None
        if on_progress:
            new_cells.append((ci, cj, cl))
            if len(new_cells) >= progress_every:
                on_progress(cm, new_cells)
                new_cells = []
        # in-plane moves
        for nd, (di, dj) in enumerate(_DIRS):
            ni, nj = ci + di, cj + dj
            if not cm.in_bounds(ni, nj) or cm.blocked_at(cl, ni, nj):
                continue
            if di and dj:
                if cm.blocked_at(cl, ci + di, cj) or cm.blocked_at(cl, ci, cj + dj):
                    continue
                step = math.sqrt(2)
            else:
                step = 1.0
            turn = 0 if cd == _NODIR else min(abs(cd - nd), 8 - abs(cd - nd))
            cost = step + turn * params.turn_cost
            if cm.attract[cl][cm.idx(ni, nj)]:
                cost = max(0.1, cost - attract_bonus)
            if params.jitter:
                cost += params.jitter * params._rng.random()
            ng = gc + cost
            nst = (ni, nj, nd)
            key = (ni, nj, cl, nd)
            if ng < g.get(key, 1e18):
                g[key] = ng
                came[key] = st
                heapq.heappush(open_heap, (ng + h(ni, nj), ng, key))
        # via moves (change layer, same cell)
        if nlayers > 1 and cm.via_ok(ci, cj):
            for nl in range(nlayers):
                if nl == cl or cm.blocked_at(nl, ci, cj):
                    continue
                ng = gc + params.via_cost
                key = (ci, cj, nl, cd)
                if ng < g.get(key, 1e18):
                    g[key] = ng
                    came[key] = st
                    heapq.heappush(open_heap, (ng + h(ci, cj), ng, key))
    return None


def _reconstruct(came, st):
    out = [(st[0], st[1], st[2])]
    while st in came:
        st = came[st]
        out.append((st[0], st[1], st[2]))
    out.reverse()
    return out


def split_layer_runs(cells3):
    """[(i,j,li)] -> ([run_of_cells_per_layer...], [via_cells]).
    Each run is (layer_index, [(i,j),...]); vias are (i,j) where layer changed."""
    runs = []
    vias = []
    cur_layer = cells3[0][2]
    cur = [(cells3[0][0], cells3[0][1])]
    for k in range(1, len(cells3)):
        i, j, li = cells3[k]
        if li != cur_layer:
            runs.append((cur_layer, cur))
            vias.append((i, j))
            cur_layer = li
            cur = [(i, j)]
        else:
            cur.append((i, j))
    runs.append((cur_layer, cur))
    return runs, vias


def _octi_corner(a, b):
    """Corner making a->b an octilinear L: 45deg diagonal then orthogonal."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    diag = min(abs(dx), abs(dy))
    return (a[0] + _sgn(dx) * diag, a[1] + _sgn(dy) * diag)


def _seg_clear(cm, li, a, b):
    """Is the octilinear segment a->b free on layer li (no corner-cutting)?"""
    x, y = a
    dx, dy = _sgn(b[0] - a[0]), _sgn(b[1] - a[1])
    n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
    for _ in range(n + 1):
        if not cm.in_bounds(x, y) or cm.blocked_at(li, x, y):
            return False
        if dx and dy and cm.blocked_at(li, x + dx, y) and cm.blocked_at(li, x, y + dy):
            return False
        x += dx
        y += dy
    return True


def octi_pull(cm, li, cells):
    """Collapse an A* cell path into the fewest octilinear segments: greedily
    connect the farthest reachable point with a 45deg+orthogonal L. Removes the
    grid staircase."""
    if len(cells) <= 2:
        return [cm.to_world(*c) for c in cells]
    out = [cells[0]]
    i = 0
    while i < len(cells) - 1:
        best = i + 1
        for j in range(len(cells) - 1, i, -1):
            corner = _octi_corner(cells[i], cells[j])
            if _seg_clear(cm, li, cells[i], corner) and \
               _seg_clear(cm, li, corner, cells[j]):
                best = j
                break
        corner = _octi_corner(cells[i], cells[best])
        if corner != cells[i] and corner != cells[best]:
            out.append(corner)
        out.append(cells[best])
        i = best
    return [cm.to_world(*c) for c in out]


# --- pad entry + neck-down --------------------------------------------------

def _octi_stub(pad_pt, grid_pt):
    """Octilinear (<=2 seg) connection pad_pt -> grid_pt: 45 diagonal then
    orthogonal. Returns the intermediate points [pad_pt, corner, grid_pt]."""
    dx, dy = grid_pt[0] - pad_pt[0], grid_pt[1] - pad_pt[1]
    diag = min(abs(dx), abs(dy))
    corner = (pad_pt[0] + _sgn(dx) * diag, pad_pt[1] + _sgn(dy) * diag)
    pts = [pad_pt, corner, grid_pt]
    return [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]


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


def build_segments(pts, layer_id, main_w, neck_start, neck_end, neck_len=0.5):
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
        segs.append((pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], w,
                     layer_id))
    return segs


# --- orchestration ---------------------------------------------------------

def _find_net(board, net_name):
    for c in range(1, board.GetNetCount()):
        n = board.FindNet(c)
        if n and n.GetNetname() == net_name:
            return n
    return None


def _pad_layers_at(board, net_code, x, y, routable):
    """Routable layer_ids the pad nearest (x,y) sits on."""
    best, bd = None, 1e18
    for pad in board.GetPads():
        if pad.GetNetCode() != net_code:
            continue
        p = pad.GetPosition()
        d = math.hypot(p.x / _NM - x, p.y / _NM - y)
        if d < bd:
            bd = d
            best = [L for L in routable if pad.IsOnLayer(L)]
    return best or list(routable)


def route_net(board, net_name, gaps, params, prior_segments=None,
              prior_vias=None, attract_paths=None, on_progress=None):
    net = _find_net(board, net_name)
    if net is None:
        return {"net": net_name, "ok": False, "reason": "net not found"}
    net_code = net.GetNetCode()
    routable = params.layer_ids

    width, clearance, via_dia, via_drill = params.net_class(net_name)
    edge_m = params.edge_clearance + width / 2.0 + params.pitch
    cm = CostMap(board, routable, net_code, params.pitch, edge_m,
                 clearance, width, via_dia)
    if prior_segments or prior_vias:
        cm.stamp_prior(prior_segments or [], prior_vias or [], width)
    if attract_paths:
        for li, cells in attract_paths.items():
            cm.add_attraction(li, cells)
    lidx = {L: k for k, L in enumerate(routable)}

    segments, vias, path_cells_by_layer = [], [], {}
    routed = 0
    for (x1, y1, x2, y2) in gaps:
        sl = [lidx[L] for L in _pad_layers_at(board, net_code, x1, y1, routable)]
        gl = [lidx[L] for L in _pad_layers_at(board, net_code, x2, y2, routable)]
        starts = []
        for li in sl:
            c = nearest_free(cm, li, cm.to_cell(x1, y1))
            if c:
                starts.append((c[0], c[1], li))
        goal_cell = None
        for li in gl:
            c = nearest_free(cm, li, cm.to_cell(x2, y2))
            if c:
                goal_cell = c
                break
        if not starts or goal_cell is None:
            continue
        cells3 = astar(cm, starts, goal_cell, gl, params, on_progress=on_progress)
        if not cells3:
            continue

        runs, via_cells = split_layer_runs(cells3)
        ps = _pad_min_dim(board, net_code, x1, y1)
        pe = _pad_min_dim(board, net_code, x2, y2)
        neck_s = max(params.min_track, min(width, ps)) if ps else width
        neck_e = max(params.min_track, min(width, pe)) if pe else width

        for ri, (li, run) in enumerate(runs):
            path_cells_by_layer.setdefault(li, []).extend(run)
            pts = octi_pull(cm, li, run)
            if ri == 0:  # attach the start pad
                pts = _octi_stub((x1, y1), pts[0])[:-1] + pts
            if ri == len(runs) - 1:  # attach the end pad
                pts = pts + _octi_stub((x2, y2), pts[-1])[:-1][::-1]
            ns = neck_s if ri == 0 else width
            ne = neck_e if ri == len(runs) - 1 else width
            segments += build_segments(pts, routable[li], width, ns, ne)

        for (vi, vj) in via_cells:
            wx_, wy_ = cm.to_world(vi, vj)
            vias.append((wx_, wy_, via_dia, via_drill))
            # stamp our own via so later gaps of THIS net don't stack on it
            for li in range(len(cm.layers)):
                cm._disc(cm.blocked[li], wx_, wy_, via_dia / 2.0 + cm.track_inflate)
            cm._disc(cm.via_blocked, wx_, wy_, via_dia / 2.0 + cm.via_inflate)
        routed += 1

    return {"net": net_name, "ok": routed == len(gaps) and len(gaps) > 0,
            "gaps": len(gaps), "routed": routed, "segments": segments,
            "vias": vias, "net_code": net_code, "width": width,
            "path_cells": path_cells_by_layer, "costmap": cm}


def write_result(board, net_code, result):
    n = 0
    for (x1, y1, x2, y2, w, layer_id) in result["segments"]:
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(int(round(x1 * _NM)), int(round(y1 * _NM))))
        t.SetEnd(pcbnew.VECTOR2I(int(round(x2 * _NM)), int(round(y2 * _NM))))
        t.SetWidth(int(round(w * _NM)))
        t.SetLayer(layer_id)
        t.SetNetCode(net_code)
        board.Add(t)
        n += 1
    for (x, y, dia, drill) in result.get("vias", []):
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(pcbnew.VECTOR2I(int(round(x * _NM)), int(round(y * _NM))))
        try:
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        except Exception:
            pass
        v.SetWidth(int(round(dia * _NM)))
        v.SetDrill(int(round(drill * _NM)))
        v.SetNetCode(net_code)
        board.Add(v)
        n += 1
    return n


def refill_zones(board):
    try:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception:
        pass


def route_batch(board, net_names, unconn, params, on_progress=None):
    """Route a batch. Nets already routed this batch are (a) obstacles for the
    rest (no crossing/shorting) and (b) attractors (bus bundling). Longest net
    first anchors the bus. Does NOT mutate the board."""
    def gap_len(name):
        return sum(math.hypot(g[2] - g[0], g[3] - g[1])
                   for g in unconn.get(name, []))
    ordered = sorted(net_names, key=gap_len, reverse=True)

    results = []
    prior_segments, prior_vias, attract = [], [], {}
    for name in ordered:
        gaps = unconn.get(name, [])
        if not gaps:
            results.append({"net": name, "ok": True, "reason": "already routed",
                            "gaps": 0, "routed": 0, "segments": [], "vias": []})
            continue
        r = route_net(board, name, gaps, params, prior_segments=prior_segments,
                      prior_vias=prior_vias, attract_paths=attract,
                      on_progress=on_progress)
        results.append(r)
        prior_segments += r.get("segments", [])
        prior_vias += r.get("vias", [])
        for li, cells in r.get("path_cells", {}).items():
            attract.setdefault(li, []).extend(cells)
    return results


def solve(board_path, net_names, out_path, params=None, prefer_name=None):
    import os
    board = pcbnew.LoadBoard(board_path)
    params = params or RouteParams(board)
    unconn = shim.drc_unconnected(board_path)

    results = route_batch(board, net_names, unconn, params)
    total = 0
    for r in results:
        if r.get("segments") or r.get("vias"):
            total += write_result(board, r["net_code"], r)
        for k in ("segments", "vias", "path_cells", "costmap"):
            r.pop(k, None)

    refill_zones(board)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    pcbnew.SaveBoard(out_path, board)
    return {"out": out_path, "tracks_added": total, "results": results}
