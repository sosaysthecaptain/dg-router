#!/usr/bin/env python3
"""Trigger the shim from code — no KiCad GUI.

Run with KiCad's embedded interpreter (it has pcbnew):

  KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
  $KPY headless.py BOARD.kicad_pcb --list
  $KPY headless.py BOARD.kicad_pcb --render
  $KPY headless.py BOARD.kicad_pcb --route SPI_CLK SPI_MOSI --layer B.Cu --emit

This is the "triggerable from code" seam: same shim.py functions the GUI uses.
"""

import os
import sys
import json
import argparse

# Import shim.py directly (package dir on path) so we DON'T run the package
# __init__, which registers the GUI ActionPlugin and asserts outside KiCad.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "dg_router_plugin"))

import pcbnew  # noqa: E402  (embedded interpreter provides this)

import shim  # noqa: E402
import router  # noqa: E402
import placement  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="dg-router shim (headless)")
    ap.add_argument("board", help="path to .kicad_pcb")
    ap.add_argument("--list", action="store_true", help="list nets and layers")
    ap.add_argument("--status", action="store_true",
                    help="per-net routing status via DRC (routed/partial/unrouted)")
    ap.add_argument("--render", action="store_true", help="render board to SVG")
    ap.add_argument("--render-png", dest="render_png", default=None,
                    help="emit a 2D PNG render (readable by Claude)")
    ap.add_argument("--route", nargs="*", default=[], help="net names to route")
    ap.add_argument("--list-connections", dest="list_connections", nargs="*",
                    default=None,
                    help="list a net's addressable connections (pad-pairs); "
                         "give net names, or omit to use --route nets")
    ap.add_argument("--connect", nargs="*", default=[],
                    help="route only these connections, e.g. U5.1:C12.2 "
                         "(a 'job' of specific pad-pairs); implies --solve")
    ap.add_argument("--auto-trunk", dest="auto_trunk", default=None,
                    help="route a power net as trunk (MST spine) + branches")
    ap.add_argument("--classify", action="store_true",
                    help="infer component tiers (anchor/subsystem/satellite) + parents")
    ap.add_argument("--classify-write", dest="classify_write", action="store_true",
                    help="write the inferred classification to the sidecar JSON")
    ap.add_argument("--layer", default="B.Cu", help="prefer layer")
    ap.add_argument("--via-cost", type=int, default=80)
    ap.add_argument("--edge-hug", type=float, default=0.0)
    ap.add_argument("--emit", action="store_true", help="write job.json")
    ap.add_argument("--solve", action="store_true",
                    help="actually route --route nets, write a copy, DRC-verify")
    ap.add_argument("--pitch", type=float, default=0.2, help="grid pitch mm")
    ap.add_argument("--route-via-cost", type=float, default=10.0,
                    help="A* cost (grid steps) per via")
    ap.add_argument("--layers", default="F.Cu,B.Cu",
                    help="comma-separated routable copper layers")
    ap.add_argument("--objective", default="least_obtrusive",
                    help="direct | follow | hug | least_obtrusive")
    ap.add_argument("--prefer", default=None, help="prefer this layer")
    ap.add_argument("--out", default=None, help="output dir (default: next to board)")
    args = ap.parse_args(argv)

    board = pcbnew.LoadBoard(args.board)
    out_dir = args.out or os.path.join(os.path.dirname(os.path.abspath(args.board)),
                                       "dg-router-out")

    if args.list or not (args.status or args.render or args.render_png
                         or args.emit or args.solve or args.auto_trunk
                         or args.classify or args.classify_write
                         or args.list_connections is not None or args.connect):
        nets = shim.list_nets(board)
        print("copper layers:", ", ".join(shim.copper_layer_names(board)))
        print("nets: %d" % len(nets))
        for n in nets:
            print("  [%3d] %-28s %d pads" % (n["code"], n["name"], n["pads"]))

    if args.list_connections is not None:
        nets = args.list_connections or args.route
        if not nets:
            ap.error("--list-connections needs net names (or --route nets)")
        unconn = shim.drc_unconnected(args.board)
        for nm in nets:
            conns = shim.net_connections(board, nm, unconn)
            print("%s: %d connection(s)" % (nm, len(conns)))
            for c in sorted(conns, key=lambda c: -c["length"]):
                print("  %-24s %.2f mm" % (c["id"], c["length"]))

    if args.connect:
        sub, missing = shim.connections_to_unconn(board, args.board, args.connect)
        if missing:
            print("unresolved connections: %s" % ", ".join(missing))
        if not sub:
            ap.error("no valid --connect connections resolved")
        before = sum(len(v) for v in shim.drc_unconnected(args.board).values())
        out = os.path.join(out_dir, "routed.kicad_pcb")
        params = router.RouteParams(
            board, pitch_mm=args.pitch, via_cost=args.route_via_cost,
            layer_names=[s.strip() for s in args.layers.split(",") if s.strip()],
            objective=args.objective, prefer_layer=args.prefer)
        summary = router.solve(args.board, None, out, params=params, unconn=sub)
        for r in summary["results"]:
            print("  %-22s ok=%s  %s" % (
                r["net"], r["ok"],
                r.get("reason") or "gaps=%d routed=%d"
                % (r.get("gaps", 0), r.get("routed", 0))))
        import shutil
        src_pro = os.path.splitext(os.path.abspath(args.board))[0] + ".kicad_pro"
        if os.path.exists(src_pro):
            try:
                shutil.copyfile(src_pro, os.path.join(out_dir, "routed.kicad_pro"))
            except OSError:
                pass
        after = sum(len(v) for v in shim.drc_unconnected(out).values())
        print("tracks added: %d" % summary["tracks_added"])
        print("unconnected: %d -> %d  (wrote %s)" % (before, after, out))

    if args.classify or args.classify_write:
        table = placement.effective_table(board, args.board)
        by_type = {"anchor": [], "subsystem_anchor": [], "satellite": []}
        for ref, info in table.items():
            by_type.setdefault(info["type"], []).append((ref, info))
        for t in ("anchor", "subsystem_anchor", "satellite"):
            rows = sorted(by_type.get(t, []))
            print("== %s (%d) ==" % (t, len(rows)))
            for ref, info in rows:
                par = ", ".join(info["parents"]) if info["parents"] else "-"
                print("  %-8s %-14s parents: %s" % (ref, info["value"], par))
        if args.classify_write:
            out = {"components": {r: {"type": i["type"], "parents": i["parents"]}
                                  for r, i in table.items()}}
            print("wrote:", placement.save_table(args.board, out))

    if args.auto_trunk:
        before = sum(len(v) for v in shim.drc_unconnected(args.board).values())
        params = router.RouteParams(
            board, pitch_mm=args.pitch, via_cost=args.route_via_cost,
            layer_names=[s.strip() for s in args.layers.split(",") if s.strip()],
            objective=args.objective, prefer_layer=args.prefer)
        r = router.auto_trunk(board, args.auto_trunk, params)
        print("  %-22s ok=%s  trunk+branches=%d routed=%d"
              % (r["net"], r.get("ok"), r.get("gaps", 0), r.get("routed", 0)))
        added = 0
        if r.get("segments") or r.get("vias"):
            added = len(router.write_result(board, r["net_code"], r))
        router.refill_zones(board)
        out = os.path.join(out_dir, "routed.kicad_pcb")
        os.makedirs(out_dir, exist_ok=True)
        pcbnew.SaveBoard(out, board)
        import shutil
        src_pro = os.path.splitext(os.path.abspath(args.board))[0] + ".kicad_pro"
        if os.path.exists(src_pro):
            try:
                shutil.copyfile(src_pro, os.path.join(out_dir, "routed.kicad_pro"))
            except OSError:
                pass
        after = sum(len(v) for v in shim.drc_unconnected(out).values())
        print("tracks added: %d" % added)
        print("unconnected: %d -> %d  (wrote %s)" % (before, after, out))

    if args.status:
        status, unconn = shim.net_status_map(board, args.board)
        counts = {"routed": 0, "partial": 0, "unrouted": 0}
        for name in sorted(status):
            counts[status[name]] += 1
            print("  %-9s %-28s %d gap(s)"
                  % (status[name], name, len(unconn.get(name, []))))
        print("routed=%d partial=%d unrouted=%d"
              % (counts["routed"], counts["partial"], counts["unrouted"]))

    if args.render:
        out = os.path.join(out_dir, "preview.svg")
        print("rendered:", shim.render_board_svg(args.board, out))

    if args.render_png:
        print("rendered:", shim.render_board_png(args.board, args.render_png))

    if args.solve:
        if not args.route:
            ap.error("--solve requires --route NET [NET ...]")
        before = sum(len(v) for v in shim.drc_unconnected(args.board).values())
        out = os.path.join(out_dir, "routed.kicad_pcb")
        params = router.RouteParams(
            board, pitch_mm=args.pitch, via_cost=args.route_via_cost,
            layer_names=[s.strip() for s in args.layers.split(",") if s.strip()],
            objective=args.objective, prefer_layer=args.prefer)
        summary = router.solve(args.board, args.route, out, params=params)
        # Copy the sibling .kicad_pro so DRC / KiCad read the board's REAL rules
        # (netclass clearances live in the project file; without it KiCad falls
        # back to a 0.2mm default clearance and reports phantom violations).
        import shutil
        src_pro = os.path.splitext(os.path.abspath(args.board))[0] + ".kicad_pro"
        if os.path.exists(src_pro):
            try:
                shutil.copyfile(src_pro,
                                os.path.join(out_dir, "routed.kicad_pro"))
            except OSError:
                pass
        for r in summary["results"]:
            print("  %-22s ok=%s  %s" % (
                r["net"], r["ok"],
                r.get("reason") or "layer=%s gaps=%d routed=%d"
                % (r.get("layer", "?"), r.get("gaps", 0), r.get("routed", 0))))
        after = sum(len(v) for v in shim.drc_unconnected(out).values())
        print("tracks added: %d" % summary["tracks_added"])
        print("unconnected: %d -> %d  (wrote %s)" % (before, after, out))
        png = os.path.join(out_dir, "routed.png")
        try:
            shim.render_board_png(out, png)
            print("render: %s" % png)
        except Exception as e:  # noqa: BLE001
            print("render failed: %s" % e)

    if args.emit:
        job = shim.build_job(
            args.route,
            {"layer": args.layer, "viaCost": args.via_cost, "edgeHug": args.edge_hug},
        )
        out = shim.write_job(job, os.path.join(out_dir, "job.json"))
        print("wrote:", out)
        print(json.dumps(job, indent=2))


if __name__ == "__main__":
    main()
