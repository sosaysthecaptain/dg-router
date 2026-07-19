"""Component classification + placement for dg-router (Phase 2).

The placement tool cascades top-down: the user places ANCHORS (connectors, major
chips); the plugin places SUBSYSTEM ANCHORS (the IC at the heart of each
subsystem) which the user tweaks; then the plugin places SATELLITES (the passives
that serve a parent) opportunistically for short, clean connectivity.

Every component gets {type, parents}. This module infers a strong first pass from
the netlist + footprints; the result is stored in a sidecar JSON next to the
board (never touching the .kicad_pcb) and is editable by hand, by an agent, or in
the GUI. `pcbnew` is imported lazily so this stays importable headless.
"""

import os
import json

_NM = 1e6

# ref-prefix -> coarse role hint
_ANCHOR_PREFIX = {"J", "P", "CN", "X", "SW", "TP", "H", "MH", "BT", "K", "M"}
_IC_PREFIX = {"U", "IC", "A"}
_ACTIVE_PREFIX = {"Q", "D"}          # transistors / diodes
_ANCHOR_MIN_IC_PADS = 40             # only MCU-scale ICs anchor; regulators/
                                     # drivers/expanders are subsystem anchors


def _prefix(ref):
    return "".join(c for c in ref if c.isalpha()).upper()


def _infer_type(prefix, npads):
    if prefix in _ANCHOR_PREFIX:
        return "anchor"
    if prefix in _IC_PREFIX:
        return "anchor" if npads >= _ANCHOR_MIN_IC_PADS else "subsystem_anchor"
    if prefix in _ACTIVE_PREFIX:
        return "subsystem_anchor" if npads >= 3 else "satellite"
    return "satellite"


def _component_nets(board):
    """{ref: set(netcode)}, {netcode: fanout}, {ref: footprint}."""
    comp_nets, net_size, fps = {}, {}, {}
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if not ref:
            continue
        fps[ref] = fp
        nets = set()
        for pad in fp.Pads():
            nc = pad.GetNetCode()
            if nc > 0:
                nets.add(nc)
                net_size[nc] = net_size.get(nc, 0) + 1
        comp_nets[ref] = nets
    return comp_nets, net_size, fps


def classify(board):
    """Infer {ref: {type, parents, value, pads}} from netlist + footprints.

    Parent = the anchor / subsystem-anchor a component shares the most nets with,
    weighting each shared net by 1/fanout so a 2-pad VCC net (very telling)
    dominates a 100-pad GND net (tells you nothing). Satellites parent onto
    subsystem-anchors or anchors; subsystem-anchors parent onto anchors.
    """
    import pcbnew
    comp_nets, net_size, fps = _component_nets(board)
    code_name = {}
    for code in range(1, board.GetNetCount()):
        nn = board.FindNet(code)
        if nn is not None:
            code_name[code] = nn.GetNetname()
    types = {ref: _infer_type(_prefix(ref), fp.GetPadCount())
             for ref, fp in fps.items()}
    anchors = {r for r, t in types.items() if t == "anchor"}
    subs = {r for r, t in types.items() if t == "subsystem_anchor"}

    out = {}
    for ref, fp in fps.items():
        t = types[ref]
        if t == "subsystem_anchor":
            cands = anchors
        elif t == "satellite":
            cands = anchors | subs
        else:
            cands = set()
        scores = {}
        for c in cands:
            if c == ref:
                continue
            shared = comp_nets[ref] & comp_nets[c]
            s = sum(1.0 / net_size[nc] for nc in shared if net_size[nc] > 1)
            if s > 0:
                scores[c] = s
        parents = []
        if scores:
            ranked = [c for c, _ in sorted(scores.items(), key=lambda x: -x[1])]
            # a satellite belongs to ONE subsystem; a subsystem anchor may relate
            # to a couple of anchors
            parents = ranked[:1] if t == "satellite" else \
                [c for c in ranked if scores[c] >= max(scores.values()) * 0.5][:2]
        out[ref] = {"type": t, "parents": parents,
                    "value": fp.GetValue(), "pads": fp.GetPadCount()}

    # --- refine satellite parents + human names ----------------------------
    # A rail cap (only power rail + GND) belongs to the regulator that SOURCES
    # that rail — matched by name (+5VP -> "5V-P"). Otherwise a satellite is
    # named "<subsystem> <signal>" from the most-telling net it shares.
    def _clean(s):
        return "".join(c for c in (s or "").upper() if c.isalnum())

    def _is_gnd(n):
        return "GND" in (n or "").upper() or (n or "").upper() in ("VSS",)

    def _is_rail(n):
        return bool(n) and (n.lstrip("/").startswith("+")
                            or _clean(n) in ("3V3", "5V", "12V", "36V", "6V"))

    sub_by_name = {}
    for r in subs:
        sub_by_name.setdefault(_clean(fps[r].GetValue()), r)

    for ref, info in out.items():
        if info["type"] != "satellite":
            out[ref]["name"] = info["value"]
            continue
        nets = [code_name.get(nc, "") for nc in comp_nets[ref]]
        non_gnd = sorted([n for n in nets if n and not _is_gnd(n)],
                         key=lambda n: net_size.get(
                             next((c for c in comp_nets[ref]
                                   if code_name.get(c) == n), 0), 1e9))
        # rail cap -> its regulator, if the rail name matches a subsystem
        rail = next((n for n in non_gnd if _is_rail(n)
                     and _clean(n) in sub_by_name
                     and sub_by_name[_clean(n)] != ref), None)
        if rail:
            info["parents"] = [sub_by_name[_clean(rail)]]

        par = info["parents"][0] if info["parents"] else None
        pname = out.get(par, {}).get("value") if par else None
        big = any(u in info["value"].lower() for u in ("uf",)) and \
            not info["value"].lower().startswith(("0.", "1uf", "2.2uf", "4.7uf"))
        role = "bulk" if (ref[:1].upper() == "C" and big) else \
               "decoupling" if ref[:1].upper() == "C" else info["value"]
        # signal net = a chip-specific low-fanout net if any, else the rail
        sig = next((n for n in non_gnd
                    if "_" in n and not _is_rail(n)), None)
        if sig and pname:
            out[ref]["name"] = "%s %s" % (pname, sig.lstrip("/").split("_")[-1])
        elif rail:
            out[ref]["name"] = "%s %s" % (rail.lstrip("/"), role)
        elif non_gnd:
            out[ref]["name"] = "%s %s" % (non_gnd[0].lstrip("/"), role)
        elif pname:
            out[ref]["name"] = "%s %s" % (pname, info["value"])
        else:
            out[ref]["name"] = info["value"]
    return out


