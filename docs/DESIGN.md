# dg-router — Design & Direction

## What it is
A **steerable autorouter (and, in Phase 2, placer) for KiCad**. It is an
autorouter — but not a fire-and-forget black box. It is driven, step by step,
by a strategist.

**The thesis:** a strategist (Claude *or* the user) is good at routing
*strategy* — what to route, in what order, with what preferences, what to keep
clear — but bad at the mechanical execution. Autorouters are the reverse.
dg-router integrates the two: the strategist makes the calls, the router
executes them obediently. That combination is the product.

## Principles
- **Shared state is the `.kicad_pcb` file.** No new formats or databases.
- **Two equal drivers.** Claude via the CLI (`headless.py` = the live API) and
  the human via the plugin GUI. Same core, same operations. The user can hand
  the wheel to Claude ("roll, I'll watch") or drive by hand.
- **Roll our own router. Pure Python + pcbnew.** No node, no Java, no external
  autorouter. (freerouting rejected: Java, crashed on launch, deprecated out of
  KiCad. No modern non-Java engine is compelling enough to depend on.)
- **The router is a competent, steerable executor — not a global genius.** The
  driver supplies global strategy (order, keepouts, "route these first, clear
  this channel"). This is what keeps the router's job tractable.
- **Visualization is the backbone, not a feature.** It is how the human signs
  off and how Claude reviews in the loop (rendered PNGs). It is reused wholesale
  by Phase 2. Never rip it out.

## Non-negotiables (learned the hard way)
- Never leave geometry or artifacts on the user's board.
- Never short. Fail cleanly and say why, rather than shorting.
- Everything responds instantly — loading states, progress bars, no
  indeterminate waits.
- When the user says "let's talk," talk (prose, not menus).

## Router (Phase 1)
Octilinear grid A*, multi-layer with vias, netclass-aware, per-gap routing
(completes partial nets, never restarts them), writes into the board on Accept.

Steering knobs — set identically from the GUI or from `job.json` / CLI flags:
- **Route on** — allowed copper layers (checkboxes).
- **Prefer** — a cost bias toward one allowed layer ("route F+B, prefer B" =
  use front only when back won't do).
- **Via cost** — slider.
- **Objective / style** (radio) — different weightings of the same cost terms:
  - *Most direct* — shortest, fewest bends.
  - *Follow existing* — hug parallel / same-net / bus tracks.
  - *Hug edges* — bias toward the board perimeter.
  - ***Least obtrusive* (default)** — consume as little useful territory as
    possible: hug edges and existing copper, avoid slicing through open regions
    that future nets / fanout / planes will want. This is the antidote to
    "walling off a chip," and the right default for a multi-pass strategist.
- **Keepouts / avoid regions** — so the driver can reserve space (fanout,
  channels) the router must route around.

Fails cleanly (never shorts); reports the reason per net.

## Route workflow
1. Driver selects nets (+ style / layers / via / keepouts). **Select all / none**
   available.
2. **Route** → runs, recording the search and the result.
3. **Watch** — record-then-replay playback (timer-driven, safe; no `SafeYield`),
   with a **determinate progress bar** (per-net while routing, timeline while
   replaying).
4. Result highlighted → **Accept / Reject / Try again**. Accept keeps it (user
   saves in KiCad); the next pass sees accepted routes as obstacles (sequential
   passes). Try again re-routes with jitter for a different solution. After
   Accept/Reject the selection resets, re-cocked for the next pass.

## Visualization backbone
- Board rendered to a **full-resolution SVG → bitmap once**, cached. Zoom/pan
  *within* it fluidly (visible-sub-region blit — never regenerate on interaction).
- **Ratlines of selected/checked nets highlighted** in the preview (checked
  bright, the active one brighter). Overlays live in the preview, never on the
  board.
- **Live routing playback + progress bar.**
- **Result highlighted for sign-off.**
- **CLI emits PNG renders Claude reads automatically** to review and iterate.
- Because we generate the preview content, we can render the exact composite to
  a PNG for self-verification (catches visual regressions without a live GUI).

## Driving from Claude
- A **"Driving from Claude" button** in the plugin opens a window of markdown
  explaining the CLI / API, so the user can paste it to Claude and say "go."
- `headless.py` is the live API: select nets by name or selector, set
  style/layers/via/keepouts, route, accept, and emit renders — everything the
  GUI does. Claude runs it, reads the renders, iterates.
- **Shared selector language** (with placement): `--scope <subsystem|board>
  --tier <tier>` or explicit refs (`U7`), plus net names.

## Phase 2 — Placement (future, same repo & plugin, second tab)
Same philosophy: human defines topology and places what matters; algorithms
optimize narrow decisions; the strategist directs order and reviews renders.
Shared state is the board file.
- **Tiers** (custom footprint field): `board-anchor` (connectors/MCU, human
  places), `sub-anchor` (key chip of a subsystem), `satellite` (passives).
- **Subsystem = schematic sheet.** **Locking** is the ratchet: placer/router
  never touch locked items.
- `place --scope X --tier Y`: sub-anchors placed space-aware (external ratsnest
  pull + local free area + soft reservations); satellites placed by affinity
  (near served pin, minimize ratsnest, trial-route close calls); then a tidy
  pass (rows, snap grid, equal gaps) judged by renders.
- Reuses Phase 1: board model, costmap, ratsnest, SVG/PNG renderer, DRC loop,
  router (for trial-routing candidates).

## Build order (now)
1. **Visualization backbone** — full-res cached preview, fluid zoom, ratline
   highlight, PNG export. (Load-bearing for everything; do first.)
2. **Live routing playback + progress bar.**
3. **Router steering** — route-on/prefer layers, style radio (incl. least
   obtrusive keepout behavior), keepouts, select all/none.
4. **"Driving from Claude" doc button + CLI API polish.**
5. Later: Phase 2 placement.
