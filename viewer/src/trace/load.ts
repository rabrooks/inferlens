/**
 * Browser-side trace reader — the TypeScript mirror of
 * `inferlens.trace_io.read_trace`, with the same tolerance rules from
 * TRACE_SPEC.md: unknown record fields are ignored, unknown kinds are
 * skipped, an undecodable line or truncated gzip stream ends the trace
 * (a crashed recording keeps its readable prefix), and an unsupported
 * schema *major* version is the one hard error.
 */

import {
  SUPPORTED_SCHEMA_MAJOR,
  type CollectorGap,
  type EngineSnapshot,
  type KVBlockRemoved,
  type KVBlockStored,
  type KVCacheCleared,
  type RequestFinished,
  type Trace,
  type TraceMeta,
  type TraceWarnings,
} from './types'

export class TraceSchemaError extends Error {}

/** Required fields and defaults per kind (normative: schema/events.py). */
const KINDS: Record<
  string,
  { required: string[]; defaults: Record<string, unknown> }
> = {
  trace_meta: {
    required: ['engine', 'engine_version', 'model', 'wall_time_unix', 'monotonic_time'],
    defaults: { schema_version: '0.3', extra: {} },
  },
  engine_snapshot: {
    required: ['ts', 'num_running_reqs', 'num_waiting_reqs', 'kv_cache_usage'],
    defaults: {
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
    },
  },
  request_finished: {
    required: [
      'ts',
      'request_id',
      'finish_reason',
      'e2e_latency_s',
      'queued_time_s',
      'prefill_time_s',
      'decode_time_s',
      'num_prompt_tokens',
      'num_generation_tokens',
    ],
    defaults: { num_cached_tokens: 0 },
  },
  kv_block_stored: {
    required: [
      'ts',
      'seq',
      'wall_time_unix',
      'block_hashes',
      'parent_block_hash',
      'num_tokens',
      'block_size',
    ],
    defaults: { medium: null, group_idx: 0 },
  },
  kv_block_removed: {
    required: ['ts', 'seq', 'wall_time_unix', 'block_hashes'],
    defaults: { medium: null, group_idx: 0 },
  },
  kv_cache_cleared: {
    required: ['ts', 'seq', 'wall_time_unix'],
    defaults: {},
  },
  collector_gap: {
    required: ['ts', 'source', 'reason'],
    defaults: { first_seq: null, last_seq: null },
  },
}

/**
 * KV block hashes are 64-bit integers; JSON.parse silently rounds numbers
 * past 2^53, which would collide distinct hashes. Where the runtime supports
 * the source-access reviver (V8 ≥ ~12.2), integers outside the safe range
 * load as their exact source string instead — `BlockHash` is typed
 * `number | string` for exactly this.
 */
const parseJson: (line: string) => unknown = (() => {
  try {
    const probe = JSON.parse('9007199254740993', (_k, v, ctx?: { source?: string }) =>
      ctx?.source !== undefined ? ctx.source : v,
    )
    if (probe !== '9007199254740993') return JSON.parse
  } catch {
    return JSON.parse
  }
  return (line: string) =>
    JSON.parse(line, (_key, value, ctx?: { source?: string }) => {
      if (
        typeof value === 'number' &&
        !Number.isSafeInteger(value) &&
        Number.isInteger(value) &&
        ctx?.source !== undefined &&
        !/[.eE]/.test(ctx.source)
      ) {
        return ctx.source
      }
      return value
    })
})()

async function isGzip(blob: Blob): Promise<boolean> {
  const head = new Uint8Array(await blob.slice(0, 2).arrayBuffer())
  return head.length === 2 && head[0] === 0x1f && head[1] === 0x8b
}

/**
 * Yield lines from the (possibly gzipped) blob. A mid-stream decode error —
 * a gzip member cut off by a crashed recorder — ends the stream quietly:
 * everything decompressed so far is the trace's readable prefix.
 */
async function* lines(blob: Blob): AsyncGenerator<string> {
  // lib.dom types each transform's writable side as BufferSource, which TS 6
  // no longer unifies with the blob stream's Uint8Array — runtime-safe casts.
  let stream = blob.stream() as ReadableStream<Uint8Array>
  if (await isGzip(blob)) {
    stream = stream.pipeThrough(
      new DecompressionStream('gzip') as unknown as ReadableWritablePair<
        Uint8Array,
        Uint8Array
      >,
    )
  }
  const reader = stream
    .pipeThrough(
      new TextDecoderStream() as unknown as ReadableWritablePair<string, Uint8Array>,
    )
    .getReader()
  let buf = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += value
      let nl
      while ((nl = buf.indexOf('\n')) >= 0) {
        yield buf.slice(0, nl)
        buf = buf.slice(nl + 1)
      }
    }
  } catch {
    // Truncated gzip: fall through with whatever decompressed cleanly. The
    // partial line below then fails JSON.parse, which ends the trace.
  }
  if (buf) yield buf
}

