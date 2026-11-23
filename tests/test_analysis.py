"""Tests for trace analysis and the ``inferlens explain`` report."""

import json

import pytest

from inferlens.analysis import KV_SATURATED, analyze
from inferlens.cli import main
from inferlens.explain import EXPLAIN_VERSION, render_text, to_json
from inferlens.schema import (
    CollectorGap,
    EngineSnapshot,
    KVBlockRemoved,
    RequestFinished,
    TraceMeta,
)
from inferlens.trace_io import TraceWriter

# Anchor monotonic 0 at unix 1000, so placement = 1000 + ts.
META = TraceMeta(
    engine="vllm",
    engine_version="0.34.0",
    model="m",
    wall_time_unix=1000.0,
    monotonic_time=0.0,
)


def _snap(ts, kv=0.3, running=4, waiting=0, preempted=0):
    return EngineSnapshot(
        ts=ts,
        num_running_reqs=running,
        num_waiting_reqs=waiting,
        kv_cache_usage=kv,
        num_preempted_reqs=preempted,
    )


def _req(finish, e2e, rid, queued=0.001, prefill=0.05, decode=None, gen=11, **kw):
    decode = e2e - queued - prefill - 0.01 if decode is None else decode
    return RequestFinished(
        ts=finish,
        request_id=rid,
        finish_reason="stop",
        e2e_latency_s=e2e,
        queued_time_s=queued,
        prefill_time_s=prefill,
        decode_time_s=decode,
        num_prompt_tokens=100,
        num_generation_tokens=gen,
        **kw,
    )


def _calm_engine(seconds=60):
    return [_snap(float(t)) for t in range(seconds)]


def _calm_requests(n=10, start=1.0, every=5.0):
    # 1 s requests spaced out so they avoid the storm windows used below.
    return [_req(start + i * every + 1.0, 1.0, f"calm{i}") for i in range(n)]


def test_calm_trace_has_no_incidents():
    a = analyze([META, *_calm_engine(), *_calm_requests()])
    assert a.incidents == []
    assert [d.kind for d in a.diagnoses] == ["calm"]
    assert a.calm.count == 10
    assert a.affected.count == 0
    assert a.engine_version == "0.34.0"


def test_kv_storm_is_one_kv_bound_incident_with_slowdown():
    engine = _calm_engine()
    # Seconds 30-32: KV saturated and requests preempted.
    for t in (30, 31, 32):
        engine[t] = _snap(float(t), kv=1.0, running=8, waiting=6, preempted=4)
    slow = [_req(33.5, 4.0, "slow0"), _req(34.0, 4.5, "slow1")]
    calm = _calm_requests(n=5, start=0.0, every=4.0)  # all finish by t=18
    a = analyze([META, *engine, *calm, *slow])

    assert len(a.incidents) == 1
    inc = a.incidents[0]
    assert (inc.start_s, inc.end_s) == (30.0, 33.0)
    assert inc.kind == "kv_bound"
    assert inc.preemptions == 12
    assert inc.kv_usage_max == 1.0
    assert inc.affected.count == 2
    assert inc.slowdown_p95 == pytest.approx(4.475 / 1.0)
    assert {r.request_id: r.incidents for r in a.attributions}["slow0"] == [1]
    assert a.diagnoses[0].kind == "kv_bound"
    assert any("--max-num-seqs" in s for s in a.diagnoses[0].suggestions)


def test_queue_backlog_without_kv_pressure_is_queue_bound():
    engine = _calm_engine()
    for t in (20, 21):
        engine[t] = _snap(float(t), kv=0.5, running=4, waiting=9)
    a = analyze([META, *engine, *_calm_requests()])
    assert [i.kind for i in a.incidents] == ["queue_bound"]
    assert a.diagnoses[0].kind == "queue_bound"


def test_near_full_kv_counts_as_saturated():
    engine = _calm_engine()
    engine[10] = _snap(10.0, kv=KV_SATURATED)
    a = analyze([META, *engine])
    assert len(a.incidents) == 1


def test_short_lulls_are_bridged_long_ones_split():
    engine = _calm_engine()
    for t in (10, 12, 13, 30):  # 1-bin lull between 10 and 12; long gap to 30
        engine[t] = _snap(float(t), kv=1.0, preempted=1)
    a = analyze([META, *engine])
    assert [(i.start_s, i.end_s) for i in a.incidents] == [(10.0, 14.0), (30.0, 31.0)]


