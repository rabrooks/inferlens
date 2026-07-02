# Upstream gaps

Data InferLens needs that vLLM/SGLang don't expose today. Each entry is a
future upstream RFC/PR with InferLens as the motivating consumer. We do NOT
monkeypatch around these — that's the whole point of the list.

Template: what's missing / where in the code / why we need it / proposed
change / status.

## vLLM

### 1. Per-step batch composition

- **Missing:** which request IDs were scheduled in each engine step, and
  each one's prefill vs. decode token split for that step.
- **Where:** `vllm/v1/metrics/stats.py` — `IterationStats` aggregates token
  counts per iteration; `SchedulerOutput` has the data internally but it
  never reaches stat loggers.
- **Why:** the per-request Gantt and "what shared my batch?" analysis — the
  core viewer feature — is impossible to reconstruct from aggregates.
- **Proposed:** an opt-in detailed mode where stat loggers receive
  per-step scheduled-request summaries (ids + token splits), gated by a flag
  since it adds per-step allocation.
- **Status:** not filed; validate need during collector implementation.

### 2. Per-request preemption attribution

- **Missing:** *which* request was preempted (and why); only
  `IterationStats.num_preempted_reqs` (a count) is exposed.
- **Where:** `vllm/v1/metrics/stats.py`, scheduler preemption path.
- **Why:** explaining a specific slow request requires knowing it was
  preempted, not that "someone" was.
- **Proposed:** preemption events (request id, reason, step) surfaced to
  stat loggers, or in `FinishedRequestStats` (preemption count per request).
- **Status:** not filed; validate need during collector implementation.

### 3. Request arrival events

- **Missing:** stat loggers see requests only at completion
  (`FinishedRequestStats`); arrival/queue-entry has no plugin-visible event.
- **Why:** live views and traces of *incomplete* requests (the interesting
  ones during an incident) need arrival timestamps at record time.
- **Workaround:** derive arrival post-hoc from `e2e_latency` at completion;
  API-server middleware can capture arrivals for online serving. Neither
  covers in-flight requests at trace end.
- **Status:** workaround acceptable for now; revisit once the viewer needs
  in-flight requests.

## SGLang

(Populate when the SGLang adapter work begins. Expect scheduler stats to
need a pluggable hook comparable to vLLM's `vllm.stat_logger_plugins`.)
