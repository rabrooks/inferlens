# InferLens Trace Format — v0.3 (DRAFT)

> **Status: draft.** Until v1.0, the format may change between releases
> without migration support. Schema changes MUST update this document and
> `inferlens/schema/events.py` in the same PR.

## Design goals

1. **Engine-neutral.** Events describe inference-engine semantics
   (scheduling, batching, KV cache, request lifecycle), never one engine's
   internals. Collectors translate; the schema does not know about vLLM or
   SGLang. Where an existing community vocabulary exists (e.g. llm-d's
   KVEvents), prefer its naming.
2. **Crash-tolerant.** A truncated trace must remain readable up to the
   truncation point (this is what makes the flight-recorder use case free),
   and writers must bound the loss window: the reference writer flushes at
   least every second, so a hard-killed recording loses at most that much.
   (Surviving power loss — fsync — is out of scope.)
3. **Cheap to produce.** Writable from a hot path with negligible overhead;
   no schema registry or service required to read a file later.
4. **Self-describing.** A trace file alone is enough to render every view in
   the viewer — no side channels.

## Container

A trace is a **JSON Lines** stream: one JSON object ("record") per `\n`
terminated line, UTF-8, optionally gzip-compressed (detected by the `.gz`
suffix). Conventional extension: `.ilens` / `.ilens.gz`.

Readers MUST ignore unknown record fields, MUST skip records whose `kind`
they don't know (both are how *minor* schema additions stay
forward-compatible — see Versioning), and MUST treat a final line that
fails to parse as end-of-trace, not an error.

## Envelope

Every record carries a `kind` string tag. The first record of a trace SHOULD
be `trace_meta`. All other records carry `ts`.

## Clock model

`ts` is a **monotonic-clock** reading in seconds (float). `trace_meta`
records one `(wall_time_unix, monotonic_time)` pair captured at the same
instant, anchoring monotonic time to wall time. Merged event sources (e.g.
the stat logger and the KV-event subscriber) must share the anchor or record
their own `trace_meta`.

vLLM's KV events don't carry a monotonic timestamp at all — only one
wall-clock `time.time()` per batch, stamped when the scheduler assembled
the batch (not when it was published or received). The KV-event subscriber
therefore uses its own receive-time monotonic clock for `ts`, keeping the
cross-source invariant that every `ts` is directly comparable, and
preserves vLLM's `time.time()` as `wall_time_unix` for reference. Order
`kv_*` events by `seq` (the ZMQ transport sequence number), not `ts` —
see the `kv_*` note under Event kinds below.

## Multi-source recording

A single recording often draws from more than one collector running in
**different OS processes** — for vLLM, the in-process stat logger (inside the
engine's frontend process) and the out-of-process KV-event subscriber (inside
the recorder). Two processes cannot safely share one open file handle, and
their monotonic clocks share no zero point, so raw `ts` is **not** comparable
across processes.

The format therefore records **one file per source**, and merges on read:

- Each source writes an independent, self-contained trace stream, beginning
  with its own `trace_meta` — including its own `(wall_time_unix,
  monotonic_time)` clock anchor. A source that emits no `trace_meta` is not
  independently mergeable.
- To combine sources, convert each stream's monotonic `ts` to wall time via
  *that stream's own* anchor, then merge-sort on the resulting wall time.
  Alignment precision is bounded by the anchor's capture skew and wall-clock
  drift over the recording (typically sub-millisecond to millisecond) — fine
  for timeline correlation, not intended for exact cross-source causal
  ordering. Within a single source, order is exact (`kv_*` by `seq`, others by
  arrival).

This keeps each collector's write path local and crash-tolerant (no
cross-process I/O funnel that could back-pressure an engine's event loop), and
generalizes to N sources — additional engines, data-parallel ranks, or future
collectors. A recorder MAY deliver the merged result as a single trace file;
the merge is defined entirely by the per-source `trace_meta` anchors above, so
it can equally happen at record time or lazily on read.

## Event kinds

### Implemented (schema v0.3)

