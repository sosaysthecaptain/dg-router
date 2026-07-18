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
                 layer_names=None, seed=0, jitter=0.0,
                 objective="least_obtrusive", prefer_layer=None):
        ds = board.GetDesignSettings()
        self.pitch = pitch_mm
        self.turn_cost = turn_cost
        self.via_cost = via_cost           # extra A* cost (grid steps) per via
        self.pad_via_penalty = 20.0        # extra cost for a via hugging a pad
        self.repel_cost = 2.5              # cost to cross another pin's fanout
        # routing objective (cost-term preset): direct | follow | hug |
        # least_obtrusive. least_obtrusive hugs edges/copper and avoids grabbing
        # open territory (keeps it from walling off chips).
        self.objective = objective
        self.prefer_layer = prefer_layer   # bias toward this layer if set
        self.prefer_layer_id = (board.GetLayerID(prefer_layer)
                                if prefer_layer else None)
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
        self.near_pad = bytearray(n)   # cells hugging a pad -> discourage vias here
        self.repel = bytearray(n)      # steer clear of others' unrouted fanout
        self.dist = [None for _ in self.layers]  # dist-to-nearest-obstacle (cells)
        # clearance + width/2 is the true minimum; add a full cell of
        # discretization safety so cell-center paths never violate clearance.
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

    def _rect(self, grid, cx, cy, hx, hy, ang, val=1):
        """Stamp a rotated rectangle (half-extents hx,hy already inflated).
        Uses the true pad rectangle instead of a diagonal disc — a diagonal disc
        balloons tall/narrow pads into big circles that seal escape corridors
        between fine-pitch pads."""
        p = self.pitch
        rmax = math.hypot(hx, hy)
        i0 = max(0, int((cx - rmax - self.x0) / p))
        i1 = min(self.nx - 1, int((cx + rmax - self.x0) / p))
        j0 = max(0, int((cy - rmax - self.y0) / p))
        j1 = min(self.ny - 1, int((cy + rmax - self.y0) / p))
        ca, sa = math.cos(-ang), math.sin(-ang)
        for j in range(j0, j1 + 1):
            wy = self.y0 + (j + 0.5) * p
            row = j * self.nx
            for i in range(i0, i1 + 1):
                wx = self.x0 + (i + 0.5) * p
                dx, dy = wx - cx, wy - cy
                if abs(dx * ca - dy * sa) <= hx and abs(dx * sa + dy * ca) <= hy:
                    grid[row + i] = val

    def _stamp_obstacles(self, board, net_code):
        tclr, vclr = self.track_inflate, self.via_inflate
        lset = set(self.layers)
        lidx = {L: k for k, L in enumerate(self.layers)}
        for pad in board.GetPads():
            if pad.GetNetCode() == net_code:
                continue
            pos = pad.GetPosition()
            sz = pad.GetSize()
            hx, hy = sz.x / 2.0 / _NM, sz.y / 2.0 / _NM
            ang = _safe(lambda: pad.GetOrientation().AsRadians(), 0.0)
            px, py = pos.x / _NM, pos.y / _NM
            self._rect(self.via_blocked, px, py, hx + vclr, hy + vclr, ang)
            self._disc(self.near_pad, px, py, math.hypot(hx, hy) + 1.0)
            for L in self.layers:
                if pad.IsOnLayer(L):
                    self._rect(self.blocked[lidx[L]], px, py,
                               hx + tclr, hy + tclr, ang)
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

    def compute_dist(self, li, cap=60):
        """Multi-source BFS: distance (in cells, capped) from each free cell to
        the nearest obstacle. Drives the hug/least-obtrusive/follow objectives."""
        if self.dist[li] is not None:
            return
        from collections import deque
        nx, ny = self.nx, self.ny
        blk = self.blocked[li]
        d = bytearray(b'\xff' * (nx * ny))
        dq = deque()
        for idx in range(nx * ny):
            if blk[idx]:
                d[idx] = 0
                dq.append(idx)
        while dq:
            idx = dq.popleft()
            cd = d[idx]
            if cd >= cap:
                continue
            i, j = idx % nx, idx // nx
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ni, nj = i + di, j + dj
                if 0 <= ni < nx and 0 <= nj < ny:
                    nidx = nj * nx + ni
                    if d[nidx] > cd + 1:
                        d[nidx] = cd + 1
                        dq.append(nidx)
        self.dist[li] = d

    def dist_at(self, li, i, j):
        d = self.dist[li]
        return 60 if d is None else min(60, d[j * self.nx + i])

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

    def add_repulsion_world(self, cx, cy, radius_mm):
        """Mark a disc as costly-to-cross (all layers). Used to steer clear of
        the fanout space of OTHER pins that still need routing, so we don't wall
        in work that's coming."""
        self._disc(self.repel, cx, cy, radius_mm)


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
            if cm.repel[cm.idx(ni, nj)]:        # keep clear of others' fanout
                cost += params.repel_cost
            obj = params.objective
            if obj == "least_obtrusive":       # avoid open space; hug stuff
                cost += 0.06 * cm.dist_at(cl, ni, nj)
            elif obj == "hug":                 # prefer near the board edge
                cost += 0.03 * min(ni, cm.nx - 1 - ni, nj, cm.ny - 1 - nj)
            elif obj == "follow":              # run alongside existing copper
                if cm.dist_at(cl, ni, nj) <= 3:
                    cost = max(0.1, cost - 0.5)
            if (params.prefer_layer_id is not None
                    and cm.layers[cl] != params.prefer_layer_id):
                cost += 0.35
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
            via_extra = (params.pad_via_penalty
                         if cm.near_pad[cm.idx(ci, cj)] else 0.0)
            for nl in range(nlayers):
                if nl == cl or cm.blocked_at(nl, ci, cj):
                    continue
                ng = gc + params.via_cost + via_extra
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


