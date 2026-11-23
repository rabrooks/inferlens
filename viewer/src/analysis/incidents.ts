/**
 * Incident windows for the timeline overlay.
 *
 * A port of the binning + window detection in `inferlens/analysis.py`
 * (the `inferlens explain` engine), so the viewer marks exactly the
 * incidents the CLI reports. Keep the two in sync — thresholds and rules
 * here must match the Python constants; `incidents.test.ts` cross-checks
 * them on the real fixture when it is present.
 *
 * Only what the overlay needs is ported (bins, windows, kind); latency
 * attribution stays in Python.
 */
import type { Trace } from '../trace/types'

export const DEFAULT_BIN_S = 1.0
/** KV usage at or above this fraction counts as saturated (analysis.KV_SATURATED). */
export const KV_SATURATED = 0.95
/** Calm gaps of up to this many bins are bridged (analysis.MERGE_GAP_BINS). */
export const MERGE_GAP_BINS = 2

export interface Bin {
  /** Bin start, seconds since trace start. */
  t: number
  kvUsageMax: number
  runningMax: number
  waitingMax: number
  preemptions: number
  evictedBlocks: number
  snapshots: number
}

export type IncidentKind = 'kv_bound' | 'queue_bound'

export interface Incident {
  id: number
  /** Seconds since trace start. */
  startS: number
  endS: number
  kind: IncidentKind
  kvUsageMax: number
  preemptions: number
  evictedBlocks: number
  waitingMax: number
}

export function kvSaturated(b: Bin): boolean {
  return b.kvUsageMax >= KV_SATURATED
}

function queueBackedUp(b: Bin): boolean {
  return b.waitingMax >= Math.max(2, b.runningMax)
}

export function stressed(b: Bin): boolean {
  return kvSaturated(b) || b.preemptions > 0 || queueBackedUp(b)
}

/** Aggregate engine state into fixed-width bins from trace start. */
export function binTrace(trace: Trace, binS = DEFAULT_BIN_S): Bin[] {
  const n = Math.max(1, Math.floor((trace.end - trace.start) / binS) + 1)
  const bins: Bin[] = Array.from({ length: n }, (_, i) => ({
    t: i * binS,
    kvUsageMax: 0,
    runningMax: 0,
    waitingMax: 0,
    preemptions: 0,
    evictedBlocks: 0,
    snapshots: 0,
  }))
  const index = (t: number) =>
    Math.min(n - 1, Math.max(0, Math.floor((t - trace.start) / binS)))
  for (const s of trace.snapshots) {
    const b = bins[index(s.t)]
    b.snapshots += 1
    b.kvUsageMax = Math.max(b.kvUsageMax, s.kv_cache_usage)
    b.runningMax = Math.max(b.runningMax, s.num_running_reqs)
    b.waitingMax = Math.max(b.waitingMax, s.num_waiting_reqs)
    b.preemptions += s.num_preempted_reqs
  }
  for (const e of trace.kvRemoved) {
    bins[index(e.t)].evictedBlocks += e.block_hashes.length
  }
  return bins
}

/** Group stressed bins into incidents, bridging short calm gaps. */
export function findIncidents(bins: Bin[], binS = DEFAULT_BIN_S): Incident[] {
  const spans: [number, number][] = []
  bins.forEach((b, i) => {
    if (!stressed(b)) return
    const last = spans.at(-1)
    if (last && i - last[1] - 1 <= MERGE_GAP_BINS) last[1] = i
    else spans.push([i, i])
  })
  return spans.map(([first, lastIdx], k) => {
    const w = bins.slice(first, lastIdx + 1)
    return {
      id: k + 1,
      startS: bins[first].t,
      endS: bins[lastIdx].t + binS,
      kind: w.some((b) => kvSaturated(b) || b.preemptions > 0) ? 'kv_bound' : 'queue_bound',
      kvUsageMax: Math.max(...w.map((b) => b.kvUsageMax)),
      preemptions: w.reduce((a, b) => a + b.preemptions, 0),
      evictedBlocks: w.reduce((a, b) => a + b.evictedBlocks, 0),
      waitingMax: Math.max(...w.map((b) => b.waitingMax)),
    }
  })
}
