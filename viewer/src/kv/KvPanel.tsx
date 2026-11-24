/**
 * KV / prefix-cache panel: what the cache did, on the shared time axis.
 *
 * Lanes (each labeled, one y-scale per lane):
 * - prefix-cache lookups per second (violet): the hit part solid from the
 *   baseline, misses a lighter step of the same hue above it — so both the
 *   hit share and the lookup surge during a storm (preempted requests
 *   looking their prompts up again) read at once. A hit-*rate* line was
 *   tried first: lookups are bursty, so per-pixel ratios swing 0↔66% and
 *   the line fragments; the exact rate lives in the tooltip and table.
 * - KV blocks stored per second (aqua bars);
 * - blocks recomputed after eviction per second (aqua bars) — a block
 *   stored again after it was evicted: work an eviction storm forces the
 *   engine to repeat. Stored and recomputed share one scale, so the
 *   recomputed share reads directly as a fraction of the stored bar above.
 * KV usage and eviction volume live on the engine timeline; this panel
 * doesn't repeat them. Encoding rules: viewer/DESIGN.md.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Incident } from '../analysis/incidents'
import type { RequestAttr } from '../analysis/requests'
import { axis, fmtRate, maxOf, roundTop, timeAxis } from '../timeline/draw'
import { niceScale, rates } from '../timeline/series'
import { rgba, usePalette, type Palette } from '../timeline/theme'
import { GUTTER, RIGHT, usePanZoom, xOf, type TimeView, type View } from '../timeline/view'
import type { Trace } from '../trace/types'
import { kvBins, kvColumns, prepareKv, type KvBin, type KvColumns } from './series'

const TOP = 10
const LANE_GAP = 18
const AXIS = 26
const BAR_PX = 8 // 6px bar + 2px surface gap
const TOOLTIP_W = 264

type LaneKey = 'hit' | 'stored' | 'recomputed'
interface Lane {
  key: LaneKey
  title: string
  height: number
}

const ALL_LANES: Lane[] = [
  { key: 'hit', title: 'Prefix-cache lookups', height: 72 },
  { key: 'stored', title: 'KV blocks stored', height: 56 },
  { key: 'recomputed', title: 'Recomputed after eviction', height: 56 },
]

function layout(lanes: Lane[]) {
  let y = TOP
  const tops = lanes.map((l) => {
    const top = y
    y += l.height + LANE_GAP
    return top
  })
  const bottom = tops.at(-1)! + lanes.at(-1)!.height
  return { tops, bottom, height: bottom + AXIS }
}

export function KvPanel({
  trace,
  incidents,
  tv,
  selected,
}: {
  trace: Trace
  incidents: Incident[]
  tv: TimeView
  selected: RequestAttr | null
}) {
  const data = useMemo(() => prepareKv(trace), [trace])
  const bins = useMemo(() => kvBins(data, trace.end - trace.start), [data, trace])
  const gaps = useMemo(() => trace.gaps.map((g) => g.t - trace.start), [trace])
  // Without KV events (no subscriber), only the snapshot-based lane exists.
  const lanes = useMemo(() => (data.hasEvents ? ALL_LANES : ALL_LANES.slice(0, 1)), [data])
  const geo = useMemo(() => layout(lanes), [lanes])
  const { view, hoverT } = tv
  const [width, setWidth] = useState(0)
  const [showTable, setShowTable] = useState(false)
  const palette = usePalette()
  const wrap = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const el = wrap.current
    if (!el) return
    const ro = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const plotW = Math.max(0, width - GUTTER - RIGHT)
  const cols = useMemo(() => (plotW > 0 ? kvColumns(data, view.t0, view.t1, plotW) : null), [data, view, plotW])
  const span = useMemo(() => (selected ? ([selected.arrivalS, selected.finishS] as const) : null), [selected])

  useEffect(() => {
    const c = canvas.current
    if (!c || !cols || !palette || width === 0) return
    const dpr = window.devicePixelRatio || 1
    c.width = width * dpr
    c.height = geo.height * dpr
    const ctx = c.getContext('2d')
    if (!ctx) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    draw(ctx, { width, plotW, view, cols, palette, lanes, geo, incidents, gaps, span })
  }, [cols, palette, width, plotW, view, lanes, geo, incidents, gaps, span])

  const pz = usePanZoom(canvas, plotW, tv)
  const hoverX = hoverT === null || plotW <= 0 ? null : xOf(hoverT, view, plotW)
  const { totals } = data
  const hitPct = totals.queries > 0 ? (totals.hits / totals.queries) * 100 : null

  return (
    <section className="rounded border border-edge bg-surface">
      <header className="flex flex-wrap items-center gap-x-3 border-b border-grid px-2 py-1">
        <h2 className="text-xs font-medium text-ink-secondary">KV / prefix cache</h2>
        <span className="font-data text-xs text-ink-muted">
          {hitPct === null ? 'no prefix-cache lookups' : `${hitPct.toFixed(1)}% hit of ${fmtCount(totals.queries)} tokens looked up`}
          {data.hasEvents &&
            ` · ${totals.stored.toLocaleString()} blocks stored · ${totals.recomputed.toLocaleString()} recomputed after eviction` +
              (totals.stored > 0 ? ` (${Math.round((totals.recomputed / totals.stored) * 100)}%)` : '')}
        </span>
        <button
          type="button"
          onClick={() => setShowTable((s) => !s)}
          aria-pressed={showTable}
          className="ml-auto rounded px-1.5 text-xs text-ink-secondary hover:bg-page"
        >
          {showTable ? 'Hide table' : 'Table'}
        </button>
      </header>
      <div
        ref={wrap}
        className="relative outline-none focus-visible:ring-1 focus-visible:ring-axis"
        tabIndex={0}
        onKeyDown={pz.onKeyDown}
        aria-label="KV and prefix-cache panel. Arrow keys pan, plus and minus zoom, 0 resets."
      >
        <canvas
          ref={canvas}
          role="img"
          aria-label={summary(data.totals, hitPct, data.hasEvents)}
          style={{ width: '100%', height: geo.height, display: 'block', cursor: pz.dragging ? 'grabbing' : 'crosshair' }}
          {...pz.handlers}
        />
        <LaneLabels lanes={lanes} tops={geo.tops} />
        {!data.hasEvents && (
          <p className="absolute right-3 bottom-1 text-[11px] text-ink-muted">
            No KV events in this trace — record with <span className="font-data">inferlens record</span> to capture
            block activity.
          </p>
        )}
        {hoverX !== null && hoverT !== null && (
          <Cursor
            x={hoverX}
            t={hoverT}
            width={width}
            top={geo.tops[0]}
            bottom={geo.bottom}
            bin={bins[Math.min(bins.length - 1, Math.max(0, Math.floor(hoverT)))]}
            hasEvents={data.hasEvents}
            incident={incidents.find((i) => hoverT >= i.startS && hoverT < i.endS)}
          />
        )}
      </div>
      {showTable && <BinTable bins={bins} incidents={incidents} hasEvents={data.hasEvents} />}
    </section>
  )
}

function fmtCount(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e4) return `${Math.round(n / 1e3)}K`
  return n.toLocaleString()
}

function summary(t: ReturnType<typeof prepareKv>['totals'], hitPct: number | null, hasEvents: boolean): string {
  const hit = hitPct === null ? 'No prefix-cache lookups.' : `Prefix-cache hit rate ${hitPct.toFixed(1)}% overall.`
  if (!hasEvents) return `${hit} No KV block events recorded.`
  return `${hit} ${t.stored} KV blocks stored, ${t.recomputed} of them recomputed after being evicted.`
}

// --- drawing ---------------------------------------------------------------

interface DrawArgs {
  width: number
  plotW: number
  view: View
  cols: KvColumns
  palette: Palette
  lanes: Lane[]
  geo: ReturnType<typeof layout>
  incidents: Incident[]
  gaps: number[]
  span: readonly [number, number] | null
}

function draw(ctx: CanvasRenderingContext2D, a: DrawArgs) {
  const { width, plotW, view, cols, palette: p, geo } = a
  ctx.clearRect(0, 0, width, geo.height)
  const x = (t: number) => xOf(t, view, plotW)
  const top0 = geo.tops[0]

  timeAxis(ctx, p, view, plotW, top0 - 4, geo.bottom)
  ctx.save()
  ctx.beginPath()
  ctx.rect(GUTTER, 0, plotW, geo.height)
  ctx.clip()
  for (const inc of a.incidents) {
    ctx.fillStyle = rgba(p.ink, 0.05)
    ctx.fillRect(x(inc.startS), top0 - 4, x(inc.endS) - x(inc.startS), geo.bottom - top0 + 8)
  }
  for (const g of a.gaps) {
    ctx.fillStyle = rgba(p['status-warning'], 0.85)
    ctx.fillRect(x(g) - 1, top0 - 4, 2, geo.bottom - top0 + 8)
  }
  if (a.span) {
    const [s0, s1] = a.span.map(x)
    ctx.fillStyle = rgba(p.ink, 0.04)
    ctx.fillRect(s0, top0 - 4, s1 - s0, geo.bottom - top0 + 8)
    ctx.fillStyle = rgba(p['ink-secondary'])
    for (const sx of [s0, s1]) ctx.fillRect(Math.round(sx) - 0.5, top0 - 8, 1, geo.bottom - top0 + 12)
  }
  ctx.restore()

  const storedR = rates(cols.stored, BAR_PX, cols.colS)
  const recomputedR = rates(cols.recomputed, BAR_PX, cols.colS)
  // One scale for both block lanes: recomputed is a subset of stored.
  const blocks = niceScale(maxOf(storedR), 2)

  a.lanes.forEach((lane, li) => {
    const top = geo.tops[li]
    const h = lane.height
    if (lane.key === 'hit') {
      const q = rates(cols.queries, BAR_PX, cols.colS)
      const hit = rates(cols.hits, BAR_PX, cols.colS)
      const s = niceScale(maxOf(q), 2)
      axis(ctx, p, top, h, s, fmtRate)
      q.forEach((v, g) => {
        if (v <= 0) return
        const x0 = GUTTER + g * BAR_PX
        const qh = Math.max(2, (v / s.max) * h)
        const hh = hit[g] > 0 ? Math.max(2, (hit[g] / s.max) * h) : 0
        // Misses: a lighter step of the hit hue, 2px surface gap above hits.
        const missTop = top + h - qh
        const missBottom = top + h - hh - (hh > 0 ? 2 : 0)
        if (missBottom - missTop >= 1) {
          ctx.fillStyle = rgba(p.cachehit, 0.32)
          roundTop(ctx, x0, missTop, BAR_PX - 2, missBottom - missTop, 2)
        }
        if (hh > 0) {
          ctx.fillStyle = rgba(p.cachehit)
          if (missBottom - missTop >= 1) ctx.fillRect(x0, top + h - hh, BAR_PX - 2, hh)
          else roundTop(ctx, x0, top + h - hh, BAR_PX - 2, hh, 2)
        }
      })
    } else {
      const r = lane.key === 'stored' ? storedR : recomputedR
      axis(ctx, p, top, h, blocks, fmtRate)
      ctx.fillStyle = rgba(p.kv)
      r.forEach((v, g) => {
        if (v <= 0) return
        const bh = Math.max(2, (v / blocks.max) * h)
        roundTop(ctx, GUTTER + g * BAR_PX, top + h - bh, BAR_PX - 2, bh, 2)
      })
    }
  })
}

// --- DOM overlays ----------------------------------------------------------

function LaneLabels({ lanes, tops }: { lanes: Lane[]; tops: number[] }) {
  return (
    <div className="pointer-events-none absolute top-0 left-0" style={{ width: GUTTER - 44 }}>
      {lanes.map((lane, i) => (
        <div key={lane.key} className="absolute left-2" style={{ top: tops[i] - 2 }}>
          <div className="text-xs leading-tight font-medium text-ink-secondary">{lane.title}</div>
          <div className="mt-0.5 flex items-start gap-1.5 text-[11px] text-ink-muted">
            {lane.key === 'hit' ? (
              <span className="flex flex-col gap-0.5">
                <span className="flex items-center gap-1.5">
                  <span className="inline-block h-2 w-1.5 rounded-t-sm" style={{ background: 'var(--il-cachehit)' }} />
                  hit tokens / s
                </span>
                <span className="flex items-center gap-1.5">
                  <span
                    className="inline-block h-2 w-1.5 rounded-t-sm opacity-35"
                    style={{ background: 'var(--il-cachehit)' }}
                  />
                  missed tokens / s
                </span>
              </span>
            ) : (
              <>
                <span className="inline-block h-2 w-1.5 rounded-t-sm" style={{ background: 'var(--il-kv)' }} />
                blocks / s
              </>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

function Cursor({
  x,
  t,
  width,
  top,
  bottom,
  bin,
  hasEvents,
  incident,
}: {
  x: number
  t: number
  width: number
  top: number
  bottom: number
  bin: KvBin | undefined
  hasEvents: boolean
  incident: Incident | undefined
}) {
  const left = x + 12 + TOOLTIP_W > width ? x - TOOLTIP_W - 12 : x + 12
  return (
    <>
      <div className="pointer-events-none absolute bg-ink-muted" style={{ left: x, top: top - 4, width: 1, height: bottom - top + 8 }} />
      <div
        className="pointer-events-none absolute rounded border border-edge bg-surface px-2 py-1.5 shadow-sm"
        style={{ left, top, width: TOOLTIP_W }}
      >
        <div className="font-data text-xs text-ink-secondary">
          t = {t.toFixed(2)} s · second {Math.floor(t)}–{Math.floor(t) + 1}
        </div>
        {bin && (
          <dl className="mt-1 grid grid-cols-[auto_1fr] items-baseline gap-x-2 gap-y-0.5">
            <Row
              value={bin.queries > 0 ? `${((bin.hits / bin.queries) * 100).toFixed(1)}%` : '–'}
              label={`hit of ${bin.queries.toLocaleString()} tokens looked up`}
              color="cachehit"
            />
            {hasEvents && <Row value={bin.stored.toLocaleString()} label="blocks stored" color="kv" />}
            {hasEvents && <Row value={bin.recomputed.toLocaleString()} label="recomputed after eviction" color="kv" />}
          </dl>
        )}
        {incident && (
          <div className="mt-1 border-t border-grid pt-1 text-xs text-ink-secondary">
            Incident #{incident.id} · {incident.kind === 'kv_bound' ? 'KV-bound' : 'queue-bound'}
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
        <span className="inline-block h-0.5 w-2.5 shrink-0 rounded" style={{ background: `var(--il-${color})` }} />
        {label}
      </dt>
    </>
  )
}

function BinTable({ bins, incidents, hasEvents }: { bins: KvBin[]; incidents: Incident[]; hasEvents: boolean }) {
  const incidentAt = (t: number) => incidents.find((i) => t >= i.startS && t < i.endS)
  const heads = ['t (s)', 'tokens looked up', 'hit rate', ...(hasEvents ? ['blocks stored', 'recomputed'] : []), 'incident']
  return (
    <div className="max-h-72 overflow-y-auto border-t border-grid">
      <table className="w-full font-data text-xs tabular-nums">
        <caption className="sr-only">KV and prefix-cache activity per second</caption>
        <thead className="sticky top-0 bg-surface text-ink-secondary">
          <tr>
            {heads.map((h) => (
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
                <td className="px-2 py-0.5 text-right">{b.queries.toLocaleString()}</td>
                <td className="px-2 py-0.5 text-right">{b.queries > 0 ? `${((b.hits / b.queries) * 100).toFixed(1)}%` : '–'}</td>
                {hasEvents && <td className="px-2 py-0.5 text-right">{b.stored.toLocaleString()}</td>}
                {hasEvents && <td className="px-2 py-0.5 text-right">{b.recomputed.toLocaleString()}</td>}
                <td className="px-2 py-0.5 text-right text-ink-secondary">
                  {inc ? `#${inc.id} ${inc.kind === 'kv_bound' ? 'KV-bound' : 'queue-bound'}` : ''}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
