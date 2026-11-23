/**
 * Timeline series, aggregated to screen resolution.
 *
 * 30k snapshots over a few thousand pixels: each pixel column draws one
 * value. Levels (running, waiting, KV usage) take the column's *peak* —
 * the incident story is about peaks, and a mean would hide a one-step
 * spike — and carry forward through columns with no snapshot, because
 * engine state persists between snapshots. Events (preemptions, evicted
 * blocks) are summed and drawn as per-second rates, so bar heights don't
 * change with zoom.
 */
import type { Trace } from '../trace/types'

/** Columnar copy of the trace, times in seconds since trace start. */
export interface SeriesData {
  snapT: Float64Array
  running: Float64Array
  waiting: Float64Array
  kv: Float64Array
  preempted: Float64Array
  evictT: Float64Array
  evictN: Float64Array
  /** Seconds from trace start to trace end. */
  span: number
}

export function prepare(trace: Trace): SeriesData {
  const n = trace.snapshots.length
  const snapT = new Float64Array(n)
  const running = new Float64Array(n)
  const waiting = new Float64Array(n)
  const kv = new Float64Array(n)
  const preempted = new Float64Array(n)
  trace.snapshots.forEach((s, i) => {
    snapT[i] = s.t - trace.start
    running[i] = s.num_running_reqs
    waiting[i] = s.num_waiting_reqs
    kv[i] = s.kv_cache_usage
    preempted[i] = s.num_preempted_reqs
  })
  const evictT = new Float64Array(trace.kvRemoved.length)
  const evictN = new Float64Array(trace.kvRemoved.length)
  trace.kvRemoved.forEach((e, i) => {
    evictT[i] = e.t - trace.start
    evictN[i] = e.block_hashes.length
  })
  return {
    snapT,
    running,
    waiting,
    kv,
    preempted,
    evictT,
    evictN,
    span: Math.max(0, trace.end - trace.start),
  }
}

/** Index of the first element >= t (arrays are sorted ascending). */
export function lowerBound(a: Float64Array, t: number): number {
  let lo = 0
  let hi = a.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (a[mid] < t) lo = mid + 1
    else hi = mid
  }
  return lo
}

export interface Columns {
  /** Seconds per column. */
  colS: number
  running: Float64Array
  waiting: Float64Array
  kv: Float64Array
  /** Event sums per column. */
  preempted: Float64Array
  evicted: Float64Array
}

/**
 * Aggregate the visible range `[t0, t1)` into `n` columns.
 * Level columns before the first snapshot are NaN (no data yet).
 */
export function columns(d: SeriesData, t0: number, t1: number, n: number): Columns {
  const colS = (t1 - t0) / n
  const out: Columns = {
    colS,
    running: new Float64Array(n).fill(Number.NaN),
    waiting: new Float64Array(n).fill(Number.NaN),
    kv: new Float64Array(n).fill(Number.NaN),
    preempted: new Float64Array(n),
    evicted: new Float64Array(n),
  }
  let i = lowerBound(d.snapT, t0)
  // Carry in the state from the last snapshot before the view.
  let lastR = i > 0 ? d.running[i - 1] : Number.NaN
  let lastW = i > 0 ? d.waiting[i - 1] : Number.NaN
  let lastK = i > 0 ? d.kv[i - 1] : Number.NaN
  for (let c = 0; c < n; c++) {
    const end = t0 + (c + 1) * colS
    let r = Number.NaN
    let w = Number.NaN
    let k = Number.NaN
    let p = 0
    for (; i < d.snapT.length && d.snapT[i] < end; i++) {
      r = Number.isNaN(r) ? d.running[i] : Math.max(r, d.running[i])
      w = Number.isNaN(w) ? d.waiting[i] : Math.max(w, d.waiting[i])
      k = Number.isNaN(k) ? d.kv[i] : Math.max(k, d.kv[i])
      p += d.preempted[i]
      lastR = d.running[i]
      lastW = d.waiting[i]
      lastK = d.kv[i]
    }
    // An empty column shows the state carried from its last snapshot.
    out.running[c] = Number.isNaN(r) ? lastR : r
    out.waiting[c] = Number.isNaN(w) ? lastW : w
    out.kv[c] = Number.isNaN(k) ? lastK : k
    out.preempted[c] = p
  }
  for (let j = lowerBound(d.evictT, t0); j < d.evictT.length && d.evictT[j] < t1; j++) {
    const c = Math.min(n - 1, Math.floor((d.evictT[j] - t0) / colS))
    out.evicted[c] += d.evictN[j]
  }
  return out
}

/** Sum event columns into groups of `size` and convert to per-second rates. */
export function rates(cols: Float64Array, size: number, colS: number): Float64Array {
  const groups = Math.ceil(cols.length / size)
  const out = new Float64Array(groups)
  for (let c = 0; c < cols.length; c++) out[Math.floor(c / size)] += cols[c]
  const seconds = size * colS
  for (let g = 0; g < groups; g++) out[g] /= seconds
  return out
}

/** Engine state at time `t`: the last snapshot at or before it. */
export function stateAt(
  d: SeriesData,
  t: number,
): { running: number; waiting: number; kv: number } | null {
  const i = lowerBound(d.snapT, t + 1e-9) - 1
  if (i < 0) return null
  return { running: d.running[i], waiting: d.waiting[i], kv: d.kv[i] }
}

/** Nice axis maximum and tick step for a positive data maximum. */
export function niceScale(max: number, ticks = 3): { max: number; step: number } {
  if (!(max > 0)) return { max: 1, step: 1 }
  const raw = max / ticks
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag
  return { max: Math.ceil(max / step) * step, step }
}
