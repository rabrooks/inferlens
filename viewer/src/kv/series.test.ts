import { describe, expect, it } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { loadTrace } from '../trace/load'
import type { KVBlockRemoved, KVBlockStored, KVCacheCleared, Trace } from '../trace/types'
import { kvBins, kvColumns, prepareKv } from './series'

let seq = 0
const stored = (t: number, hashes: (number | string)[], medium: string | null = null): KVBlockStored => ({
  kind: 'kv_block_stored',
  t,
  ts: t,
  seq: seq,
  order: seq++,
  wall_time_unix: t,
  block_hashes: hashes,
  parent_block_hash: null,
  num_tokens: 16 * hashes.length,
  block_size: 16,
  medium,
  group_idx: 0,
})
const removed = (t: number, hashes: (number | string)[]): KVBlockRemoved => ({
  kind: 'kv_block_removed',
  t,
  ts: t,
  seq: seq,
  order: seq++,
  wall_time_unix: t,
  block_hashes: hashes,
  medium: null,
  group_idx: 0,
})
const cleared = (t: number): KVCacheCleared => ({
  kind: 'kv_cache_cleared',
  t,
  ts: t,
  seq: seq,
  order: seq++,
  wall_time_unix: t,
})

function traceOf(kv: { s?: KVBlockStored[]; r?: KVBlockRemoved[]; c?: KVCacheCleared[] }): Trace {
  return {
    meta: null,
    snapshots: [],
    requests: [],
    kvStored: kv.s ?? [],
    kvRemoved: kv.r ?? [],
    kvCleared: kv.c ?? [],
    gaps: [],
    start: 0,
    end: 10,
    eventCount: 0,
    warnings: { unknownKinds: {}, malformedRecords: 0, extraMetas: 0, truncated: false, noMeta: true },
  }
}

describe('prepareKv recompute-after-eviction', () => {
  it('counts a block stored again after it was evicted', () => {
    const d = prepareKv(traceOf({ s: [stored(1, [1, 2]), stored(3, [2, 3])], r: [removed(2, [2])] }))
    expect(Array.from(d.recomputed)).toEqual([0, 1])
    expect(d.totals).toMatchObject({ stored: 4, recomputed: 1, evicted: 1 })
  })

  it('replays one publisher batch in file order', () => {
    // Evict and re-store inside one batch: same t and seq, distinct order.
    // Sorting the per-kind lists by (t, seq) alone put every store first.
    const r = { ...removed(2, [1]), seq: 7, order: 10 }
    const s = { ...stored(2, [1]), seq: 7, order: 11 }
    const d = prepareKv(traceOf({ s: [stored(1, [1]), s], r: [r] }))
    expect(d.totals.recomputed).toBe(1)
  })

  it('does not count a re-store of a block that was never evicted', () => {
    const d = prepareKv(traceOf({ s: [stored(1, [1]), stored(2, [1])] }))
    expect(d.totals.recomputed).toBe(0)
  })

  it('counts each eviction at most once', () => {
    const d = prepareKv(traceOf({ s: [stored(1, [1]), stored(3, [1]), stored(4, [1])], r: [removed(2, [1])] }))
    expect(d.totals.recomputed).toBe(1)
  })

  it('treats a cache clear as evicting everything resident', () => {
    const d = prepareKv(traceOf({ s: [stored(1, [1, 2]), stored(3, [1])], c: [cleared(2)] }))
    expect(d.totals.recomputed).toBe(1)
  })

  it('keys blocks by medium, so an offloaded copy is not a recompute', () => {
    const d = prepareKv(traceOf({ s: [stored(1, [1]), stored(3, [1], 'CPU')], r: [removed(2, [1])] }))
    expect(d.totals.recomputed).toBe(0)
  })

  it('keeps 64-bit hashes (strings) distinct', () => {
    const d = prepareKv(
      traceOf({ s: [stored(1, ['18446744073709551615']), stored(3, ['18446744073709551614'])], r: [removed(2, ['18446744073709551615'])] }),
    )
    expect(d.totals.recomputed).toBe(0)
  })
})

describe('kvColumns / kvBins', () => {
  it('sums lookups, hits and stored blocks per column', () => {
    const t = traceOf({ s: [stored(0.5, [1, 2, 3])] })
    t.snapshots = [
      { t: 0.1, prefix_cache_queries: 100, prefix_cache_hits: 90 },
      { t: 0.2, prefix_cache_queries: 900, prefix_cache_hits: 0 },
    ].map((x) => ({ ...x, ts: x.t })) as unknown as Trace['snapshots']
    const c = kvColumns(prepareKv(t), 0, 2, 2)
    expect(Array.from(c.queries)).toEqual([1000, 0])
    expect(Array.from(c.hits)).toEqual([90, 0])
    expect(Array.from(c.stored)).toEqual([3, 0])
    const bins = kvBins(prepareKv(t), 2)
    expect(bins[0]).toMatchObject({ queries: 1000, hits: 90, stored: 3 })
  })
})

// Regression on the real GPU trace. Totals were computed independently in
// Python (read_trace, replayed in file order); recomputed was 6,910 when
// one batch's stores were replayed ahead of its removals.
const fixture = join(homedir(), 'Projects/inferlens-traces/gpu-incident.ilens.gz')

describe('gpu-incident.ilens.gz (skipped when absent)', () => {
  it.skipIf(!existsSync(fixture))(
    'replays the cache to the independently computed totals',
    async () => {
      const t = await loadTrace(new Blob([readFileSync(fixture)]))
      expect(prepareKv(t).totals).toMatchObject({ stored: 40766, evicted: 40256, recomputed: 6999 })
    },
    60_000,
  )
})