def satellites_of(table, subsystem_ref):
    """Refs of the satellites parented onto a subsystem anchor."""
    return sorted(r for r, i in table.items()
                  if i["type"] == "satellite"
                  and subsystem_ref in i.get("parents", []))


def _fp_size(fp):
    """(w, h) in mm of a footprint — its COURTYARD (the real keep-out), which is
    what collision must use. Pad extent badly underestimates connectors/ICs
    (their body is much bigger than their pads); the plain bounding box
    overestimates (includes ref text). Fall back to pad extent if no courtyard."""
    import pcbnew
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        try:
            cy = fp.GetCourtyard(layer)
            if cy and not cy.IsEmpty():
                bb = cy.BBox()
                w, h = bb.GetWidth() / _NM, bb.GetHeight() / _NM
                if w > 0.1 and h > 0.1:
                    return w + 0.3, h + 0.3
        except Exception:
            pass
    xs, ys = [], []
    for pad in fp.Pads():
        p = pad.GetPosition()
        sz = pad.GetSize()
        r = max(sz.x, sz.y) / 2.0
        xs += [p.x - r, p.x + r]
        ys += [p.y - r, p.y + r]
    if not xs:
        return 2.0, 2.0
    return (max(xs) - min(xs)) / _NM + 0.4, (max(ys) - min(ys)) / _NM + 0.4


def _overlap(ax, ay, aw, ah, bx, by, bw, bh, gap=0.4):
    return (abs(ax - bx) < (aw + bw) / 2.0 + gap and
            abs(ay - by) < (ah + bh) / 2.0 + gap)


def _board_region(board):
    bb = board.GetBoardEdgesBoundingBox()
    return (bb.GetX() / _NM, bb.GetY() / _NM,
            bb.GetRight() / _NM, bb.GetBottom() / _NM)


def is_unplaced(fp, region):
    """A part parked outside the board outline counts as unplaced."""
    p = fp.GetPosition()
    return not (region[0] <= p.x / _NM <= region[2] and
                region[1] <= p.y / _NM <= region[3])