def _seg_clear_lg(lg, a, b):
    """Is the octilinear segment a->b free on the fine LocalGrid?"""
    x, y = a
    dx, dy = _sgn(b[0] - a[0]), _sgn(b[1] - a[1])
    n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
    for _ in range(n + 1):
        if not lg.in_bounds(x, y) or lg.is_blocked(x, y):
            return False
        if dx and dy and lg.is_blocked(x + dx, y) and lg.is_blocked(x, y + dy):
            return False
        x += dx
        y += dy
    return True


def _octi_pull_lg(lg, cells):
    """Octilinear string-pull on the fine LocalGrid — same greedy L-collapse as
    octi_pull, so the escape leaves the pad in clean 45deg segments instead of a
    grid staircase (kills the one-cell kink at the fine/coarse seam)."""
    if len(cells) <= 2:
        return [lg.to_world(*c) for c in cells]
    out = [cells[0]]
    i = 0
    while i < len(cells) - 1:
        best = i + 1
        for j in range(len(cells) - 1, i, -1):
            corner = _octi_corner(cells[i], cells[j])
            if _seg_clear_lg(lg, cells[i], corner) and \
               _seg_clear_lg(lg, corner, cells[j]):
                best = j
                break
        corner = _octi_corner(cells[i], cells[best])
        if corner != cells[i] and corner != cells[best]:
            out.append(corner)
        out.append(cells[best])
        i = best
    return [lg.to_world(*c) for c in out]


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


def _pad_at(board, net_code, x, y):
    """(px, py, sx, sy) mm of the net's pad nearest (x, y)."""
    best, bd = None, 1e18
    for pad in board.GetPads():
        if pad.GetNetCode() != net_code:
            continue
        p = pad.GetPosition()
        d = math.hypot(p.x / _NM - x, p.y / _NM - y)
        if d < bd:
            bd = d
            sz = pad.GetSize()
            best = (p.x / _NM, p.y / _NM, sz.x / _NM, sz.y / _NM)
    return best


