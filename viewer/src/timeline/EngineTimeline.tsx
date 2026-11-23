/**
 * Engine timeline: stacked lanes on one shared time axis.
 *
 * Lanes (each with its own y-scale — never two scales on one plot):
 * requests (running vs waiting), KV usage, preemption rate (ticks), KV
 * eviction rate (bars). Incident windows from the `inferlens explain`
 * rules are a recessive wash across every lane; collector gaps wear the
 * reserved warning color. Encoding rules and obligations: viewer/DESIGN.md.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'
import { binTrace, DEFAULT_BIN_S, findIncidents, KV_SATURATED, type Incident } from '../analysis/incidents'
import type { Trace } from '../trace/types'
import { columns, niceScale, prepare, rates, stateAt, type Columns } from './series'
import { rgba, usePalette, type Palette } from './theme'

const GUTTER = 156
const RIGHT = 12
const INCIDENT_ROW = 22
const LANE_GAP = 18
const AXIS = 26
const TICK_GROUP_PX = 3
const BAR_PX = 8 // 6px bar + 2px surface gap
const MIN_VIEW_S = 0.5
const TOOLTIP_W = 248

interface Lane {
  key: 'requests' | 'kv' | 'preempt' | 'evict'
  title: string
  unit: string
  height: number
}

const LANES: Lane[] = [
  { key: 'requests', title: 'Requests', unit: 'count', height: 112 },
  { key: 'kv', title: 'KV cache usage', unit: '%', height: 84 },
  { key: 'preempt', title: 'Preemptions', unit: 'per s', height: 56 },
  { key: 'evict', title: 'KV evictions', unit: 'blocks / s', height: 56 },
]

function laneTops(): number[] {
  let y = INCIDENT_ROW
  return LANES.map((l) => {
    const top = y
    y += l.height + LANE_GAP
    return top
  })
}
const TOPS = laneTops()
const HEIGHT = TOPS.at(-1)! + LANES.at(-1)!.height + AXIS

interface View {
  t0: number
  t1: number
}

export function EngineTimeline({ trace }: { trace: Trace }) {
  const data = useMemo(() => prepare(trace), [trace])
  const bins = useMemo(() => binTrace(trace), [trace])
  const incidents = useMemo(() => findIncidents(bins), [bins])
  const gaps = useMemo(
    () => trace.gaps.map((g) => g.t - trace.start),
    [trace],
  )
  const full: View = useMemo(() => ({ t0: 0, t1: Math.max(data.span, MIN_VIEW_S) }), [data])
  const [view, setView] = useState<View>(full)
  const [width, setWidth] = useState(0)
  const [hoverX, setHoverX] = useState<number | null>(null)
  const [showTable, setShowTable] = useState(false)
  const palette = usePalette()
  const wrap = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const drag = useRef<{ x: number; view: View } | null>(null)

  useEffect(() => setView(full), [full])

  useEffect(() => {
    const el = wrap.current
    if (!el) return
    const ro = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const plotW = Math.max(0, width - GUTTER - RIGHT)
  const cols = useMemo(
    () => (plotW > 0 ? columns(data, view.t0, view.t1, plotW) : null),
    [data, view, plotW],
  )
  const tOf = (x: number) => view.t0 + ((x - GUTTER) / plotW) * (view.t1 - view.t0)

  useEffect(() => {
    const c = canvas.current
    if (!c || !cols || !palette || width === 0) return
    const dpr = window.devicePixelRatio || 1
    c.width = width * dpr
    c.height = HEIGHT * dpr
    const ctx = c.getContext('2d')
    if (!ctx) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    draw(ctx, { width, plotW, view, cols, palette, incidents, gaps })
  }, [cols, palette, width, plotW, view, incidents, gaps])

  // Wheel must be non-passive to stop the page scrolling while zooming.
  useEffect(() => {
    const el = canvas.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      const rect = el.getBoundingClientRect()
      const x = e.clientX - rect.left
      if (x < GUTTER) return
      e.preventDefault()
      // Wheel doesn't fire pointermove; keep the crosshair under the pointer.
      setHoverX(x <= GUTTER + plotW ? x : null)
      setView((v) => zoom(v, full, v.t0 + ((x - GUTTER) / plotW) * (v.t1 - v.t0), Math.exp(e.deltaY * 0.002)))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [full, plotW])

  const localX = (e: ReactPointerEvent) => e.clientX - e.currentTarget.getBoundingClientRect().left

  function onPointerMove(e: ReactPointerEvent<HTMLCanvasElement>) {
    const x = localX(e)
    if (drag.current) {
      const dt = ((drag.current.x - x) / plotW) * (drag.current.view.t1 - drag.current.view.t0)
      setView(clamp({ t0: drag.current.view.t0 + dt, t1: drag.current.view.t1 + dt }, full))
    }
    setHoverX(x >= GUTTER && x <= GUTTER + plotW ? x : null)
  }

  function onKeyDown(e: KeyboardEvent) {
    const span = view.t1 - view.t0
    const mid = (view.t0 + view.t1) / 2
    const pan = (d: number) => setView(clamp({ t0: view.t0 + d, t1: view.t1 + d }, full))
    if (e.key === 'ArrowLeft') pan(-span * 0.1)
    else if (e.key === 'ArrowRight') pan(span * 0.1)
    else if (e.key === '+' || e.key === '=') setView(zoom(view, full, mid, 0.8))
    else if (e.key === '-') setView(zoom(view, full, mid, 1.25))
    else if (e.key === '0') setView(full)
    else return
    e.preventDefault()
  }

  const hoverT = hoverX === null ? null : tOf(hoverX)
  const zoomed = view.t0 > 0 || view.t1 < full.t1

  return (
    <section className="rounded border border-edge bg-surface">
      <header className="flex items-center gap-3 border-b border-grid px-2 py-1">
        <h2 className="text-xs font-medium text-ink-secondary">Engine timeline</h2>
        <span className="font-data text-xs text-ink-muted">
          {view.t0.toFixed(1)}–{view.t1.toFixed(1)} s
          {incidents.length > 0 && ` · ${incidents.length} incident${incidents.length > 1 ? 's' : ''}`}
        </span>
        <span className="ml-auto text-xs text-ink-muted">
          scroll to zoom · drag to pan · double-click to reset
        </span>
        {zoomed && (
          <button
            type="button"
            onClick={() => setView(full)}
            className="rounded px-1.5 text-xs text-ink-secondary hover:bg-page"
          >
            Reset zoom
          </button>
        )}
        <button
          type="button"
          onClick={() => setShowTable((s) => !s)}
          aria-pressed={showTable}
          className="rounded px-1.5 text-xs text-ink-secondary hover:bg-page"
        >
          {showTable ? 'Hide table' : 'Table'}
        </button>
      </header>
      {/* Focusable so the keyboard can zoom/pan, same as pointer users. */}
      <div
        ref={wrap}
        className="relative outline-none focus-visible:ring-1 focus-visible:ring-axis"
        tabIndex={0}
        onKeyDown={onKeyDown}
        aria-label="Engine timeline. Arrow keys pan, plus and minus zoom, 0 resets."
      >
        <canvas
          ref={canvas}
          role="img"
          aria-label={summary(incidents, data.span)}
          style={{ width: '100%', height: HEIGHT, display: 'block', cursor: drag.current ? 'grabbing' : 'crosshair' }}
          onPointerMove={onPointerMove}
          onPointerLeave={() => {
            setHoverX(null)
            drag.current = null
          }}
          onPointerDown={(e) => {
            e.currentTarget.setPointerCapture(e.pointerId)
            drag.current = { x: localX(e), view }
          }}
          onPointerUp={() => {
            drag.current = null
          }}
          onDoubleClick={() => setView(full)}
        />
        <LaneLabels />
        {hoverX !== null && hoverT !== null && (
          <Cursor
            x={hoverX}
            t={hoverT}
            width={width}
            data={data}
            bins={bins}
            incidents={incidents}
            gapNear={gaps.some((g) => Math.abs(g - hoverT) <= (view.t1 - view.t0) / plotW * 3)}
          />
        )}
      </div>
      {showTable && <BinTable bins={bins} incidents={incidents} />}
    </section>
  )
}

