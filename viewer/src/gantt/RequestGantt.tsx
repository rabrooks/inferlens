/**
 * Request Gantt: every finished request as a bar from arrival to finish,
 * on the engine timeline's shared time view and cursor.
 *
 * Requests are packed into lanes by arrival (a lane never holds two
 * overlapping requests — the Perfetto export uses the same rule). Phases
 * render *as reported*: right-aligned to finish, ordinal blue ramp
 * queued → prefill → decode, with the unattributed remainder at the head
 * as a thin gray whisker. Phases are never stretched to fill e2e.
 * Encoding rules: viewer/DESIGN.md.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Incident } from '../analysis/incidents'
import { phaseSegments, type Phase, type RequestAnalysis, type RequestAttr } from '../analysis/requests'
import { timeAxis } from '../timeline/draw'
import { rgba, usePalette, type Palette } from '../timeline/theme'
import { GUTTER, RIGHT, tOf, usePanZoom, xOf, type TimeView, type View } from '../timeline/view'
import { fmtS, shortId } from './format'

const TOP = 8
const AXIS = 26
/** Target plot height; row pitch adapts to the lane count within bounds. */
const TARGET_PLOT_H = 360
const MIN_PITCH = 4
const MAX_PITCH = 14
const HIT_SLOP_PX = 4
const TOOLTIP_W = 260
const DOUBLE_CLICK_MS = 500

export function RequestGantt({
  analysis,
  incidents,
  tv,
  selected,
  onSelect,
}: {
  analysis: RequestAnalysis
  incidents: Incident[]
  tv: TimeView
  selected: RequestAttr | null
  onSelect: (r: RequestAttr | null) => void
}) {
  const { view, hoverT } = tv
  const [width, setWidth] = useState(0)
  const [hoverY, setHoverY] = useState<number | null>(null)
  const palette = usePalette()
  const wrap = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)

  const pitch = Math.min(MAX_PITCH, Math.max(MIN_PITCH, Math.floor(TARGET_PLOT_H / Math.max(1, analysis.lanes))))
  const plotH = analysis.lanes * pitch
  const height = TOP + plotH + AXIS
  const byLane = useMemo(() => {
    const lanes: RequestAttr[][] = Array.from({ length: analysis.lanes }, () => [])
    for (const r of [...analysis.requests].sort((a, b) => a.arrivalS - b.arrivalS)) lanes[r.lane].push(r)
    return lanes
  }, [analysis])
  const anyPreempted = analysis.requests.some((r) => (r.numPreemptions ?? 0) > 0)

  useEffect(() => {
    const el = wrap.current
    if (!el) return
    const ro = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const plotW = Math.max(0, width - GUTTER - RIGHT)

  function hit(x: number, y: number | null): RequestAttr | null {
    if (y === null || plotW <= 0) return null
    const lane = Math.floor((y - TOP) / pitch)
    if (lane < 0 || lane >= byLane.length) return null
    const t = tOf(x, view, plotW)
    const slop = (HIT_SLOP_PX / plotW) * (view.t1 - view.t0)
    return byLane[lane].find((r) => t >= r.arrivalS - slop && t <= r.finishS + slop) ?? null
  }

  const hoverX = hoverT === null || plotW <= 0 ? null : xOf(hoverT, view, plotW)
  const hovered = hoverX === null ? null : hit(hoverX, hoverY)

  useEffect(() => {
    const c = canvas.current
    if (!c || !palette || plotW <= 0) return
    const dpr = window.devicePixelRatio || 1
    c.width = width * dpr
    c.height = height * dpr
    const ctx = c.getContext('2d')
    if (!ctx) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    draw(ctx, { width, height, plotW, plotH, pitch, view, palette, byLane, incidents, selected, hovered })
  }, [width, height, plotW, plotH, pitch, view, palette, byLane, incidents, selected, hovered])

  // A double-click (reset zoom) starts with a click; remember what was
  // selected before it so the reset doesn't also change the selection.
  // Only a click that isn't the second of a pair records the "before" state.
  const beforeClick = useRef<{ sel: RequestAttr | null; at: number }>({ sel: null, at: 0 })
  const pz = usePanZoom(canvas, plotW, tv, (x, y) => {
    const now = performance.now()
    if (now - beforeClick.current.at > DOUBLE_CLICK_MS) beforeClick.current.sel = selected
    beforeClick.current.at = now
    onSelect(hit(x, y))
  })
  const inView = useMemo(
    () => analysis.requests.filter((r) => r.finishS >= view.t0 && r.arrivalS <= view.t1).length,
    [analysis, view],
  )

  return (
    <section className="rounded border border-edge bg-surface">
      <header className="flex items-center gap-3 border-b border-grid px-2 py-1">
        <h2 className="text-xs font-medium text-ink-secondary">Requests</h2>
        <span className="font-data text-xs text-ink-muted">
          {inView.toLocaleString()} of {analysis.requests.length.toLocaleString()} in view · {analysis.lanes} lanes,
          packed by arrival
        </span>
        <span className="ml-auto text-xs text-ink-muted">click a request for its breakdown</span>
      </header>
      <div
        ref={wrap}
        className="relative outline-none focus-visible:ring-1 focus-visible:ring-axis"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && selected) {
            onSelect(null)
            e.preventDefault()
          } else pz.onKeyDown(e)
        }}
        aria-label="Request Gantt. Arrow keys pan, plus and minus zoom, 0 resets, Escape clears the selection. The request table beside it lists the same requests."
      >
        <canvas
          ref={canvas}
          role="img"
          aria-label={`Gantt of ${analysis.requests.length} requests in ${analysis.lanes} lanes; phases queued, prefill, decode as reported.`}
          style={{
            width: '100%',
            height,
            display: 'block',
            cursor: pz.dragging ? 'grabbing' : hovered ? 'pointer' : 'crosshair',
          }}
          {...pz.handlers}
          onPointerMove={(e) => {
            setHoverY(e.clientY - e.currentTarget.getBoundingClientRect().top)
            pz.handlers.onPointerMove(e)
          }}
          onPointerLeave={() => {
            setHoverY(null)
            pz.handlers.onPointerLeave()
          }}
          onDoubleClick={() => {
            onSelect(beforeClick.current.sel)
            pz.handlers.onDoubleClick()
          }}
        />
        <Legend anyPreempted={anyPreempted} />
        {hoverX !== null && (
          <div
            className="pointer-events-none absolute bg-ink-muted"
            style={{ left: hoverX, top: TOP - 4, width: 1, height: plotH + 8 }}
          />
        )}
        {hovered && hoverX !== null && hoverY !== null && (
          <Tooltip r={hovered} x={hoverX} y={hoverY} width={width} height={height} />
        )}
      </div>
    </section>
  )
}

