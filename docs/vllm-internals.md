# vLLM V1 internals — study notes

> Working notes on the vLLM V1 internals that the collector builds on.
> Written against vLLM `main`; pin commit hashes next to claims since these
> internals move fast.

## Reading list

- [ ] `vllm/v1/core/sched/scheduler.py` — the V1 scheduler loop: waiting vs.
      running queues, token budgets, preemption
- [ ] `vllm/v1/core/kv_cache_manager.py` — block allocation, prefix caching,
      eviction
- [ ] `vllm/v1/metrics/stats.py` + `loggers.py` — what stat loggers receive,
      and when (`SchedulerStats`, `IterationStats`, `FinishedRequestStats`)
- [ ] `vllm/distributed/kv_events.py` — KV event publishing (`BlockStored`,
      `BlockRemoved`), ZMQ transport, `--kv-events-config`
- [ ] `vllm/plugins/__init__.py` — plugin loading;
      `STAT_LOGGER_PLUGINS_GROUP = "vllm.stat_logger_plugins"`

## Questions to answer during the read

1. Exact `StatLoggerBase` interface: method signatures, call frequency
   (every step vs. logging interval), threading context.
2. When `record()` is called relative to scheduler steps — one call per
   `EngineCoreOutputs`, or batched?
3. KV-event timestamps: which clock, and how to align with stat-logger time.
4. What triggers preemption in V1 (KV-block exhaustion only?), and what
   `kv_cache_eviction_events` actually contains.
5. Overhead budget: what do the built-in Prometheus/logging stat loggers cost
   per step, as a baseline for ours.

## Notes

(fill in during the deep-read)