function clamp(v: View, full: View): View {
  const span = Math.min(v.t1 - v.t0, full.t1 - full.t0)
  const t0 = Math.min(Math.max(v.t0, full.t0), full.t1 - span)
  return { t0, t1: t0 + span }
}

function zoom(v: View, full: View, at: number, factor: number): View {
  const span = Math.min(Math.max((v.t1 - v.t0) * factor, MIN_VIEW_S), full.t1 - full.t0)
  const frac = (at - v.t0) / (v.t1 - v.t0)
  return clamp({ t0: at - frac * span, t1: at - frac * span + span }, full)
}

function summary(incidents: Incident[], span: number): string {
  const head = `Engine timeline over ${span.toFixed(0)} seconds.`
  if (incidents.length === 0) return `${head} No incidents.`
  const list = incidents
    .map((i) => `incident ${i.id} from ${i.startS.toFixed(0)} to ${i.endS.toFixed(0)} s, ${kindLabel(i)}, ${i.preemptions} preemptions`)
    .join('; ')
  return `${head} ${incidents.length} incidents: ${list}.`
}

function kindLabel(i: Incident): string {
  return i.kind === 'kv_bound' ? 'KV-bound' : 'queue-bound'
}

// --- drawing ---------------------------------------------------------------

interface DrawArgs {
  width: number
  plotW: number
  view: View
  cols: Columns
  palette: Palette
  incidents: Incident[]
  gaps: number[]
}

