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
import math

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


def _connected_pins(fp):
    """How many of a footprint's pads are actually on a net (the pins that must
    fan out). A 2-pad cap has 2; a buck IC has ~15-20; a fine-pitch QFN many."""
    return sum(1 for pad in fp.Pads() if pad.GetNetCode() > 0)


def _fanout_halo(fp):
    """Clear ring (mm) a part needs around it for its pins to escape and route.
    The rule: MORE PINS, and MORE DENSELY packed -> MORE clearance. So it grows
    with raw connected-pin count (each pin is an escape) AND with pin density
    (pins per mm of courtyard perimeter — fine pitch needs deeper room to
    untangle). A 2-pin cap lands ~0.6mm; a buck ~1.4mm; a dense QFN ~3mm."""
    pins = _connected_pins(fp)
    if pins <= 0:
        return 0.3
    w, h = _fp_size(fp)
    perim = max(2.0 * (w + h), 1.0)
    halo = 0.25 + 0.05 * pins + 0.4 * (pins / perim)
    return round(min(halo, 3.0), 2)


def _fp_box(fp):
    """(cx, cy, w, h) mm — courtyard bbox CENTER + size. Center matters for ICs
    whose courtyard isn't centered on the footprint anchor."""
    import pcbnew
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        try:
            cy = fp.GetCourtyard(layer)
            if cy and not cy.IsEmpty():
                bb = cy.BBox()
                c = bb.GetCenter()
                return (c.x / _NM, c.y / _NM,
                        bb.GetWidth() / _NM + 0.3, bb.GetHeight() / _NM + 0.3)
        except Exception:
            pass
    p = fp.GetPosition()
    w, h = _fp_size(fp)
    return (p.x / _NM, p.y / _NM, w, h)


def _canonical_wh(fp):
    """(w, h) of a footprint at orientation 0 (pads horizontal), regardless of
    how it's currently rotated — so we can reason about it at any target angle."""
    w, h = _fp_size(fp)
    if int(round(fp.GetOrientationDegrees())) % 180 != 0:
        w, h = h, w
    return w, h


def _pad_on_net(fp, netcode):
    for pad in fp.Pads():
        if pad.GetNetCode() == netcode:
            return pad
    return None


def _rot(x, y, deg):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return (x * c - y * s, x * s + y * c)


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
        placed.append([p.x / _NM, p.y / _NM, w, h, _fanout_halo(fp)])

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
        pos = _spiral_free(tx, ty, w, h, placed, region, 1.0,
                           halo=_fanout_halo(fps[ref]))
        if pos is None:                 # no room — skip rather than overlap
            continue
        proposed[ref] = pos
        placed.append([pos[0], pos[1], w, h, _fanout_halo(fps[ref])])
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
                placed.append([pos[0], pos[1], w, h, _fanout_halo(fp)])

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
        hz = _fanout_halo(fps[ref])
        pos = _spiral_free(tx, ty, rw, rh, placed, region, m, halo=hz)
        if pos is None:                 # reserved box won't fit — try bare chip
            pos = _spiral_free(tx, ty, w, h, placed, region, m, halo=hz)
            rw, rh = w, h
        if pos is None:                 # truly no room — skip, don't overlap
            continue
        proposed[ref] = pos
        placed.append([pos[0], pos[1], rw, rh, hz])
    return proposed


def _sat_target(board, ref, info, fps, cn, net_size, region):
    """The parent PAD a satellite serves: the lowest-fanout shared net = the
    specific signal pin, so its trace is short. Returns (parent_ref, px, py,
    netcode) in mm, or None if the parent isn't placed."""
    par = None
    for p in info.get("parents", []):
        if p in fps and not is_unplaced(fps[p], region):
            par = p
            break
    if par is None:
        return None
    shared = cn.get(ref, set()) & cn.get(par, set())
    best_net, best_sz = None, 1e9
    for nc in shared:
        if net_size.get(nc, 1e9) < best_sz:
            best_sz, best_net = net_size[nc], nc
    parfp = fps[par]
    pad = _pad_on_net(parfp, best_net) if best_net is not None else None
    if pad is not None:
        pp = pad.GetPosition()
        return (par, pp.x / _NM, pp.y / _NM, best_net)
    p = parfp.GetPosition()
    return (par, p.x / _NM, p.y / _NM, best_net)


