"""Tests for the Perfetto / Chrome trace-event export."""

import gzip
import json

import pytest

from inferlens.cli import main
from inferlens.export import KV_PID, REQUESTS_PID, to_perfetto
from inferlens.schema import (
    CollectorGap,
    EngineSnapshot,
    KVBlockRemoved,
    KVBlockStored,
    KVCacheCleared,
    RequestFinished,
    TraceMeta,
)
from inferlens.trace_io import TraceWriter

# Anchor: monotonic 100.0 == unix 1000.0, so placement = ts + 900.
META = TraceMeta(
    engine="vllm",
    engine_version="0.34.0",
    model="m",
    wall_time_unix=1000.0,
    monotonic_time=100.0,
)


def _snapshot(ts, **overrides):
    fields = dict(ts=ts, num_running_reqs=2, num_waiting_reqs=1, kv_cache_usage=0.5)
    return EngineSnapshot(**{**fields, **overrides})


def _request(ts, request_id="r1", **overrides):
    fields = dict(
        ts=ts,
        request_id=request_id,
        finish_reason="stop",
        e2e_latency_s=2.0,
        queued_time_s=0.5,
        prefill_time_s=0.25,
        decode_time_s=1.0,
        num_prompt_tokens=10,
        num_generation_tokens=5,
    )
    return RequestFinished(**{**fields, **overrides})


def _by(doc, **match):
    return [
        e for e in doc["traceEvents"] if all(e.get(k) == v for k, v in match.items())
    ]


def test_events_are_placed_relative_to_trace_start_through_the_anchor():
    doc = to_perfetto([META, _snapshot(100.0), _snapshot(101.5)])
    ts = [e["ts"] for e in _by(doc, ph="C", name="Requests")]
    assert ts == [0.0, 1_500_000.0]
    assert doc["otherData"]["engine_version"] == "0.34.0"
    assert doc["otherData"]["trace_start_unix"] == 1000.0


def test_kv_events_are_placed_by_wall_time_not_receive_time():
    # Received (ts) 5 s late, e.g. recovered via replay; it happened at 1001.
    removed = KVBlockRemoved(
        ts=106.0, seq=0, wall_time_unix=1001.0, block_hashes=[1, 2]
    )
    doc = to_perfetto([META, _snapshot(100.0), removed], kv_bin_s=1.0)
    rates = _by(doc, ph="C", pid=KV_PID)
    by_ts = {e["ts"]: e["args"] for e in rates}
    assert by_ts[1_000_000.0] == {"stored": 0.0, "evicted": 2.0}
    # Closing bin returns the counter to zero instead of holding the rate.
    assert by_ts[2_000_000.0] == {"stored": 0.0, "evicted": 0.0}


def test_kv_rate_bins_include_empty_bins_between_activity():
    stored = KVBlockStored(
        ts=100.0,
        seq=0,
        wall_time_unix=1000.2,
        block_hashes=[1],
        parent_block_hash=None,
        num_tokens=16,
        block_size=16,
    )
    removed = KVBlockRemoved(ts=103.0, seq=1, wall_time_unix=1003.5, block_hashes=[1])
    doc = to_perfetto([META, stored, removed], kv_bin_s=1.0)
    rates = [e["args"] for e in _by(doc, ph="C", pid=KV_PID)]
    assert rates == [
        {"stored": 1.0, "evicted": 0.0},
        {"stored": 0.0, "evicted": 0.0},
        {"stored": 0.0, "evicted": 0.0},
        {"stored": 0.0, "evicted": 1.0},
        {"stored": 0.0, "evicted": 0.0},
    ]


def test_request_phases_are_nested_and_right_aligned_to_finish():
    # Finishes at t=1003 (2 s e2e -> arrival 1001); phases sum to 1.75 s.
    doc = to_perfetto([META, _snapshot(100.0), _request(103.0)])
    parent = _by(doc, ph="X", name="r1")[0]
    assert parent["pid"] == REQUESTS_PID
    assert parent["ts"] == 1_000_000.0
    assert parent["dur"] == 2_000_000.0
    assert parent["args"]["unattributed_ms"] == pytest.approx(250.0)
    spans = {
        name: (e["ts"], e["ts"] + e["dur"])
        for name in ("queued", "prefill", "decode")
        for e in _by(doc, ph="X", name=name)
    }
    assert spans["decode"] == (2_000_000.0, 3_000_000.0)
    assert spans["prefill"] == (1_750_000.0, 2_000_000.0)
    assert spans["queued"] == (1_250_000.0, 1_750_000.0)


def test_phases_exceeding_e2e_are_clipped_at_arrival():
    request = _request(103.0, queued_time_s=1.5)  # phases sum 2.75 s > 2 s e2e
    doc = to_perfetto([META, request])
    parent = _by(doc, ph="X", name="r1")[0]
    queued = _by(doc, ph="X", name="queued")[0]
    assert queued["ts"] == parent["ts"]
    assert queued["ts"] + queued["dur"] <= parent["ts"] + parent["dur"]
    assert parent["args"]["unattributed_ms"] == pytest.approx(-750.0)


def test_overlapping_requests_get_separate_lanes_and_lanes_are_reused():
    events = [
        META,
        _request(103.0, "a"),  # 1001..1003
        _request(104.0, "b"),  # 1002..1004, overlaps a
        _request(106.0, "c"),  # 1004..1006, fits after a
    ]
    doc = to_perfetto(events)
    lanes = {name: _by(doc, ph="X", name=name)[0]["tid"] for name in "abc"}
    assert lanes["a"] != lanes["b"]
    assert lanes["c"] == lanes["a"]
    names = _by(doc, ph="M", name="thread_name", pid=REQUESTS_PID)
    assert len(names) == 2