const DATA_FONT = '10px ui-monospace, SFMono-Regular, Menlo, monospace'

function draw(ctx: CanvasRenderingContext2D, a: DrawArgs) {
  const { width, plotW, view, cols, palette: p } = a
  ctx.clearRect(0, 0, width, HEIGHT)
  const x = (t: number) => GUTTER + ((t - view.t0) / (view.t1 - view.t0)) * plotW
  const lanesTop = TOPS[0]
  const lanesBottom = TOPS.at(-1)! + LANES.at(-1)!.height

  ctx.save()
  ctx.beginPath()
  ctx.rect(GUTTER, 0, plotW, HEIGHT)
  ctx.clip()

  // Incident windows: a recessive ink wash; labels in the incident row.
  ctx.font = DATA_FONT
  ctx.textBaseline = 'middle'
  for (const inc of a.incidents) {
    const x0 = x(inc.startS)
    const x1 = x(inc.endS)
    if (x1 < GUTTER || x0 > GUTTER + plotW) continue
    ctx.fillStyle = rgba(p.ink, 0.05)
    ctx.fillRect(x0, lanesTop - 4, x1 - x0, lanesBottom - lanesTop + 8)
    ctx.fillStyle = rgba(p.ink, 0.08)
    ctx.fillRect(x0, 3, x1 - x0, INCIDENT_ROW - 8)
    const label = `#${inc.id} ${kindLabel(inc)}`
    const short = `#${inc.id}`
    const text = ctx.measureText(label).width + 8 <= x1 - x0 ? label : ctx.measureText(short).width + 6 <= x1 - x0 ? short : ''
    if (text) {
      ctx.fillStyle = rgba(p['ink-secondary'])
      ctx.fillText(text, Math.max(x0, GUTTER) + 4, 3 + (INCIDENT_ROW - 8) / 2)
    }
  }

  // Collector gaps: reserved warning color, always labelled (⚠ in the row).
  for (const g of a.gaps) {
    const gx = x(g)
    ctx.fillStyle = rgba(p['status-warning'], 0.85)
    ctx.fillRect(gx - 1, lanesTop - 4, 2, lanesBottom - lanesTop + 8)
    ctx.fillStyle = rgba(p.ink)
    ctx.fillText('⚠', gx + 3, 3 + (INCIDENT_ROW - 8) / 2)
  }
  ctx.restore()

  LANES.forEach((lane, li) => {
    const top = TOPS[li]
    const h = lane.height
    const base = top + h
    if (lane.key === 'requests') {
      const s = niceScale(Math.max(maxOf(cols.running), maxOf(cols.waiting)))
      axis(ctx, p, top, h, s, (v) => String(v))
      area(ctx, cols.waiting, s.max, top, h, p.waiting)
      area(ctx, cols.running, s.max, top, h, p.running)
    } else if (lane.key === 'kv') {
      const s = { max: 1, step: 0.5 }
      axis(ctx, p, top, h, s, (v) => `${Math.round(v * 100)}%`)
      area(ctx, cols.kv, 1, top, h, p.kv)
      // Saturation strip: reserved critical color, labelled in the gutter key.
      ctx.fillStyle = rgba(p['status-critical'])
      for (let c = 0; c < cols.kv.length; c++) {
        if (cols.kv[c] >= KV_SATURATED) ctx.fillRect(GUTTER + c, top, 1, 3)
      }
    } else if (lane.key === 'preempt') {
      const r = rates(cols.preempted, TICK_GROUP_PX, cols.colS)
      const s = niceScale(maxOf(r), 2)
      axis(ctx, p, top, h, s, fmtRate)
      ctx.fillStyle = rgba(p.preempt)
      r.forEach((v, g) => {
        if (v <= 0) return
        const bh = Math.max(2, (v / s.max) * h)
        // Ticks: 2px strokes, distinct from the eviction bars below.
        ctx.fillRect(GUTTER + g * TICK_GROUP_PX, base - bh, 2, bh)
      })
    } else {
      const r = rates(cols.evicted, BAR_PX, cols.colS)
      const s = niceScale(maxOf(r), 2)
      axis(ctx, p, top, h, s, fmtRate)
      ctx.fillStyle = rgba(p.kv)
      r.forEach((v, g) => {
        if (v <= 0) return
        const bh = Math.max(2, (v / s.max) * h)
        roundTop(ctx, GUTTER + g * BAR_PX, base - bh, BAR_PX - 2, bh, 2)
      })
    }
  })

  timeAxis(ctx, p, view, plotW, lanesBottom)
}

