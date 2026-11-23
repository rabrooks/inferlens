import { describe, expect, it } from 'vitest'
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { loadTrace } from '../trace/load'
import { binTrace, findIncidents } from './incidents'
import { analyzeRequests, packLanes, phaseSegments, type RequestAttr } from './requests'

function attr(over: Partial<RequestAttr>): RequestAttr {
  return {
    index: 0,
    requestId: 'r',
    arrivalS: 10,
    finishS: 15,
    e2eS: 5,
    queuedS: 2,
    prefillS: 0.5,
    decodeS: 2,
    unattributedS: 0.5,
    dominant: 'queued',
    numPreemptions: null,
    promptTokens: 0,
    cachedTokens: 0,
    generationTokens: 0,
    finishReason: 'stop',
    incidents: [],
    queuedVsCalm: null,
    tpotVsCalm: null,
    lane: 0,
    ...over,
  }
}

describe('phaseSegments', () => {
  it('right-aligns phases to finish and leaves the remainder at the head', () => {
    expect(phaseSegments(attr({}))).toEqual([
      { phase: 'unattributed', t0: 10, t1: 10.5 },
      { phase: 'queued', t0: 10.5, t1: 12.5 },
      { phase: 'prefill', t0: 12.5, t1: 13 },
      { phase: 'decode', t0: 13, t1: 15 },
    ])
  })

  it('clips phases that sum past e2e at arrival instead of rescaling', () => {
    const segs = phaseSegments(attr({ queuedS: 4, unattributedS: -1.5 }))
    expect(segs[0]).toEqual({ phase: 'queued', t0: 10, t1: 12.5 })
    expect(segs.map((s) => s.phase)).toEqual(['queued', 'prefill', 'decode'])
  })
})

describe('packLanes', () => {
  it('reuses a lane only after its last request ended', () => {
    expect(
      packLanes([
        [0, 5],
        [1, 3],
        [5, 6], // starts exactly when lane 0 ends: strict gap → new lane
        [4, 8],
      ]),
    ).toEqual({ lane: [0, 1, 2, 1], count: 3 })
  })
})

// Cross-check every request against `inferlens explain --json`, so the
// detail panel and the CLI can't silently drift apart.
const fixture = join(homedir(), 'Projects/inferlens-traces/gpu-incident.ilens.gz')
const repoRoot = resolve(__dirname, '../../..')

function pythonAttributions(): Record<string, unknown> | null {
  try {
    const out = execFileSync(
      'uv',
      ['run', '--project', repoRoot, 'inferlens', 'explain', fixture, '--json', '--worst', '1000000'],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], maxBuffer: 64 << 20 },
    )
    return JSON.parse(out)
  } catch {
    return null
  }
}

describe('gpu-incident.ilens.gz vs inferlens explain (skipped when absent)', () => {
  it.skipIf(!existsSync(fixture))(
    'matches the Python attribution for every request',
    async (ctx) => {
      const py = pythonAttributions()
      if (py === null) ctx.skip()
      if (py === null) return
      const trace = await loadTrace(new Blob([readFileSync(fixture)]))
      const a = analyzeRequests(trace, findIncidents(binTrace(trace)))
      const close = (x: number | null, y: unknown) =>
        x === null ? y === null : typeof y === 'number' && Math.abs(x - y) < 1e-9
      expect(a.calmQueuedP50).toSatisfy((v: number | null) => close(v, py.calm_queued_p50_s))
      expect(a.calmTpotP50).toSatisfy((v: number | null) => close(v, py.calm_tpot_p50_s))
      const want = new Map(
        (py.attributions as Record<string, unknown>[]).map((r) => [r.request_id as string, r]),
      )
      expect(a.requests).toHaveLength(want.size)
      for (const r of a.requests) {
        const w = want.get(r.requestId)!
        const mismatch = [
          ['arrival_s', r.arrivalS],
          ['finish_s', r.finishS],
          ['unattributed_s', r.unattributedS],
          ['queued_vs_calm', r.queuedVsCalm],
          ['tpot_vs_calm', r.tpotVsCalm],
        ].filter(([k, v]) => !close(v as number | null, w[k as string]))
        expect({ id: r.requestId, mismatch }).toEqual({ id: r.requestId, mismatch: [] })
        expect([r.dominant, r.incidents]).toEqual([w.dominant, w.incidents])
      }
    },
    60_000,
  )
})
