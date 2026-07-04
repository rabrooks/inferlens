# Viewer design system

The viewer is an instrument panel for trace files: dense, quiet chrome around
canvas-rendered tracks, in both light and dark themes from the same tokens.
This file is the contract for its visual decisions — read it before adding a
color or a chart.

## Principles

- **Color follows the entity, never the chart.** A hue means the same thing
  everywhere it appears: blue = active work, yellow = waiting/dead time,
  aqua = KV-cache activity, red = preemption, green = prefix-cache hits.
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
| Prefix-cache hit rate | `--il-cachehit` | `#008300` | `#008300` |
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