function maxOf(a: Float64Array): number {
  let m = 0
  for (const v of a) if (v > m) m = v
  return m
}

function fmtRate(v: number): string {
  if (v >= 1000) return `${(v / 1000).toFixed(v >= 10_000 ? 0 : 1)}K`
  return Number.isInteger(v) ? String(v) : v.toFixed(1)
}

function axis(
  ctx: CanvasRenderingContext2D,
  p: Palette,
  top: number,
  h: number,
  s: { max: number; step: number },
  fmt: (v: number) => string,
) {
  ctx.font = DATA_FONT
  ctx.textAlign = 'right'
  ctx.textBaseline = 'middle'
  for (let v = 0; v <= s.max + 1e-9; v += s.step) {
    const y = Math.round(top + h - (v / s.max) * h) + 0.5
    ctx.strokeStyle = rgba(v === 0 ? p.axis : p.grid)
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.moveTo(GUTTER, y)
    ctx.lineTo(ctx.canvas.width, y)
    ctx.stroke()
    ctx.fillStyle = rgba(p['ink-muted'])
    ctx.fillText(fmt(v), GUTTER - 6, y)
  }
  ctx.textAlign = 'left'
}

/** 2px line over a ~10% wash, NaN columns left blank (no data yet). */
function area(
  ctx: CanvasRenderingContext2D,
  values: Float64Array,
  max: number,
  top: number,
  h: number,
  color: [number, number, number],
) {
  const y = (v: number) => top + h - (Math.min(v, max) / max) * h
  let run: [number, number][] = []
  const flush = () => {
    if (run.length === 0) return
    ctx.beginPath()
    ctx.moveTo(run[0][0], top + h)
    for (const [px, py] of run) ctx.lineTo(px, py)
    ctx.lineTo(run.at(-1)![0], top + h)
    ctx.closePath()
    ctx.fillStyle = rgba(color, 0.1)
    ctx.fill()
    ctx.beginPath()
    run.forEach(([px, py], i) => (i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py)))
    ctx.strokeStyle = rgba(color)
    ctx.lineWidth = 2
    ctx.lineJoin = 'round'
    ctx.lineCap = 'round'
    ctx.stroke()
    run = []
  }
  for (let c = 0; c < values.length; c++) {
    const v = values[c]
    if (Number.isNaN(v)) flush()
    else run.push([GUTTER + c + 0.5, y(v)])
  }
  flush()
}

