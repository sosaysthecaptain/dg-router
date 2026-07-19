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
    comp_nets, net_size, fps = _component_nets(board)
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
            best = max(scores.values())
            parents = [c for c, s in sorted(scores.items(), key=lambda x: -x[1])
                       if s >= best * 0.5][:2]
        out[ref] = {"type": t, "parents": parents,
                    "value": fp.GetValue(), "pads": fp.GetPadCount()}
    return out


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
                               if k in ("type", "parents")})
    return table
