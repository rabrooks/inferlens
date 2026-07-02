"""Reading and writing trace files.

A trace is a JSON Lines stream, one event record per line, optionally
gzip-compressed (detected by a ``.gz`` suffix). The first record should be a
``trace_meta`` event; readers must tolerate traces that were truncated
mid-line (a crash or an interrupted recording must not make the prefix
unreadable).
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import IO, Literal

from inferlens.schema import TraceEvent, from_record, to_record


def _open(path: Path, mode: Literal["r", "w"]) -> IO[str]:
    if path.suffix == ".gz":
        if mode == "r":
            return gzip.open(path, "rt", encoding="utf-8")
        return gzip.open(path, "wt", encoding="utf-8")
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


def read_trace(path: str | Path) -> Iterator[TraceEvent]:
    """Yield events from a trace file, stopping at a truncated final line."""
    with _open(Path(path), "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # Interrupted recording: the readable prefix is still valid.
                return
            yield from_record(record)
