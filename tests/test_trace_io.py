"""Round-trip tests for the trace reader/writer."""

import gzip
import json
import time

import pytest

from inferlens.schema import (
    SCHEMA_VERSION,
    EngineSnapshot,
    RequestFinished,
    TraceMeta,
    from_record,
    to_record,
)
from inferlens.trace_io import BufferedTraceWriter, TraceWriter, read_trace

META = TraceMeta(
    engine="vllm",
    engine_version="0.11.0",
    model="Qwen/Qwen2.5-1.5B-Instruct",
    wall_time_unix=1_700_000_000.0,
    monotonic_time=1000.0,
)
SNAPSHOT = EngineSnapshot(
    ts=1001.5,
    num_running_reqs=8,
    num_waiting_reqs=3,
    kv_cache_usage=0.72,
    num_preempted_reqs=1,
)
FINISHED = RequestFinished(
    ts=1002.0,
    request_id="req-1",
    finish_reason="stop",
    e2e_latency_s=1.9,
    queued_time_s=0.2,
    prefill_time_s=0.3,
    decode_time_s=1.4,
    num_prompt_tokens=512,
    num_generation_tokens=128,
    num_cached_tokens=256,
)


@pytest.mark.parametrize("suffix", [".ilens", ".ilens.gz"])
def test_roundtrip(tmp_path, suffix):
    path = tmp_path / f"trace{suffix}"
    with TraceWriter(path) as writer:
        for event in (META, SNAPSHOT, FINISHED):
            writer.write(event)

    events = list(read_trace(path))
    assert events == [META, SNAPSHOT, FINISHED]
    assert events[0].schema_version == SCHEMA_VERSION


def test_record_roundtrip():
    record = to_record(SNAPSHOT)
    assert record["kind"] == "engine_snapshot"
    assert from_record(record) == SNAPSHOT


def test_from_record_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown event kind"):
        from_record({"kind": "nope"})
    with pytest.raises(ValueError, match="no 'kind' tag"):
        from_record({"ts": 1.0})


def test_from_record_ignores_unknown_fields():
    # A newer minor schema version may add optional fields; per the spec,
    # readers must ignore them rather than fail.
    record = to_record(SNAPSHOT)
    record["field_from_the_future"] = 42
    assert from_record(record) == SNAPSHOT


def test_read_trace_skips_unknown_and_malformed_records(tmp_path):
    # Unknown kinds (added by a newer minor version) and malformed records
    # are skipped; valid records *after* them must still be read.
    path = tmp_path / "trace.ilens"
    with TraceWriter(path) as writer:
        writer.write(META)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"kind": "kind_from_the_future", "ts": 3.0}\n')
        f.write('{"kind": "engine_snapshot"}\n')  # missing required fields
        f.write(json.dumps(to_record(SNAPSHOT)) + "\n")

    assert list(read_trace(path)) == [META, SNAPSHOT]


def test_read_trace_rejects_future_major(tmp_path):
    path = tmp_path / "trace.ilens"
    future_meta = TraceMeta(
        engine="vllm",
        engine_version="99.0",
        model="m",
        wall_time_unix=0.0,
        monotonic_time=0.0,
        schema_version="1.0",
    )
    with TraceWriter(path) as writer:
        writer.write(future_meta)

    with pytest.raises(ValueError, match="unsupported trace schema"):
        list(read_trace(path))


def test_read_trace_tolerates_truncated_gzip(tmp_path):
    # A recorder killed mid-write leaves a gzip stream with no trailer;
    # the decompressed prefix must still be readable without raising.
    lines = "".join(
        json.dumps(to_record(event)) + "\n" for event in (META, SNAPSHOT, FINISHED)
    )
    blob = gzip.compress(lines.encode())
    path = tmp_path / "trace.ilens.gz"
    path.write_bytes(blob[:-12])  # chop the gzip trailer and then some

    events = list(read_trace(path))  # must not raise
    assert events == [META, SNAPSHOT, FINISHED][: len(events)]


def test_truncated_trace_keeps_readable_prefix(tmp_path):
    path = tmp_path / "trace.ilens"
    with TraceWriter(path) as writer:
        writer.write(META)
        writer.write(SNAPSHOT)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"kind": "engine_snapshot", "ts"')  # simulate a crash mid-write

    assert list(read_trace(path)) == [META, SNAPSHOT]


def test_gzip_output_is_actually_gzip(tmp_path):
    path = tmp_path / "trace.ilens.gz"
    with TraceWriter(path) as writer:
        writer.write(META)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        assert '"kind":"trace_meta"' in f.read()


def test_buffered_trace_writer_roundtrip(tmp_path):
    path = tmp_path / "trace.ilens"
    with BufferedTraceWriter(path) as writer:
        for event in (META, SNAPSHOT, FINISHED):
            writer.write(event)

    assert list(read_trace(path)) == [META, SNAPSHOT, FINISHED]


def test_buffered_trace_writer_flushes_without_close(tmp_path):
    # The durability contract: events must reach disk within the flush
    # interval even if the writer is never closed (hard-killed recorder).
    path = tmp_path / "trace.ilens"
    writer = BufferedTraceWriter(path, flush_interval_s=0.05)
    try:
        writer.write(META)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if list(read_trace(path)) == [META]:
                break
            time.sleep(0.02)
        assert list(read_trace(path)) == [META]
    finally:
        writer.close()


def test_buffered_trace_writer_close_is_idempotent(tmp_path):
    writer = BufferedTraceWriter(tmp_path / "trace.ilens")
    writer.write(META)
    writer.close()
    writer.close()  # e.g. an explicit close racing the atexit hook

    assert list(read_trace(tmp_path / "trace.ilens")) == [META]


def test_buffered_trace_writer_drops_past_maxsize(tmp_path):
    path = tmp_path / "trace.ilens"
    writer = BufferedTraceWriter(path, maxsize=1)
    # The writer thread may drain the queue between put_nowait calls, so
    # flood it enough to guarantee at least one drop regardless of timing.
    for _ in range(1000):
        writer.write(SNAPSHOT)
    writer.close()

    assert writer.dropped > 0
    events = list(read_trace(path))
    assert 0 < len(events) < 1000
