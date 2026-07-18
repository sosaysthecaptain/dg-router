# dg-router

AI-drivable incremental autorouter for KiCad. **Current status: the visual
outline (Milestone 0/0.1) — a KiCad plugin that installs, shows choices, and
previews the selected net highlighted on the board render, live, in-window. It
does not route anything yet.**

![preview](docs/preview.png)

## Layout

```
dg_router_plugin/        KiCad Action Plugin (SWIG/pcbnew) — the toolbar button
  __init__.py            registers the plugin
  action_plugin.py       ActionPlugin subclass (thin)
  dialog.py              wx dialog — choices on the left, live net preview on the right
  shim.py                core logic (no wx): list nets, render, net geometry, coord map, emit job
  icon.png               generated toolbar icon
headless.py              trigger the same logic from code (no GUI)
tools/generate_icon.py   regenerates icon.png (pure stdlib)
install.sh               symlink the plugin into KiCad's plugins dir
```

## Install

```
./install.sh
```
Then in KiCad **PCB Editor → Tools → External Plugins → Refresh Plugins**, and
click the **dg-router** button on the toolbar.

- **Click** a net → its pads, existing copper, and an approximate ratsnest
  highlight on the board render (rendered in-process via `wx.svg`, no external
  deps).
- **Check** nets, set prefer-layer / via cost / edge-hug / follow-existing, then
  **Emit job.json** → the interchange the future TS core consumes, written to
  `dg-router-out/` next to the board.
- **Open full SVG** opens the full-resolution render in a browser.

## Headless (trigger from code)

```
KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
$KPY headless.py path/to/board.kicad_pcb --list
$KPY headless.py path/to/board.kicad_pcb --render
$KPY headless.py path/to/board.kicad_pcb --route SPI_CLK SPI_MOSI --layer B.Cu --emit
```

## How the highlight lines up

`kicad-cli pcb export svg --page-size-mode 2` plots geometry in **mm** in a
viewBox whose origin is the board plot origin. That origin is the Edge.Cuts
bounding-box center minus half the viewBox size (the viewBox excludes the
~0.05mm/side edge line width the pcbnew bbox includes). So
`svg_mm = pcb_mm − plot_origin`, sub-pixel accurate. Pad/track/via geometry
comes straight from `pcbnew`.

## Roadmap

1. ✅ M0 — plugin shim: install, choices, live net-highlight preview, no-op
2. S-expr parser + board model (TS core, node v22)
3. SVG renderer + HTML viewer
4. Costmap + heatmap overlay
5. A* core + job spec + writeback
6. DRC loop + tscircuit fill pass
7. KiCad IPC (kipy) plugin shim

Runtime for the TS core is **node** (v22) unless a compelling reason to switch
to Bun emerges.

## License

MIT
