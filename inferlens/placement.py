"""Timeline placement: where each trace event sits on the wall clock.

One implementation of the TRACE_SPEC placement rules, shared by every
consumer (export, analysis) so they never disagree about when something
happened. The viewer loader mirrors the same rules in TypeScript.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from inferlens.schema import (
    KVBlockRemoved,
    KVBlockStored,
    KVCacheCleared,
    TraceEvent,
    TraceMeta,
)


@dataclass(slots=True)
class Clock:
    """Maps a trace's events to wall-clock placement times (unix seconds).

    Events are rebased through the first ``trace_meta`` anchor, except
    ``kv_*``, which are placed by their own ``wall_time_unix`` so replayed
    events land when they happened rather than when they were recovered.
    """

    offset_s: float = 0.0

    @classmethod
    def from_meta(cls, meta: TraceMeta | None) -> Clock:
        """Build the clock for a trace anchored by ``meta`` (if any)."""
        if meta is None:
            # No anchor: keep raw monotonic ts. Relative placement still
            # holds, and consumers only show time since trace start.
            return cls()
        return cls(offset_s=meta.wall_time_unix - meta.monotonic_time)

    def place(self, event: TraceEvent) -> float:
        """Return ``event``'s placement time in unix seconds."""
        if isinstance(event, KVBlockStored | KVBlockRemoved | KVCacheCleared):
            return event.wall_time_unix
        if isinstance(event, TraceMeta):
            return event.wall_time_unix
        return event.ts + self.offset_s


def split_meta(
    events: Iterable[TraceEvent],
) -> tuple[TraceMeta | None, list[TraceEvent]]:
    """Separate the anchoring ``trace_meta`` from the event body.

    The first meta is the anchor; later ones are ignored, per spec.
    """
    meta: TraceMeta | None = None
    body: list[TraceEvent] = []
    for event in events:
        if isinstance(event, TraceMeta):
            if meta is None:
                meta = event
        else:
            body.append(event)
    return meta, body
