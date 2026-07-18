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


def main(argv=None):
    ap = argparse.ArgumentParser(description="dg-router shim (headless)")
    ap.add_argument("board", help="path to .kicad_pcb")
    ap.add_argument("--list", action="store_true", help="list nets and layers")
    ap.add_argument("--status", action="store_true",
                    help="per-net routing status via DRC (routed/partial/unrouted)")
    ap.add_argument("--render", action="store_true", help="render board to SVG")
    ap.add_argument("--route", nargs="*", default=[], help="net names to route")
    ap.add_argument("--layer", default="B.Cu", help="prefer layer")
    ap.add_argument("--via-cost", type=int, default=80)
    ap.add_argument("--edge-hug", type=float, default=0.0)
    ap.add_argument("--emit", action="store_true", help="write job.json")
    ap.add_argument("--out", default=None, help="output dir (default: next to board)")
    args = ap.parse_args(argv)

    board = pcbnew.LoadBoard(args.board)
    out_dir = args.out or os.path.join(os.path.dirname(os.path.abspath(args.board)),
                                       "dg-router-out")

    if args.list or not (args.status or args.render or args.emit):
        nets = shim.list_nets(board)
        print("copper layers:", ", ".join(shim.copper_layer_names(board)))
        print("nets: %d" % len(nets))
        for n in nets:
            print("  [%3d] %-28s %d pads" % (n["code"], n["name"], n["pads"]))

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
