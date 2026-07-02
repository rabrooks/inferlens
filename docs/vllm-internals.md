# vLLM V1 internals — study notes

> Working notes on the vLLM V1 internals that the collector builds on.
> Deep-read done against vLLM `main` @ `63fcce4de` (2026-07-01); all
> `file:line` citations below are pinned to that commit. These internals
> move fast — re-verify line numbers before filing anything upstream.

## Reading list

- [x] `vllm/v1/core/sched/scheduler.py` — the V1 scheduler loop: waiting vs.
      running queues, token budgets, preemption
- [x] `vllm/v1/core/kv_cache_manager.py` — block allocation, prefix caching,
      eviction (plus `block_pool.py`, `kv_cache_utils.py`,
      `kv_cache_metrics.py`)
- [x] `vllm/v1/metrics/stats.py` + `loggers.py` — what stat loggers receive,
      and when (`SchedulerStats`, `IterationStats`, `FinishedRequestStats`)
- [x] `vllm/distributed/kv_events.py` — KV event publishing (`BlockStored`,
      `BlockRemoved`), ZMQ transport, `--kv-events-config`
- [x] `vllm/plugins/__init__.py` — plugin loading;
      `STAT_LOGGER_PLUGINS_GROUP = "vllm.stat_logger_plugins"`

## Questions to answer during the read

1. Exact `StatLoggerBase` interface: method signatures, call frequency
   (every step vs. logging interval), threading context.
   **Answered — see §3.** `record()` fires once per `EngineCoreOutputs`
   batch (≈ once per scheduler step) *on the asyncio event loop*; `log()`
   is the ~10 s interval flush.
2. When `record()` is called relative to scheduler steps — one call per
   `EngineCoreOutputs`, or batched?
   **Answered — see §3.1.** One `record()` per `EngineCoreOutputs`; the
   engine core emits one of those per scheduler step (empty steps may be
   skipped/coalesced).
3. KV-event timestamps: which clock, and how to align with stat-logger time.
   **Answered — see §6 and the clock model in §3.4.** KV events: wall clock
   (`time.time()`), one `ts` per batch. Stats: a *mix* of wall and
   engine-core-monotonic. Trace writer must record its own
   (monotonic, wall) anchor pair.
4. What triggers preemption in V1 (KV-block exhaustion only?), and what
   `kv_cache_eviction_events` actually contains.
   **Answered — see §2 and §4.4.** KV-block exhaustion only (plus a forced
   path via `reset_prefix_cache`). `kv_cache_eviction_events` is a *sampled*
   (default 1%) list of block-residency timings — no identities.
5. Overhead budget: what do the built-in Prometheus/logging stat loggers cost
   per step, as a baseline for ours.
   **Answered — see §3.5.** O(batch tokens + finished requests) of in-memory
   counter/histogram ops, zero I/O. Our budget: same order, and *never*
   block or raise in `record()`.

---

## Notes

### 1. The scheduler loop (`vllm/v1/core/sched/scheduler.py`)

One `schedule()` call == one model forward pass (`interface.py:52`). There
is no explicit prefill-vs-decode phase (design note at `scheduler.py:390`):
every request has `num_computed_tokens` and a target `num_tokens_with_spec`,
and each step tries to close the gap. Chunked prefill, prefix caching, and
spec decode all fall out of this one mechanism.

**Budgets** (set per step): `token_budget = max_num_scheduled_tokens`
(`scheduler.py:408`, from `max_num_batched_tokens` if unset,
`scheduler.py:109`) and a running-request cap `max_num_seqs`
(`scheduler.py:108`).

**Phase A — running queue first** (`scheduler.py:432`): each running request
gets `num_new_tokens = num_tokens_with_spec + placeholders -
num_computed_tokens` (`scheduler.py:465`) — typically 1 (+ spec) for decode,
or the remaining prompt chunk — clamped by `long_prefill_token_threshold`,
the token budget, and `max_model_len` (`scheduler.py:470-481`). KV blocks
via `allocate_slots()` (`scheduler.py:527`); a `None` return triggers the
preemption loop (§2).

