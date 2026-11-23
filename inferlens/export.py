"""Export a trace to the Chrome trace-event JSON format (Perfetto, chrome://tracing).

Gives any trace an immediately usable timeline in ui.perfetto.dev without
the InferLens viewer. Stdlib-only, like the rest of the core package.

Layout (one "process" per concern, so Perfetto groups the tracks):

- **Engine** — counter tracks from ``engine_snapshot``: request counts, KV
  usage, preemptions per step, prefix-cache hit rate, scheduled
  prefill/decode tokens and iteration time (v0.4, when recorded), and
  TTFT/ITL summaries.
- **Requests** — one slice per ``request_finished``, packed into
  non-overlapping lanes, with queued/prefill/decode child slices.
- **KV cache** — stored/evicted block rates binned over time, plus instant
  markers for cache resets.

``collector_gap`` events become global instant markers so missing data is
visible on every track.
"""

from __future__ import annotations

import gzip
import json
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from inferlens.placement import Clock, split_meta
from inferlens.schema import (
    CollectorGap,
    EngineSnapshot,
    KVBlockRemoved,
    KVBlockStored,
    KVCacheCleared,
    RequestFinished,
    TraceEvent,
    TraceMeta,
)

ENGINE_PID = 1
REQUESTS_PID = 2
KV_PID = 3

DEFAULT_KV_BIN_S = 1.0

# Request lanes are threads of the Requests process. Perfetto's JSON importer
# treats tid 0 as the process's main thread (tid == pid), so lane tids must
# never be 0 or collide with a pid — lane 0 and lane 2 would otherwise merge
# into one thread and their slices spill as overlaps.
LANE_TID_BASE = 1000


def to_perfetto(
    events: Iterable[TraceEvent], kv_bin_s: float = DEFAULT_KV_BIN_S
) -> dict[str, Any]:
    """Convert trace events to a Chrome trace-event JSON document.

    Args:
        events: Events as yielded by :func:`inferlens.trace_io.read_trace`
            (a single or already-merged trace).
        kv_bin_s: Bin width, in seconds, for the KV stored/evicted rate
            counters. Per-event KV markers would swamp the UI on real
            traces (tens of thousands of evictions), so rates are binned.

    Returns:
        A JSON-serializable dict with ``traceEvents`` and ``otherData``.

    Raises:
        ValueError: If ``kv_bin_s`` is not positive.
    """
    if not kv_bin_s > 0:
        raise ValueError(f"kv_bin_s must be positive, got {kv_bin_s!r}")

    meta, body = split_meta(events)
    clock = Clock.from_meta(meta)
    placed = [(clock.place(e), e) for e in body]
    requests = [
        (t, e) for t, e in placed if isinstance(e, RequestFinished) and e.e2e_latency_s
    ]
    # Request slices start at arrival, which can predate every other event.
    starts = [t for t, _ in placed] + [t - e.e2e_latency_s for t, e in requests]
    t0 = min(starts, default=0.0)

    def us(t: float) -> int:
        # Integer microseconds: with float ts and dur rounded separately, a
        # child's end can land a few ns past its parent's and Perfetto (which
        # works in integer ns) spills it as an overlap. Integer endpoints make
        # dur = end - start exact, so nesting and touching siblings hold.
        return round((t - t0) * 1e6)

    out: list[dict[str, Any]] = _process_metadata()
    for t, event in placed:
        if isinstance(event, EngineSnapshot):
            out.extend(_snapshot_counters(event, us(t)))
        elif isinstance(event, KVCacheCleared):
            out.append(_instant("KV cache cleared", us(t), KV_PID, scope="p"))
        elif isinstance(event, CollectorGap):
            out.append(
                _instant(
                    f"collector_gap: {event.reason}",
                    us(t),
                    ENGINE_PID,
                    scope="g",
                    args={
                        "source": event.source,
                        "first_seq": event.first_seq,
                        "last_seq": event.last_seq,
                    },
                )
            )
    out.extend(_request_slices([(t, e) for t, e in requests], us))
    out.extend(_kv_rate_counters(placed, kv_bin_s, us))

    return {
        "traceEvents": out,
        "displayTimeUnit": "ms",
        "otherData": _other_data(meta, t0),
    }