def _seg_hits_copper(board, net_code, layer_id, x1, y1, x2, y2):
    """True if the segment crosses ANOTHER net's actual copper (a short) on
    this layer. Grazing a clearance zone is fine — only real copper counts, so
    a stub into a cramped pad connects (with a DRC clearance warning) but never
    shorts. Proximity-filtered for speed."""
    def d_seg(px, py):
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0,
                                             ((px - x1) * dx + (py - y1) * dy) / L2))
        return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    seg_len = math.hypot(x2 - x1, y2 - y1)
    reach = seg_len / 2 + 2.0
    # dense samples of the segment, to test against (rotated) pad rectangles
    ns = max(1, int(seg_len / 0.05))
    samples = [(x1 + (x2 - x1) * k / ns, y1 + (y2 - y1) * k / ns)
               for k in range(ns + 1)]
    for pad in board.GetPads():
        if pad.GetNetCode() == net_code or not pad.IsOnLayer(layer_id):
            continue
        p = pad.GetPosition()
        px, py = p.x / _NM, p.y / _NM
        if abs(px - mx) > reach or abs(py - my) > reach:
            continue
        sz = pad.GetSize()
        hx, hy = sz.x / 2.0 / _NM, sz.y / 2.0 / _NM
        try:
            ang = math.radians(pad.GetOrientationDegrees())
        except Exception:
            ang = 0.0
        ca, sa = math.cos(-ang), math.sin(-ang)
        for (qx, qy) in samples:            # any sample inside the pad rect = short
            dx, dy = qx - px, qy - py
            if abs(dx * ca - dy * sa) <= hx and abs(dx * sa + dy * ca) <= hy:
                return True
    for t in board.GetTracks():
        if t.GetNetCode() == net_code:
            continue
        if t.Type() == pcbnew.PCB_VIA_T:
            p = t.GetPosition()
            px, py = p.x / _NM, p.y / _NM
            if abs(px - mx) <= reach and abs(py - my) <= reach:
                if d_seg(px, py) < _via_width_mm(t, layer_id) / 2.0:
                    return True
        elif t.IsOnLayer(layer_id):
            s, e = t.GetStart(), t.GetEnd()
            sx, sy, ex, ey = s.x / _NM, s.y / _NM, e.x / _NM, e.y / _NM
            if min(sx, ex) - reach > mx or max(sx, ex) + reach < mx:
                continue
            tw = t.GetWidth() / 2.0 / _NM
            n = max(1, int(math.hypot(ex - sx, ey - sy) / 0.2))
            for k in range(n + 1):
                tt = k / n
                if d_seg(sx + (ex - sx) * tt, sy + (ey - sy) * tt) < tw:
                    return True
    return False


def _stub_ok(board, net_code, layer_id, pts):
    for k in range(len(pts) - 1):
        if _seg_hits_copper(board, net_code, layer_id,
                            pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1]):
            return False
    return True


# --- local fine-grid pad fanout -------------------------------------------
# Coarse A* can't thread into fine-pitch pads (the channel is < one coarse
# cell). So around each pad we build a small FINE grid, escape out of the pad
# to a point that is free on the COARSE grid, and hand off to the coarse router.
# The whole route stays clearance-clean (no grazing stub).