**Phase B — waiting queue** (`scheduler.py:628`): entered *only if nothing
was preempted this step* (`scheduler.py:629`). Prefix-cache lookup
(`get_computed_blocks`, `scheduler.py:713`), then chunked prefill:
`num_new_tokens = num_tokens - num_computed_tokens`, capped to the remaining
token budget (`scheduler.py:831`) — that cap is what splits a long prompt
across steps. An `allocate_slots` failure here just breaks the loop; the
waiting phase never preempts (`scheduler.py:908`).

**Queues** (`request_queue.py`): `waiting` is an FCFS deque or a priority
heap ordered by `(priority, arrival_time, request_id)`
(`request_queue.py:75`, `:131`; `request.py:309`); `running` is a plain
list (`scheduler.py:184`). A second `skipped_waiting` queue holds requests
deferred within a step (blocked on grammar/remote-KV/LoRA cap,
`scheduler.py:1830`). **`SchedulerStats.num_waiting_reqs` counts only
`waiting`** — `num_skipped_waiting_reqs` is separate, and a collector must
sum both (the built-in logger does: `loggers.py:232`).

### 2. Preemption

**Trigger: KV-block exhaustion, nothing else.** `allocate_slots()` returns
`None` for a *running* request → preemption loop (`scheduler.py:527-574`).
(One maintenance exception: `reset_prefix_cache(reset_running_requests=True)`
force-preempts everything, `scheduler.py:2190`.)

**Victim:** FCFS policy pops the *tail* of the running list
(`scheduler.py:564`); priority policy picks the lowest-priority,
latest-arriving request (`scheduler.py:540`). The loop keeps preempting
until allocation succeeds or the victim would be the request being
scheduled.

**Mechanics** (`_preempt_request`, `scheduler.py:1131`): blocks freed (not
instantly evicted — see §4.4), status → `PREEMPTED`,
**`num_computed_tokens = 0`** (all progress discarded; recompute relies on
prefix-cache re-hits), `request.num_preemptions += 1`, a `PREEMPTED`
engine-core event recorded (only if `log_stats`, `scheduler.py:1148`), and
the request is prepended to the *front* of the waiting queue.

**How it surfaces to a stat logger:** only as the aggregate
`IterationStats.num_preempted_reqs` (`stats.py:332`), incremented when the
frontend sees the `PREEMPTED` event (`stats.py:424`). `SchedulerStats` has
**no** preemption field. `SchedulerOutput.preempted_req_ids` exists
(`output.py:217`) but never reaches the metrics layer.

**Observability couplings worth remembering:**

- A preemption suppresses *all* new admissions for that step
  (`scheduler.py:629`) — preemption storms and waiting-queue growth are
  structurally linked; expect them to co-occur in traces.
- Wasted recompute after preemption is partially measurable:
  `PrefixCacheStats.preempted_{requests,queries,hits}` (`stats.py:122`)
  segregates resumed-request cache lookups from fresh ones.
- A re-scheduled preempted request does **not** reset `scheduled_ts`
  (`stats.py:421`) — `queued_time`/`prefill_time` for a preempted request
  measure the *first* scheduling only.

### 3. The stat-logger path (`vllm/v1/metrics/loggers.py`, `stats.py`)

#### 3.1 Interface and call semantics

`StatLoggerBase` (`loggers.py:44`):

```python
__init__(vllm_config: VllmConfig, engine_index: int = 0)      # abstract
record(scheduler_stats: SchedulerStats | None,
       iteration_stats: IterationStats | None,
       mm_cache_stats: MultiModalCacheStats | None = None,
       engine_idx: int = 0)                                    # abstract
log_engine_initialized()                                       # abstract
log()                                                          # no-op default
record_sleep_state(is_awake: int, level: int)                  # no-op default
```