_SIDE_TOWARD = {"R": (-1.0, 0.0), "L": (1.0, 0.0), "T": (0.0, 1.0), "B": (0.0, -1.0)}


def _sat_orientation(fp, netcode, side):
    """A 90-degree orientation for a satellite so its connecting pad points TOWARD
    the chip (shortest trace) and its body lies flat along the rank. L/R -> pads
    horizontal (0deg), T/B -> pads vertical (90deg); +180 if that would aim the
    connecting pad away from the chip."""
    base = 0.0 if side in ("L", "R") else 90.0
    pad = _pad_on_net(fp, netcode) if netcode is not None else None
    if pad is not None:
        p0 = pad.GetFPRelativePosition()  # pad offset in the unrotated fp frame
        ox, oy = _rot(p0.x, p0.y, base)
        tx, ty = _SIDE_TOWARD[side]
        if ox * tx + oy * ty < 0:        # pad points away from chip -> flip
            base += 180.0
    return base % 360.0


KNOB_DEFAULTS = {"compactness": 0.5, "orderliness": 1.0, "min_distance": 0.7}


def _knobs(k):
    d = dict(KNOB_DEFAULTS)
    if k:
        d.update({kk: max(0.0, min(1.0, vv)) for kk, vv in k.items() if kk in d})
    return d


def _make_item(ref, fps, deg, want):
    w0, h0 = _canonical_wh(fps[ref])
    w, h = (w0, h0) if int(round(deg)) % 180 == 0 else (h0, w0)
    return {"ref": ref, "deg": deg, "w": w, "h": h, "want": want,
            "halo": _fanout_halo(fps[ref])}


def place_satellites(board, table, reposition=None, knobs=None):
    """Tidy per-side placement. Each subsystem's passives are grouped by the chip
    EDGE nearest the pin they serve, then laid in a straight, evenly-spaced,
    aligned RANK just off that edge — oriented (in 90deg steps) so the connecting
    pad faces the chip for the shortest trace. Ranks push outward to dodge other
    parts; anything with no room is skipped (never stacked).

    Returns {ref: (x, y, orient_deg)}. Bulk = full clean ranks; a subset lands
    each part on its rank line near its pin. Rough-place for short nets, THEN
    neaten — exactly the two-phase intent.
    """
    reposition = set(reposition or [])
    region = _board_region(board)
    fps = {fp.GetReference(): fp for fp in board.GetFootprints()
           if fp.GetReference()}
    cn, net_size, _ = _component_nets(board)

    sats = [r for r, i in table.items() if i["type"] == "satellite"
            and r in fps and (is_unplaced(fps[r], region) or r in reposition)]
    moving = set(sats)

    # obstacles = every on-board part we're NOT moving (courtyard box + halo)
    obstacles = []
    for r, fp in fps.items():
        if r in moving or is_unplaced(fp, region):
            continue
        cx, cy, w, h = _fp_box(fp)
        obstacles.append([cx, cy, w, h, _fanout_halo(fp)])

    # assign each mover to a parent + chip edge (side) based on the pin it serves
    by_parent, meta = {}, {}
    for ref in sats:
        tgt = _sat_target(board, ref, table[ref], fps, cn, net_size, region)
        if tgt is None:
            continue
        par, px, py, nc = tgt
        pcx, pcy, pw, ph = _fp_box(fps[par])
        dx, dy = px - pcx, py - pcy
        phw, phh = max(pw / 2.0, 0.1), max(ph / 2.0, 0.1)
        if abs(dx) / phw >= abs(dy) / phh:
            side = "R" if dx >= 0 else "L"
        else:
            side = "B" if dy >= 0 else "T"
        meta[ref] = (par, px, py, nc, side)
        by_parent.setdefault(par, []).append(ref)

    knobs = _knobs(knobs)
    proposed = {}
    for par, refs in by_parent.items():
        pcx, pcy, pw, ph = _fp_box(fps[par])
        H = _fanout_halo(fps[par])
        sides = {}
        for ref in refs:
            sides.setdefault(meta[ref][4], []).append(ref)
        for side, members in sides.items():
            vert = side in ("L", "R")
            items = []
            for ref in members:
                nc = meta[ref][3]
                deg = _sat_orientation(fps[ref], nc, side)
                want = meta[ref][2] if vert else meta[ref][1]
                items.append(_make_item(ref, fps, deg, want))
            _place_rank(side, items, pcx, pcy, pw, ph, H,
                        region, obstacles, proposed, knobs)
    return proposed