def place_anchors(board, table, reposition=None):
    """Propose positions for unplaced anchors: connectors snap to the board edge
    nearest what they connect to; other anchors go central. Simple by design —
    anchors are usually hand-placed; this is just a starting point to nudge.
    """
    reposition = set(reposition or [])
    region = _board_region(board)
    rx0, ry0, rx1, ry1 = region
    fps = {fp.GetReference(): fp for fp in board.GetFootprints()
           if fp.GetReference()}
    cn, _, _ = _component_nets(board)

    placed = []
    for ref, info in table.items():
        fp = fps.get(ref)
        if fp is None or is_unplaced(fp, region):
            continue
        w, h = _fp_size(fp)
        p = fp.GetPosition()
        placed.append([p.x / _NM, p.y / _NM, w, h])

    def is_connector(ref):
        return "".join(c for c in ref if c.isalpha()).upper() in \
            ("J", "P", "CN", "X")

    anchors = [r for r, i in table.items() if i["type"] == "anchor"
               and r in fps and (is_unplaced(fps[r], region) or r in reposition)]
    proposed = {}
    for ref in anchors:
        w, h = _fp_size(fps[ref])
        # centroid of the placed parts this anchor connects to
        pts = []
        for other, ofp in fps.items():
            if other != ref and not is_unplaced(ofp, region) and \
                    (cn.get(ref, set()) & cn.get(other, set())):
                p = ofp.GetPosition()
                pts.append((p.x / _NM, p.y / _NM))
        cx = sum(p[0] for p in pts) / len(pts) if pts else (rx0 + rx1) / 2.0
        cy = sum(p[1] for p in pts) / len(pts) if pts else (ry0 + ry1) / 2.0
        if is_connector(ref):
            # snap to the nearest edge
            d = {"L": cx - rx0, "R": rx1 - cx, "T": cy - ry0, "B": ry1 - cy}
            side = min(d, key=d.get)
            if side == "L":
                tx, ty = rx0 + w / 2.0 + 1.0, cy
            elif side == "R":
                tx, ty = rx1 - w / 2.0 - 1.0, cy
            elif side == "T":
                tx, ty = cx, ry0 + h / 2.0 + 1.0
            else:
                tx, ty = cx, ry1 - h / 2.0 - 1.0
        else:
            tx, ty = cx, cy
        pos = _spiral_free(tx, ty, w, h, placed, region, 1.0)
        if pos is None:                 # no room — skip rather than overlap
            continue
        proposed[ref] = pos
        placed.append([pos[0], pos[1], w, h])
    return proposed


def place_subsystems(board, table, reposition=None):
    """Propose positions for subsystem anchors: each near the anchors it serves,
    reserving room for its satellites, non-overlapping, snapped to grid.

    Only places parts currently OUTSIDE the board (unplaced), unless their ref is
    in `reposition` (opt-in to move already-placed parts). Returns {ref: (x,y)}
    in mm. Does not mutate the board.
    """
    reposition = set(reposition or [])
    region = _board_region(board)
    rx0, ry0, rx1, ry1 = region
    m = 2.0
    fps = {fp.GetReference(): fp for fp in board.GetFootprints()
           if fp.GetReference()}

    # satellite room per subsystem: rough cluster area of its satellites
    sat_area = {}
    for ref, info in table.items():
        if info["type"] != "satellite":
            continue
        for par in info["parents"][:1]:
            if par in fps:
                w, h = _fp_size(fps[par])
                sat_area[par] = sat_area.get(par, 0.0) + w * h

    # obstacles: every placed part (anchors + already-placed subsystems) as boxes
    placed = []
    subsystems = []
    for ref, info in table.items():
        fp = fps.get(ref)
        if fp is None:
            continue
        w, h = _fp_size(fp)
        p = fp.GetPosition()
        pos = (p.x / _NM, p.y / _NM)
        if info["type"] == "subsystem_anchor" and (
                is_unplaced(fp, region) or ref in reposition):
            subsystems.append((ref, info, w, h))
        else:
            if not is_unplaced(fp, region):
                placed.append([pos[0], pos[1], w, h])

    cn = _component_nets(board)[0]

    def anchor_pull(ref):
        pts = []
        for par in table[ref]["parents"]:
            if par in fps and not is_unplaced(fps[par], region):
                p = fps[par].GetPosition()
                shared_n = len(cn.get(ref, set()) & cn.get(par, set())) or 1
                pts.append((p.x / _NM, p.y / _NM, shared_n))
        return pts

    # most-connected-to-anchors first (they pin the layout)
    subsystems.sort(key=lambda s: -len(anchor_pull(s[0])))

    proposed = {}
    for ref, info, w, h in subsystems:
        pulls = anchor_pull(ref)
        if pulls:
            tw = sum(p[2] for p in pulls)
            tx = sum(p[0] * p[2] for p in pulls) / tw
            ty = sum(p[1] * p[2] for p in pulls) / tw
        else:
            tx, ty = (rx0 + rx1) / 2.0, (ry0 + ry1) / 2.0
        # reserve satellite room by inflating this subsystem's box
        extra = (sat_area.get(ref, 0.0) * 1.4) ** 0.5
        rw, rh = w + extra, h + extra
        pos = _spiral_free(tx, ty, rw, rh, placed, region, m)
        if pos is None:                 # reserved box won't fit — try bare chip
            pos = _spiral_free(tx, ty, w, h, placed, region, m)
            rw, rh = w, h
        if pos is None:                 # truly no room — skip, don't overlap
            continue
        proposed[ref] = pos
        placed.append([pos[0], pos[1], rw, rh])
    return proposed