The docstring explicitly marks `SchedulerStats`/`IterationStats` as
**unstable interfaces** (`loggers.py:47`) — pin the vLLM version per
InferLens release (already planned).

`record()` is called once per `EngineCoreOutputs` batch in the async
engine's `output_handler` task (`async_llm.py:656-702`) — effectively once
per scheduler step. `SchedulerStats` is produced by the scheduler's
`make_stats()` (`scheduler.py:2252`) and attached to *one* frontend's
outputs (`scheduler.py:1819`); `IterationStats` is built frontend-side from
per-request `EngineCoreOutput`s and their engine-core events.

`log()` is unrelated to `record()`: it's the interval flush, driven by
`VLLM_LOG_STATS_INTERVAL` (default 10 s, `envs.py:795`).

**None-handling a plugin must do:** `iteration_stats is None` whenever a
batch has zero request outputs (`async_llm.py:663`); `scheduler_stats is
None` on non-scheduler steps and always when `log_stats` is disabled
(`make_stats` returns `None`, `scheduler.py:2259`).

Empirically confirmed on the macOS CPU backend (opt-125m, 3 concurrent
requests, 32 tokens each): the InferLens plugin was instantiated once,
received 34 `record()` calls — `SchedulerStats` present in all,
`IterationStats` `None` in exactly one (an empty batch). Two gotchas for
local runs: vLLM's plugin loader *requires* a real `StatLoggerBase`
subclass (duck typing raises `TypeError` at load, `loggers.py:81`), and on
the CPU backend `gpu_memory_utilization` reserves that fraction of *system
RAM* — the 0.92 default wants ~15 GiB; use ~0.15 for tiny models.

#### 3.2 Threading — the hard constraint on our writer

`record()` runs **inside the asyncio event loop** (the output-handler task;
`async_llm.py:656`, kept deliberately synchronous per the TODO at
`async_llm.py:694`). Consequences for the InferLens plugin:

- Blocking I/O in `record()` stalls token streaming for *every* request.
  The writer must append to an in-memory buffer and flush from a separate
  thread.
- An exception escaping `record()` propagates to the output handler and
  **fails all in-flight requests** (`async_llm.py:703` →
  `propagate_error`). The plugin must be wrapped in a
  never-raise guard.
- Plugin *load* failures, by contrast, are caught and skipped
  (`plugins/__init__.py:60`) — the inert-unless-configured design is safe.

Plugin loading: entry-point group `vllm.stat_logger_plugins`
(`plugins/__init__.py:22`), loaded in async mode only, filtered by
`VLLM_PLUGINS`, instantiated at `StatLoggerManager.__init__`
(`loggers.py:1315`) — factory signature `(vllm_config, engine_index)`, one
instance per engine index under data parallelism (aggregate-logger variant
exists via `AggregateStatLoggerBase`, `loggers.py:91`).

#### 3.3 What `record()` actually delivers

`SchedulerStats` (`stats.py:170`) — per-step gauges/deltas:
`num_running_reqs`, `num_waiting_reqs`, `num_skipped_waiting_reqs`,
`kv_cache_usage` (single 0–1 float), `prefix_cache_stats` (token-granular
deltas, drained-and-reset each stats pull, `kv_cache_manager.py:190`),
`kv_cache_eviction_events` (sampled, §4.4), optional
spec-decoding/cudagraph/perf/connector stats, LoRA queue-depth dicts.

`IterationStats` (`stats.py:325`) — per-batch deltas:
`iteration_timestamp` (wall clock), `num_generation_tokens`,
`prompt_token_stats` (computed vs. local-cache-hit vs. external split),
`num_preempted_reqs`, `finished_requests: list[FinishedRequestStats]`, and
**unkeyed** `time_to_first_tokens_iter` / `inter_token_latencies_iter`
arrays — no request IDs attached (`stats.py:336`).