def place_subsystem_cluster(board, table, ref, origin=None, knobs=None,
                            anchor_deg=None):
    """Arrange a whole subsystem (anchor `ref` + its satellites) as ONE compact
    cluster centered at `origin`=(x,y) mm — independent of where the anchor
    currently sits. For the 'park it in empty space, then drag the mass to its
    final spot' workflow. Returns {ref: (x, y, orient_deg)} for the anchor and
    every placeable satellite. `knobs` = {compactness, orderliness, min_distance}
    each 0..1."""
    knobs = _knobs(knobs)
    region = _board_region(board)
    rx0, ry0, rx1, ry1 = region
    if origin is None:                       # default: park just off the right edge
        origin = (rx1 + 12.0, ry0 + 12.0)
    ox, oy = origin
    # allow the cluster to live off-board (parking); don't clip to the outline
    big = (min(rx0, ox) - 300, min(ry0, oy) - 300,
           max(rx1, ox) + 300, max(ry1, oy) + 300)
    fps = {fp.GetReference(): fp for fp in board.GetFootprints()
           if fp.GetReference()}
    if ref not in fps:
        return {}
    cn, net_size, _ = _component_nets(board)
    anchor = fps[ref]
    if anchor_deg is None:
        anchor_deg = anchor.GetOrientationDegrees()
    aw, ah = _canonical_wh(anchor)
    pw, ph = (aw, ah) if int(round(anchor_deg)) % 180 == 0 else (ah, aw)
    H = _fanout_halo(anchor)
    proposed = {ref: (ox, oy, anchor_deg)}

    members = set([ref]) | set(satellites_of(table, ref))
    obstacles = []
    for r, fp in fps.items():
        if r in members or is_unplaced(fp, region):
            continue
        cx, cy, w, h = _fp_box(fp)
        obstacles.append([cx, cy, w, h, _fanout_halo(fp)])
    obstacles.append([ox, oy, pw, ph, H])    # the parked anchor itself

    # assign satellites to sides from the anchor's LOCAL pin geometry, so it
    # works no matter where the anchor currently is on (or off) the board
    byside, lm = {}, {}
    for s in (x for x in satellites_of(table, ref) if x in fps):
        shared = cn.get(s, set()) & cn.get(ref, set())
        best_net, best_sz = None, 1e9
        for nc in shared:
            if net_size.get(nc, 1e9) < best_sz:
                best_sz, best_net = net_size[nc], nc
        pad = _pad_on_net(anchor, best_net) if best_net is not None else None
        if pad is not None:
            p0 = pad.GetFPRelativePosition()
            lx, ly = _rot(p0.x, p0.y, anchor_deg)
            lx, ly = lx / _NM, ly / _NM
        else:
            lx, ly = 0.0, 0.0
        phw, phh = max(pw / 2.0, 0.1), max(ph / 2.0, 0.1)
        if abs(lx) / phw >= abs(ly) / phh:
            side = "R" if lx >= 0 else "L"
        else:
            side = "B" if ly >= 0 else "T"
        lm[s] = (best_net, side, ox + lx, oy + ly)
        byside.setdefault(side, []).append(s)

    for side, group in byside.items():
        vert = side in ("L", "R")
        items = []
        for s in group:
            nc, _, wx_, wy_ = lm[s]
            deg = _sat_orientation(fps[s], nc, side)
            items.append(_make_item(s, fps, deg, wy_ if vert else wx_))
        _place_rank(side, items, ox, oy, pw, ph, H, big, obstacles, proposed, knobs)
    return proposed


