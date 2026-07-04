/**
 * Trace event types — the TypeScript mirror of `inferlens/schema/events.py`
 * (schema v0.3). Field lists are normative there; keep the two in sync in
 * the same PR as any schema change.
 *
 * Every loaded event additionally carries `t`: its *placement* time on the
 * viewer timeline, in wall-clock unix seconds. For most events `t` is the
 * monotonic `ts` rebased through the trace_meta wall anchor; for `kv_*`
 * events it is their own `wall_time_unix`, per the spec — receive-time `ts`
 * would draw gap-replayed events at recovery time, not when they happened.
 */

export const SUPPORTED_SCHEMA_MAJOR = '0'

/** 64-bit block hashes overflow JS numbers; past 2^53 they load as strings. */
export type BlockHash = number | string

export interface TraceMeta {
  kind: 'trace_meta'
  engine: string
  engine_version: string
  model: string
  wall_time_unix: number
  monotonic_time: number
  schema_version: string
  extra: Record<string, unknown>
}

interface Placed {
  /** Placement time on the viewer timeline (wall-clock unix seconds). */
  t: number
  /** Monotonic-clock reading from the source process (seconds). */
  ts: number
}

export interface EngineSnapshot extends Placed {
  kind: 'engine_snapshot'
  num_running_reqs: number
  num_waiting_reqs: number
  kv_cache_usage: number
  prefix_cache_queries: number
  prefix_cache_hits: number
  num_preempted_reqs: number
  num_generation_tokens: number
  num_prompt_tokens: number
  ttft_count: number
  ttft_mean_s: number | null
  ttft_p50_s: number | null
  ttft_max_s: number | null
  itl_count: number
  itl_mean_s: number | null
  itl_p50_s: number | null
  itl_max_s: number | null
}

export interface RequestFinished extends Placed {
  kind: 'request_finished'
  request_id: string
  finish_reason: string
  e2e_latency_s: number
  queued_time_s: number
  prefill_time_s: number
  decode_time_s: number
  num_prompt_tokens: number
  num_generation_tokens: number
  num_cached_tokens: number
}

export interface KVBlockStored extends Placed {
  kind: 'kv_block_stored'
  seq: number
  wall_time_unix: number
  block_hashes: BlockHash[]
  parent_block_hash: BlockHash | null
  num_tokens: number
  block_size: number
  medium: string | null
  group_idx: number
}

export interface KVBlockRemoved extends Placed {
  kind: 'kv_block_removed'
  seq: number
  wall_time_unix: number
  block_hashes: BlockHash[]
  medium: string | null
  group_idx: number
}

export interface KVCacheCleared extends Placed {
  kind: 'kv_cache_cleared'
  seq: number
  wall_time_unix: number
}

export interface CollectorGap extends Placed {
  kind: 'collector_gap'
  source: string
  reason: string
  first_seq: number | null
  last_seq: number | null
}

export type TimedEvent =
  | EngineSnapshot
  | RequestFinished
  | KVBlockStored
  | KVBlockRemoved
  | KVCacheCleared
  | CollectorGap

export type TraceEvent = TraceMeta | TimedEvent

/** Reading anomalies the UI should surface rather than hide. */
export interface TraceWarnings {
  /** Records skipped because their kind is unknown (kind → count). */
  unknownKinds: Record<string, number>
  /** Records skipped because required fields for their kind were missing. */
  malformedRecords: number
  /** Mid-stream trace_meta records ignored (not part of the format). */
  extraMetas: number
  /** The stream ended early (truncated gzip or an undecodable line). */
  truncated: boolean
  /** No trace_meta anchor: `t` falls back to raw `ts` and kv_* events are
   * not comparable to other sources. */
  noMeta: boolean
}

export interface Trace {
  meta: TraceMeta | null
  snapshots: EngineSnapshot[]
  requests: RequestFinished[]
  kvStored: KVBlockStored[]
  kvRemoved: KVBlockRemoved[]
  kvCleared: KVCacheCleared[]
  gaps: CollectorGap[]
  /** Timeline domain: [start, end] over all events' placement `t`. */
  start: number
  end: number
  /** Total events loaded (including trace_meta). */
  eventCount: number
  warnings: TraceWarnings
}
