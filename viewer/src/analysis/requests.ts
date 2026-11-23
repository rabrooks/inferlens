/**
 * Per-request latency attribution for the request Gantt and detail panel.
 *
 * A port of the attribution half of `inferlens/analysis.py` (`analyze` /
 * `_attribute`), so the request detail shows exactly what
 * `inferlens explain --request ID` prints. Keep the two in sync —
 * `requests.test.ts` cross-checks every request on the real fixture.
 *
 * Phases are reported by the engine and do not tile e2e: the remainder
 * (`unattributedS`) is frontend time and anything the engine does not
 * attribute to a phase. It is shown, never redistributed.
 */
import type { RequestFinished, Trace } from '../trace/types'
import type { Incident } from './incidents'

/** analysis.MIN_CALM_REQUESTS: below this there is no calm baseline. */
export const MIN_CALM_REQUESTS = 5
/** analysis.MIN_RATIO_BASELINE_S: calm queue medians below this aren't divided by. */
export const MIN_RATIO_BASELINE_S = 0.01

export type Phase = 'queued' | 'prefill' | 'decode' | 'unattributed'

export interface RequestAttr {
  /** Index into the attribution list (stable id for selection). */
  index: number
  requestId: string
  /** Seconds since trace start (arrival may be negative). */
  arrivalS: number
  finishS: number
  e2eS: number
  queuedS: number
  prefillS: number
  decodeS: number
  unattributedS: number
  dominant: Phase
  numPreemptions: number | null
  promptTokens: number
  cachedTokens: number
  generationTokens: number
  finishReason: string
  /** Ids of incidents overlapping [arrival, finish] in time. */
  incidents: number[]
  queuedVsCalm: number | null
  tpotVsCalm: number | null
  /** Packed Gantt lane (requests in one lane never overlap). */
  lane: number
}

export interface LatencyStats {
  count: number
  p50: number | null
  p95: number | null
  max: number | null
}

export interface RequestAnalysis {
  requests: RequestAttr[]
  lanes: number
  all: LatencyStats
  calm: LatencyStats
  calmQueuedP50: number | null
  calmTpotP50: number | null
}

export function analyzeRequests(trace: Trace, incidents: Incident[]): RequestAnalysis {
  const valid = trace.requests.filter((r) => r.e2e_latency_s > 0)
  const spans = valid.map((r) => {
    const finish = r.t - trace.start
    return { r, finish, arrival: finish - r.e2e_latency_s }
  })
  const overlaps = spans.map(({ arrival, finish }) =>
    incidents.filter((i) => arrival < i.endS && finish > i.startS).map((i) => i.id),
  )
  const calmReqs = valid.filter((_, k) => overlaps[k].length === 0)
  const calm = stats(calmReqs.map((r) => r.e2e_latency_s))
  const haveCalm = calm.count >= MIN_CALM_REQUESTS
  const calmQueuedP50 = haveCalm ? median(calmReqs.map((r) => r.queued_time_s)) : null
  const calmTpotP50 = haveCalm
    ? median(calmReqs.map(tpot).filter((t): t is number => t !== null))
    : null

  const lanes = packLanes(spans.map((s) => [s.arrival, s.finish]))
  const requests = spans.map(({ r, arrival, finish }, k): RequestAttr => {
    const unattributedS = r.e2e_latency_s - (r.queued_time_s + r.prefill_time_s + r.decode_time_s)
    const parts: [Phase, number][] = [
      ['queued', r.queued_time_s],
      ['prefill', r.prefill_time_s],
      ['decode', r.decode_time_s],
      ['unattributed', unattributedS],
    ]
    // First max wins on ties, like Python's max() over the dict.
    const dominant = parts.reduce((best, p) => (p[1] > best[1] ? p : best))[0]
    return {
      index: k,
      requestId: r.request_id,
      arrivalS: arrival,
      finishS: finish,
      e2eS: r.e2e_latency_s,
      queuedS: r.queued_time_s,
      prefillS: r.prefill_time_s,
      decodeS: r.decode_time_s,
      unattributedS,
      dominant,
      numPreemptions: r.num_preemptions,
      promptTokens: r.num_prompt_tokens,
      cachedTokens: r.num_cached_tokens,
      generationTokens: r.num_generation_tokens,
      finishReason: r.finish_reason,
      incidents: overlaps[k],
      queuedVsCalm: ratio(r.queued_time_s, calmQueuedP50, MIN_RATIO_BASELINE_S),
      tpotVsCalm: ratio(tpot(r), calmTpotP50),
      lane: lanes.lane[k],
    }
  })
  return {
    requests,
    lanes: lanes.count,
    all: stats(valid.map((r) => r.e2e_latency_s)),
    calm,
    calmQueuedP50,
    calmTpotP50,
  }
}

/**
 * Greedy interval packing by arrival (same rule as the Perfetto export):
 * each request takes the lowest lane whose last request ended before it
 * arrived. Returns a lane per input interval, in input order.
 */
export function packLanes(spans: [number, number][]): { lane: number[]; count: number } {
  const order = spans.map((_, i) => i).sort((a, b) => spans[a][0] - spans[b][0])
  const freeAt: number[] = []
  const lane = new Array<number>(spans.length)
  for (const i of order) {
    const [start, end] = spans[i]
    let l = freeAt.findIndex((f) => f < start)
    if (l === -1) {
      l = freeAt.length
      freeAt.push(end)
    } else freeAt[l] = end
    lane[i] = l
  }
  return { lane, count: freeAt.length }
}

/**
 * Phase segments as drawn: right-aligned to finish (decode ends at finish,
 * prefill before it, queued before that), clipped at arrival. Whatever is
 * left at the head is the unattributed remainder — the same placement as
 * `inferlens export`.
 */
export function phaseSegments(r: RequestAttr): { phase: Phase; t0: number; t1: number }[] {
  const out: { phase: Phase; t0: number; t1: number }[] = []
  let end = r.finishS
  for (const [phase, dur] of [
    ['decode', r.decodeS],
    ['prefill', r.prefillS],
    ['queued', r.queuedS],
  ] as const) {
    const start = Math.max(end - Math.max(dur, 0), r.arrivalS)
    if (end > start) out.unshift({ phase, t0: start, t1: end })
    end = start
  }
  if (end > r.arrivalS) out.unshift({ phase: 'unattributed', t0: r.arrivalS, t1: end })
  return out
}

function tpot(r: RequestFinished): number | null {
  // The first output token comes from prefill.
  return r.num_generation_tokens < 2 ? null : r.decode_time_s / (r.num_generation_tokens - 1)
}

export function stats(values: number[]): LatencyStats {
  if (values.length === 0) return { count: 0, p50: null, p95: null, max: null }
  const s = [...values].sort((a, b) => a - b)
  return { count: s.length, p50: percentile(s, 50), p95: percentile(s, 95), max: s.at(-1)! }
}

function median(values: number[]): number | null {
  return values.length ? percentile([...values].sort((a, b) => a - b), 50) : null
}

/** Linear-interpolated percentile of a sorted, non-empty list (analysis._percentile). */
function percentile(sorted: number[], pct: number): number {
  if (sorted.length === 1) return sorted[0]
  const pos = ((sorted.length - 1) * pct) / 100
  const lo = Math.floor(pos)
  const hi = Math.min(lo + 1, sorted.length - 1)
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo)
}

function ratio(value: number | null, baseline: number | null, floor = 0): number | null {
  if (value === null || baseline === null || baseline <= floor || baseline <= 0) return null
  return value / baseline
}
