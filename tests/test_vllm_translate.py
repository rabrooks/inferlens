"""Tests for the vLLM stat-translation logic (no vLLM import required)."""

from types import SimpleNamespace

import pytest

from inferlens.collectors.vllm import translate


def _scheduler_stats(**overrides):
    defaults = dict(
        num_running_reqs=4,
        num_waiting_reqs=2,
        kv_cache_usage=0.55,
        prefix_cache_stats=SimpleNamespace(queries=100, hits=40),
    )
    return SimpleNamespace(**{**defaults, **overrides})


def _finished_stat(**overrides):
    defaults = dict(
        request_id="req-1",
        finish_reason=SimpleNamespace(name="STOP"),
        e2e_latency=1.9,
        queued_time=0.2,
        prefill_time=0.3,
        decode_time=1.4,
        num_prompt_tokens=512,
        num_generation_tokens=128,
        num_cached_tokens=256,
    )
    return SimpleNamespace(**{**defaults, **overrides})


def _iteration_stats(**overrides):
    defaults = dict(
        num_preempted_reqs=1,
        num_generation_tokens=16,
        num_prompt_tokens=32,
        finished_requests=[],
        time_to_first_tokens_iter=[],
        inter_token_latencies_iter=[],
    )
    return SimpleNamespace(**{**defaults, **overrides})


def test_engine_snapshot_none_when_scheduler_stats_missing():
    assert translate.engine_snapshot(None, _iteration_stats(), ts=1.0) is None


def test_engine_snapshot_full():
    snapshot = translate.engine_snapshot(_scheduler_stats(), _iteration_stats(), ts=1.5)
    assert snapshot.ts == 1.5
    assert snapshot.num_running_reqs == 4
    assert snapshot.num_waiting_reqs == 2
    assert snapshot.kv_cache_usage == 0.55
    assert snapshot.prefix_cache_queries == 100
    assert snapshot.prefix_cache_hits == 40
    assert snapshot.num_preempted_reqs == 1
    assert snapshot.num_generation_tokens == 16
    assert snapshot.num_prompt_tokens == 32


def test_engine_snapshot_defaults_iteration_fields_when_none():
    snapshot = translate.engine_snapshot(_scheduler_stats(), None, ts=1.0)
    assert snapshot.num_preempted_reqs == 0
    assert snapshot.num_generation_tokens == 0
    assert snapshot.num_prompt_tokens == 0
    assert snapshot.ttft_count == 0
    assert snapshot.ttft_mean_s is None
    assert snapshot.itl_count == 0
    assert snapshot.itl_mean_s is None


def test_engine_snapshot_latency_summaries():
    stats = _iteration_stats(
        time_to_first_tokens_iter=[0.2, 0.4, 0.9],
        inter_token_latencies_iter=[0.01, 0.03],
    )
    snapshot = translate.engine_snapshot(_scheduler_stats(), stats, ts=1.0)
    assert snapshot.ttft_count == 3
    assert snapshot.ttft_mean_s == pytest.approx(0.5)
    assert snapshot.ttft_p50_s == pytest.approx(0.4)
    assert snapshot.ttft_max_s == pytest.approx(0.9)
    assert snapshot.itl_count == 2
    assert snapshot.itl_mean_s == pytest.approx(0.02)
    assert snapshot.itl_p50_s == pytest.approx(0.02)
    assert snapshot.itl_max_s == pytest.approx(0.03)


def test_engine_snapshot_latency_summaries_none_when_empty():
    snapshot = translate.engine_snapshot(_scheduler_stats(), _iteration_stats(), ts=1.0)
    assert snapshot.ttft_count == 0
    assert snapshot.ttft_mean_s is None
    assert snapshot.ttft_p50_s is None
    assert snapshot.ttft_max_s is None
    assert snapshot.itl_count == 0
    assert snapshot.itl_mean_s is None


def test_engine_snapshot_defaults_prefix_stats_when_absent():
    stats = _scheduler_stats(prefix_cache_stats=None)
    snapshot = translate.engine_snapshot(stats, _iteration_stats(), ts=1.0)
    assert snapshot.prefix_cache_queries == 0
    assert snapshot.prefix_cache_hits == 0


def test_request_finished_events_empty_when_iteration_stats_missing():
    assert translate.request_finished_events(None, ts=1.0) == []


def test_request_finished_events_empty_when_no_finished_requests():
    assert translate.request_finished_events(_iteration_stats(), ts=1.0) == []


def test_request_finished_events_translates_and_lowercases_reason():
    stats = _iteration_stats(finished_requests=[_finished_stat()])
    [event] = translate.request_finished_events(stats, ts=2.0)
    assert event.ts == 2.0
    assert event.request_id == "req-1"
    assert event.finish_reason == "stop"
    assert event.e2e_latency_s == 1.9
    assert event.queued_time_s == 0.2
    assert event.prefill_time_s == 0.3
    assert event.decode_time_s == 1.4
    assert event.num_prompt_tokens == 512
    assert event.num_generation_tokens == 128
    assert event.num_cached_tokens == 256


