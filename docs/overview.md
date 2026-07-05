# Why InferLens exists

A technical case for readers who already run vLLM (or llm-d) in anger and
want to know what problem this solves before they read any code.

## The gap between dashboards and profilers

Today's tooling for inference engines is bimodal:

- **Grafana/Prometheus dashboards** aggregate counters —
  `kv_cache_usage`, `num_running_reqs`, throughput. Good for "is something
  wrong," useless for "why was *this* request slow." You can't see
  individual preemptions, which requests shared a batch, or whether a
  4-second TTFT was caused by queueing, prefill contention, or a KV
  eviction storm.
- **Kernel/GPU profilers** (Nsight, torch profiler) see every kernel
  launch and none of the scheduler's decisions. Too low-level to answer a
  scheduling question.

The scheduler's actual behavior — per-step batch composition, KV-cache
pressure and eviction, preemption, prefix-cache hits, per-request
queued→prefill→decode timing — exists in the engine process for a few
microseconds and then is gone. InferLens's thesis: that middle layer needs
to be a durable, replayable artifact, not something reconstructed from
log-grepping during an incident.

## What it is

A recording and viewing pipeline, engine-neutral by design:

```text
vLLM ──(stat-logger plugin + KV-event subscriber)──►  .ilens.gz trace file  ──►  interactive timeline viewer
SGLang (planned) ──(same collector contract)──────►     one shared schema
```

`pip install inferlens`, then `inferlens record -- vllm serve ...` — no
engine fork, no monkeypatching. It uses vLLM's public stat-logger plugin
interface plus its existing KV-events ZMQ stream, so it works against a
stock, auto-upgraded vLLM build. The output is a portable trace
(`docs/TRACE_SPEC.md` is the normative schema) that the viewer renders as
a Gantt-style timeline: engine-level scheduler/KV state on one track,
per-request lifecycles on another, so a slow request's cause — preempted
here, re-queued, batch starved of KV blocks until this eviction — is
visible directly.

## Safe to run in production

- **Zero behavioral change when idle.** The plugin is a no-op unless
  `INFERLENS_TRACE_PATH` is set — installing the package cannot change
  engine behavior.
- **Measured overhead, not assumed.** A/B'd on an A100 against real
  `vllm serve` at non-saturating load (10 req/s, 150 s, 3+3 interleaved
  runs to cancel drift): throughput −0.05%, p50 latency 0%, p95 +≤10 ms,
  zero dropped trace events. See [overhead.md](overhead.md) for the full
  method. The design reason is mechanical: the stat-logger hands snapshots
  to a bounded background-writer queue, and the KV subscriber runs
  out-of-process, so nothing blocks the engine's step loop.
- **Bounded loss on crash, not silent corruption.** The writer flushes
  every 1 s by default; a gap is recorded in-trace as a `collector_gap`
  event rather than faking continuity or crashing the reader.

## A hard problem it had to solve: cross-process clock alignment

The stat-logger runs inside vLLM's frontend process; the KV-event
subscriber runs in the wrapper process. Two OS processes means two
monotonic clocks that are never comparable. InferLens's fix — each source
stamps its own wall-clock anchor in its `trace_meta`, and merge/replay
rebases everything off those anchors — is the permanent mechanism (matches
Perfetto/OTel practice for merging cross-process traces), not a stopgap.
This was validated end-to-end on a GPU incident trace: evictions and
preemptions correlate at r = 0.934 per-2-second bin across the two
sources, and request e2e p50 independently matched the workload
generator's own reported p50.

## Honest about limits, not papering over them

[`upstream-gaps.md`](upstream-gaps.md) tracks data InferLens wants but
vLLM doesn't expose yet — e.g. per-step batch composition (which request
IDs shared a step and their prefill/decode split), per-request preemption
attribution, absolute KV block counts vs. the single 0–1 usage float.
Rather than reverse-engineering these from internals, each gap is written
up as a future upstream RFC candidate with exact line citations against a
pinned vLLM commit. That review also turned up a real vLLM bug independent
of InferLens: the reference KV-event subscriber uses a ZMQ REQ socket,
whose FSM allows only one `recv()` per `send()` — so it silently drops
every replayed event past the first on any gap. InferLens's own subscriber
uses DEALER and does not have this bug; not yet reported upstream.

## Relevance to llm-d

llm-d's value proposition is disaggregated, multi-node vLLM (separate
prefill/decode pools, KV transfer between them), which multiplies exactly
the gap InferLens targets: now scheduler/KV state needs correlating
*across* engine instances, not just within one. InferLens's
per-source-file-merged-by-wall-anchor model is the same primitive a
multi-node trace would need — nothing in the current design assumes
single-node, though multi-node recording and the SGLang collector are
future work, not shipped.

## Current status

Pre-alpha. The vLLM collector (stat-logger + KV events) and trace format
are implemented and GPU-validated end-to-end against an incident trace
with real preemption storms. The viewer (the timeline UI) is an early
scaffold. The SGLang collector has not been started. The trace schema is
explicitly unstable pre-1.0.