`FinishedRequestStats` (`stats.py:223`) — the only place request identity
appears: `request_id`, `finish_reason`, `e2e_latency`, `queued_time`,
`prefill_time`, `decode_time`, `inference_time`,
`mean_time_per_output_token`, `num_prompt_tokens`,
`num_generation_tokens`, `num_cached_tokens`.

Phase-span derivation (`stats.py:428-459`): `queued_time = scheduled_ts -
queued_ts`, `prefill_time = first_token_ts - scheduled_ts`, `decode_time =
last_token_ts - first_token_ts` — all engine-core monotonic;
`e2e_latency = iteration_timestamp - arrival_time` — wall clock.

#### 3.4 Clock model (feeds TRACE_SPEC)

Two clocks flow through the stats, and they must not be mixed:

| Clock | Used for | Source |
| --- | --- | --- |
| Wall (`time.time()`) | `IterationStats.iteration_timestamp` (`stats.py:329`), `arrival_time`, TTFT, `e2e_latency`; **all KV-event batch `ts`** (`scheduler.py:1791`) | frontend + scheduler |
| Monotonic (`time.monotonic()`) | engine-core events → `queued_ts`/`scheduled_ts`/`first_token_ts`/`last_token_ts`, ITL, all phase durations (`engine/__init__.py:159`, `stats.py:210`) | engine-core process |

The engine-core monotonic clock is explicitly documented as
not-comparable-across-processes (`engine/__init__.py:159`). **Collector
rule:** capture a `(time.monotonic(), time.time())` anchor pair at every
`record()` and at trace start; store durations as-is; align KV events on
wall clock but order them by transport sequence number, and record our own
receive time as a third reference. This validates the monotonic +
wall-anchor model in TRACE_SPEC.

#### 3.5 Overhead baseline

`LoggingStatLogger.record` is a handful of int adds and deque appends
(`loggers.py:144`); `PrometheusStatLogger.record` is ~a dozen gauge/counter
ops plus one histogram observe per TTFT/ITL sample and ~10 per finished
request (`loggers.py:1173-1222`). No I/O in either; formatting happens in
`log()` every ~10 s. Our per-`record()` budget: same order — cheap
in-memory appends, async flush, target <1% total.

### 4. KV cache manager (`vllm/v1/core/kv_cache_manager.py`, `block_pool.py`)

#### 4.1 Allocation

`BlockPool` owns all physical blocks in a `FreeKVCacheBlockQueue` intrusive
doubly-linked list (`kv_cache_utils.py:179`); block 0 is reserved as the
null block, so usable = `num_gpu_blocks - 1` (`block_pool.py:191`).
`allocate_slots` (`kv_cache_manager.py:244`) checks
`need + watermark > free - reserved` → returns `None` (the preemption
trigger), else attaches prefix-hit blocks and pops fresh ones. Fresh-block
acquisition (`get_new_blocks`, `block_pool.py:542`) is where lazy eviction
happens (§4.3).

#### 4.2 Prefix caching

Block hash = `hash(parent_block_hash, block_token_ids, extra_keys)` — a
**prefix chain**, so one hash fingerprints the whole prefix
(`kv_cache_utils.py:577`). `extra_keys` mix in LoRA name, multimodal item
hashes, `cache_salt` (first block only), and prompt-embed hashes
(`kv_cache_utils.py:539`). Note: the seed hash is random per process unless
`PYTHONHASHSEED` is set (`kv_cache_utils.py:99`) — block hashes are not
comparable across engine restarts by default.

Hit detection walks the chain and stops at the first miss
(`single_type_kv_cache_manager.py:566`); a hit is capped at
`num_tokens - 1` (the last token must be recomputed for logits,
`kv_cache_manager.py:221`).

`PrefixCacheStats` granularity: `queries`/`hits` are **token counts**
(`queries += request.num_tokens`, `hits += num_new_computed_tokens`,
`stats.py:131`; `kv_cache_manager.py:234`); `requests` is a request count.
Preempted-request lookups go to the separate `preempted_*` bucket and are
excluded from the built-in hit-rate window (`stats.py:77`).