// --- drawing ---------------------------------------------------------------

interface DrawArgs {
  width: number
  height: number
  plotW: number
  plotH: number
  pitch: number
  view: View
  palette: Palette
  byLane: RequestAttr[][]
  incidents: Incident[]
  selected: RequestAttr | null
  hovered: RequestAttr | null
}

const PHASE_TOKEN = {
  queued: 'phase-queued',
  prefill: 'phase-prefill',
  decode: 'phase-decode',
} as const

function draw(ctx: CanvasRenderingContext2D, a: DrawArgs) {
  const { width, height, plotW, plotH, pitch, view, palette: p } = a
  ctx.clearRect(0, 0, width, height)
  const x = (t: number) => xOf(t, view, plotW)
  const barH = pitch - (pitch >= 8 ? 2 : 1)

  timeAxis(ctx, p, view, plotW, TOP - 4, TOP + plotH + 4)
  ctx.save()
  ctx.beginPath()
  ctx.rect(GUTTER, 0, plotW, height)
  ctx.clip()

  // Incident windows: the same recessive wash as the engine timeline.
  for (const inc of a.incidents) {
    ctx.fillStyle = rgba(p.ink, 0.05)
    ctx.fillRect(x(inc.startS), TOP - 4, x(inc.endS) - x(inc.startS), plotH + 8)
  }

  // With a selection, everything else recedes so the one request reads.
  const dim = a.selected ? 0.28 : 1
  a.byLane.forEach((lane, li) => {
    const y = TOP + li * pitch
    for (const r of lane) {
      if (r.finishS < view.t0 || r.arrivalS > view.t1) continue
      const alpha = a.selected && r.index !== a.selected.index ? dim : 1
      drawRequest(ctx, p, r, x, y, barH, alpha)
    }
  })

  // Hover lifts the bar with a hairline ring; selection keeps full color.
  const ring = (r: RequestAttr, color: [number, number, number]) => {
    const y = TOP + r.lane * pitch
    const x0 = Math.floor(x(r.arrivalS)) - 1.5
    const x1 = Math.ceil(x(r.finishS)) + 1.5
    ctx.strokeStyle = rgba(color)
    ctx.lineWidth = 1
    ctx.strokeRect(x0, y - 1.5, x1 - x0, barH + 3)
  }
  if (a.selected) ring(a.selected, p.ink)
  if (a.hovered && a.hovered.index !== a.selected?.index) ring(a.hovered, p['ink-secondary'])
  ctx.restore()
}