def test_evictions_are_binned_by_wall_time():
    engine = _calm_engine()
    engine[40] = _snap(40.0, kv=1.0, preempted=1)
    # Received late (ts) but happened at t=40.5 by wall clock.
    late = KVBlockRemoved(ts=55.0, seq=0, wall_time_unix=1040.5, block_hashes=[1, 2, 3])
    a = analyze([META, *engine, late])
    assert a.incidents[0].evicted_blocks == 3


def test_attribution_reports_phases_as_reported_and_the_remainder():
    r = _req(10.0, 2.0, "r", queued=0.5, prefill=0.2, decode=1.0)
    a = analyze([META, *_calm_engine(), r])
    att = a.request("r")
    assert att is not None
    assert att.unattributed_s == pytest.approx(0.3)
    assert att.dominant == "decode"
    assert att.arrival_s == pytest.approx(8.0)
    assert a.request("missing") is None


def test_vs_calm_ratios_and_queue_floor():
    calm = [_req(5.0 + i, 1.0, f"c{i}", queued=0.000_01, decode=0.5) for i in range(6)]
    probe = _req(20.0, 3.0, "probe", queued=2.0, decode=1.0)
    a = analyze([META, *_calm_engine(), *calm, probe])
    att = a.request("probe")
    assert att is not None
    # Calm decode 0.5 s / 10 tokens; probe 1.0 s / 10 tokens.
    assert att.tpot_vs_calm == pytest.approx(2.0)
    # Calm requests barely queued: a ratio would be meaningless.
    assert att.queued_vs_calm is None
    assert a.calm_queued_p50_s == pytest.approx(0.000_01)
    assert "vs a calm median" in render_text(a, worst=3, request_id="probe")


def test_too_few_calm_requests_gives_no_baseline():
    engine = _calm_engine()
    engine[5] = _snap(5.0, kv=1.0, preempted=2)
    a = analyze([META, *engine, _req(7.0, 3.0, "only")])
    assert a.incidents[0].slowdown_p95 is None
    assert any("no calm baseline" in c for c in a.caveats)


def test_caveats_for_gaps_and_missing_preemption_counts():
    gap = CollectorGap(ts=3.0, source="vllm_kv_events", reason="replay_incomplete")
    a = analyze([META, *_calm_engine(), *_calm_requests(), gap])
    assert a.gaps == 1
    assert any("collector gap" in c for c in a.caveats)
    assert any("preemption counts were not recorded" in c for c in a.caveats)

    counted = [_req(5.0 + i, 1.0, f"p{i}", num_preemptions=0) for i in range(5)]
    b = analyze([META, *_calm_engine(), *counted])
    assert not any("preemption counts" in c for c in b.caveats)


def test_empty_trace_and_bad_bin_width():
    a = analyze([META])
    assert a.requests.count == 0
    assert a.caveats == ["trace has no events"]
    assert "Incidents: none" in render_text(a, worst=5)
    with pytest.raises(ValueError):
        analyze([META], bin_s=0)


def test_json_report_shape():
    engine = _calm_engine()
    engine[30] = _snap(30.0, kv=1.0, preempted=1)
    a = analyze([META, *engine, *_calm_requests()])
    doc = to_json(a, worst=2)
    assert doc["explain_version"] == EXPLAIN_VERSION
    assert "bins" not in doc
    assert len(doc["attributions"]) == 2
    assert doc["incidents"][0]["duration_s"] == 1.0
    json.dumps(doc)  # serializable


def _write(tmp_path, events):
    path = tmp_path / "t.ilens"
    with TraceWriter(path) as w:
        for e in events:
            w.write(e)
    return path


def test_cli_explain_text_and_json(tmp_path, capsys):
    path = _write(tmp_path, [META, *_calm_engine(), *_calm_requests()])
    assert main(["explain", str(path), "--worst", "3"]) == 0
    out = capsys.readouterr().out
    assert "Incidents: none" in out
    assert "Slowest 3 requests" in out
    assert main(["explain", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["calm"]["count"] == 10


def test_cli_explain_unknown_request_exits_nonzero(tmp_path, capsys):
    path = _write(tmp_path, [META, *_calm_engine(), *_calm_requests()])
    assert main(["explain", str(path), "--request", "nope"]) == 1
    assert "not found" in capsys.readouterr().out
