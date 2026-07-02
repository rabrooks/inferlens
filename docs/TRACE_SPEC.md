# InferLens Trace Format — v0.1 (DRAFT)

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
   truncation point (this is what makes the flight-recorder use case free).
3. **Cheap to produce.** Writable from a hot path with negligible overhead;
   no schema registry or service required to read a file later.
4. **Self-describing.** A trace file alone is enough to render every view in
   the viewer — no side channels.

## Container

A trace is a **JSON Lines** stream: one JSON object ("record") per `\n`
terminated line, UTF-8, optionally gzip-compressed (detected by the `.gz`
suffix). Conventional extension: `.ilens` / `.ilens.gz`.

Readers MUST ignore unknown record fields (forward compatibility) and MUST
treat a final line that fails to parse as end-of-trace, not an error.

## Envelope

Every record carries a `kind` string tag. The first record of a trace SHOULD
be `trace_meta`. All other records carry `ts`.

## Clock model

`ts` is a **monotonic-clock** reading in seconds (float). `trace_meta`
records one `(wall_time_unix, monotonic_time)` pair captured at the same
instant, anchoring monotonic time to wall time. Merged event sources (e.g.
the stat logger and the KV-event subscriber) must share the anchor or record
their own `trace_meta`.

## Event kinds

### Implemented (schema v0.1)

| kind | one line | emitted by |
| --- | --- | --- |
| `trace_meta` | engine/model identity + clock anchor | recorder startup |
| `engine_snapshot` | per logging step: queue depths, KV usage, prefix-cache stats, preemption count, token throughput | vLLM `SchedulerStats` + `IterationStats` |
| `request_finished` | per-request lifecycle summary: queued/prefill/decode times, token counts, cached tokens, finish reason | vLLM `FinishedRequestStats` |

Field lists are normative in `inferlens/schema/events.py` (dataclasses).

### Planned

| kind | one line | source |
| --- | --- | --- |
| `kv_block_stored` / `kv_block_removed` / `kv_cache_cleared` | KV block lifecycle with block hashes | vLLM KV events (ZMQ, `--kv-events-config`) |
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
