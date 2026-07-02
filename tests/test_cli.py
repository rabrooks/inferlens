"""Tests for the inferlens CLI."""

from inferlens.cli import main
from inferlens.schema import EngineSnapshot, TraceMeta
from inferlens.trace_io import TraceWriter


def _write_sample_trace(path):
    with TraceWriter(path) as writer:
        writer.write(
            TraceMeta(
                engine="vllm",
                engine_version="0.11.0",
                model="test-model",
                wall_time_unix=0.0,
                monotonic_time=0.0,
            )
        )
        writer.write(
            EngineSnapshot(
                ts=1.0, num_running_reqs=1, num_waiting_reqs=0, kv_cache_usage=0.1
            )
        )
        writer.write(
            EngineSnapshot(
                ts=2.0, num_running_reqs=2, num_waiting_reqs=1, kv_cache_usage=0.2
            )
        )


def test_info(tmp_path, capsys):
    trace = tmp_path / "sample.ilens"
    _write_sample_trace(trace)

    assert main(["info", str(trace)]) == 0
    out = capsys.readouterr().out
    assert "vllm" in out
    assert "engine_snapshot" in out
    assert "span:    1.000s" in out


def test_info_empty_trace(tmp_path, capsys):
    trace = tmp_path / "empty.ilens"
    trace.touch()
    assert main(["info", str(trace)]) == 1
    assert "empty trace" in capsys.readouterr().err


def test_record_and_view_are_stubs(capsys):
    assert main(["record"]) == 2
    assert main(["view"]) == 2
    assert "not implemented" in capsys.readouterr().err
