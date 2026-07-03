# Writing a collector

A collector translates one engine's runtime signals (stats callbacks, event
streams, log hooks) into the engine-neutral events defined in
[`TRACE_SPEC.md`](TRACE_SPEC.md). The schema, trace I/O, and CLI know
nothing about any engine; collectors are the only place engine knowledge
lives. This page is the contract a new collector (SGLang, TGI, ...) must
follow — the vLLM collector (`inferlens/collectors/vllm/`) is the
reference implementation for every rule below.

## Layout

```text
inferlens/collectors/<engine>/
    __init__.py      # docstring only — no eager imports (see rule 1)
    translate.py     # pure engine-stats → trace-events functions
    ...              # transport/plugin modules as the engine requires
```

## The contract

1. **Never import the engine at module import time.**
   `import inferlens` (and importing your collector package) must work
   without the engine installed. Import lazily, at the point where the
   engine itself is already running — see the module `__getattr__` trick in
   `vllm/stat_logger.py`. Transport-only dependencies (e.g. `pyzmq`)
   belong in an optional extra in `pyproject.toml`.

2. **Be inert unless explicitly enabled.**
   Merely installing inferlens must never change an engine's behavior.
   The vLLM collector does nothing unless `INFERLENS_TRACE_PATH` is set.

3. **Emit `trace_meta` first**, carrying the engine identity and the
   `(wall_time_unix, monotonic_time)` clock anchor captured at the same
   instant.

4. **Stamp `ts` from your own monotonic clock**, never from engine wall
   time — see the clock model in `TRACE_SPEC.md`. Engine-supplied
   timestamps that are worth preserving go in dedicated fields (the way
   `kv_*` events keep `wall_time_unix`).

5. **Never block or raise into the engine.**
   Collectors run inside or alongside a latency-sensitive serving process.
   Write through `trace_io.EventSink` (in practice `BufferedTraceWriter`,
   which enqueues without blocking and drops on overload). On an
   unexpected translation failure, disable yourself loudly (log an
   exception) rather than taking requests down with you.

6. **Holes are allowed; silent holes are not — and a gap is never fatal.**
   If you know events were lost (a dropped stream, an unrecoverable
   sequence gap), keep recording and write a `collector_gap` event so the
   viewer can render "data missing here".

7. **Map values onto the spec's vocabularies** — e.g. `finish_reason`'s
   canonical `stop`/`length`/`abort`/`error` — rather than inventing
   engine-flavored strings. Extensions are allowed where the spec says so.

8. **Keep translation pure and duck-typed.**
   Functions in `translate.py` take engine stats objects by attribute
   access only, so they are unit-testable with `SimpleNamespace` fakes and
   no engine installed. Read required fields directly (fail loudly on
   engine drift); use `getattr` defaults only for genuinely optional data.

9. **Schema changes ship with the spec.** A new event kind or field
   updates `TRACE_SPEC.md` in the same PR, following its versioning rules.

10. **Never monkeypatch engine internals.** Data the engine doesn't expose
    goes in [`upstream-gaps.md`](upstream-gaps.md) as an
    upstream-contribution candidate.

## Tests a collector needs

- Translation unit tests with faked stats objects (no engine required) —
  see `tests/test_vllm_translate.py`.
- Transport tests against the real wire protocol where feasible — see
  `tests/test_vllm_kv_events.py`, which runs real ZMQ sockets.
- A runnable end-to-end smoke script under `examples/` against the real
  engine, for catching upstream drift — see `examples/vllm_plugin_smoke.py`.