class LocalGrid:
    def __init__(self, board, layer_id, net_code, cx, cy, half, pitch, inflate,
                 prior_segments=None, prior_vias=None):
        self.pitch = pitch
        self.x0 = cx - half
        self.y0 = cy - half
        self.nx = max(1, int(2 * half / pitch) + 1)
        self.ny = self.nx
        self.blocked = bytearray(self.nx * self.ny)
        self._stamp(board, layer_id, net_code, inflate)
        self._stamp_prior(layer_id, inflate, prior_segments, prior_vias)
        # Our own pads are copper we're allowed to land on — a track there is not
        # a short. Clear them LAST so a neighbor's clearance inflation (which
        # bleeds across fine pad pitches) never blocks our own escape start.
        self._clear_own_pads(board, layer_id, net_code)

    def in_bounds(self, i, j):
        return 0 <= i < self.nx and 0 <= j < self.ny

    def is_blocked(self, i, j):
        return self.blocked[j * self.nx + i]

    def to_cell(self, x, y):
        return (int((x - self.x0) / self.pitch), int((y - self.y0) / self.pitch))

    def to_world(self, i, j):
        return (self.x0 + (i + 0.5) * self.pitch, self.y0 + (j + 0.5) * self.pitch)

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

    def _seg(self, x1, y1, x2, y2, r):
        length = math.hypot(x2 - x1, y2 - y1)
        n = max(1, int(length / (self.pitch * 0.5)))
        for s in range(n + 1):
            t = s / n
            self._disc(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, r)

    def _stamp(self, board, layer_id, net_code, inflate):
        wx0, wy0 = self.x0, self.y0
        wx1 = self.x0 + self.nx * self.pitch
        wy1 = self.y0 + self.ny * self.pitch
        m = inflate + 1.0
        for pad in board.GetPads():
            if pad.GetNetCode() == net_code or not pad.IsOnLayer(layer_id):
                continue
            p = pad.GetPosition()
            px, py = p.x / _NM, p.y / _NM
            if px < wx0 - m or px > wx1 + m or py < wy0 - m or py > wy1 + m:
                continue
            sz = pad.GetSize()
            ang = _safe(lambda: pad.GetOrientation().AsRadians(), 0.0)
            self._rect(px, py, sz.x / 2.0 / _NM + inflate,
                       sz.y / 2.0 / _NM + inflate, ang, 1)
        for t in board.GetTracks():
            if t.GetNetCode() == net_code:
                continue
            if t.Type() == pcbnew.PCB_VIA_T:
                p = t.GetPosition()
                px, py = p.x / _NM, p.y / _NM
                if wx0 - m < px < wx1 + m and wy0 - m < py < wy1 + m:
                    self._disc(px, py, _via_width_mm(t, layer_id) / 2.0 + inflate)
            elif t.IsOnLayer(layer_id):
                s, e = t.GetStart(), t.GetEnd()
                sx, sy, ex, ey = s.x / _NM, s.y / _NM, e.x / _NM, e.y / _NM
                if (max(sx, ex) < wx0 - m or min(sx, ex) > wx1 + m
                        or max(sy, ey) < wy0 - m or min(sy, ey) > wy1 + m):
                    continue
                self._seg(sx, sy, ex, ey, t.GetWidth() / 2.0 / _NM + inflate)

    def _rect(self, cx, cy, hx, hy, ang_rad, val):
        """Set cells whose center is inside a (possibly rotated) rectangle to
        `val` (1=block, 0=clear). Half-extents are pre-inflated by the caller."""
        p = self.pitch
        rmax = math.hypot(hx, hy)
        i0 = max(0, int((cx - rmax - self.x0) / p))
        i1 = min(self.nx - 1, int((cx + rmax - self.x0) / p))
        j0 = max(0, int((cy - rmax - self.y0) / p))
        j1 = min(self.ny - 1, int((cy + rmax - self.y0) / p))
        ca, sa = math.cos(-ang_rad), math.sin(-ang_rad)
        for j in range(j0, j1 + 1):
            wy = self.y0 + (j + 0.5) * p
            row = j * self.nx
            for i in range(i0, i1 + 1):
                wx = self.x0 + (i + 0.5) * p
                dx, dy = wx - cx, wy - cy
                if abs(dx * ca - dy * sa) <= hx and abs(dx * sa + dy * ca) <= hy:
                    self.blocked[row + i] = val

    def _clear_own_pads(self, board, layer_id, net_code):
        wx0, wy0 = self.x0, self.y0
        wx1 = self.x0 + self.nx * self.pitch
        wy1 = self.y0 + self.ny * self.pitch
        for pad in board.GetPads():
            if pad.GetNetCode() != net_code or not pad.IsOnLayer(layer_id):
                continue
            p = pad.GetPosition()
            px, py = p.x / _NM, p.y / _NM
            if px < wx0 - 1 or px > wx1 + 1 or py < wy0 - 1 or py > wy1 + 1:
                continue
            sz = pad.GetSize()
            ang = _safe(lambda: pad.GetOrientation().AsRadians(), 0.0)
            self._rect(px, py, sz.x / 2.0 / _NM, sz.y / 2.0 / _NM, ang, 0)

    def _stamp_prior(self, layer_id, inflate, prior_segments, prior_vias):
        """Stamp sibling nets' PROPOSED (preview-only) copper. These are never
        on the board, so _stamp can't see them — but a fanout escape must still
        avoid them or same-batch nets short."""
        wx0, wy0 = self.x0, self.y0
        wx1 = self.x0 + self.nx * self.pitch
        wy1 = self.y0 + self.ny * self.pitch
        m = inflate + 1.0
        for (sx, sy, ex, ey, w, seg_layer) in (prior_segments or []):
            if seg_layer != layer_id:
                continue
            if (max(sx, ex) < wx0 - m or min(sx, ex) > wx1 + m
                    or max(sy, ey) < wy0 - m or min(sy, ey) > wy1 + m):
                continue
            self._seg(sx, sy, ex, ey, w / 2.0 + inflate)
        for (vx, vy, dia, _drill) in (prior_vias or []):     # through-vias: all layers
            if wx0 - m < vx < wx1 + m and wy0 - m < vy < wy1 + m:
                self._disc(vx, vy, dia / 2.0 + inflate)


