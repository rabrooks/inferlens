"""Trace event types (schema v0).

Events are engine-neutral: collectors translate engine-specific stats into
these types. Every event except :class:`TraceMeta` carries ``ts``, a
monotonic-clock reading in seconds; ``TraceMeta`` anchors that clock to wall
time so multiple event sources can be aligned.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

SCHEMA_VERSION = "0.1"


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
    """Engine-wide state at one logging step (queue depths, cache usage)."""

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


TraceEvent = TraceMeta | EngineSnapshot | RequestFinished

EVENT_TYPES: dict[str, type] = {
    cls.KIND: cls for cls in (TraceMeta, EngineSnapshot, RequestFinished)
}


def to_record(event: TraceEvent) -> dict[str, Any]:
    """Serialize an event to a JSON-ready record with a ``kind`` tag."""
    record = asdict(event)
    record["kind"] = event.KIND
    return record


def from_record(record: dict[str, Any]) -> TraceEvent:
    """Deserialize a record produced by :func:`to_record`.

    Raises:
        ValueError: If the record has no ``kind`` tag or an unknown one.
    """
    fields = dict(record)
    kind = fields.pop("kind", None)
    if kind is None:
        raise ValueError("record has no 'kind' tag")
    event_type = EVENT_TYPES.get(kind)
    if event_type is None:
        raise ValueError(f"unknown event kind: {kind!r}")
    return event_type(**fields)
