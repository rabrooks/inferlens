# Viewer design system

The viewer is an instrument panel for trace files: dense, quiet chrome around
canvas-rendered tracks, in both light and dark themes from the same tokens.
This file is the contract for its visual decisions — read it before adding a
color or a chart.

## Principles

- **Color follows the entity, never the chart.** A hue means the same thing
  everywhere it appears: blue = active work, yellow = waiting/dead time,
  aqua = KV-cache activity, red = preemption, violet = prefix-cache hits.
  A new chart reuses the entity's color; a new color means a new entity.
- **Status colors are reserved.** `collector_gap` regions wear the warning
  color; KV saturation (usage = 1.0) wears critical. Status colors are never
  used for a data series, and never appear without an icon or label.
- **Text wears ink tokens, never series colors.** A colored mark next to the
  label carries identity.
- **Both themes are first-class.** Every color token is defined once with
  `light-dark()` in `src/index.css`; the theme toggle only flips
  `color-scheme`. Canvas code reads tokens via `getComputedStyle` — never
  hard-code a hex in a component or a render loop.
- **Phases render as reported.** Queued/prefill/decode spans come from the
  engine and do not necessarily sum to e2e latency (re-queue time after
  preemption is not fully attributed). Never stretch or normalize them.

## Data color assignments

| Entity | Token | Light | Dark |
| --- | --- | --- | --- |
| Running requests / active work | `--il-running` | `#2a78d6` | `#3987e5` |
| Waiting queue / dead time | `--il-waiting` | `#eda100` | `#c98500` |
| KV-cache usage **and evictions** | `--il-kv` | `#1baf7a` | `#199e70` |
| Preemption events | `--il-preempt` | `#e34948` | `#e66767` |
| Prefix-cache hit rate | `--il-cachehit` | `#6250d6` | `#9085e9` |
| Phase: queued | `--il-phase-queued` | `#86b6ef` | `#184f95` |
| Phase: prefill | `--il-phase-prefill` | `#3987e5` | `#3987e5` |
| Phase: decode | `--il-phase-decode` | `#184f95` | `#86b6ef` |

Two deliberate choices here:

- **Request phases are an ordinal ramp, not three hues.** Queued → prefill →
  decode is a sequence, so it takes one blue ramp with monotone lightness —
  order survives every color-vision deficiency by construction. The recessive
  end (queued, the "nothing is happening" phase) sits nearest the surface in
  each theme, so busy bars are salient and stalled bars recede.
- **Evictions wear the KV color, not their own hue.** An eviction *is*
  KV-cache activity; it renders as a volume histogram in its own labeled
  lane, so lane + mark shape distinguish it from the usage area. This keeps
  the timeline at four hues, which is what makes the dark palette pass CVD
  validation.

## Validation status and obligations

Palettes were validated with the dataviz six-check validator (lightness band,
chroma floor, Machado-2009 CVD separation, surface contrast) per chart set,
per theme, against surfaces `#fcfcfb` / `#1a1a19`. All sets pass, with two
standing obligations:

- **Dark timeline: red↔aqua ΔE 9.7 (protan)** is in the 8–12 floor band —
  legal only with secondary encoding. Obligation: preemption and eviction
  marks live in separate, directly-labeled lanes with distinct mark shapes
  (ticks vs. bars). Do not overlay them on one lane.
- **Light theme: yellow (2.11:1) and aqua (2.74:1) are sub-3:1 fills** —
  relief required. Obligation: lanes are directly labeled, and hover
  tooltips expose exact values everywhere these fills appear.

Re-run validation whenever a chart gains a series or a token changes hue:
the check is per *rendered combination*, not per palette.

## Chrome, type, layout

- Chrome tokens (`--il-page`, `--il-surface`, ink scale, hairline grid/axis)
  are in `src/index.css`. The chrome stays recessive: hairline borders,
  muted labels, no decoration that competes with data.
- UI text is the system sans (`--font-ui`); all numerals, IDs, and axis
  labels use the monospace stack (`--font-data`) with tabular figures.
- Layout is stacked full-width tracks sharing one time axis: engine timeline,
  request Gantt, KV/prefix-cache panel, with a detail sidebar on request
  selection.
- The signature interaction is the **shared time cursor**: one crosshair
  spanning every track, so cause (KV saturation, preemption ticks) and
  effect (queue growth, stalled Gantt bars) line up under your pointer.

## Engine timeline