def _lg_nearest_free(lg, cell, rings):
    ci, cj = cell
    if lg.in_bounds(ci, cj) and not lg.is_blocked(ci, cj):
        return cell
    for r in range(1, rings + 1):
        for di in range(-r, r + 1):
            for dj in (-r, r):
                i, j = ci + di, cj + dj
                if lg.in_bounds(i, j) and not lg.is_blocked(i, j):
                    return (i, j)
        for dj in range(-r + 1, r):
            for di in (-r, r):
                i, j = ci + di, cj + dj
                if lg.in_bounds(i, j) and not lg.is_blocked(i, j):
                    return (i, j)
    return None


def _local_astar(lg, start, is_goal, max_expansions=250_000):
    if lg.is_blocked(*start):
        return None
    open_heap = [(0.0, start)]
    g = {start: 0.0}
    came = {}
    seen = 0
    while open_heap:
        gc, cur = heapq.heappop(open_heap)
        if is_goal(*cur):
            return _reconstruct2(came, cur)
        if gc > g.get(cur, 1e18):
            continue
        seen += 1
        if seen > max_expansions:
            return None
        ci, cj = cur
        for di, dj in _DIRS:
            ni, nj = ci + di, cj + dj
            if not lg.in_bounds(ni, nj) or lg.is_blocked(ni, nj):
                continue
            if di and dj and (lg.is_blocked(ci + di, cj) and lg.is_blocked(ci, cj + dj)):
                continue
            ng = gc + (math.sqrt(2) if di and dj else 1.0)
            nxt = (ni, nj)
            if ng < g.get(nxt, 1e18):
                g[nxt] = ng
                came[nxt] = cur
                heapq.heappush(open_heap, (ng, nxt))
    return None


def _reconstruct2(came, cur):
    out = [cur]
    while cur in came:
        cur = came[cur]
        out.append(cur)
    out.reverse()
    return out


def _merge_collinear(cells):
    if len(cells) <= 2:
        return list(cells)
    keep = [cells[0]]
    prev = (_sgn(cells[1][0] - cells[0][0]), _sgn(cells[1][1] - cells[0][1]))
    for k in range(1, len(cells) - 1):
        d = (_sgn(cells[k + 1][0] - cells[k][0]),
             _sgn(cells[k + 1][1] - cells[k][1]))
        if d != prev:
            keep.append(cells[k])
            prev = d
    keep.append(cells[-1])
    return keep