| kind | one line | emitted by |
| --- | --- | --- |
| `trace_meta` | engine/model identity + clock anchor | recorder startup |
| `engine_snapshot` | per logging step: queue depths, KV usage, prefix-cache stats, preemption count, token throughput, TTFT/ITL summaries (v0.3) | vLLM `SchedulerStats` + `IterationStats` |
| `request_finished` | per-request lifecycle summary: queued/prefill/decode times, token counts, cached tokens, finish reason | vLLM `FinishedRequestStats` |
| `kv_block_stored` | a KV block chain was stored: block hashes, token count (not token content), block size, cache medium/group | vLLM KV events (ZMQ, `--kv-events-config`), `BlockStored` |
| `kv_block_removed` | a KV block was evicted: block hashes, cache medium/group | vLLM KV events (ZMQ), `BlockRemoved` |
| `kv_cache_cleared` | the whole prefix cache was reset | vLLM KV events (ZMQ), `AllBlocksCleared` |
| `collector_gap` | events the collector knows it lost (source stream, cause, seq range) — a trace may have holes, never silent ones | any collector; new in v0.2 |

Field lists are normative in `inferlens/schema/events.py` (dataclasses).

`engine_snapshot`'s `ttft_*`/`itl_*` fields (v0.3) summarize that
iteration's time-to-first-token and inter-token-latency samples as
count/mean/p50/max in seconds (`null` = no samples that iteration). They
are **engine-level distributions, not per-request values** — vLLM's
source arrays carry no request IDs (`upstream-gaps.md` §1), so a viewer
may draw latency bands over time from them but must never attribute them
to individual requests.

### `finish_reason` vocabulary

`request_finished.finish_reason` draws from a canonical, engine-neutral
core:

| value | meaning |
| --- | --- |
| `stop` | natural completion — a stop token/string/pattern matched |
| `length` | `max_tokens` or the context-window limit was reached |
| `abort` | cancelled by the client or by engine shutdown |
| `error` | the request failed with an engine-internal error |

Collectors MUST map engine-native reasons onto these values where the
meaning matches (vLLM's `STOP/LENGTH/ABORT/ERROR` and SGLang's
`stop/length/abort` all map 1:1) and MAY pass through reasons with no
canonical equivalent as additional lowercase strings (e.g. vLLM's
`repetition`). Readers MUST treat unknown values as opaque, not as errors.

`kv_*` events carry three time references instead of one, because their
source batch has no monotonic timestamp (see Clock model above): `ts` is
this collector's own monotonic receive time (comparable to every other
event's `ts`), `wall_time_unix` is vLLM's wall-clock batch-assembly
timestamp (excludes ZMQ queue latency, useful for cross-checking against
`trace_meta`'s anchor), and `seq` is the ZMQ transport sequence number —
the only field that gives exact ordering, since one wall-clock `ts` can
cover a batch of several events.

Viewers SHOULD place `kv_*` events on the timeline by `wall_time_unix`,
not by anchor-converted `ts`. Receive time includes ZMQ delivery latency,
and for events recovered via gap replay it can be *seconds* after the
fact — placement by `ts` would draw a recovered eviction at recovery time,
not when it happened. The `ts`-vs-`wall_time_unix` delta is still useful
as a delivery-latency diagnostic. (If an engine ever publishes properly
anchored event timestamps — see `upstream-gaps.md` §7 — `ts` becomes the
placement field and this note retires; the kv stream's `trace_meta` says
which policy produced it, see Multi-source recording.)

### Planned

| kind | one line | source |
| --- | --- | --- |
| `kv_eviction` | eviction bursts with cause | `SchedulerStats.kv_cache_eviction_events` |
| `request_queued` | request arrival (enables live queue Gantt, not just post-hoc) | API-server middleware or upstream hook |

### Blocked on upstream (see `upstream-gaps.md`)

| kind | one line | blocker |
| --- | --- | --- |
| `step_schedule` | which request IDs ran in a step, with per-request prefill/decode token split | vLLM aggregates per-iteration; no per-step composition exposed |

## Versioning

`trace_meta.schema_version` is `MAJOR.MINOR`. Minor bumps only add optional
fields or new kinds; readers stay compatible. Major bumps may break; the
reader rejects majors it doesn't know. Pre-1.0, all bets are off (see status
banner).

## Open questions

- JSONL vs. a binary framing (msgspec/protobuf) once volume grows — decide
  after measuring real collector overhead; JSONL wins on debuggability until
  then.
- Should block hashes be recorded raw (privacy: they fingerprint prompts) or
  salted per trace? Leaning salted-per-trace by default.
- Multi-engine / data-parallel traces: one file per engine index vs. an
  `engine_index` field on every event. Leaning per-event field.
