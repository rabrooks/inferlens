"""Translate vLLM stat-logger callbacks into InferLens trace events.

Pure functions over duck-typed vLLM stats objects (``SchedulerStats``,
``IterationStats``, ``FinishedRequestStats``) — attribute access only, no
vLLM import — so this module is unit-testable without vLLM installed. See
``docs/vllm-internals.md`` §3.3 for the source field reference and §3.4 for
the clock model these functions assume (``ts`` is the collector's own
monotonic-clock reading, captured once per ``record()`` call; durations
inside ``FinishedRequestStats`` are engine-supplied deltas, passed through
as-is).

Attribute-access convention: fields we require are read directly, so a
renamed vLLM field raises ``AttributeError`` into the stat logger's
catch-all, which disables the plugin loudly rather than recording wrong
data. Only genuinely optional or nested stats use ``getattr`` defaults.
"""

from __future__ import annotations

import statistics
from typing import Any

from inferlens.schema import EngineSnapshot, RequestFinished


def engine_snapshot(
    scheduler_stats: Any, iteration_stats: Any, ts: float
) -> EngineSnapshot | None:
    """Build an :class:`EngineSnapshot`, or ``None`` if there's nothing to report.

    ``scheduler_stats`` is ``None`` on non-scheduler steps and whenever
    ``log_stats`` is disabled (vLLM never produced a snapshot this step);
    ``iteration_stats`` is ``None`` on batches with zero request outputs.
    """
    if scheduler_stats is None:
        return None
    prefix_stats = getattr(scheduler_stats, "prefix_cache_stats", None)
    ttft = _latency_summary(getattr(iteration_stats, "time_to_first_tokens_iter", None))
    itl = _latency_summary(getattr(iteration_stats, "inter_token_latencies_iter", None))
    return EngineSnapshot(
        ts=ts,
        num_running_reqs=scheduler_stats.num_running_reqs,
        num_waiting_reqs=scheduler_stats.num_waiting_reqs,
        kv_cache_usage=scheduler_stats.kv_cache_usage,
        prefix_cache_queries=getattr(prefix_stats, "queries", 0),
        prefix_cache_hits=getattr(prefix_stats, "hits", 0),
        num_preempted_reqs=getattr(iteration_stats, "num_preempted_reqs", 0),
        num_generation_tokens=getattr(iteration_stats, "num_generation_tokens", 0),
        num_prompt_tokens=getattr(iteration_stats, "num_prompt_tokens", 0),
        ttft_count=ttft[0],
        ttft_mean_s=ttft[1],
        ttft_p50_s=ttft[2],
        ttft_max_s=ttft[3],
        itl_count=itl[0],
        itl_mean_s=itl[1],
        itl_p50_s=itl[2],
        itl_max_s=itl[3],
    )


def _latency_summary(
    samples: list[float] | None,
) -> tuple[int, float | None, float | None, float | None]:
    """Summarize one iteration's latency samples as (count, mean, p50, max).

    vLLM's per-iteration TTFT/ITL arrays are unkeyed (no request ID at the
    source, `docs/upstream-gaps.md` §1), so distribution summaries are the
    most a trace can record faithfully — never attribute them to requests.
    """
    if not samples:
        return 0, None, None, None
    return (
        len(samples),
        statistics.fmean(samples),
        statistics.median(samples),
        max(samples),
    )


def request_finished_events(iteration_stats: Any, ts: float) -> list[RequestFinished]:
    """Extract the :class:`RequestFinished` events from one ``record()`` call."""
    finished = getattr(iteration_stats, "finished_requests", None)
    if not finished:
        return []
    return [_request_finished(stat, ts) for stat in finished]


def _request_finished(stat: Any, ts: float) -> RequestFinished:
    finish_reason = stat.finish_reason
    reason_name = getattr(finish_reason, "name", None)
    # Lowercasing vLLM's enum names (STOP/LENGTH/ABORT/ERROR/REPETITION) is
    # the whole mapping onto the trace spec's finish_reason vocabulary.
    return RequestFinished(
        ts=ts,
        # None is possible on the vLLM side (defaulted field); the schema
        # wants a string.
        request_id=stat.request_id or "",
        finish_reason=(reason_name or str(finish_reason)).lower(),
        e2e_latency_s=stat.e2e_latency,
        queued_time_s=stat.queued_time,
        prefill_time_s=stat.prefill_time,
        decode_time_s=stat.decode_time,
        num_prompt_tokens=stat.num_prompt_tokens,
        num_generation_tokens=stat.num_generation_tokens,
        num_cached_tokens=stat.num_cached_tokens,
    )