def escape_pad(board, net_code, layer_id, li, px, py, pad_max, coarse_cm,
               fine_pitch, inflate, prior_segments=None, prior_vias=None):
    """Route out of a pad on a fine local grid to a point that's free on the
    coarse grid. Returns (fine_world_path [pad..escape], escape_coarse_cell) or
    None if the pad can't be escaped."""
    half = pad_max / 2.0 + 3.0
    lg = LocalGrid(board, layer_id, net_code, px, py, half, fine_pitch, inflate,
                   prior_segments=prior_segments, prior_vias=prior_vias)
    start = lg.to_cell(px, py)
    if lg.is_blocked(*start):
        start = _lg_nearest_free(lg, start, int(pad_max / fine_pitch) + 2)
        if start is None:
            return None
    esc2 = (pad_max / 2.0 + 0.35) ** 2

    def is_goal(i, j):
        wx, wy = lg.to_world(i, j)
        if (wx - px) ** 2 + (wy - py) ** 2 < esc2:
            return False
        ci, cj = coarse_cm.to_cell(wx, wy)
        return coarse_cm.in_bounds(ci, cj) and not coarse_cm.blocked_at(li, ci, cj)

    cells = _local_astar(lg, start, is_goal)
    if not cells:
        return None
    pts = _octi_pull_lg(lg, cells)      # clean octilinear escape, no staircase
    pts[0] = (px, py)
    ecx, ecy = lg.to_world(*cells[-1])
    return pts, coarse_cm.to_cell(ecx, ecy)


def route_net(board, net_name, gaps, params, prior_segments=None,
              prior_vias=None, attract_paths=None, on_progress=None,
              avoid_points=None):
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
    for (ax, ay) in (avoid_points or []):   # others' unrouted fanout — steer clear
        cm.add_repulsion_world(ax, ay, 0.6)
    if params.objective in ("least_obtrusive", "follow"):
        for li in range(len(routable)):
            cm.compute_dist(li)
    lidx = {L: k for k, L in enumerate(routable)}

    fine_inflate = clearance + width / 2.0 + 0.04
    segments, vias, path_cells_by_layer = [], [], {}
    routed = 0
    for (x1, y1, x2, y2) in gaps:
        spad = _pad_at(board, net_code, x1, y1)
        epad = _pad_at(board, net_code, x2, y2)
        if not spad or not epad:
            continue
        smax, emax = max(spad[2], spad[3]), max(epad[2], epad[3])
        neck_s = max(params.min_track, min(spad[2], spad[3]))
        neck_e = max(params.min_track, min(epad[2], epad[3]))
        sl = [lidx[L] for L in _pad_layers_at(board, net_code, x1, y1, routable)]
        gl = [lidx[L] for L in _pad_layers_at(board, net_code, x2, y2, routable)]

        gap_ok = False
        for sli in sl:
            es = escape_pad(board, net_code, routable[sli], sli, x1, y1, smax,
                            cm, 0.05, fine_inflate,
                            prior_segments=prior_segments, prior_vias=prior_vias)
            if not es:
                continue
            es_pts, es_ec = es
            for gli in gl:
                eg = escape_pad(board, net_code, routable[gli], gli, x2, y2, emax,
                                cm, 0.05, fine_inflate,
                                prior_segments=prior_segments, prior_vias=prior_vias)
                if not eg:
                    continue
                eg_pts, eg_ec = eg
                cells3 = astar(cm, [(es_ec[0], es_ec[1], sli)], eg_ec, [gli],
                               params, on_progress=on_progress)
                if not cells3:
                    continue
                # stitch: start fanout + coarse path + end fanout (reversed)
                runs, via_cells = split_layer_runs(cells3)
                gap_segs, gap_cells = [], {}
                for ri, (li, run) in enumerate(runs):
                    cpoly = octi_pull(cm, li, run)
                    if ri == 0:
                        cpoly = es_pts[:-1] + cpoly
                    if ri == len(runs) - 1:
                        cpoly = cpoly + eg_pts[::-1][1:]
                    ns = neck_s if ri == 0 else width
                    ne = neck_e if ri == len(runs) - 1 else width
                    gap_segs += build_segments(cpoly, routable[li], width, ns, ne)
                    gap_cells.setdefault(li, []).extend(run)
                segments += gap_segs
                for li, cells in gap_cells.items():
                    path_cells_by_layer.setdefault(li, []).extend(cells)
                for (vi, vj) in via_cells:
                    wx_, wy_ = cm.to_world(vi, vj)
                    vias.append((wx_, wy_, via_dia, via_drill))
                    for li in range(len(cm.layers)):
                        cm._disc(cm.blocked[li], wx_, wy_,
                                 via_dia / 2.0 + cm.track_inflate)
                    cm._disc(cm.via_blocked, wx_, wy_, via_dia / 2.0 + cm.via_inflate)
                routed += 1
                gap_ok = True
                break
            if gap_ok:
                break

    return {"net": net_name, "ok": routed == len(gaps) and len(gaps) > 0,
            "gaps": len(gaps), "routed": routed, "segments": segments,
            "vias": vias, "net_code": net_code, "width": width,
            "path_cells": path_cells_by_layer, "costmap": cm}


