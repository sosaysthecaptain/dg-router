"""JLC PCBA fabrication files (BoM + CPL) from a KiCad 10 board — headless-safe.

This produces the two files JLCPCB's assembly service wants:

  * BoM  ("Bill of Materials")            -> Comment,Designator,Footprint,LCSC Part #
  * CPL  ("Component Placement List",     -> Designator,Mid X,Mid Y,Layer,Rotation
          a.k.a. pick-and-place / position file)

LCSC part numbers are NOT in the board — they live in a JSON sidecar next to the
board (`<board>.dg-bom.json`), mirroring the placement sidecar pattern in
placement.py: only overrides are stored, everything else is inferred from the
footprints. An agent (or a human) fills LCSC parts either per-component or, more
efficiently, by "value/footprint" key so one assignment covers every part that
shares it. `pcbnew` is imported lazily so this module stays importable headless.

COORDINATE + ROTATION CONVENTIONS (verified against KiCad's own place-file
exporter, pcbnew.PLACE_FILE_EXPORTER.GenPositionData, on KiCad 10.0.4):

  * Origin: the board's AUX (drill/place) origin, board.GetDesignSettings()
    .GetAuxOrigin() (a VECTOR2I in nm). If it is (0,0) — which is the common
    "unset" case — this degenerates to the board origin, and coordinates are
    absolute board mm. (The test board has aux origin (0,0).)

  * X: mid_x = (fp.x - aux.x) / _NM         (KiCad X is positive-right, same as
    the gerber/JLC convention, so no sign flip.)

  * Y: mid_y = -(fp.y - aux.y) / _NM        KiCad stores Y positive-DOWN; JLC /
    gerber want Y positive-UP, so the Y axis is negated. This reproduces the
    native exporter EXACTLY: e.g. CB14 at raw y=+188.5mm exports as PosY
    -188.5mm, which is what this code emits.

  * Rotation: start from fp.GetOrientationDegrees(), ADD the sidecar
    rotation_offset (default 0), normalize to [0,360). For BOTTOM-layer parts
    JLC mirrors the part, so the emitted angle is (180 - rot) % 360. (The test
    board is single-sided/top-only, so the bottom path is exercised only by
    unit reasoning — flag it.)

  ROTATION IS THE FIELD MOST LIKELY TO NEED PER-PART CORRECTION. JLC's own part
  library defines a "0-degree" orientation per package that frequently disagrees
  with KiCad's footprint orientation (polarized caps, diodes, ICs, connectors).
  When a placement comes back rotated 90/180 deg, set `rotation_offset` for that
  ref (or that value/footprint) in the sidecar rather than editing the board.
"""

import os
import re
import csv
import json

_NM = 1e6  # nanometres per mm (matches placement.py)


# --------------------------------------------------------------------------- #
# sidecar (mirrors placement.py: sidecar_path / load_table / save_table)
# --------------------------------------------------------------------------- #
def bom_sidecar_path(board_path):
    return os.path.splitext(os.path.abspath(board_path))[0] + ".dg-bom.json"


