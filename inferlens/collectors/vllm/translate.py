"""Translate vLLM stat-logger callbacks into InferLens trace events.

Pure functions over duck-typed vLLM stats objects (``SchedulerStats``,
``IterationStats``, ``FinishedRequestStats``) — attribute access only, no
vLLM import — so this module is unit-testable without vLLM installed. See
``docs/vllm-internals.md`` §3.3 for the source field reference and §3.4 for
the clock model these functions assume (``ts`` is the collector's own
monotonic-clock reading, captured once per ``record()`` call; durations
inside ``FinishedRequestStats`` are engine-supplied deltas, passed through
as-is).
"""

from __future__ import annotations

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
    return RequestFinished(
        ts=ts,
        request_id=stat.request_id,
        finish_reason=(reason_name or str(finish_reason)).lower(),
        e2e_latency_s=stat.e2e_latency,
        queued_time_s=stat.queued_time,
        prefill_time_s=stat.prefill_time,
        decode_time_s=stat.decode_time,
        num_prompt_tokens=stat.num_prompt_tokens,
        num_generation_tokens=stat.num_generation_tokens,
        num_cached_tokens=stat.num_cached_tokens,
    )
