"""Reading and writing trace files.

A trace is a JSON Lines stream, one event record per line, optionally
gzip-compressed (detected by a ``.gz`` suffix). The first record should be a
``trace_meta`` event; readers must tolerate traces that were truncated
mid-line (a crash or an interrupted recording must not make the prefix
unreadable).
"""

from __future__ import annotations

import gzip
import heapq
import json
import logging
import queue
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Literal, Protocol

from inferlens.schema import (
    SCHEMA_VERSION,
    TimedEvent,
    TraceEvent,
    TraceMeta,
    from_record,
    to_record,
)

_logger = logging.getLogger(__name__)


class EventSink(Protocol):
    """The minimal interface a collector needs to emit trace events.

    Engine collectors write to this Protocol rather than to a concrete
    writer, so a recording pipeline can hand them a :class:`TraceWriter`,
    a :class:`BufferedTraceWriter`, or something else entirely (e.g. an
    in-process queue feeding a single shared writer) without the collector
    knowing the difference.
    """

    def write(self, event: TraceEvent) -> None:
        """Record one event; must be safe to call from the collector's thread."""


def _open(path: Path, mode: Literal["r", "w"]) -> IO[str]:
    if path.suffix == ".gz":
        if mode == "r":
            return gzip.open(path, "rt", encoding="utf-8")
        # Level 6 over the default 9: on JSONL the size difference is a few
        # percent, but level 9 costs roughly double the CPU — and the writer
        # thread shares cores with the engine being traced.
        return gzip.open(path, "wt", encoding="utf-8", compresslevel=6)
    return open(path, mode, encoding="utf-8")