def export_perfetto(
    events: Iterable[TraceEvent],
    output: str | Path,
    kv_bin_s: float = DEFAULT_KV_BIN_S,
) -> int:
    """Write :func:`to_perfetto` output to ``output`` (gzipped if ``.gz``).

    Returns:
        The number of trace events written.
    """
    doc = to_perfetto(events, kv_bin_s=kv_bin_s)
    output = Path(output)
    text = json.dumps(doc, separators=(",", ":"))
    if output.suffix == ".gz":
        with gzip.open(output, "wt", encoding="utf-8") as f:
            f.write(text)
    else:
        output.write_text(text, encoding="utf-8")
    return len(doc["traceEvents"])


def _process_metadata() -> list[dict[str, Any]]:
    out = []
    for pid, name in (
        (ENGINE_PID, "Engine"),
        (REQUESTS_PID, "Requests"),
        (KV_PID, "KV cache"),
    ):
        out.append(
            {"ph": "M", "pid": pid, "name": "process_name", "args": {"name": name}}
        )
        out.append(
            {
                "ph": "M",
                "pid": pid,
                "name": "process_sort_index",
                "args": {"sort_index": pid},
            }
        )
    return out


def _counter(
    name: str, ts: float, args: dict[str, float], pid: int = ENGINE_PID
) -> dict[str, Any]:
    # Process-scoped tracks live on the process's main thread (tid == pid).
    return {"ph": "C", "name": name, "pid": pid, "tid": pid, "ts": ts, "args": args}


def _instant(
    name: str,
    ts: float,
    pid: int,
    scope: str,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "ph": "i",
        "name": name,
        "pid": pid,
        "tid": pid,
        "ts": ts,
        "s": scope,
    }
    if args:
        event["args"] = args
    return event


def _snapshot_counters(s: EngineSnapshot, ts: float) -> list[dict[str, Any]]:
    queues: dict[str, float] = {
        "running": s.num_running_reqs,
        "waiting": s.num_waiting_reqs,
    }
    if s.num_deferred_waiting_reqs is not None:
        queues["deferred"] = s.num_deferred_waiting_reqs
    out = [
        _counter("Requests", ts, queues),
        _counter("KV cache usage (%)", ts, {"usage": round(s.kv_cache_usage * 100, 3)}),
        _counter("Preemptions per step", ts, {"preempted": s.num_preempted_reqs}),
    ]
    if s.prefix_cache_queries:
        rate = 100 * s.prefix_cache_hits / s.prefix_cache_queries
        out.append(
            _counter("Prefix cache hit rate (%)", ts, {"hit_rate": round(rate, 3)})
        )
    if s.num_prefill_tokens is not None and s.num_decode_tokens is not None:
        out.append(
            _counter(
                "Scheduled tokens",
                ts,
                {"prefill": s.num_prefill_tokens, "decode": s.num_decode_tokens},
            )
        )
    if s.iteration_time_s is not None and s.iteration_time_source is not None:
        # Named by source so wall-clock and GPU time never share a track.
        out.append(
            _counter(
                f"Iteration time (ms, {s.iteration_time_source})",
                ts,
                {"time": round(s.iteration_time_s * 1000, 3)},
            )
        )
    if s.ttft_count and s.ttft_p50_s is not None and s.ttft_max_s is not None:
        out.append(
            _counter(
                "TTFT (ms)",
                ts,
                {
                    "p50": round(s.ttft_p50_s * 1000, 3),
                    "max": round(s.ttft_max_s * 1000, 3),
                },
            )
        )
    if s.itl_count and s.itl_p50_s is not None and s.itl_max_s is not None:
        out.append(
            _counter(
                "ITL (ms)",
                ts,
                {
                    "p50": round(s.itl_p50_s * 1000, 3),
                    "max": round(s.itl_max_s * 1000, 3),
                },
            )
        )
    return out


