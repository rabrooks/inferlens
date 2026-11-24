import { describe, expect, it } from 'vitest'
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { loadTrace } from '../trace/load'
import type { EngineSnapshot, Trace } from '../trace/types'
import { binTrace, findIncidents, KV_SATURATED } from './incidents'

function snap(t: number, over: Partial<EngineSnapshot> = {}): EngineSnapshot {
  return {
    kind: 'engine_snapshot',
    t,
    ts: t,
    order: 0,
    num_running_reqs: 4,
    num_waiting_reqs: 0,
    kv_cache_usage: 0.3,
    prefix_cache_queries: 0,
    prefix_cache_hits: 0,
    num_preempted_reqs: 0,
    num_generation_tokens: 0,
    num_prompt_tokens: 0,
    ttft_count: 0,
    ttft_mean_s: null,
    ttft_p50_s: null,
    ttft_max_s: null,
    itl_count: 0,
    itl_mean_s: null,
    itl_p50_s: null,
    itl_max_s: null,
    num_deferred_waiting_reqs: null,
    iterations: null,
    num_prefill_reqs: null,
    num_prefill_tokens: null,
    num_decode_reqs: null,
    num_decode_tokens: null,
    iteration_time_s: null,
    iteration_time_source: null,
    ...over,
  }
}

function traceOf(snapshots: EngineSnapshot[]): Trace {
  return {
    meta: null,
    snapshots,
    requests: [],
    kvStored: [],
    kvRemoved: [],
    kvCleared: [],
    gaps: [],
    start: snapshots[0].t,
    end: snapshots.at(-1)!.t,
    eventCount: snapshots.length,
    warnings: {
      unknownKinds: {},
      malformedRecords: 0,
      extraMetas: 0,
      truncated: false,
      noMeta: true,
    },
  }
}

function calm(seconds = 60): EngineSnapshot[] {
  return Array.from({ length: seconds }, (_, t) => snap(t))
}

describe('findIncidents', () => {
  it('finds none on a calm engine', () => {
    expect(findIncidents(binTrace(traceOf(calm())))).toEqual([])
  })

  it('classifies KV saturation / preemption as kv_bound', () => {
    const s = calm()
    for (const t of [30, 31, 32]) s[t] = snap(t, { kv_cache_usage: 1, num_preempted_reqs: 4 })
    const [inc] = findIncidents(binTrace(traceOf(s)))
    expect(inc).toMatchObject({ startS: 30, endS: 33, kind: 'kv_bound', preemptions: 12 })
  })

  it('classifies a backlog without KV pressure as queue_bound', () => {
    const s = calm()
    s[20] = snap(20, { kv_cache_usage: 0.5, num_waiting_reqs: 9 })
    expect(findIncidents(binTrace(traceOf(s)))[0].kind).toBe('queue_bound')
  })

  it('treats near-full KV as saturated', () => {
    const s = calm()
    s[10] = snap(10, { kv_cache_usage: KV_SATURATED })
    expect(findIncidents(binTrace(traceOf(s)))).toHaveLength(1)
  })

  it('bridges short lulls and splits long ones', () => {
    const s = calm()
    for (const t of [10, 12, 13, 30]) s[t] = snap(t, { kv_cache_usage: 1, num_preempted_reqs: 1 })
    const spans = findIncidents(binTrace(traceOf(s))).map((i) => [i.startS, i.endS])
    expect(spans).toEqual([
      [10, 14],
      [30, 31],
    ])
  })
})

// Cross-check against the Python engine (`inferlens explain --json`) on the
// real GPU trace, so the overlay and the CLI can't silently drift apart.
const fixture = join(homedir(), 'Projects/inferlens-traces/gpu-incident.ilens.gz')
const repoRoot = resolve(__dirname, '../../..')

function pythonIncidents(): unknown[] | null {
  try {
    const out = execFileSync(
      'uv',
      ['run', '--project', repoRoot, 'inferlens', 'explain', fixture, '--json', '--worst', '0'],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
    )
    return JSON.parse(out).incidents
  } catch {
    return null
  }
}

describe('gpu-incident.ilens.gz vs inferlens explain (skipped when absent)', () => {
  it.skipIf(!existsSync(fixture))(
    'matches the Python incident windows exactly',
    async (ctx) => {
      const expected = pythonIncidents()
      if (expected === null) ctx.skip() // uv / Python env unavailable
      if (expected === null) return
      const trace = await loadTrace(new Blob([readFileSync(fixture)]))
      const got = findIncidents(binTrace(trace)).map((i) => ({
        id: i.id,
        start_s: i.startS,
        end_s: i.endS,
        kind: i.kind,
        kv_usage_max: i.kvUsageMax,
        preemptions: i.preemptions,
        evicted_blocks: i.evictedBlocks,
        waiting_max: i.waitingMax,
      }))
      const want = (expected as Record<string, unknown>[]).map((i) => ({
        id: i.id,
        start_s: i.start_s,
        end_s: i.end_s,
        kind: i.kind,
        kv_usage_max: i.kv_usage_max,
        preemptions: i.preemptions,
        evicted_blocks: i.evicted_blocks,
        waiting_max: i.waiting_max,
      }))
      expect(got).toEqual(want)
    },
    60_000,
  )
})
