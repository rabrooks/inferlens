# InferLens

**Engine-neutral observability for LLM inference engines.** Record what vLLM
(and soon SGLang) is actually doing internally — scheduling, batching,
KV-cache behavior — and explore it as an interactive timeline that answers
*"why was this request slow?"*

> **Status: pre-alpha.** The trace schema and vLLM collector are under
> active development. Nothing here is stable yet — including the trace
> format.

## Why

Today's tooling for inference engines is bimodal: Grafana dashboards of
aggregate metrics (too high-level to explain a single slow request) and
kernel-level profiler traces (too low-level to see scheduler semantics).
InferLens targets the missing middle layer:

- per-step scheduler state: queue depths, batch composition, preemptions
- KV-cache behavior: usage, prefix-cache hits, evictions
- per-request lifecycle: queued → prefill → decode, with token timings

…captured with a pip-installed plugin (no engine fork), written to a portable
trace file, and rendered in a local viewer.

## How it will work

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