#### 4.3 Eviction is lazy

Freed blocks keep their hash and stay discoverable: unhashed blocks are
*prepended* to the free queue (evict first), hashed blocks *appended*
(evict last) (`block_pool.py:622`). Actual eviction — hash-map removal +
`BlockRemoved` event — happens only when a free block is *re-allocated*
(`_maybe_evict_cached_block`, `block_pool.py:574`). A block can be freed,
re-hit via `touch` (`block_pool.py:597`), and never emit a removal.
Consequence: **`kv_cache_usage` (= `1 - free/(total-1)`,
`block_pool.py:700`) measures live allocation, not cache occupancy** — a
pool full of reusable cached blocks reports as free.

#### 4.4 Two unrelated "eviction" streams — don't conflate

| | `SchedulerStats.kv_cache_eviction_events` | KV events (`BlockRemoved`) |
| --- | --- | --- |
| What | `KVCacheEvictionEvent(lifetime_seconds, idle_seconds, reuse_gaps_seconds)` (`stats.py:161`) | exact per-block removal with hashes (`kv_events.py:92`) |
| Coverage | **sampled**, default 1% of blocks (`kv_cache_metrics.py:46`; rate via `observability_config`, `scheduler.py:89`) | complete |
| Identity | none (timings only) | block hashes + group |
| Gate | `observability_config.kv_cache_metrics` | `--kv-events-config` |
| Provenance | upstream `cabc77cc8` (#27793, "KV cache residency metrics") — recent | long-standing |

Preemption vs. eviction: preempting a request *frees* its blocks (hashed →
tail of free queue) but doesn't evict them; if rescheduled soon it re-hits
its own prefix. Blocks are truly evicted only if another allocation claims
them first.

### 5. `SchedulerOutput` — the data we can't have (yet)

`SchedulerOutput` carries exactly what the viewer's Gantt needs:
`num_scheduled_tokens: dict[req_id, int]`, `total_num_scheduled_tokens`,
new/resumed request split, `preempted_req_ids`, `finished_req_ids`
(`output.py:180-245`). It flows only scheduler → model runner →
`update_from_output` (`scheduler.py:1488`) and **never reaches the metrics
layer**. Prefill-vs-decode isn't even explicit there — it's derived from
`num_computed_tokens < num_tokens` (`scheduler.py:1171`). This is
upstream-gap #1, now confirmed at code level.

### 6. KV event publishing (`vllm/distributed/kv_events.py`)

**Schema** (msgspec structs, msgpack on the wire): `KVEventBatch` is
`array_like` — positional `[ts, events, data_parallel_rank]`
(`kv_events.py:25`). Events are tag-discriminated by class name:

- `BlockStored`: `block_hashes`, `parent_block_hash`, `token_ids`,
  `block_size`, `lora_name`, `medium` ("GPU"), per-block `extra_keys`,
  `group_idx`, spec kind/sliding-window (annotated in `take_events`,
  `kv_cache_manager.py:584`) (`kv_events.py:48`).
- `BlockRemoved`: `block_hashes`, `medium`, `group_idx` only
  (`kv_events.py:92`) — a consumer must remember spec info from the store.
- `AllBlocksCleared`: no fields; fired on prefix-cache reset
  (`block_pool.py:688`).

Block-hash type is env-dependent: `VLLM_KV_EVENTS_USE_INT_BLOCK_HASHES`
defaults **true** → 64-bit-truncated ints; false → raw sha256 bytes
(`envs.py:1753`, `kv_cache_utils.py:79`). Not self-describing in the
payload.

**Timestamps:** one wall-clock `ts = time.time()` per batch, set at batch
assembly in `update_from_output` — once per scheduler step that produced
events (`scheduler.py:1789`). No per-event timestamps; publish happens
later on a background thread, so `ts` excludes queue latency. Order by the
transport **sequence number**, not `ts`.

**Transport:** PUB socket (default `tcp://*:5557`, binds on wildcard),
3-frame multipart `[topic, seq (8-byte big-endian), msgpack payload]`
(`kv_events.py:438`). Optional ROUTER replay endpoint: subscriber sends a
start-seq as 8-byte BE, receives buffered batches (in-memory ring, default
`buffer_steps=10_000`), terminated by a `-1` marker (`kv_events.py:448`).
PUB drops silently past HWM or with no subscriber; no heartbeat — idle and
dead are indistinguishable. Subscriber contract: track `seq`, replay on
gaps, dedup across replay+live, record receive time.

**Replay-client bug (found implementing our subscriber, not yet reported
upstream):** vLLM's ROUTER-side `_service_replay` (`kv_events.py:493`)
sends one `send_multipart` per buffered batch plus an end marker — multiple
independent replies to one request. But vLLM's own reference client
(`examples/features/kv_events/kv_events_subscriber.py`) and its test
double (`tests/distributed/conftest.py::MockSubscriber.receive_replay`)
both request replay over a `zmq.REQ` socket and loop `recv_multipart()` on
it. A `REQ` socket's send/recv FSM allows exactly one `recv()` per
`send()`; verified empirically that a second `recv()` (or
`poller.poll()`-gated `recv()`) after one `send()` raises `EFSM`
("Operation cannot be accomplished in current state") — so both only ever
retrieve the *first* replayed batch, silently missing the rest on any
gap wider than one. Our subscriber uses `DEALER` for the replay socket
instead (no such FSM restriction), manually prepending the empty
delimiter frame `REQ` would otherwise add automatically — verified
wire-compatible with the real `ROUTER`-side framing.

**Config:** `--kv-events-config '{"enable_kv_cache_events": true, ...}'`
(`config/kv_events.py:10`; fields: `endpoint`, `replay_endpoint`,
`buffer_steps`, `hwm`, `topic`). Prefix caching should be enabled or
events are sparse (`config/vllm.py:1347`). Data parallel: one publisher
per rank at `base_port + dp_rank` (`kv_events.py:471`) — topology known
out-of-band only.

### 7. Collector design implications (summary)

1. `inferlens record` must set/verify **three independent gates**:
   `log_stats=True` (on by default for serve; disabled → we get nothing),
   `--kv-events-config` for exact block events, and
   `observability_config.kv_cache_metrics` if we want residency samples.
2. The plugin's `record()` is on the event loop: buffer in memory, flush on
   a thread, never raise.
3. Trace writer anchors: `(monotonic, wall)` pair captured at start and
   periodically; KV events merged on wall clock + own receive time; ordered
   by ZMQ seq.
4. The per-request Gantt is reconstructable **only at request finish**
   (`FinishedRequestStats` spans). Live per-step per-request composition
   needs upstream work (gaps #1/#2).
5. Aggregate timeline (running/waiting counts, KV usage, preemption counts,
   prefix-hit rate) is fully available today — the viewer's headline view
   has no upstream dependency.

## Gaps (consolidated — tracked in `upstream-gaps.md`)

- Per-step batch composition and prefill/decode split (`SchedulerOutput`
  never reaches loggers) — gap #1.
- Preemption identity/cause (aggregate count only; `preempted_req_ids`
  internal) — gap #2.
- Arrival/in-flight request visibility (identity only at finish) — gap #3.
- Unkeyed per-iteration TTFT/ITL arrays (no request association).
- Absolute block counts, per-group usage, and prefix-cache *occupancy*
  (usage scalar hides reusable cached blocks).
- Eviction counts/rates and identity in the stats path (samples are
  timing-only, 1%); exact stream exists but only via ZMQ events.
- Token-budget saturation per step not exposed.
- KV-event stream: no schema version, env-dependent hash encoding, no
  per-event timestamps, bounded non-durable replay.