def _satellite_target(board, ref, info, fps, cn, net_size, region):
    """Where a satellite wants to sit: on top of the parent PAD it serves (the
    lowest-fanout shared net = the specific signal pin), so the trace is tiny."""
    par = None
    for p in info["parents"]:
        if p in fps and not is_unplaced(fps[p], region):
            par = p
            break
    if par is None:
        return (None, None)
    shared = cn.get(ref, set()) & cn.get(par, set())
    best_net, best_sz = None, 1e9
    for nc in shared:
        if net_size.get(nc, 1e9) < best_sz:
            best_sz, best_net = net_size[nc], nc
    parfp = fps[par]
    if best_net is not None:
        for pad in parfp.Pads():
            if pad.GetNetCode() == best_net:
                pp = pad.GetPosition()
                return (pp.x / _NM, pp.y / _NM)
    p = parfp.GetPosition()
    return (p.x / _NM, p.y / _NM)


def place_satellites(board, table, reposition=None):
    """Place each satellite at the nearest free spot to the PARENT pad it serves
    (kept local to its subsystem — never chases far-flung connected parts).
    Works one-at-a-time or in bulk. Placed satellites' current spots are freed so
    a re-place doesn't collide with itself."""
    reposition = set(reposition or [])
    region = _board_region(board)
    fps = {fp.GetReference(): fp for fp in board.GetFootprints()
           if fp.GetReference()}
    cn, net_size, _ = _component_nets(board)

    sats = [r for r, i in table.items() if i["type"] == "satellite"
            and r in fps and (is_unplaced(fps[r], region) or r in reposition)]
    moving = set(sats)
    placed = []                                     # obstacles: on-board, not moving
    for r, fp in fps.items():
        if r in moving or is_unplaced(fp, region):
            continue
        w, h = _fp_size(fp)
        p = fp.GetPosition()
        placed.append([p.x / _NM, p.y / _NM, w, h])

    proposed = {}
    for ref in sats:
        info = table[ref]
        w, h = _fp_size(fps[ref])
        tx, ty = _satellite_target(board, ref, info, fps, cn, net_size, region)
        if tx is None:
            continue
        pos = _spiral_free(tx, ty, w, h, placed, region, 0.3)
        if pos is None:                 # no room — skip rather than overlap
            continue
        proposed[ref] = pos
        placed.append([pos[0], pos[1], w, h])
    return proposed


def _spiral_free(tx, ty, w, h, placed, region, m, gap=0.6):
    """Nearest grid position to (tx,ty) whose box (+gap for fanout room) clears
    all placed boxes and stays in the board. Expanding-ring search."""
    rx0, ry0, rx1, ry1 = region
    step = 1.0

    def ok(x, y):
        if not (rx0 + w / 2 + m <= x <= rx1 - w / 2 - m and
                ry0 + h / 2 + m <= y <= ry1 - h / 2 - m):
            return False
        for (px, py, pw, ph) in placed:
            if _overlap(x, y, w, h, px, py, pw, ph, gap):
                return False
        return True

    def snap(v):
        return round(v * 2) / 2.0

    tx, ty = snap(tx), snap(ty)
    if ok(tx, ty):
        return (tx, ty)
    r = step
    while r < max(rx1 - rx0, ry1 - ry0):
        n = int(r / step)
        for k in range(-n, n + 1):
            for (x, y) in ((tx + k * step, ty - r), (tx + k * step, ty + r),
                           (tx - r, ty + k * step), (tx + r, ty + k * step)):
                if ok(snap(x), snap(y)):
                    return (snap(x), snap(y))
        r += step
    return None   # no free spot anywhere — REJECT (never stack on another part)


def sidecar_path(board_path):
    return os.path.splitext(os.path.abspath(board_path))[0] + ".dg-place.json"


def load_table(board_path):
    p = sidecar_path(board_path)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def save_table(board_path, table):
    p = sidecar_path(board_path)
    with open(p, "w") as f:
        json.dump(table, f, indent=2, sort_keys=True)
    return p


def effective_table(board, board_path):
    """Inferred classification with any sidecar overrides applied on top."""
    table = classify(board)
    saved = load_table(board_path).get("components", {})
    for ref, ov in saved.items():
        if ref in table:
            table[ref].update({k: v for k, v in ov.items()
                               if k in ("type", "parents", "name")})
    return table