function schemaMajor(version: string): string {
  return version.split('.', 1)[0]
}

/**
 * Load a trace file into typed, timeline-placed event arrays.
 *
 * @throws {TraceSchemaError} If the trace declares a schema major version
 *   this reader doesn't support.
 */
export async function loadTrace(blob: Blob): Promise<Trace> {
  const warnings: TraceWarnings = {
    unknownKinds: {},
    malformedRecords: 0,
    extraMetas: 0,
    truncated: false,
    noMeta: false,
  }

  let meta: TraceMeta | null = null
  const snapshots: EngineSnapshot[] = []
  const requests: RequestFinished[] = []
  const kvStored: KVBlockStored[] = []
  const kvRemoved: KVBlockRemoved[] = []
  const kvCleared: KVCacheCleared[] = []
  const gaps: CollectorGap[] = []
  const byKind: Record<string, unknown[]> = {
    engine_snapshot: snapshots,
    request_finished: requests,
    kv_block_stored: kvStored,
    kv_block_removed: kvRemoved,
    kv_cache_cleared: kvCleared,
    collector_gap: gaps,
  }
  let eventCount = 0

  for await (const line of lines(blob)) {
    const text = line.trim()
    if (!text) continue
    let record: unknown
    try {
      record = parseJson(text)
    } catch {
      // Interrupted recording: the readable prefix is valid.
      warnings.truncated = true
      break
    }
    if (typeof record !== 'object' || record === null || Array.isArray(record)) {
      warnings.malformedRecords++
      continue
    }
    const rec = record as Record<string, unknown>
    const kind = rec.kind
    if (typeof kind !== 'string' || !(kind in KINDS)) {
      const key = String(kind)
      warnings.unknownKinds[key] = (warnings.unknownKinds[key] ?? 0) + 1
      continue
    }
    const { required, defaults } = KINDS[kind]
    if (required.some((f) => rec[f] === undefined)) {
      warnings.malformedRecords++
      continue
    }
    const event = { ...defaults, ...rec }
    if (kind === 'trace_meta') {
      const m = event as unknown as TraceMeta
      if (schemaMajor(m.schema_version) !== SUPPORTED_SCHEMA_MAJOR) {
        throw new TraceSchemaError(
          `unsupported trace schema version '${m.schema_version}' ` +
            `(this viewer supports major ${SUPPORTED_SCHEMA_MAJOR})`,
        )
      }
      if (meta === null) {
        meta = m
        eventCount++
      } else {
        // A mid-stream re-anchor isn't part of the format; its events were
        // anchored by the first meta (matches trace_io's merge behavior).
        warnings.extraMetas++
      }
      continue
    }
    byKind[kind].push(event)
    eventCount++
  }

  // Placement: rebase monotonic ts onto wall time via the anchor; kv_* place
  // by their own wall_time_unix (see types.ts). Without an anchor, fall back
  // to raw ts everywhere so a single-source trace still renders.
  warnings.noMeta = meta === null
  const offset = meta === null ? 0 : meta.wall_time_unix - meta.monotonic_time
  for (const list of [snapshots, requests, gaps] as { t: number; ts: number }[][]) {
    for (const e of list) e.t = e.ts + offset
  }
  for (const list of [kvStored, kvRemoved, kvCleared]) {
    for (const e of list) e.t = meta === null ? e.ts : e.wall_time_unix
  }

  for (const list of [snapshots, requests, gaps]) {
    ;(list as { t: number }[]).sort((a, b) => a.t - b.t)
  }
  for (const list of [kvStored, kvRemoved, kvCleared]) {
    ;(list as { t: number; seq: number }[]).sort((a, b) => a.t - b.t || a.seq - b.seq)
  }

  let start = Infinity
  let end = -Infinity
  for (const list of Object.values(byKind) as { t: number }[][]) {
    if (list.length > 0) {
      start = Math.min(start, list[0].t)
      end = Math.max(end, list[list.length - 1].t)
    }
  }
  if (start === Infinity) {
    start = meta?.wall_time_unix ?? 0
    end = start
  }

  return {
    meta,
    snapshots,
    requests,
    kvStored,
    kvRemoved,
    kvCleared,
    gaps,
    start,
    end,
    eventCount,
    warnings,
  }
}