function roundTop(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  const rr = Math.min(r, w / 2, h)
  ctx.beginPath()
  ctx.moveTo(x, y + h)
  ctx.lineTo(x, y + rr)
  ctx.arcTo(x, y, x + rr, y, rr)
  ctx.lineTo(x + w - rr, y)
  ctx.arcTo(x + w, y, x + w, y + rr, rr)
  ctx.lineTo(x + w, y + h)
  ctx.closePath()
  ctx.fill()
}

function timeAxis(ctx: CanvasRenderingContext2D, p: Palette, view: View, plotW: number, bottom: number) {
  const span = view.t1 - view.t0
  const { step } = niceScale(span, Math.max(2, Math.floor(plotW / 110)))
  ctx.font = DATA_FONT
  ctx.textAlign = 'center'
  ctx.textBaseline = 'top'
  const first = Math.ceil(view.t0 / step) * step
  for (let t = first; t <= view.t1 + 1e-9; t += step) {
    const x = Math.round(GUTTER + ((t - view.t0) / span) * plotW) + 0.5
    ctx.strokeStyle = rgba(p.grid)
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.moveTo(x, TOPS[0])
    ctx.lineTo(x, bottom)
    ctx.stroke()
    ctx.fillStyle = rgba(p['ink-muted'])
    const digits = step < 1 ? Math.min(3, Math.ceil(-Math.log10(step))) : 0
    ctx.fillText(`${t.toFixed(digits)} s`, x, bottom + 8)
  }
  ctx.textAlign = 'left'
}

// --- DOM overlays ----------------------------------------------------------

function LaneLabels() {
  return (
    <div className="pointer-events-none absolute top-0 left-0" style={{ width: GUTTER - 44 }}>
      {LANES.map((lane, i) => (
        <div key={lane.key} className="absolute left-2" style={{ top: TOPS[i] - 2 }}>
          <div className="text-xs font-medium text-ink-secondary">{lane.title}</div>
          <div className="mt-0.5 flex flex-col gap-0.5 text-[11px] text-ink-muted">
            {lane.key === 'requests' && (
              <>
                <Key swatch="line" color="running" label="running" />
                <Key swatch="line" color="waiting" label="waiting" />
              </>
            )}
            {lane.key === 'kv' && <Key swatch="line" color="kv" label="usage" />}
            {lane.key === 'kv' && <Key swatch="bar" color="status-critical" label="⚠ ≥95% full" />}
            {lane.key === 'preempt' && <Key swatch="tick" color="preempt" label={lane.unit} />}
            {lane.key === 'evict' && <Key swatch="bar" color="kv" label={lane.unit} />}
          </div>
        </div>
      ))}
    </div>
  )
}

function Key({ swatch, color, label }: { swatch: 'line' | 'bar' | 'tick'; color: string; label: string }) {
  const style = { background: `var(--il-${color})` }
  return (
    <span className="flex items-center gap-1.5">
      {swatch === 'line' && <span className="inline-block h-0.5 w-3 rounded" style={style} />}
      {swatch === 'bar' && <span className="inline-block h-2 w-1.5 rounded-t-sm" style={style} />}
      {swatch === 'tick' && <span className="inline-block h-2.5 w-0.5" style={style} />}
      {label}
    </span>
  )
}

