"""Core shim logic for dg-router — NO wx, NO GUI.

Importable from both the Action Plugin (GUI) and headless.py (code).
`pcbnew` is imported lazily inside functions so this module can be imported
in environments where pcbnew isn't on the path (it always is under KiCad's
embedded interpreter).

Milestone 0/0.1: this does NOT route anything. It reads the board, renders a
visualization, extracts per-net geometry for highlighting, and emits a job
spec. Those are the seams the real router plugs into later.
"""

import os
import re
import math
import json
import shutil
import subprocess

# kicad-cli is the ground-truth renderer / (later) DRC oracle.
_KICAD_CLI_CANDIDATES = [
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "kicad-cli",
]

# Default layers to show in a preview render (copper + context).
DEFAULT_PREVIEW_LAYERS = "F.Cu,B.Cu,F.SilkS,B.SilkS,Edge.Cuts,F.Mask,B.Mask"

_NM = 1e6  # KiCad internal units (nm) per mm


def find_kicad_cli():
    """Absolute path to kicad-cli, or None if not found."""
    for c in _KICAD_CLI_CANDIDATES:
        if os.path.isabs(c) and os.path.exists(c):
            return c
        found = shutil.which(c)
        if found:
            return found
    return None


def copper_layer_names(board):
    """Enabled copper layer names, in stack order (F.Cu ... B.Cu)."""
    import pcbnew

    names = []
    enabled = board.GetEnabledLayers()
    for lid in range(pcbnew.PCB_LAYER_ID_COUNT):
        try:
            if enabled.Contains(lid) and pcbnew.IsCopperLayer(lid):
                names.append(board.GetLayerName(lid))
        except Exception:
            continue
    return names


def list_nets(board):
    """List real nets on the board: [{'code', 'name', 'pads'}], sorted by name.

    Net 0 (unconnected) and unnamed nets are skipped. Iterating by netcode
    avoids fragile std::map iteration across SWIG versions.
    """
    nets = []
    for code in range(1, board.GetNetCount()):
        net = board.FindNet(code)
        if net is None:
            continue
        name = net.GetNetname()
        if not name:
            continue
        nets.append({"code": code, "name": name})

    counts = {}
    for pad in board.GetPads():
        counts[pad.GetNetCode()] = counts.get(pad.GetNetCode(), 0) + 1
    for n in nets:
        n["pads"] = counts.get(n["code"], 0)

    nets.sort(key=lambda n: n["name"])
    return nets


def render_board_svg(board_path, out_svg, layers=None):
    """Render the saved board file to a single flat SVG via kicad-cli.

    Returns the output path. Raises RuntimeError with captured stderr on failure.
    """
    cli = find_kicad_cli()
    if not cli:
        raise RuntimeError("kicad-cli not found (looked in KiCad.app and PATH)")

    layers = layers or DEFAULT_PREVIEW_LAYERS
    os.makedirs(os.path.dirname(os.path.abspath(out_svg)), exist_ok=True)
    cmd = [
        cli, "pcb", "export", "svg",
        "--mode-single",
        "--page-size-mode", "2",   # 2 = board area only (tight crop)
        "--exclude-drawing-sheet",
        "-l", layers,
        "-o", out_svg,
        board_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "kicad-cli export svg failed (%d):\n%s\n%s"
            % (proc.returncode, proc.stdout, proc.stderr)
        )
    return out_svg


# --- coordinate mapping: pcbnew board coords (mm) -> SVG user coords (mm) ----
#
# The kicad-cli SVG plots geometry in mm, in a viewBox whose origin is the
# board plot origin. For page-size-mode 2 that origin is the Edge.Cuts
# bounding-box CENTER minus half the viewBox size (the viewBox excludes the
# Edge.Cuts line width, ~0.05mm/side, which the pcbnew bbox includes). So:
#     svg_mm = pcb_mm - plot_origin
# and plot_origin = edges_center - viewBox/2. Sub-pixel accurate.

def board_edges_center_mm(board):
    bb = board.GetBoardEdgesBoundingBox()
    return ((bb.GetX() + bb.GetRight()) / 2.0 / _NM,
            (bb.GetY() + bb.GetBottom()) / 2.0 / _NM)


def parse_svg_viewbox(svg_path):
    """(width_mm, height_mm) from the SVG viewBox."""
    with open(svg_path) as f:
        head = f.read(4096)
    m = re.search(r'viewBox="\s*([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)"',
                  head)
    if not m:
        raise RuntimeError("no viewBox found in " + svg_path)
    return float(m.group(3)), float(m.group(4))


def plot_origin(board, viewbox_w, viewbox_h):
    """Board-coord (mm) that maps to SVG (0,0)."""
    cx, cy = board_edges_center_mm(board)
    return (cx - viewbox_w / 2.0, cy - viewbox_h / 2.0)


def _via_radius_mm(via):
    """Via radius in mm. No-arg PCB_VIA.GetWidth() raises under a running wx.App
    (KiCad GUI) in KiCad 10; the layer-arg form works."""
    import pcbnew
    for L in (pcbnew.F_Cu, pcbnew.B_Cu):
        try:
            w = via.GetWidth(L)
            if w:
                return w / 2.0 / _NM
        except Exception:
            continue
    return 0.3


