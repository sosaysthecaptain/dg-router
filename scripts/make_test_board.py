#!/usr/bin/env python3
"""Make a placement/routing test board from a real one.

Unroutes the board (removes tracks + vias) and moves every NON-anchor component
into a limbo grid off to the right of the board outline. Anchors (connectors,
MCU-scale chips) stay where they are — so dg-router placement starts from a
realistic "anchors placed, everything else unplaced" state.

Usage (KiCad's python):
  KPY scripts/make_test_board.py SRC.kicad_pcb DST.kicad_pcb
Never writes SRC.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "dg_router_plugin"))
import pcbnew        # noqa: E402
import placement     # noqa: E402

_NM = 1e6


def main(src, dst):
    if os.path.abspath(src) == os.path.abspath(dst):
        raise SystemExit("refusing to overwrite the source board")
    board = pcbnew.LoadBoard(src)

    # classify + capture footprint handles BEFORE mutating (Remove() invalidates
    # the GetFootprints iterator)
    table = placement.classify(board)
    fps = list(board.GetFootprints())
    bb = board.GetBoardEdgesBoundingBox()

    # unroute
    ntrk = 0
    for t in list(board.GetTracks()):
        board.Remove(t)
        ntrk += 1

    lx0 = bb.GetRight() / _NM + 15.0     # limbo starts right of the board
    ly0 = bb.GetY() / _NM
    step, cols = 4.5, 36
    col = row = moved = 0
    for fp in fps:
        info = table.get(fp.GetReference())
        if not info or info["type"] == "anchor":
            continue
        x = lx0 + col * step
        y = ly0 + row * step
        fp.SetPosition(pcbnew.VECTOR2I(int(x * _NM), int(y * _NM)))
        moved += 1
        col += 1
        if col >= cols:
            col, row = 0, row + 1

    pcbnew.SaveBoard(dst, board)
    # bring the project file along so DRC / netclasses resolve
    src_pro = os.path.splitext(src)[0] + ".kicad_pro"
    dst_pro = os.path.splitext(dst)[0] + ".kicad_pro"
    if os.path.exists(src_pro):
        import shutil
        shutil.copyfile(src_pro, dst_pro)
    print("wrote %s  (removed %d tracks/vias, moved %d parts to limbo)"
          % (dst, ntrk, moved))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