def _place_rank(side, items, pcx, pcy, pw, ph, H, region, obstacles, proposed,
                knobs):
    """Lay one chip edge's satellites in a rank. `items` are pre-built dicts
    {ref,deg,w,h,want,halo}. Knobs shape it:
      compactness  -> spacing tightness (gap + halo scale)
      orderliness  -> along-edge uniformity (0 = hug each pin/ragged, 1 = even)
      min_distance -> perp closeness to the chip (1 = hug, 0 = pushed out)
    Mutates `proposed` (ref -> (x,y,deg)) and `obstacles`."""
    if not items:
        return
    rx0, ry0, rx1, ry1 = region
    vert = side in ("L", "R")
    sign = 1.0 if side in ("R", "B") else -1.0
    c, o, m = knobs["compactness"], knobs["orderliness"], knobs["min_distance"]
    gap = 0.45 * (2.0 - 1.6 * c)             # airy .. tight
    hscale = 1.6 - 0.9 * c
    Hs = H * hscale
    items.sort(key=lambda it: it["want"])
    n = len(items)

    def along(it):
        return it["h"] if vert else it["w"]

    def perp(it):
        return it["w"] if vert else it["h"]

    # pin-aligned positions (min pitch enforced both directions)
    pin = [it["want"] for it in items]
    for i in range(1, n):
        need = (along(items[i - 1]) + along(items[i])) / 2.0 + gap
        pin[i] = max(pin[i], pin[i - 1] + need)
    for i in range(n - 2, -1, -1):
        need = (along(items[i]) + along(items[i + 1])) / 2.0 + gap
        pin[i] = min(pin[i], pin[i + 1] - need)
    # evenly distributed, centered on the mean pin coordinate
    total = sum(along(it) for it in items) + gap * (n - 1)
    mean = sum(it["want"] for it in items) / n
    even, cur = [], mean - total / 2.0
    for it in items:
        cur += along(it) / 2.0
        even.append(cur)
        cur += along(it) / 2.0 + gap
    # orderliness: 0 -> pin-aligned (ragged), 1 -> evenly spaced (neat)
    pos = [pin[i] * (1.0 - o) + even[i] * o for i in range(n)]

    maxperp = max(perp(it) for it in items)
    phalf = (pw if vert else ph) / 2.0
    base_center = pcx if vert else pcy
    extra = (1.0 - m) * 3.0                   # min_distance: push out when low

    def clears(x, y, it):
        w, h = it["w"], it["h"]
        if not (rx0 + w / 2 <= x <= rx1 - w / 2 and
                ry0 + h / 2 <= y <= ry1 - h / 2):
            return False
        for ob in obstacles:
            if _overlap(x, y, w, h, ob[0], ob[1], ob[2], ob[3],
                        max(it["halo"] * hscale, ob[4])):
                return False
        return True

    RANKS = 10
    best = None
    for rank_i in range(RANKS):
        rc = base_center + sign * (phalf + Hs + maxperp / 2.0 + 0.15 + extra +
                                   rank_i * (maxperp + gap))
        row = []
        for it, a in zip(items, pos):
            x, y = (rc, a) if vert else (a, rc)
            if clears(x, y, it):
                row.append((x, y, it))
        if len(row) == n:
            best = (n, rank_i, row)
            break
        if best is None or len(row) > best[0]:
            best = (len(row), rank_i, row)
    if best:
        for x, y, it in best[2]:
            proposed[it["ref"]] = (x, y, it["deg"])
            obstacles.append([x, y, it["w"], it["h"], it["halo"] * hscale])


def _spiral_free(tx, ty, w, h, placed, region, m, halo=0.6):
    """Nearest grid position to (tx,ty) whose box clears all placed boxes and
    stays in the board. Expanding-ring search. Each obstacle box is
    [x,y,w,h,halo]; the gap enforced against it is max(this part's halo, the
    obstacle's halo) — so the denser/pinnier of the two sets the spacing, which
    is what leaves room for a chip's pins to fan out."""
    rx0, ry0, rx1, ry1 = region
    step = 1.0

    def ok(x, y):
        if not (rx0 + w / 2 + m <= x <= rx1 - w / 2 - m and
                ry0 + h / 2 + m <= y <= ry1 - h / 2 - m):
            return False
        for box in placed:
            px, py, pw, ph = box[0], box[1], box[2], box[3]
            phalo = box[4] if len(box) > 4 else 0.6
            if _overlap(x, y, w, h, px, py, pw, ph, max(halo, phalo)):
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