def write_result(board, net_code, result):
    """Add tracks + vias to the board. Returns the list of added board items
    (so a caller can Remove() them to revert)."""
    items = []
    for (x1, y1, x2, y2, w, layer_id) in result["segments"]:
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(int(round(x1 * _NM)), int(round(y1 * _NM))))
        t.SetEnd(pcbnew.VECTOR2I(int(round(x2 * _NM)), int(round(y2 * _NM))))
        t.SetWidth(int(round(w * _NM)))
        t.SetLayer(layer_id)
        t.SetNetCode(net_code)
        board.Add(t)
        items.append(t)
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
        items.append(v)
    return items


def refill_zones(board):
    try:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception:
        pass


def route_batch(board, net_names, unconn, params, on_progress=None, on_net=None):
    """Route a batch. Nets already routed this batch are (a) obstacles for the
    rest (no crossing/shorting) and (b) attractors (bus bundling). Longest net
    first anchors the bus. Does NOT mutate the board. on_net(done, total, result)
    fires after each net (for progress + progressive reveal)."""
    def gap_len(name):
        return sum(math.hypot(g[2] - g[0], g[3] - g[1])
                   for g in unconn.get(name, []))
    ordered = sorted(net_names, key=gap_len, reverse=True)
    total = len(ordered)

    # Every unrouted pin is a fanout that someone will need — repel other nets
    # away from each one so we don't wall in work that's coming.
    endpoints = {}   # net name -> [(x,y), ...] of its unrouted pins
    for oname, gs in unconn.items():
        pts = []
        for g in gs:
            pts.append((g[0], g[1]))
            pts.append((g[2], g[3]))
        endpoints[oname] = pts

    results = []
    prior_segments, prior_vias, attract = [], [], {}
    routed_names = set()
    for name in ordered:
        gaps = unconn.get(name, [])
        if not gaps:
            r = {"net": name, "ok": True, "reason": "already routed",
                 "gaps": 0, "routed": 0, "segments": [], "vias": []}
        else:
            avoid = [pt for onm, pts in endpoints.items()
                     if onm != name and onm not in routed_names for pt in pts]
            r = route_net(board, name, gaps, params,
                          prior_segments=prior_segments, prior_vias=prior_vias,
                          attract_paths=attract, on_progress=on_progress,
                          avoid_points=avoid)
            routed_names.add(name)
            prior_segments += r.get("segments", [])
            prior_vias += r.get("vias", [])
            for li, cells in r.get("path_cells", {}).items():
                attract.setdefault(li, []).extend(cells)
        results.append(r)
        if on_net:
            on_net(len(results), total, r)
    return results


def solve(board_path, net_names, out_path, params=None, prefer_name=None,
          unconn=None):
    """Route net_names and write a copy. If `unconn` is given (a
    {net: [gaps]} subset — e.g. only the connections the user clicked), route
    exactly those instead of the full ratsnest; net_names may be None to mean
    'every net in unconn'."""
    import os
    board = pcbnew.LoadBoard(board_path)
    params = params or RouteParams(board)
    if unconn is None:
        unconn = shim.drc_unconnected(board_path)
    if net_names is None:
        net_names = list(unconn.keys())

    results = route_batch(board, net_names, unconn, params)
    total = 0
    for r in results:
        if r.get("segments") or r.get("vias"):
            total += len(write_result(board, r["net_code"], r))
        for k in ("segments", "vias", "path_cells", "costmap"):
            r.pop(k, None)

    refill_zones(board)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    pcbnew.SaveBoard(out_path, board)
    return {"out": out_path, "tracks_added": total, "results": results}
