"""Tests for `inferlens record` (KV-config planning + wrapper lifecycle)."""

import json
import sys

from inferlens.cli import main
from inferlens.record import KVEventsPlan, _connectable, plan_kv_events, run_record
from inferlens.schema import EngineSnapshot, TraceMeta
from inferlens.trace_io import read_trace

# --- planning (pure) ---------------------------------------------------------


def test_plan_injects_config_into_vllm_serve():
    command, plan = plan_kv_events(["vllm", "serve", "some/model"])

    assert command[:3] == ["vllm", "serve", "some/model"]
    assert command[3] == "--kv-events-config"
    config = json.loads(command[4])
    assert config["enable_kv_cache_events"] is True
    assert config["publisher"] == "zmq"
    # The injected PUB endpoint must be the wildcard *bind* form — vLLM
    # connects rather than binds concrete addresses — while the subscriber
    # connects to its loopback equivalent.
    assert config["endpoint"].startswith("tcp://*:")
    assert plan is not None
    assert plan.endpoint == config["endpoint"].replace("tcp://*:", "tcp://127.0.0.1:")
    assert plan.replay_endpoint == config["replay_endpoint"]
    assert config["replay_endpoint"].startswith("tcp://127.0.0.1:")
    assert plan.endpoint != plan.replay_endpoint


def test_plan_injects_for_absolute_vllm_path():
    command, plan = plan_kv_events(["/some/venv/bin/vllm", "serve", "m"])
    assert plan is not None
    assert "--kv-events-config" in command


def test_plan_respects_existing_config():
    config = json.dumps(
        {
            "enable_kv_cache_events": True,
            "publisher": "zmq",
            "endpoint": "tcp://*:5557",
            "replay_endpoint": "tcp://0.0.0.0:5558",
            "topic": "kv",
        }
    )
    original = ["vllm", "serve", "m", "--kv-events-config", config]

    command, plan = plan_kv_events(list(original))

    assert command == original  # nothing injected
    assert plan == KVEventsPlan(
        endpoint="tcp://127.0.0.1:5557",
        replay_endpoint="tcp://127.0.0.1:5558",
        topic="kv",
    )


def test_plan_respects_equals_form_config():
    arg = "--kv-events-config=" + json.dumps(
        {"enable_kv_cache_events": True, "endpoint": "tcp://127.0.0.1:7777"}
    )
    command, plan = plan_kv_events(["vllm", "serve", "m", arg])
    assert command == ["vllm", "serve", "m", arg]
    assert plan is not None
    assert plan.endpoint == "tcp://127.0.0.1:7777"
    assert plan.replay_endpoint is None


def test_plan_no_subscription_when_events_disabled_in_config():
    config = json.dumps({"enable_kv_cache_events": False})
    _, plan = plan_kv_events(["vllm", "serve", "m", "--kv-events-config", config])
    assert plan is None


def test_plan_leaves_non_vllm_commands_alone():
    original = [sys.executable, "-m", "myengine"]
    command, plan = plan_kv_events(list(original))
    assert command == original
    assert plan is None


def test_connectable_rewrites_wildcard_binds():
    assert _connectable("tcp://*:5557") == "tcp://127.0.0.1:5557"
    assert _connectable("tcp://0.0.0.0:5557") == "tcp://127.0.0.1:5557"
    assert _connectable("tcp://10.0.0.7:5557") == "tcp://10.0.0.7:5557"


# --- wrapper lifecycle -------------------------------------------------------

# A stand-in for `vllm serve` with the stat-logger plugin active: writes a
# small stats stream to $INFERLENS_TRACE_PATH and exits.
_FAKE_ENGINE = """\
import os
from inferlens.schema import EngineSnapshot, TraceMeta
from inferlens.trace_io import TraceWriter

with TraceWriter(os.environ["INFERLENS_TRACE_PATH"]) as writer:
    writer.write(
        TraceMeta(
            engine="vllm",
            engine_version="1.0",
            model="fake",
            wall_time_unix=1000.0,
            monotonic_time=0.0,
        )
    )
    writer.write(
        EngineSnapshot(
            ts=1.0, num_running_reqs=1, num_waiting_reqs=0, kv_cache_usage=0.1
        )
    )
"""


def test_run_record_merges_stats_into_output(tmp_path):
    output = tmp_path / "trace.ilens.gz"

    rc = run_record(output, [sys.executable, "-c", _FAKE_ENGINE])

    assert rc == 0
    meta, *events = list(read_trace(output))
    assert isinstance(meta, TraceMeta)
    assert meta.model == "fake"
    assert len(meta.extra["merged_sources"]) == 1  # stats only: no KV plan
    assert [type(e) for e in events] == [EngineSnapshot]
    # Part files are cleaned up unless asked to keep them.
    assert list(tmp_path.iterdir()) == [output]


def test_run_record_keep_parts(tmp_path):
    output = tmp_path / "trace.ilens"
    rc = run_record(
        output, [sys.executable, "-c", _FAKE_ENGINE], kv_events=False, keep_parts=True
    )
    assert rc == 0
    [parts_dir] = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert (parts_dir / "stats.ilens").exists()


def test_run_record_propagates_child_exit_code(tmp_path):
    rc = run_record(
        tmp_path / "trace.ilens",
        [sys.executable, "-c", _FAKE_ENGINE + "raise SystemExit(3)"],
        kv_events=False,
    )
    assert rc == 3
    # The trace that was collected is still delivered.
    assert (tmp_path / "trace.ilens").exists()


def test_run_record_command_not_found(tmp_path, capsys):
    rc = run_record(tmp_path / "trace.ilens", ["definitely-not-a-command-xyz"])
    assert rc == 127
    assert "command not found" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []  # no output, no leftover parts


def test_run_record_reports_when_no_data_produced(tmp_path, capsys):
    output = tmp_path / "trace.ilens"
    rc = run_record(output, [sys.executable, "-c", "pass"], kv_events=False)
    assert rc == 1
    assert "no trace data" in capsys.readouterr().err
    assert not output.exists()


def test_cli_record_requires_a_command(tmp_path, capsys):
    assert main(["record", "-o", str(tmp_path / "t.ilens")]) == 2
    assert "no command to record" in capsys.readouterr().err


def test_cli_record_end_to_end(tmp_path, capsys):
    output = tmp_path / "trace.ilens"
    rc = main(["record", "-o", str(output), "--", sys.executable, "-c", _FAKE_ENGINE])
    assert rc == 0
    assert "wrote" in capsys.readouterr().out
    assert output.exists()
