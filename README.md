# InferLens

**Engine-neutral observability for LLM inference engines.** Record what vLLM
(and soon SGLang) is actually doing internally — scheduling, batching,
KV-cache behavior — and explore it as an interactive timeline that answers
*"why was this request slow?"*

> **Status: pre-alpha.** The trace schema and vLLM collector are under
> active development. Nothing here is stable yet — including the trace
> format.

## Why

Today's tooling for inference engines is bimodal:

- **Dashboards** (Grafana/Prometheus) aggregate counters — `kv_cache_usage`,
  `num_running_reqs`, throughput. Good for "is something wrong," useless for
  "why was *this* request slow."
- **Profilers** (Nsight, torch profiler) see every kernel launch and none of
  the scheduler's decisions. Too low-level to answer a scheduling question.

The scheduler's actual behavior — per-step batch composition, KV-cache
pressure and eviction, preemption, prefix-cache hits, per-request
queued→prefill→decode timing — exists in the engine process for a few
microseconds and then is gone. InferLens targets that missing middle layer,
captured with a pip-installed plugin and rendered as an interactive
timeline:

- per-step scheduler state: queue depths, batch composition, preemptions
- KV-cache behavior: usage, prefix-cache hits, evictions
- per-request lifecycle: queued → prefill → decode, with token timings

## How it works

```text
vLLM ──(stat-logger plugin + KV events)──►  trace file (.ilens.gz)  ──►  inferlens view
SGLang ──(collector, planned)───────────►       one shared schema        interactive timeline
```

```bash
pip install inferlens
inferlens record -o run.ilens.gz -- vllm serve Qwen/Qwen2.5-1.5B-Instruct   # in development
inferlens info run.ilens.gz
inferlens view run.ilens.gz                                                  # in development
```

It uses vLLM's public stat-logger plugin interface and its existing
KV-events stream — no engine fork, no monkeypatching, works against a stock
build.

## Safe to run in production

- **Zero behavioral change when idle.** The plugin is a no-op unless
  `INFERLENS_TRACE_PATH` is set — installing the package can't change engine
  behavior.
- **Measured overhead, not assumed.** A/B'd on an A100 against real
  `vllm serve` at non-saturating load: throughput −0.05%, p50 latency 0%,
  p95 +≤10 ms, zero dropped trace events. Full method in
  [docs/overhead.md](docs/overhead.md).
- **Bounded loss on crash, not silent corruption.** The writer flushes every
  1s by default; a gap is recorded in-trace as a `collector_gap` event
  rather than faking continuity or crashing the reader.

## Why this matters for llm-d

Disaggregated serving — separate prefill/decode pools, KV transfer between
them — multiplies the same gap: scheduler and KV state now need correlating
*across* engine instances, not just within one. InferLens's per-source-file,
wall-clock-anchored merge model is built for exactly that; multi-node
recording and an SGLang collector are the natural next steps, not yet
shipped.

See [docs/overview.md](docs/overview.md) for the full technical case,
including the cross-process clock model and an honest list of what's still
missing upstream.

## Development

```bash
uv sync
uv run pre-commit install
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for coding standards (Google style,
DCO sign-off, vLLM-style PR conventions) and
[docs/TRACE_SPEC.md](docs/TRACE_SPEC.md) for the trace format.

## License

[Apache-2.0](LICENSE)