def test_back_to_back_requests_do_not_share_a_lane():
    # b arrives on the exact microsecond a finishes (1003.0): Perfetto would
    # nest b inside a if they shared a thread.
    doc = to_perfetto([META, _request(103.0, "a"), _request(105.0, "b")])
    lanes = {name: _by(doc, ph="X", name=name)[0]["tid"] for name in "ab"}
    assert lanes["a"] != lanes["b"]


def test_kv_bin_before_trace_start_is_clamped_to_zero():
    # First KV event at unix 1000.7 floors to bin 1000.0, before trace start.
    stored = KVBlockStored(
        ts=100.0,
        seq=0,
        wall_time_unix=1000.7,
        block_hashes=[1],
        parent_block_hash=None,
        num_tokens=16,
        block_size=16,
    )
    doc = to_perfetto([META, _snapshot(100.5), stored])
    assert min(e["ts"] for e in doc["traceEvents"] if "ts" in e) == 0.0


def test_lane_tids_never_collide_with_pids_or_zero():
    events = [META] + [_request(103.0 + i * 0.1, f"r{i}") for i in range(5)]
    doc = to_perfetto(events)
    lane_tids = {e["tid"] for e in _by(doc, ph="X")}
    pids = {e["pid"] for e in doc["traceEvents"]}
    assert len(lane_tids) == 5
    assert not lane_tids & (pids | {0})


def test_lane_slices_never_overlap():
    events = [META] + [
        _request(100.0 + i * 0.3, f"r{i}", e2e_latency_s=1.0 + (i % 3) * 0.7)
        for i in range(30)
    ]
    doc = to_perfetto(events)
    parents = [e for e in _by(doc, ph="X") if "args" in e]
    by_lane: dict[int, list[tuple[float, float]]] = {}
    for e in parents:
        by_lane.setdefault(e["tid"], []).append((e["ts"], e["ts"] + e["dur"]))
    for spans in by_lane.values():
        spans.sort()
        for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
            assert start > end


def test_v04_counters_only_when_recorded_and_named_by_time_source():
    plain = to_perfetto([META, _snapshot(100.0)])
    assert not _by(plain, name="Scheduled tokens")
    assert not [e for e in plain["traceEvents"] if "Iteration time" in e["name"]]
    assert _by(plain, name="Requests")[0]["args"] == {"running": 2, "waiting": 1}

    rich = to_perfetto(
        [
            META,
            _snapshot(
                100.0,
                num_deferred_waiting_reqs=3,
                iterations=1,
                num_prefill_reqs=1,
                num_prefill_tokens=512,
                num_decode_reqs=2,
                num_decode_tokens=2,
                iteration_time_s=0.012,
                iteration_time_source="gpu",
            ),
        ]
    )
    assert _by(rich, name="Scheduled tokens")[0]["args"] == {
        "prefill": 512,
        "decode": 2,
    }
    assert _by(rich, name="Iteration time (ms, gpu)")[0]["args"] == {"time": 12.0}
    assert _by(rich, name="Requests")[0]["args"]["deferred"] == 3


def test_prefix_hit_rate_only_when_queried():
    doc = to_perfetto(
        [
            META,
            _snapshot(100.0),
            _snapshot(101.0, prefix_cache_queries=200, prefix_cache_hits=50),
        ]
    )
    rates = _by(doc, name="Prefix cache hit rate (%)")
    assert [e["args"] for e in rates] == [{"hit_rate": 25.0}]


def test_gaps_and_cache_clears_become_instant_markers():
    gap = CollectorGap(ts=101.0, source="vllm_kv_events", reason="replay_incomplete")
    cleared = KVCacheCleared(ts=150.0, seq=3, wall_time_unix=1002.0)
    doc = to_perfetto([META, gap, cleared])
    gap_marker = _by(doc, ph="i", name="collector_gap: replay_incomplete")[0]
    assert gap_marker["s"] == "g"
    assert gap_marker["args"]["source"] == "vllm_kv_events"
    # Trace start is the earliest event (the gap, at unix 1001); the clear
    # is placed by its wall time, 1 s later — not by its late ts.
    assert gap_marker["ts"] == 0.0
    clear_marker = _by(doc, ph="i", name="KV cache cleared")[0]
    assert clear_marker["ts"] == 1_000_000.0


def test_without_anchor_relative_placement_still_works():
    doc = to_perfetto([_snapshot(5.0), _snapshot(6.0)])
    assert [e["ts"] for e in _by(doc, name="Requests")] == [0.0, 1_000_000.0]


def test_rejects_non_positive_bin_width():
    with pytest.raises(ValueError):
        to_perfetto([META], kv_bin_s=0)


def test_cli_export_writes_gzipped_json(tmp_path, capsys):
    trace = tmp_path / "t.ilens"
    with TraceWriter(trace) as w:
        for event in (META, _snapshot(100.0), _request(103.0)):
            w.write(event)
    out = tmp_path / "t.json.gz"
    assert main(["export", str(trace), "-o", str(out)]) == 0
    assert "ui.perfetto.dev" in capsys.readouterr().out
    with gzip.open(out, "rt") as f:
        doc = json.load(f)
    assert _by(doc, ph="X", name="r1")


def test_cli_export_missing_file_prints_error(tmp_path, capsys):
    rc = main(["export", str(tmp_path / "nope.ilens"), "-o", str(tmp_path / "x.json")])
    assert rc == 1
    assert "error" in capsys.readouterr().err