Stacked lanes on one shared time axis, each with its own y-scale (never two
scales on one plot):

| Lane | Marks | Scale |
| --- | --- | --- |
| Requests | running (blue) and waiting (yellow): 2px line over a 10% wash | shared count axis |
| KV cache usage | aqua line + wash; a 3px critical strip on top where usage ≥ 95% | fixed 0–100% |
| Preemptions | red 2px ticks per 3px column group | per second |
| KV evictions | aqua bars, 6px + 2px surface gap, 2px rounded top | blocks per second |

- **Levels show the peak per pixel column** and carry forward through
  columns with no snapshot (engine state persists between snapshots). A
  mean would hide a one-step spike.
- **Events are per-second rates**, so bar and tick heights don't change
  with zoom.
- **Incident windows are an ink wash** (`--il-ink` at 5%, 8% in the label
  row), not a new hue: they annotate time ranges and must stay recessive
  under the data. They come from the same rules as `inferlens explain`
  (`src/analysis/incidents.ts` mirrors `inferlens/analysis.py`; a test
  cross-checks the two on the real fixture).
- **Collector gaps** are 2px warning-color rules across every lane with a
  ⚠ in the label row, and the tooltip says data is missing there.
- **Table view** (per-second bins) is the tooltip-free twin of the chart.
- Interaction: scroll zooms around the pointer, drag pans, double-click
  resets; with the timeline focused, arrow keys pan, `+`/`-` zoom, `0`
  resets.

## Request Gantt and request detail

The Gantt shares the engine timeline's view and cursor (`src/timeline/view.ts`):
zooming or panning either track moves both, and the crosshair spans both.

- **One bar per finished request**, arrival → finish, packed into lanes by
  arrival (a lane never holds two overlapping requests — the same rule as
  `inferlens export`). Row pitch adapts to the lane count (4–14 px).
- **Phases as reported, right-aligned to finish:** decode ends at finish,
  prefill before it, queued before that. The unattributed remainder
  (e2e − phases) sits at the head as a 2px gray whisker, never as a colored
  block, and phases are never stretched to fill e2e. A 1px surface gap
  separates phases wide enough to show it.
- **Preempted requests** (traces that record per-request counts) carry a
  2px preemption-red tick at arrival — a tick, as on the timeline.
- **Selection** dims every other bar to ~30% and rings the selected one in
  ink; the timeline brackets its arrival and finish across every lane, so
  its queue time lines up with the KV saturation and preemptions that
  caused it. Hover rings in secondary ink. Escape or ✕ clears.
- **Request detail** (sidebar) is the UI twin of
  `inferlens explain --request ID`: same numbers, same rules
  (`src/analysis/requests.ts` mirrors `inferlens/analysis.py`; a test
  cross-checks every request on the real fixture). Its part-to-whole is a
  tall segmented bar in time order; labels inside fills are near-black or
  white by the fill's luminance *per theme* (the ramp flips ends).
- **Request table** (sortable, searchable by id) is the Gantt's
  tooltip-free twin and the keyboard path to selection.

## KV / prefix-cache panel

Same shared view, cursor, incident wash, gap rules, and selected-request
brackets as the other tracks. It adds what the engine timeline doesn't
already show (KV usage and eviction volume live there):

| Lane | Marks | Scale |
| --- | --- | --- |
| Prefix-cache lookups | violet bars: hit tokens solid from the baseline, missed tokens a lighter step (32%) above a 2px surface gap | tokens / s |
| KV blocks stored | aqua bars | blocks / s, shared with the next lane |
| Recomputed after eviction | aqua bars | blocks / s, same scale as stored |

- **Recomputed after eviction** = a stored block whose (medium, group, hash)
  was evicted earlier and not stored since. It is KV work done twice
  because capacity ran out. Replay order is placement time, then batch
  `seq`, then file order: events in one publisher batch share `t` and
  `seq`, and replaying a batch's stores before its removals undercounts.
- Stored and recomputed share one scale so the recomputed share reads as a
  fraction of the bar above it.
- A hit-*rate* line was rejected: lookups are bursty, so per-pixel ratios
  swing between 0 and the true rate and the line fragments. The rate is in
  the header, the tooltip (per second), and the table view.
- The light miss step and light aqua are sub-3:1 fills: lanes are directly
  labeled and the table view carries every value (relief obligation).
- A trace without KV events (no subscriber) shows only the lookups lane,
  with a note saying how to record block activity.