def net_geometry(board, net_names):
    """Per-net copper geometry in mm, for highlight overlays.

    Returns {name: {'pads': [(x,y,r)], 'tracks': [(x1,y1,x2,y2,w)],
                    'vias': [(x,y,r)]}}. Arcs are approximated as segments.
    """
    import pcbnew

    names = set(net_names)
    code_to_name = {}
    for code in range(1, board.GetNetCount()):
        net = board.FindNet(code)
        if net is not None and net.GetNetname() in names:
            code_to_name[code] = net.GetNetname()

    out = {n: {"pads": [], "tracks": [], "vias": []} for n in names}

    for pad in board.GetPads():
        nm = code_to_name.get(pad.GetNetCode())
        if not nm:
            continue
        p = pad.GetPosition()
        sz = pad.GetSize()
        r = max(sz.x, sz.y) / 2.0 / _NM
        out[nm]["pads"].append((p.x / _NM, p.y / _NM, r))

    for t in board.GetTracks():
        nm = code_to_name.get(t.GetNetCode())
        if not nm:
            continue
        if t.Type() == pcbnew.PCB_VIA_T:
            p = t.GetPosition()
            out[nm]["vias"].append((p.x / _NM, p.y / _NM, _via_radius_mm(t)))
        else:
            s = t.GetStart()
            e = t.GetEnd()
            out[nm]["tracks"].append(
                (s.x / _NM, s.y / _NM, e.x / _NM, e.y / _NM, t.GetWidth() / _NM))

    return out


def mst_edges(points):
    """Minimum spanning tree over 2D points -> list of (i, j) index pairs.

    Used to draw an approximate ratsnest for a net's pads (Prim, O(n^2)).
    """
    n = len(points)
    if n < 2:
        return []
    in_tree = [False] * n
    in_tree[0] = True
    best_d = [math.hypot(points[i][0] - points[0][0],
                         points[i][1] - points[0][1]) for i in range(n)]
    best_from = [0] * n
    edges = []
    for _ in range(n - 1):
        u, du = -1, None
        for i in range(n):
            if not in_tree[i] and (du is None or best_d[i] < du):
                du, u = best_d[i], i
        if u < 0:
            break
        in_tree[u] = True
        edges.append((best_from[u], u))
        for i in range(n):
            if not in_tree[i]:
                d = math.hypot(points[i][0] - points[u][0],
                               points[i][1] - points[u][1])
                if d < best_d[i]:
                    best_d[i], best_from[i] = d, u
    return edges


_NET_IN_DESC = re.compile(r"\[([^\]]*)\]")


def drc_unconnected(board_path):
    """Ground-truth ratsnest via kicad-cli DRC.

    Returns {net_name: [(x1,y1,x2,y2), ...]} — the MISSING connections (mm,
    board coords) for each net. This is the oracle: it reflects what is *not*
    yet routed, so partial nets show only the gaps left to complete.
    """
    cli = find_kicad_cli()
    if not cli:
        raise RuntimeError("kicad-cli not found")
    import tempfile
    # unique temp file per call — a fixed path collides when a background status
    # thread and a route run concurrently, corrupting the JSON.
    fd, out = tempfile.mkstemp(suffix="-dg-drc.json")
    os.close(fd)
    try:
        # Nonzero exit just means violations exist; parse the report regardless.
        subprocess.run([cli, "pcb", "drc", "--format", "json", "-o", out,
                        board_path], capture_output=True, text=True)
        if not os.path.getsize(out):
            return {}
        with open(out) as f:
            data = json.load(f)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass

    res = {}
    for item in data.get("unconnected_items", []):
        pts = item.get("items", [])
        if len(pts) < 2:
            continue
        m = _NET_IN_DESC.search(pts[0].get("description", ""))
        if not m:
            continue
        name = m.group(1)
        a, b = pts[0]["pos"], pts[1]["pos"]
        res.setdefault(name, []).append((a["x"], a["y"], b["x"], b["y"]))
    return res


def net_status_map(board, board_path):
    """Per-net routing status + the missing-connection ratsnest.

    Returns (status, unconnected):
      status: {net_name: 'routed'|'partial'|'unrouted'} for nets with >=2 pads
      unconnected: the drc_unconnected() map
    """
    unconn = drc_unconnected(board_path)
    nets = list_nets(board)
    code_name = {n["code"]: n["name"] for n in nets}

    has_copper = {}
    for t in board.GetTracks():
        nm = code_name.get(t.GetNetCode())
        if nm:
            has_copper[nm] = True

    status = {}
    for n in nets:
        name = n["name"]
        if n["pads"] < 2:
            continue
        if len(unconn.get(name, [])) == 0:
            status[name] = "routed"
        elif has_copper.get(name):
            status[name] = "partial"
        else:
            status[name] = "unrouted"
    return status, unconn


def build_job(route_nets, prefer, avoid=None, follow_existing=True,
              expendable=None, then=None):
    """Build a job-spec dict (the interchange the TS router core will consume)."""
    job = {
        "route": list(route_nets),
        "prefer": prefer,
        "avoid": avoid or [],
        "followExisting": bool(follow_existing),
        "expendable": expendable or [],
    }
    if then:
        job["then"] = then
    return job


def write_job(job, out_path):
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(job, f, indent=2)
    return out_path
