# Driving dg-router from Claude

dg-router is steerable. You (Claude) provide the strategy — what to route, in
what order, with what preferences — and the router executes. The **CLI is the
API** and the **`.kicad_pcb` file is the shared state**. Tell the user to route
and they can sit back and watch; or drive it yourself.

## The interpreter
Use KiCad's embedded Python (it has `pcbnew`):

    KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
    cd <the dg-router repo>
    BOARD=/path/to/board.kicad_pcb

## Commands

Inspect:

    $KPY headless.py "$BOARD" --list       # nets (>=2 pads) + copper layers
    $KPY headless.py "$BOARD" --status      # per-net routed / partial / unrouted (DRC)

See the board (emit a PNG you can read):

    $KPY headless.py "$BOARD" --render-png out.png
    # then read out.png to review

Route (writes a COPY, never the open board):

    $KPY headless.py "$BOARD" --route /SDA /SCL --solve \
        --layers F.Cu,B.Cu --objective least_obtrusive --prefer B.Cu \
        --route-via-cost 10
    # -> writes <board dir>/dg-router-out/routed.kicad_pcb
    #    reports unconnected before -> after; then --render-png it to review
    #    (also copies the board's .kicad_pro so DRC uses its REAL netclass rules)

A job = a set of connections. Break a fat net into smaller jobs by naming the
specific pad-pairs to route (same atom the GUI's ratline-click builds):

    $KPY headless.py "$BOARD" --list-connections /+12V
    # -> lists addressable connections: U5.1:C12.2   4.20 mm  ...
    $KPY headless.py "$BOARD" --connect U5.1:C12.2 U5.1:C13.2
    # -> routes ONLY those connections; writes routed.kicad_pcb

Power/multi-pin nets — trunk (MST spine) + branches to nearest trunk copper:

    $KPY headless.py "$BOARD" --auto-trunk /+12V --layers F.Cu,B.Cu

Options:
- `--layers F.Cu,B.Cu` — routable copper layers.
- `--objective` — `least_obtrusive` (default; hugs edges/copper, won't wall off
  chips) | `direct` | `follow` | `hug`.
- `--prefer B.Cu` — bias toward a layer (use others only when needed).
- `--route-via-cost N` — higher = fewer vias.
- `--pitch mm` — grid pitch (default 0.2).

## The strategist's loop
1. `--status` to see what's unrouted.
2. Route the **critical / constrained** nets first, a few at a time, with
   deliberate preferences (e.g. a bus together on B.Cu; a sensitive line
   `direct`). Keep clear channels for chip fanout by routing fanout nets first.
3. `--render-png` and **look** — did it consume space something else needs?
   Adjust objective / order and re-route.
4. Bulk-route the remainder.
5. The routed copy is at `dg-router-out/routed.kicad_pcb`; review, then the user
   merges/opens it (or accepts in the GUI).

## Notes
- The router **fails cleanly rather than shorting** — a net that can't be
  entered (e.g. very fine pitch) is reported unrouted, not shorted.
- Same selection/preferences are available in the GUI, so the user can take over
  or hand back at any point.
