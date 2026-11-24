/**
 * KV / prefix-cache series for the KV panel.
 *
 * Two sources, both already in the trace:
 * - engine snapshots carry per-step prefix-cache lookups and hits (tokens);
 * - KV events (`kv_block_stored` / `kv_block_removed` / `kv_cache_cleared`)
 *   say which blocks entered and left the cache.
 *
 * From the events we derive *recomputed after eviction*: a stored block
 * whose hash was evicted earlier and not stored since. That is KV work
 * done twice because capacity ran out — the cost an eviction storm puts
 * on the engine, which neither usage nor eviction counts show directly.
 * Blocks are keyed by (medium, group, hash): a block offloaded to another
 * medium is a different copy, not a recompute.
 */
import type { Trace } from '../trace/types'
import { lowerBound } from '../timeline/series'

export interface KvData {
  /** Snapshot times (s since trace start) and per-step prefix-cache tokens. */
  snapT: Float64Array
  queries: Float64Array
  hits: Float64Array
  /** Store events (s since trace start): blocks stored, of which recomputed. */
  storeT: Float64Array
  stored: Float64Array
  recomputed: Float64Array
  totals: { queries: number; hits: number; stored: number; recomputed: number; evicted: number }
  /** Whether the trace has any KV events at all (needs the KV subscriber). */
  hasEvents: boolean
}

export function prepareKv(trace: Trace): KvData {
  const n = trace.snapshots.length
  const snapT = new Float64Array(n)
  const queries = new Float64Array(n)
  const hits = new Float64Array(n)
  let tq = 0
  let th = 0
  trace.snapshots.forEach((s, i) => {
    snapT[i] = s.t - trace.start
    queries[i] = s.prefix_cache_queries
    hits[i] = s.prefix_cache_hits
    tq += s.prefix_cache_queries
    th += s.prefix_cache_hits
  })

  // Replay the cache in event order: placement time, publisher batch seq,
  // then file order — events within one batch share t and seq, and a block
  // evicted and re-stored in the same batch must replay in that order.
  type Ev =
    | { k: 'store'; t: number; seq: number; order: number; keys: string[] }
    | { k: 'remove'; t: number; seq: number; order: number; keys: string[] }
    | { k: 'clear'; t: number; seq: number; order: number }
  const evs: Ev[] = [
    ...trace.kvStored.map((e): Ev => ({ k: 'store', t: e.t, seq: e.seq, order: e.order, keys: e.block_hashes.map((h) => key(e.medium, e.group_idx, h)) })),
    ...trace.kvRemoved.map((e): Ev => ({ k: 'remove', t: e.t, seq: e.seq, order: e.order, keys: e.block_hashes.map((h) => key(e.medium, e.group_idx, h)) })),
    ...trace.kvCleared.map((e): Ev => ({ k: 'clear', t: e.t, seq: e.seq, order: e.order })),
  ].sort((a, b) => a.t - b.t || a.seq - b.seq || a.order - b.order)

  const resident = new Set<string>()
  const evicted = new Set<string>()
  const storeT: number[] = []
  const stored: number[] = []
  const recomputed: number[] = []
  let ts = 0
  let tr = 0
  let te = 0
  for (const e of evs) {
    if (e.k === 'store') {
      let re = 0
      for (const k of e.keys) {
        if (evicted.delete(k)) re++
        resident.add(k)
      }
      storeT.push(e.t - trace.start)
      stored.push(e.keys.length)
      recomputed.push(re)
      ts += e.keys.length
      tr += re
    } else if (e.k === 'remove') {
      for (const k of e.keys) {
        resident.delete(k)
        evicted.add(k)
      }
      te += e.keys.length
    } else {
      for (const k of resident) evicted.add(k)
      resident.clear()
    }
  }
  return {
    snapT,
    queries,
    hits,
    storeT: Float64Array.from(storeT),
    stored: Float64Array.from(stored),
    recomputed: Float64Array.from(recomputed),
    totals: { queries: tq, hits: th, stored: ts, recomputed: tr, evicted: te },
    hasEvents: evs.length > 0,
  }
}

function key(medium: string | null, group: number, hash: number | string): string {
  return `${medium ?? ''}|${group}|${hash}`
}

export interface KvColumns {
  colS: number
  /** Prefix-cache tokens looked up / hit, summed per column. */
  queries: Float64Array
  hits: Float64Array
  /** Event sums per column. */
  stored: Float64Array
  recomputed: Float64Array
}

/** Aggregate the visible range `[t0, t1)` into `n` columns. */
export function kvColumns(d: KvData, t0: number, t1: number, n: number): KvColumns {
  const colS = (t1 - t0) / n
  const q = new Float64Array(n)
  const h = new Float64Array(n)
  const stored = new Float64Array(n)
  const recomputed = new Float64Array(n)
  const col = (t: number) => Math.min(n - 1, Math.floor((t - t0) / colS))
  for (let i = lowerBound(d.snapT, t0); i < d.snapT.length && d.snapT[i] < t1; i++) {
    q[col(d.snapT[i])] += d.queries[i]
    h[col(d.snapT[i])] += d.hits[i]
  }
  for (let i = lowerBound(d.storeT, t0); i < d.storeT.length && d.storeT[i] < t1; i++) {
    stored[col(d.storeT[i])] += d.stored[i]
    recomputed[col(d.storeT[i])] += d.recomputed[i]
  }
  return { colS, queries: q, hits: h, stored, recomputed }
}

export interface KvBin {
  /** Bin start, seconds since trace start. */
  t: number
  queries: number
  hits: number
  stored: number
  recomputed: number
}

/** Per-second bins for the tooltip and the table view. */
export function kvBins(d: KvData, span: number, binS = 1): KvBin[] {
  const n = Math.max(1, Math.floor(span / binS) + 1)
  const bins: KvBin[] = Array.from({ length: n }, (_, i) => ({ t: i * binS, queries: 0, hits: 0, stored: 0, recomputed: 0 }))
  const at = (t: number) => bins[Math.min(n - 1, Math.max(0, Math.floor(t / binS)))]
  d.snapT.forEach((t, i) => {
    const b = at(t)
    b.queries += d.queries[i]
    b.hits += d.hits[i]
  })
  d.storeT.forEach((t, i) => {
    const b = at(t)
    b.stored += d.stored[i]
    b.recomputed += d.recomputed[i]
  })
  return bins
}
