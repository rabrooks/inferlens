# `inferlens explain` — method and JSON report

`inferlens explain TRACE` answers *"why was it slow?"* from a trace file
alone: it finds incident windows, compares the requests caught in them with
requests that ran in calm periods, and breaks down where the slowest
requests' latency went.

```bash
inferlens explain run.ilens.gz                  # terminal report
inferlens explain run.ilens.gz --worst 20       # list more slow requests
inferlens explain run.ilens.gz --request ID     # one request in detail
inferlens explain run.ilens.gz --json           # machine-readable report
```

## Method

1. **Bin the engine timeline** (`--bin-s`, default 1 s), using the same
   placement rules as every other consumer (`TRACE_SPEC.md`: events rebased
   through the `trace_meta` anchor, `kv_*` by `wall_time_unix`). Per bin:
   peak KV usage, peak running and waiting counts, preemptions, evicted
   blocks.
2. **Mark stressed bins.** A bin is stressed if any of:
   - KV usage ≥ 95% (the usage float rarely reads exactly 1.0 while the
     allocator is starving);
   - at least one request was preempted;
   - the waiting queue is at least as long as the running batch (and ≥ 2).
3. **Group stressed bins into incidents**, bridging calm gaps of up to two
   bins, so one storm with a momentary lull is reported once.
4. **Classify each incident.** `kv_bound` if any bin saturated KV or
   preempted (capacity ran out); otherwise `queue_bound` (requests waited
   behind a full batch while KV had room — compute or admission limits).
5. **Compare with calm.** A request *overlaps* an incident if its
   `[arrival, finish]` interval intersects the window. Requests that overlap
   no incident form the calm baseline; each incident reports its overlapping
   requests' latency and their p95 relative to the calm p95. Comparisons
   need at least 5 calm requests.
6. **Attribute each request's latency** to queued, prefill, and decode time
   *as reported by the engine*, plus `unattributed` = e2e − (queued +
   prefill + decode). Phases are never rescaled to fill e2e. Queue time and
   per-token decode time are compared with the calm medians; the queue
   ratio is omitted when calm requests queued for under 10 ms (a ratio
   against microseconds is meaningless — the report shows absolutes).
7. **Diagnose.** Each incident kind maps to a short explanation and the
   settings worth looking at. These are suggestions; nothing is tuned
   automatically.

## Limits

- **Overlap is not causation.** Engines don't expose which requests ran in
  which step (`upstream-gaps.md` §1), so a request is linked to an incident
  by time overlap only.
- **Phases don't tile e2e.** Frontend time and re-queueing after preemption
  that the engine doesn't attribute to a phase show up as `unattributed`.
- **Collector gaps** mean some events are missing; counts in an affected
  window may be low. The report says so when the trace has gaps.
- **Per-request preemption counts** need vLLM ≥ 0.34 (trace schema v0.4);
  on older traces only per-step preemption counts exist.

## JSON report (`explain_version` 0.1)

The JSON report is versioned independently of the trace schema. Within a
major version, fields are only added. All times are seconds; times of day
(`start_s`, `arrival_s`, …) are seconds since the first recorded event.

| field | type | meaning |
| --- | --- | --- |
| `explain_version` | string | report format version |
| `engine`, `engine_version`, `model` | string or null | from `trace_meta` |
| `span_s` | number | first to last recorded event |
| `bin_s` | number | incident detection resolution |
| `requests`, `calm`, `affected` | latency stats | all requests / overlapping no incident / overlapping at least one |
| `calm_queued_p50_s`, `calm_tpot_p50_s` | number or null | calm medians used for the "vs calm" ratios |
| `incidents` | list of incident | see below |
| `attributions` | list of attribution | the `--worst` slowest requests |
| `diagnoses` | list of `{kind, summary, suggestions}` | `kind` is `kv_bound`, `queue_bound`, or `calm` |
| `gaps` | integer | number of `collector_gap` events |
| `caveats` | list of string | limits that apply to this trace |

Latency stats: `{count, p50_s, p95_s, max_s}` (statistics are null when
`count` is 0).

Incident: `{id, start_s, end_s, duration_s, kind, kv_usage_max,
preemptions, evicted_blocks, waiting_max, affected, slowdown_p95}` —
`affected` is latency stats of the overlapping requests; `slowdown_p95` is
their p95 over the calm p95 (null without a calm baseline).

Attribution: `{request_id, arrival_s, finish_s, e2e_s, queued_s,
prefill_s, decode_s, unattributed_s, dominant, num_preemptions,
prompt_tokens, cached_tokens, generation_tokens, finish_reason, incidents,
queued_vs_calm, tpot_vs_calm}` — `dominant` is the largest of the four
components; `incidents` lists overlapping incident ids; `num_preemptions`
is null when the trace doesn't record it.
