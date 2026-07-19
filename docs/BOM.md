# Driving dg-router BoM + CPL export (the BoM tab)

dg-router's **BoM** tab generates the two files JLC PCBA needs:

- **BoM CSV** — `Comment,Designator,Footprint,LCSC Part #` (identical parts grouped
  onto one line, designators comma-joined).
- **CPL CSV** (component placement list / pick-and-place) —
  `Designator,Mid X,Mid Y,Layer,Rotation`.

Everything the CPL needs comes straight from the board (the plugin just placed it).
The two things a flat board *doesn't* know — **which LCSC part** each component is,
and **JLC's rotation correction** — live in a sidecar you (or an agent) fill in.

## The sidecar: `<board>.dg-bom.json`

Never touches the `.kicad_pcb`. Only keys you set override; omit anything you don't
need. Shape:

```json
{
  "components": {
    "U18": { "lcsc": "C2827424", "rotation_offset": 0 },
    "D1":  { "lcsc": "C2895", "rotation_offset": 180, "comment": "SS34 Schottky" },
    "H1":  { "dnp": true }
  },
  "by_value": {
    "10uF/C_1210_3225Metric": { "lcsc": "C1525" },
    "100nF/C_0402_1005Metric": { "lcsc": "C1525" }
  }
}
```

- **`by_value`** is the efficient fill path: one LCSC assignment covers every part
  that shares a `"<value>/<footprint-lib-item>"` key. A board with 79 BoM lines
  usually has far fewer distinct value/footprint keys, so filling `by_value`
  assigns most parts at once.
- **`components`** entries override `by_value` for a specific ref.
- **`dnp: true`** drops a part from BOTH files (also honored: the KiCad
  DNP / exclude-from-BOM footprint attributes, and 0-pad parts like fiducials).
- **`comment`** overrides the BoM "Comment" (defaults to the footprint value).
- **`rotation_offset`** (degrees) is ADDED to the part's rotation in the CPL.

## If you're the agent filling this in

1. Open the BoM tab → it flags every line **"— MISSING"** an LCSC part #.
2. For each distinct value/footprint, look up the LCSC catalog number and add a
   `by_value` entry. Use `components` only for one-offs that differ.
3. Save the sidecar, click **Reload** in the tab.

## The two JLC gotchas (read this)

- **Rotation is the field most likely to be wrong at the fab.** JLC defines its own
  per-package "0°" that often disagrees with KiCad's footprint orientation
  (electrolytics, diodes/tantalums, SOT/SOIC ICs, many connectors). When a part
  comes back rotated on the assembled board, DON'T rotate the footprint — set
  `rotation_offset` for that ref (or its value/footprint) in the sidecar.
  Bottom-side parts use `(180 - rotation) % 360` before the offset is applied.
- **Coordinates use the board's aux/drill origin.** Mid X/Y are the component
  centroid relative to `GetDesignSettings().GetAuxOrigin()` (falls back to absolute
  board mm if unset), with Y negated to JLC's Y-up convention — matching KiCad's own
  position-file exporter row-for-row. Set a place origin in KiCad and the numbers
  track it automatically.

## CLI / headless

The same functions the tab uses live in `dg_router_plugin/bom.py`:
`bom_rows(board, path)`, `cpl_rows(board, path)`, `write_bom_csv(board, path, out)`,
`write_cpl_csv(board, path, out)`, and `load_bom(path)` / `save_bom(path, data)` for
the sidecar.
