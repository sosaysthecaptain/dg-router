# Driving dg-router placement (the Place tab)

dg-router's **Place** tab is a steerable component-placement tool. It does not
decide *what belongs together* — you (the designer, or the agent that designed
the board) tell it, via a **classification table**. It then places parts
top-down: you place anchors, it places subsystem anchors near them, then it
places each subsystem's satellites for short, clean connectivity. Nothing is
committed until you Accept; the `.kicad_pcb` is only written when you save.

**If you designed this board, your job is to fill in the table correctly** —
especially the things a flat netlist can't know (see "Why the table needs you").

## The classification table (the shared state)

Every component gets three things:

| field | meaning |
|---|---|
| **type** | `anchor` \| `subsystem_anchor` \| `satellite` |
| **parents** | the ref(s) this part belongs to (see below) |
| **name** | a human name — what the part *is/does*, e.g. "BUCK1 bootstrap" |

- **anchor** — connectors, the MCU, big fixed chips. *You* place these by hand.
- **subsystem_anchor** — the IC at the heart of a subsystem (a regulator, a
  driver, a sensor). The tool places these near the anchors they serve; you
  nudge. A subsystem's *name* is its anchor's name (e.g. "BUCK1", "IMU").
- **satellite** — the passives that serve one subsystem (decoupling, feedback,
  bootstrap, current-sense, gate resistors…). The tool places these against the
  pin they serve, once the anchor is placed.

**parents:**
- a `satellite`'s parent is the ONE `subsystem_anchor` (or anchor) it serves;
- a `subsystem_anchor`'s parents are the anchor(s) it connects to (used to aim
  its placement — e.g. a motor driver near its motor connector vs. the MCU).

### Storage (edit this directly, or in the GUI)

A sidecar JSON next to the board: `<board>.dg-place.json`. Never touches the
`.kicad_pcb`. Shape:

```json
{
  "components": {
    "U18": { "type": "subsystem_anchor", "parents": ["J1"], "name": "BUCK1" },
    "CBST": { "type": "satellite", "parents": ["U18"], "name": "BUCK1 bootstrap" },
    "CO1":  { "type": "satellite", "parents": ["U18"], "name": "+12V bulk (BUCK1 in)" }
  }
}
```

Only the keys you set override the inference; omit a component to accept the
inferred values. Edits made in the GUI's **Component table…** window write here,
and an agent editing this file is read on the next open / Re-infer.

## Why the table needs you (the design agent)

dg-router auto-infers a first pass from the netlist (part refs, values, and which
nets connect to what). That gets the *functionally-named* parts right — anything
on a chip-specific net (`BK1_BST` → "BUCK1 BST", `BK1_FB` → "BUCK1 FB"). **But a
decoupling cap on a shared power rail (`+3V3`, `+12V`, `GND`) cannot be attributed
to a specific chip from a flat netlist** — every chip shares that rail. The
inference will guess (by rail → regulator, or leave it unparented), and it will
often be wrong or vague.

That's the part only the schematic/design intent knows. If you designed this
board: for each such passive, set its `parents` to the chip it actually decouples
and give it a real `name` ("U19 IMU 3V3 decoupling", not "+3V3 decoupling").
Good names describe **function + what it serves**, so a human reading the table
knows what each passive is for.

## CLI (headless — the same engine the GUI uses)

    KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
    cd <dg-router repo>;  BOARD=/path/to/board.kicad_pcb

    $KPY headless.py "$BOARD" --classify           # print the inferred table
    $KPY headless.py "$BOARD" --classify-write     # write the sidecar (first pass)
    # then edit <board>.dg-place.json to correct types / parents / names

    $KPY headless.py "$BOARD" --place-subsystems   # place subsystem anchors -> copy + PNG

The intended loop for a design agent: `--classify-write` to seed the sidecar,
then rewrite it with correct parents + human names from your schematic knowledge,
then hand back to the user to place in the GUI.

## GUI flow (Place tab)

1. **Component table…** — verify/fix every part's type, parents, name (wide,
   editable window; persists to the sidecar).
2. **Subsystems** list (by name) — select one (or several).
3. **Place anchor** → the subsystem's IC drops near its parent anchors; nudge it.
4. **Place satellites** → its passives nestle against the pins they serve.
5. **All anchors / All satellites** — bulk-place everything unplaced of a kind.
6. Accept to move the footprints (Reject discards; nothing saved until Cmd+S).