function Cursor({
  x,
  t,
  width,
  data,
  bins,
  incidents,
  gapNear,
}: {
  x: number
  t: number
  width: number
  data: ReturnType<typeof prepare>
  bins: ReturnType<typeof binTrace>
  incidents: Incident[]
  gapNear: boolean
}) {
  const state = stateAt(data, t)
  const bin = bins[Math.min(bins.length - 1, Math.max(0, Math.floor(t / DEFAULT_BIN_S)))]
  const inc = incidents.find((i) => t >= i.startS && t < i.endS)
  const left = x + 12 + TOOLTIP_W > width ? x - TOOLTIP_W - 12 : x + 12
  return (
    <>
      <div
        className="pointer-events-none absolute bg-ink-muted"
        style={{ left: x, top: TOPS[0] - 4, width: 1, height: TOPS.at(-1)! + LANES.at(-1)!.height - TOPS[0] + 8 }}
      />
      <div
        className="pointer-events-none absolute rounded border border-edge bg-surface px-2 py-1.5 shadow-sm"
        style={{ left, top: TOPS[0], width: TOOLTIP_W }}
      >
        <div className="font-data text-xs text-ink-secondary">t = {t.toFixed(2)} s</div>
        {state ? (
          <dl className="mt-1 grid grid-cols-[auto_1fr] items-baseline gap-x-2 gap-y-0.5">
            <Row value={String(state.running)} label="running" color="running" />
            <Row value={String(state.waiting)} label="waiting" color="waiting" />
            <Row value={`${(state.kv * 100).toFixed(1)}%`} label="KV usage" color="kv" />
            {bin && <Row value={String(bin.preemptions)} label="preempted this second" color="preempt" />}
            {bin && <Row value={bin.evictedBlocks.toLocaleString()} label="blocks evicted this second" color="kv" />}
          </dl>
        ) : (
          <div className="mt-1 text-xs text-ink-muted">no engine data yet</div>
        )}
        {inc && (
          <div className="mt-1 border-t border-grid pt-1 text-xs text-ink-secondary">
            Incident #{inc.id} · {kindLabel(inc)} · {inc.preemptions} preemptions
          </div>
        )}
        {gapNear && (
          <div className="mt-1 text-xs text-ink-secondary">
            <span className="text-status-warning">⚠</span> collector gap: some events missing here
          </div>
        )}
      </div>
    </>
  )
}

function Row({ value, label, color }: { value: string; label: string; color: string }) {
  return (
    <>
      <dd className="text-right font-data text-xs font-semibold text-ink tabular-nums">{value}</dd>
      <dt className="flex items-center gap-1.5 text-xs text-ink-secondary">
        <span className="inline-block h-0.5 w-2.5 rounded" style={{ background: `var(--il-${color})` }} />
        {label}
      </dt>
    </>
  )
}

function BinTable({ bins, incidents }: { bins: ReturnType<typeof binTrace>; incidents: Incident[] }) {
  const incidentAt = (t: number) => incidents.find((i) => t >= i.startS && t < i.endS)
  return (
    <div className="max-h-72 overflow-y-auto border-t border-grid">
      <table className="w-full font-data text-xs tabular-nums">
        <caption className="sr-only">Engine state per second</caption>
        <thead className="sticky top-0 bg-surface text-ink-secondary">
          <tr>
            {['t (s)', 'running peak', 'waiting peak', 'KV peak', 'preemptions', 'blocks evicted', 'incident'].map((h) => (
              <th key={h} scope="col" className="px-2 py-1 text-right font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {bins.map((b) => {
            const inc = incidentAt(b.t)
            return (
              <tr key={b.t} className="border-t border-grid text-ink">
                <td className="px-2 py-0.5 text-right">{b.t.toFixed(0)}</td>
                <td className="px-2 py-0.5 text-right">{b.snapshots ? b.runningMax : '–'}</td>
                <td className="px-2 py-0.5 text-right">{b.snapshots ? b.waitingMax : '–'}</td>
                <td className="px-2 py-0.5 text-right">{b.snapshots ? `${Math.round(b.kvUsageMax * 100)}%` : '–'}</td>
                <td className="px-2 py-0.5 text-right">{b.preemptions}</td>
                <td className="px-2 py-0.5 text-right">{b.evictedBlocks.toLocaleString()}</td>
                <td className="px-2 py-0.5 text-right text-ink-secondary">{inc ? `#${inc.id} ${kindLabel(inc)}` : ''}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