function drawRequest(
  ctx: CanvasRenderingContext2D,
  p: Palette,
  r: RequestAttr,
  x: (t: number) => number,
  y: number,
  h: number,
  alpha: number,
) {
  const segs = phaseSegments(r)
  segs.forEach((s, i) => {
    const x0 = x(s.t0)
    // Every request stays visible (≥1px) even when a phase is sub-pixel.
    const x1 = Math.max(x(s.t1), i === segs.length - 1 ? x0 + 1 : x0)
    if (s.phase === 'unattributed') {
      ctx.fillStyle = rgba(p['ink-muted'], 0.7 * alpha)
      ctx.fillRect(x0, y + Math.floor(h / 2) - 1, x1 - x0, 2)
      return
    }
    ctx.fillStyle = rgba(p[PHASE_TOKEN[s.phase]], alpha)
    ctx.fillRect(x0, y, x1 - x0, h)
    // 1px surface gap between phases wide enough to show it.
    const next = segs[i + 1]
    if (next && x1 - x0 >= 4 && x(next.t1) - x(next.t0) >= 4) {
      ctx.fillStyle = rgba(p.surface)
      ctx.fillRect(Math.round(x1) - 0.5, y, 1, h)
    }
  })
  // Preempted requests (v0.4 traces): a red tick at the arrival end.
  if ((r.numPreemptions ?? 0) > 0) {
    ctx.fillStyle = rgba(p.preempt, alpha)
    ctx.fillRect(x(r.arrivalS) - 2, y - 1, 2, h + 2)
  }
}

// --- DOM overlays ----------------------------------------------------------

const PHASES: { phase: Exclude<Phase, 'unattributed'>; label: string }[] = [
  { phase: 'queued', label: 'queued' },
  { phase: 'prefill', label: 'prefill' },
  { phase: 'decode', label: 'decode' },
]

function Legend({ anyPreempted }: { anyPreempted: boolean }) {
  return (
    <div className="pointer-events-none absolute top-0 left-2" style={{ width: GUTTER - 16 }}>
      <div className="mt-1.5 text-xs font-medium text-ink-secondary">Phases</div>
      <div className="mt-0.5 flex flex-col gap-0.5 text-[11px] text-ink-muted">
        {PHASES.map(({ phase, label }) => (
          <span key={phase} className="flex items-center gap-1.5">
            <span className="inline-block h-2 w-3 rounded-sm" style={{ background: `var(--il-phase-${phase})` }} />
            {label}
          </span>
        ))}
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-0.5 w-3 bg-ink-muted opacity-70" />
          unattributed
        </span>
        {anyPreempted && (
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-2.5 w-0.5" style={{ background: 'var(--il-preempt)' }} />
            preempted
          </span>
        )}
        <span className="mt-1 leading-snug">as reported — phases don't fill e2e</span>
      </div>
    </div>
  )
}

function Tooltip({ r, x, y, width, height }: { r: RequestAttr; x: number; y: number; width: number; height: number }) {
  const left = x + 14 + TOOLTIP_W > width ? x - TOOLTIP_W - 14 : x + 14
  const top = Math.min(Math.max(4, y - 20), height - 150)
  const rows: [string, number, string][] = [
    ['queued', r.queuedS, 'phase-queued'],
    ['prefill', r.prefillS, 'phase-prefill'],
    ['decode', r.decodeS, 'phase-decode'],
    ['unattributed', r.unattributedS, 'ink-muted'],
  ]
  return (
    <div
      className="pointer-events-none absolute rounded border border-edge bg-surface px-2 py-1.5 shadow-sm"
      style={{ left, top, width: TOOLTIP_W }}
    >
      <div className="font-data text-xs text-ink-secondary">{shortId(r.requestId)}</div>
      <div className="mt-0.5 font-data text-xs text-ink">
        <span className="font-semibold">{fmtS(r.e2eS)}</span>{' '}
        <span className="text-ink-secondary">e2e · {r.finishReason}</span>
      </div>
      <dl className="mt-1 grid grid-cols-[auto_1fr] items-baseline gap-x-2 gap-y-0.5">
        {rows.map(([label, v, color]) => (
          <div key={label} className="contents">
            <dd className="text-right font-data text-xs font-semibold text-ink tabular-nums">{fmtS(v)}</dd>
            <dt className="flex items-center gap-1.5 text-xs text-ink-secondary">
              <span className="inline-block h-0.5 w-2.5 rounded" style={{ background: `var(--il-${color})` }} />
              {label}
            </dt>
          </div>
        ))}
      </dl>
      {r.incidents.length > 0 && (
        <div className="mt-1 border-t border-grid pt-1 text-xs text-ink-secondary">
          overlapped incident {r.incidents.map((i) => `#${i}`).join(', ')}
        </div>
      )}
    </div>
  )
}
