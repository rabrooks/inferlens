"""Trace event types (schema v0).

Events are engine-neutral: collectors translate engine-specific stats into
these types. Every event except :class:`TraceMeta` carries ``ts``, a
monotonic-clock reading in seconds; ``TraceMeta`` anchors that clock to wall
time so multiple event sources can be aligned.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, ClassVar

SCHEMA_VERSION = "0.3"


@dataclass(slots=True)
class TraceMeta:
    """First record of every trace: identity plus the clock anchor."""

    KIND: ClassVar[str] = "trace_meta"

    engine: str
    engine_version: str
    model: str
    wall_time_unix: float
    monotonic_time: float
    schema_version: str = SCHEMA_VERSION
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EngineSnapshot:
    """Engine-wide state at one logging step (queue depths, cache usage).

    The ``ttft_*``/``itl_*`` fields summarize this iteration's
    time-to-first-token and inter-token-latency samples (seconds). They are
    engine-level distributions, not per-request values: the samples are
    unkeyed at the source (no request ID attached — see
    ``docs/upstream-gaps.md`` §1), so summaries are all that can be
    recorded faithfully. ``None`` means the iteration had no samples.
    """

    KIND: ClassVar[str] = "engine_snapshot"

    ts: float
    num_running_reqs: int
    num_waiting_reqs: int
    kv_cache_usage: float
    prefix_cache_queries: int = 0
    prefix_cache_hits: int = 0
    num_preempted_reqs: int = 0
    num_generation_tokens: int = 0
    num_prompt_tokens: int = 0
    ttft_count: int = 0
    ttft_mean_s: float | None = None
    ttft_p50_s: float | None = None
    ttft_max_s: float | None = None
    itl_count: int = 0
    itl_mean_s: float | None = None
    itl_p50_s: float | None = None
    itl_max_s: float | None = None


@dataclass(slots=True)
class RequestFinished:
    """Lifecycle summary emitted when a request completes."""

    KIND: ClassVar[str] = "request_finished"

    ts: float
    request_id: str
    finish_reason: str
    e2e_latency_s: float
    queued_time_s: float
    prefill_time_s: float
    decode_time_s: float
    num_prompt_tokens: int
    num_generation_tokens: int
    num_cached_tokens: int = 0


@dataclass(slots=True)
class KVBlockStored:
    """A KV cache block chain was stored (e.g. vLLM's ``BlockStored``).

    Token content is deliberately not recorded — only ``num_tokens`` — so a
    trace never carries prompt text. ``block_hashes`` alone already raise a
    fingerprinting concern; see the open question in ``TRACE_SPEC.md``.
    """

    KIND: ClassVar[str] = "kv_block_stored"

    ts: float
    seq: int
    wall_time_unix: float
    block_hashes: list[int | str]
    parent_block_hash: int | str | None
    num_tokens: int
    block_size: int
    medium: str | None = None
    group_idx: int = 0


@dataclass(slots=True)
class KVBlockRemoved:
    """A KV cache block was evicted (e.g. vLLM's ``BlockRemoved``)."""

    KIND: ClassVar[str] = "kv_block_removed"

    ts: float
    seq: int
    wall_time_unix: float
    block_hashes: list[int | str]
    medium: str | None = None
    group_idx: int = 0


@dataclass(slots=True)
class KVCacheCleared:
    """The whole prefix cache was reset (e.g. vLLM's ``AllBlocksCleared``)."""

    KIND: ClassVar[str] = "kv_cache_cleared"

    ts: float
    seq: int
    wall_time_unix: float


@dataclass(slots=True)
class CollectorGap:
    """Events the collector knows it lost.

    A trace with holes is acceptable; a trace with *silent* holes is not.
    Collectors emit this instead of dropping data quietly — e.g. the KV-event
    subscriber saw a sequence-number jump it could not recover via replay —
    so a viewer can render "data missing here" rather than implying nothing
    happened. ``source`` names the collector stream (e.g.
    ``"vllm_kv_events"``), ``reason`` is a short machine-readable cause, and
    ``first_seq``/``last_seq`` bound the missed range (inclusive) when the
    stream is sequence-numbered.
    """

    KIND: ClassVar[str] = "collector_gap"

    ts: float
    source: str
    reason: str
    first_seq: int | None = None
    last_seq: int | None = None


# Every kind except TraceMeta carries a monotonic `ts` (see the spec's
# Envelope section) — the property merge logic relies on.
TimedEvent = (
    EngineSnapshot
    | RequestFinished
    | KVBlockStored
    | KVBlockRemoved
    | KVCacheCleared
    | CollectorGap
)

TraceEvent = TraceMeta | TimedEvent

EVENT_TYPES: dict[str, type[TraceEvent]] = {
    cls.KIND: cls
    for cls in (
        TraceMeta,
        EngineSnapshot,
        RequestFinished,
        KVBlockStored,
        KVBlockRemoved,
        KVCacheCleared,
        CollectorGap,
    )
}

_FIELD_NAMES: dict[str, frozenset[str]] = {
    kind: frozenset(f.name for f in fields(cls)) for kind, cls in EVENT_TYPES.items()
}


def to_record(event: TraceEvent) -> dict[str, Any]:
    """Serialize an event to a JSON-ready record with a ``kind`` tag."""
    record = asdict(event)
    record["kind"] = event.KIND
    return record


def from_record(record: dict[str, Any]) -> TraceEvent:
    """Deserialize a record produced by :func:`to_record`.

    Unknown fields are dropped, per the trace spec: a newer *minor* schema
    version may have added optional fields this reader doesn't know.

    Raises:
        ValueError: If the record has no ``kind`` tag or an unknown one.
        TypeError: If the record is missing required fields for its kind.
    """
    values = dict(record)
    kind = values.pop("kind", None)
    if kind is None:
        raise ValueError("record has no 'kind' tag")
    event_type = EVENT_TYPES.get(kind)
    if event_type is None:
        raise ValueError(f"unknown event kind: {kind!r}")
    known = _FIELD_NAMES[kind]
    return event_type(**{k: v for k, v in values.items() if k in known})
