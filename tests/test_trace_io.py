"""Round-trip tests for the trace reader/writer."""

import gzip

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