def _request_slices(
    requests: list[tuple[float, RequestFinished]], us: Any
) -> list[dict[str, Any]]:
    """Lay requests out as nested slices on non-overlapping lanes.

    A request spans ``[finish - e2e, finish]``. Its phases are placed
    right-aligned to the finish (decode ends at finish, prefill before it,
    queued before that) *as reported*: they do not tile e2e exactly — the
    remainder (frontend time, and re-queueing the engine doesn't attribute)
    shows as the uncovered head of the request slice and is reported in
    ``args.unattributed_ms``. If the phases sum past e2e, they are clipped
    at arrival so children stay nested in their parent.
    """
    out: list[dict[str, Any]] = []
    # Greedy interval packing: each lane holds requests that never overlap,
    # which X-slices on one thread require. Compared in rounded output
    # microseconds with a strict gap: Perfetto nests a slice that starts on
    # the exact microsecond its lane-predecessor ends.
    lane_free_at: list[int] = []
    for finish, r in sorted(requests, key=lambda tr: tr[0] - tr[1].e2e_latency_s):
        arrival = finish - r.e2e_latency_s
        start_us, end_us = us(arrival), us(finish)
        lane = next((i for i, free in enumerate(lane_free_at) if free < start_us), None)
        if lane is None:
            lane = len(lane_free_at)
            lane_free_at.append(end_us)
            out.append(
                {
                    "ph": "M",
                    "pid": REQUESTS_PID,
                    "tid": LANE_TID_BASE + lane,
                    "name": "thread_name",
                    "args": {"name": f"lane {lane:03d}"},
                }
            )
        else:
            lane_free_at[lane] = end_us

        phase_total = r.queued_time_s + r.prefill_time_s + r.decode_time_s
        args: dict[str, Any] = {
            "request_id": r.request_id,
            "finish_reason": r.finish_reason,
            "e2e_ms": round(r.e2e_latency_s * 1000, 3),
            "queued_ms": round(r.queued_time_s * 1000, 3),
            "prefill_ms": round(r.prefill_time_s * 1000, 3),
            "decode_ms": round(r.decode_time_s * 1000, 3),
            "unattributed_ms": round((r.e2e_latency_s - phase_total) * 1000, 3),
            "prompt_tokens": r.num_prompt_tokens,
            "cached_tokens": r.num_cached_tokens,
            "generation_tokens": r.num_generation_tokens,
        }
        if r.num_preemptions is not None:
            args["preemptions"] = r.num_preemptions
        out.append(_slice(r.request_id or "request", arrival, finish, lane, us, args))

        end = finish
        for name, dur in (
            ("decode", r.decode_time_s),
            ("prefill", r.prefill_time_s),
            ("queued", r.queued_time_s),
        ):
            start = max(end - max(dur, 0.0), arrival)
            if end > start:
                out.append(_slice(name, start, end, lane, us))
            end = start
    return out


def _slice(
    name: str,
    start: float,
    end: float,
    lane: int,
    us: Any,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ts = us(start)
    event: dict[str, Any] = {
        "ph": "X",
        "name": name,
        "pid": REQUESTS_PID,
        "tid": LANE_TID_BASE + lane,
        "ts": ts,
        # Derived from the rounded endpoints so a child can never poke past
        # its parent.
        "dur": us(end) - ts,
    }
    if args:
        event["args"] = args
    return event


def _kv_rate_counters(
    placed: list[tuple[float, TraceEvent]], bin_s: float, us: Any
) -> list[dict[str, Any]]:
    stored: dict[int, int] = {}
    removed: dict[int, int] = {}
    for t, e in placed:
        if isinstance(e, KVBlockStored):
            b = math.floor(t / bin_s)
            stored[b] = stored.get(b, 0) + len(e.block_hashes)
        elif isinstance(e, KVBlockRemoved):
            b = math.floor(t / bin_s)
            removed[b] = removed.get(b, 0) + len(e.block_hashes)
    if not stored and not removed:
        return []
    first = min([*stored, *removed])
    last = max([*stored, *removed])
    out = []
    # Emit every bin (zeros included) plus one closing bin: a counter holds
    # its last value, so skipping empty bins would draw stale rates.
    for b in range(first, last + 2):
        out.append(
            _counter(
                f"KV blocks per second ({bin_s:g}s bins)",
                # The first bin's grid start can precede the trace start;
                # Perfetto drops negative timestamps, so clamp to zero.
                max(us(b * bin_s), 0),
                {
                    "stored": round(stored.get(b, 0) / bin_s, 3),
                    "evicted": round(removed.get(b, 0) / bin_s, 3),
                },
                pid=KV_PID,
            )
        )
    return out


def _other_data(meta: TraceMeta | None, t0: float) -> dict[str, Any]:
    data: dict[str, Any] = {"exporter": "inferlens", "trace_start_unix": t0}
    if meta is not None:
        data.update(
            engine=meta.engine,
            engine_version=meta.engine_version,
            model=meta.model,
            schema_version=meta.schema_version,
        )
    return data