def test_request_finished_events_handles_multiple_and_plain_string_reason():
    stats = _iteration_stats(
        finished_requests=[
            _finished_stat(request_id="req-1"),
            _finished_stat(request_id="req-2", finish_reason="length"),
        ]
    )
    events = translate.request_finished_events(stats, ts=2.0)
    assert [e.request_id for e in events] == ["req-1", "req-2"]
    assert [e.finish_reason for e in events] == ["stop", "length"]


def _iteration_record(**overrides):
    defaults = dict(
        iteration_index=0,
        num_ctx_requests=1,
        num_ctx_tokens=512,
        num_generation_requests=3,
        num_generation_tokens=3,
        elapsed_ms=12.0,
        is_dummy=False,
    )
    return SimpleNamespace(**{**defaults, **overrides})


def test_engine_snapshot_iteration_fields_absent_by_default():
    snapshot = translate.engine_snapshot(_scheduler_stats(), _iteration_stats(), ts=1.0)
    assert snapshot.iterations is None
    assert snapshot.num_prefill_tokens is None
    assert snapshot.iteration_time_s is None
    assert snapshot.iteration_time_source is None
    assert snapshot.num_deferred_waiting_reqs is None


def test_engine_snapshot_iteration_details_single_record():
    # vLLM 0.34 shape: one record, EngineCore wall-clock elapsed time.
    stats = _scheduler_stats(
        iteration_details=_iteration_record(), num_skipped_waiting_reqs=2
    )
    snapshot = translate.engine_snapshot(stats, _iteration_stats(), ts=1.0)
    assert snapshot.iterations == 1
    assert snapshot.num_prefill_reqs == 1
    assert snapshot.num_prefill_tokens == 512
    assert snapshot.num_decode_reqs == 3
    assert snapshot.num_decode_tokens == 3
    assert snapshot.iteration_time_s == pytest.approx(0.012)
    assert snapshot.iteration_time_source == "engine_step_wall"
    assert snapshot.num_deferred_waiting_reqs == 2


def test_engine_snapshot_iteration_records_list_sums_and_skips_dummies():
    # Post-0.34 shape: several GPU-timed records per snapshot.
    stats = _scheduler_stats(
        iteration_records=[
            _iteration_record(elapsed_ms=10.0),
            _iteration_record(num_ctx_tokens=0, num_ctx_requests=0, elapsed_ms=5.0),
            _iteration_record(is_dummy=True, num_generation_tokens=99, elapsed_ms=1.0),
        ]
    )
    snapshot = translate.engine_snapshot(stats, _iteration_stats(), ts=1.0)
    assert snapshot.iterations == 2
    assert snapshot.num_prefill_tokens == 512
    assert snapshot.num_decode_tokens == 6
    assert snapshot.iteration_time_s == pytest.approx(0.015)
    assert snapshot.iteration_time_source == "gpu"


def test_engine_snapshot_partial_timing_reports_no_time():
    stats = _scheduler_stats(
        iteration_records=[
            _iteration_record(elapsed_ms=10.0),
            _iteration_record(elapsed_ms=None),
        ]
    )
    snapshot = translate.engine_snapshot(stats, _iteration_stats(), ts=1.0)
    assert snapshot.iterations == 2
    assert snapshot.iteration_time_s is None
    assert snapshot.iteration_time_source is None


def test_engine_snapshot_only_dummy_records_reports_nothing():
    stats = _scheduler_stats(iteration_records=[_iteration_record(is_dummy=True)])
    snapshot = translate.engine_snapshot(stats, _iteration_stats(), ts=1.0)
    assert snapshot.iterations is None


def test_engine_snapshot_prefers_scheduler_preemption_count():
    # Post-0.34 vLLM moves num_preempted_reqs onto SchedulerStats.
    stats = _scheduler_stats(num_preempted_reqs=5)
    iteration = SimpleNamespace(
        **{
            k: v
            for k, v in vars(_iteration_stats()).items()
            if k != "num_preempted_reqs"
        }
    )
    snapshot = translate.engine_snapshot(stats, iteration, ts=1.0)
    assert snapshot.num_preempted_reqs == 5


def test_request_finished_num_preemptions():
    with_count = translate.request_finished_events(
        _iteration_stats(finished_requests=[_finished_stat(num_preemptions=2)]),
        ts=1.0,
    )
    assert with_count[0].num_preemptions == 2
    without = translate.request_finished_events(
        _iteration_stats(finished_requests=[_finished_stat()]), ts=1.0
    )
    assert without[0].num_preemptions is None