def load_bom(board_path):
    """Load the BoM sidecar. Shape:
        {"components": {ref: {lcsc, rotation_offset, dnp, comment}},
         "by_value":   {"<value>/<footprint>": {lcsc}}}
    Missing file / bad JSON -> empty dict. Only present keys override."""
    p = bom_sidecar_path(board_path)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def save_bom(board_path, data):
    p = bom_sidecar_path(board_path)
    with open(p, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    return p


# --------------------------------------------------------------------------- #
# natural sort for designators: C1, C2, C10 (not C1, C10, C2)
# --------------------------------------------------------------------------- #
def _natkey(ref):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", ref or "")]


# --------------------------------------------------------------------------- #
# per-component records
# --------------------------------------------------------------------------- #
def _is_excluded(fp, sidecar_comp):
    """True if this footprint should be left out of assembly outputs: sidecar
    dnp override, KiCad DNP attribute, or exclude-from-BOM attribute."""
    import pcbnew
    if sidecar_comp.get("dnp"):
        return True
    try:
        if fp.IsDNP():
            return True
    except Exception:
        pass
    try:
        attrs = fp.GetAttributes()
        if attrs & getattr(pcbnew, "FP_DNP", 0):
            return True
        if attrs & getattr(pcbnew, "FP_EXCLUDE_FROM_BOM", 0):
            return True
    except Exception:
        pass
    return False


def _value_key(value, libitem):
    return "%s/%s" % (value, libitem)


def component_records(board, board_path):
    """One dict per placed, in-BoM footprint (skips 0-pad parts like fiducials/
    logos and anything DNP / excluded-from-BOM / sidecar-dnp).

    Each record: {ref, value, footprint, layer, x_mm, y_mm, rotation, lcsc,
    comment}. `lcsc` resolves per-component sidecar entry, else by_value entry,
    else "". `comment` is the sidecar comment override, else the value.
    `footprint` is the library item name (e.g. "R_0402_1005Metric")."""
    import pcbnew
    sc = load_bom(board_path)
    comps = sc.get("components", {})
    by_value = sc.get("by_value", {})

    records = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if not ref:
            continue
        if fp.GetPadCount() == 0:          # fiducials / logos / mounting art
            continue
        cov = comps.get(ref, {})
        if _is_excluded(fp, cov):
            continue

        value = fp.GetValue()
        libitem = str(fp.GetFPID().GetLibItemName())
        vkey = _value_key(value, libitem)

        lcsc = cov.get("lcsc")
        if not lcsc:
            lcsc = by_value.get(vkey, {}).get("lcsc", "")
        lcsc = lcsc or ""

        comment = cov.get("comment") or value
        layer = "Top" if fp.GetLayer() == pcbnew.F_Cu else "Bottom"
        pos = fp.GetPosition()

        records.append({
            "ref": ref,
            "value": value,
            "footprint": libitem,
            "layer": layer,
            "x_mm": pos.x / _NM,
            "y_mm": pos.y / _NM,
            "rotation": fp.GetOrientationDegrees(),
            "lcsc": lcsc,
            "comment": comment,
            "_rotation_offset": cov.get("rotation_offset", 0),
        })
    return records


# --------------------------------------------------------------------------- #
# grouped BoM rows
# --------------------------------------------------------------------------- #
def bom_rows(board, board_path):
    """Grouped JLC BoM rows. Records are grouped by (comment, footprint, lcsc).
    Returns [{comment, designators, footprint, lcsc, qty}] with designators
    naturally sorted + comma-space joined, rows sorted by first designator."""
    groups = {}
    for r in component_records(board, board_path):
        key = (r["comment"], r["footprint"], r["lcsc"])
        groups.setdefault(key, []).append(r["ref"])

    rows = []
    for (comment, footprint, lcsc), refs in groups.items():
        refs = sorted(refs, key=_natkey)
        rows.append({
            "comment": comment,
            "designators": ", ".join(refs),
            "footprint": footprint,
            "lcsc": lcsc,
            "qty": len(refs),
        })
    rows.sort(key=lambda row: _natkey(row["designators"].split(",")[0].strip()))
    return rows


# --------------------------------------------------------------------------- #
# CPL / pick-and-place rows
# --------------------------------------------------------------------------- #
def _aux_origin(board):
    """AUX (place/drill) origin in nm as (x, y). (0,0) if unset — falls back to
    board origin, i.e. absolute board coordinates."""
    try:
        ao = board.GetDesignSettings().GetAuxOrigin()
        return ao.x, ao.y
    except Exception:
        return 0, 0


def cpl_rows(board, board_path):
    """JLC CPL rows, one per placed non-DNP component.

    {designator, mid_x_mm, mid_y_mm, layer, rotation} where mid_x/mid_y are the
    centroid relative to the aux origin with JLC's Y-up sign convention, and
    rotation is (orientation + sidecar offset), mirrored for bottom parts. See
    the module docstring for the exact formulas."""
    ax, ay = _aux_origin(board)
    rows = []
    for r in component_records(board, board_path):
        mid_x = (r["x_mm"] - ax / _NM)
        mid_y = -(r["y_mm"] - ay / _NM)     # KiCad Y-down -> JLC Y-up
        rot = (r["rotation"] + r["_rotation_offset"]) % 360.0
        if r["layer"] == "Bottom":
            rot = (180.0 - rot) % 360.0     # JLC mirrors bottom-side parts
        rows.append({
            "designator": r["ref"],
            "mid_x_mm": mid_x,
            "mid_y_mm": mid_y,
            "layer": r["layer"],
            "rotation": round(rot, 4),
        })
    rows.sort(key=lambda row: _natkey(row["designator"]))
    return rows


# --------------------------------------------------------------------------- #
# CSV writers (exact JLC headers, csv module for correct quoting)
# --------------------------------------------------------------------------- #
def _fmt_mm(v):
    return "%.4fmm" % v


def write_bom_csv(board, board_path, out_path):
    """Write the JLC BoM CSV. Header: Comment,Designator,Footprint,LCSC Part #"""
    rows = bom_rows(board, board_path)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        for r in rows:
            w.writerow([r["comment"], r["designators"],
                        r["footprint"], r["lcsc"]])
    return out_path


def write_cpl_csv(board, board_path, out_path):
    """Write the JLC CPL CSV. Header: Designator,Mid X,Mid Y,Layer,Rotation"""
    rows = cpl_rows(board, board_path)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for r in rows:
            w.writerow([r["designator"], _fmt_mm(r["mid_x_mm"]),
                        _fmt_mm(r["mid_y_mm"]), r["layer"], r["rotation"]])
    return out_path
