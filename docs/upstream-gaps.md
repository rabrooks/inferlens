# Upstream gaps

Data InferLens needs that vLLM/SGLang don't expose today. Each entry is a
future upstream RFC/PR with InferLens as the motivating consumer. We do NOT
monkeypatch around these — that's the whole point of the list.

Template: what's missing / where in the code / why we need it / proposed
change / status.

Line citations pinned to vLLM `main` @ `63fcce4de` (2026-07-01); see
`vllm-internals.md` for the surrounding analysis.

## vLLM

### 1. Per-step batch composition

- **Missing:** which request IDs were scheduled in each engine step, and
  each one's prefill vs. decode token split for that step.
- **Where:** confirmed at code level — `SchedulerOutput.num_scheduled_tokens`
  (`v1/core/sched/output.py:191`) has per-request token counts, but
  `SchedulerOutput` flows only scheduler → model runner →
  `update_from_output` (`scheduler.py:1488`) and never reaches the metrics
  layer. Prefill-vs-decode isn't even explicit there (derived from
  `num_computed_tokens < num_tokens`, `scheduler.py:1171`). Related:
  `IterationStats.time_to_first_tokens_iter` /
  `inter_token_latencies_iter` are flat unkeyed lists (`stats.py:336`) —
  per-iteration samples can't be attributed to a request either.
- **Why:** the per-request Gantt and "what shared my batch?" analysis — the
  core viewer feature — is impossible to reconstruct from aggregates.
- **Proposed:** an opt-in detailed mode where stat loggers receive
  per-step scheduled-request summaries (ids + token splits), gated by a flag
  since it adds per-step allocation. Keying the existing iteration arrays by
  request id could be a smaller first step.
- **Status:** not filed; validated against `63fcce4de` during the
  2026-07 deep-read.

### 2. Per-request preemption attribution

- **Missing:** *which* request was preempted (and why); only
  `IterationStats.num_preempted_reqs` (a count) is exposed.
- **Where:** confirmed — the count is incremented from the per-request
  `PREEMPTED` engine-core event (`stats.py:424`), so identity exists
  in-process but is discarded at aggregation. `SchedulerOutput.
  preempted_req_ids` (`output.py:217`) carries the ids but is marked
  "only used for v2 model runner" and never reaches loggers.
  `Request.num_preemptions` (`request.py:175`) exists but isn't copied into
  `FinishedRequestStats`. A forced `reset_prefix_cache` preemption
  (`scheduler.py:2190`) is indistinguishable from KV-pressure preemption.
- **Why:** explaining a specific slow request requires knowing it was
  preempted, not that "someone" was.
- **Proposed:** preemption events (request id, reason, step) surfaced to
  stat loggers, or minimally `num_preemptions` on `FinishedRequestStats`.
- **Status:** not filed; validated against `63fcce4de` during the
  2026-07 deep-read.

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

### 4. Absolute KV block counts and prefix-cache occupancy

- **Missing:** `SchedulerStats.kv_cache_usage` is a single 0–1 float
  (`scheduler.py:2280`; `block_pool.py:700`). No free/total/watermark block
  counts, no per-KV-group breakdown for hybrid models, and — because
  eviction is lazy — no measure of *cache occupancy*: freed-but-still-cached
  blocks count as free, so a pool full of reusable prefixes reports as
  empty.
- **Why:** "was the cache actually cold, or full of reusable blocks?" is a
  basic capacity question the usage scalar can't answer; absolute counts
  are needed to size `--gpu-memory-utilization` experiments.
- **Proposed:** add `num_free_blocks` / `num_total_blocks` and a
  cached-hash occupancy gauge (`len(cached_block_hash_to_block)` exists
  internally, `block_pool.py:137`) to `SchedulerStats`, per KV group.
- **Status:** not filed.

### 5. Eviction counts and identity in the stats path

- **Missing:** no eviction counter/rate at all in `SchedulerStats`; the new
  residency metrics (`kv_cache_eviction_events`, upstream #27793) are a
  ~1% *sample* of timing tuples with no block hash, request, or group
  attribution (`kv_cache_metrics.py:46`; `stats.py:161`), and are gated on
  `observability_config.kv_cache_metrics`.
- **Why:** the viewer's eviction-volume panel needs at least an exact count
  per step from the stats path; full identity is available only via the
  separate ZMQ event stream, which not every deployment can enable.
- **Workaround:** subscribe to `BlockRemoved` KV events (exact, has hashes)
  and correlate — acceptable for the MVP, so this is low priority.
- **Status:** not filed; likely fold into the residency-metrics follow-ups.

### 6. Token-budget saturation per step

- **Missing:** how much of `max_num_scheduled_tokens` each step consumed.
  `total_num_scheduled_tokens` lives on `SchedulerOutput` (`output.py:194`)
  and never reaches loggers (same plumbing problem as gap 1).
- **Why:** distinguishes "engine saturated" from "engine starved" at a
  glance — the first question when throughput drops.
- **Status:** not filed; would ride along with gap 1's mechanism.

### 7. KV-event stream robustness for external subscribers

- **Missing:** no schema-version field in `KVEventBatch`; block-hash
  encoding depends on an env var not echoed in the payload
  (`VLLM_KV_EVENTS_USE_INT_BLOCK_HASHES`, `envs.py:1753`); no per-event
  timestamps (one wall-clock `ts` per batch, `scheduler.py:1791`); no
  heartbeat, so an idle publisher and a dead one look identical.
- **Why:** a trace recorder must survive vLLM upgrades and detect data
  loss; today both require out-of-band knowledge.
- **Status:** not filed; align with llm-d KVEvents schema discussions
  before proposing anything here.

## SGLang

(Populate when the SGLang adapter work begins. Expect scheduler stats to
need a pluggable hook comparable to vLLM's `vllm.stat_logger_plugins`.)