class TraceWriter:
    """Append-only trace writer, usable as a context manager."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._file = _open(self._path, "w")

    def write(self, event: TraceEvent) -> None:
        """Write one event as a single JSONL record."""
        json.dump(to_record(event), self._file, separators=(",", ":"))
        self._file.write("\n")

    def flush(self) -> None:
        """Push buffered data to the OS (for gzip, sync-flush the stream).

        After a flush, everything written so far survives the *process*
        dying; surviving power loss (fsync) is out of scope.
        """
        self._file.flush()

    def close(self) -> None:
        """Flush and close the underlying file."""
        self._file.close()

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class BufferedTraceWriter:
    """Trace writer safe to call from a latency-sensitive event loop.

    ``write()`` only enqueues; a background thread performs the actual
    (blocking, possibly gzip-compressing) file I/O. If that thread falls
    behind — a stalled disk, for instance — events are dropped rather than
    blocking the caller, since callers of this class (e.g. the vLLM
    stat-logger plugin) must never stall their engine's event loop. See
    ``dropped`` for the count.

    Durability: the file is flushed at least every ``flush_interval_s``
    seconds, so a hard-killed recording loses at most that window of
    events (see the crash-tolerance goal in ``docs/TRACE_SPEC.md``).
    """

    def __init__(
        self,
        path: str | Path,
        maxsize: int = 10_000,
        flush_interval_s: float = 1.0,
    ) -> None:
        self._writer = TraceWriter(path)
        self._queue: queue.Queue[TraceEvent | None] = queue.Queue(maxsize=maxsize)
        self._flush_interval_s = flush_interval_s
        self.dropped = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def write(self, event: TraceEvent) -> None:
        """Enqueue an event for the writer thread; never blocks."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        next_flush = time.monotonic() + self._flush_interval_s
        dirty = False
        while True:
            try:
                item = self._queue.get(timeout=self._flush_interval_s)
                if item is None:
                    return  # close() flushes via TraceWriter.close()
                try:
                    self._writer.write(item)
                    dirty = True
                except Exception:
                    _logger.exception("dropping trace event that failed to write")
            except queue.Empty:
                pass
            if dirty and time.monotonic() >= next_flush:
                try:
                    self._writer.flush()
                    dirty = False
                except Exception:
                    _logger.exception("failed to flush trace file")
                next_flush = time.monotonic() + self._flush_interval_s

    def close(self, timeout: float = 5.0) -> None:
        """Drain the queue and close the underlying file.

        Safe to call more than once (e.g. an explicit call racing an
        ``atexit`` hook): later calls are no-ops. Never blocks shutdown for
        more than ~2x ``timeout``, even with a wedged writer thread.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            _logger.warning("trace write queue still full at close; events lost")
        self._thread.join(timeout=timeout)
        self._writer.close()

    def __enter__(self) -> BufferedTraceWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def read_trace(path: str | Path) -> Iterator[TraceEvent]:
    """Yield events from a trace file.

    Tolerant by design, per the spec's forward-compatibility rules: records
    of unknown kind or shape are skipped with a warning (a newer *minor*
    schema version may have added them), and a truncated final line or gzip
    stream ends the trace rather than raising (a crashed recording keeps
    its readable prefix).

    Raises:
        ValueError: If the trace declares a schema *major* version this
            reader doesn't support.
    """
    skipped_kinds: set[Any] = set()
    with _open(Path(path), "r") as f:
        try:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # Interrupted recording: the readable prefix is valid.
                    return
                try:
                    event = from_record(record)
                except (TypeError, ValueError):
                    kind = record.get("kind") if isinstance(record, dict) else None
                    if kind not in skipped_kinds:
                        skipped_kinds.add(kind)
                        _logger.warning(
                            "skipping unreadable record kind=%r (first at %s:%d)",
                            kind,
                            path,
                            lineno,
                        )
                    continue
                if isinstance(event, TraceMeta):
                    _check_schema_major(event.schema_version, path)
                yield event
        except EOFError:
            # A gzip stream cut off mid-write (crashed recorder): everything
            # decompressed so far has already been yielded.
            return


def iter_merged(paths: Sequence[str | Path]) -> Iterator[TraceEvent]:
    """Merge per-source trace streams into one self-consistent stream.

    Implements the record-time merge defined in ``docs/TRACE_SPEC.md``
    "Multi-source recording". The first path is the *primary* source: its
    ``trace_meta`` — identity and clock anchor — becomes the merged
    stream's, and every other source's ``ts`` values are rebased onto the
    primary's monotonic clock through the two streams' wall anchors
    (``ts' = ts + (anchor_wall - anchor_mono) - (primary_wall -
    primary_mono)``; an identity for the primary's own events). Secondary
    ``trace_meta`` records are not re-emitted — their anchors would
    misdescribe the rebased ``ts`` values around them — but are preserved
    verbatim in the merged meta's ``extra["merged_sources"]``.

    Inputs must be ``ts``-ordered, which streams produced by this module's
    writers are by construction.

    Raises:
        ValueError: If ``paths`` is empty, or an input stream contains no
            ``trace_meta`` anchor (such a stream is not mergeable — see the
            spec).
    """
    if not paths:
        raise ValueError("no trace streams to merge")
    anchored = [_read_to_anchor(Path(path)) for path in paths]
    primary = anchored[0][0]
    yield replace(
        primary,
        extra={
            **primary.extra,
            "merged_sources": [to_record(meta) for meta, _ in anchored],
        },
    )
    primary_offset = primary.wall_time_unix - primary.monotonic_time
    streams = [
        _rebased(events, meta.wall_time_unix - meta.monotonic_time - primary_offset)
        for meta, events in anchored
    ]
    yield from heapq.merge(*streams, key=lambda event: event.ts)


def merge_traces(paths: Sequence[str | Path], output: str | Path) -> None:
    """Merge per-source trace files into a single trace file.

    See :func:`iter_merged` for the merge semantics.
    """
    with TraceWriter(output) as writer:
        for event in iter_merged(paths):
            writer.write(event)


def _read_to_anchor(path: Path) -> tuple[TraceMeta, Iterator[TraceEvent]]:
    """Consume a stream up to its clock anchor; return (anchor, remainder)."""
    events = read_trace(path)
    for dropped, event in enumerate(events):
        if isinstance(event, TraceMeta):
            if dropped:
                _logger.warning(
                    "%s: dropped %d events preceding the trace_meta anchor "
                    "(unanchored ts values cannot be merged)",
                    path,
                    dropped,
                )
            return event, events
    raise ValueError(f"{path}: no trace_meta anchor; stream is not mergeable")


def _rebased(events: Iterator[TraceEvent], offset_s: float) -> Iterator[TimedEvent]:
    for event in events:
        if isinstance(event, TraceMeta):
            # A mid-stream re-anchor isn't part of the format (yet); its
            # events were anchored by the first meta, so just drop it.
            _logger.warning("ignoring extra mid-stream trace_meta during merge")
            continue
        # Events come fresh off read_trace, so in-place rebasing is safe.
        event.ts += offset_s
        yield event


def _check_schema_major(version: str, path: str | Path) -> None:
    """Reject majors we don't know; minors are forward-compatible."""
    major = version.split(".", 1)[0]
    if major != SCHEMA_VERSION.split(".", 1)[0]:
        raise ValueError(
            f"{path}: unsupported trace schema version {version!r} "
            f"(this reader supports {SCHEMA_VERSION!r})"
        )
