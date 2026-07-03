"""Implementation of ``inferlens record``.

Wraps an engine-serving command (today: ``vllm serve``) so that a recording
needs no manual setup: the wrapper points the in-process stat-logger plugin
at a trace file via ``INFERLENS_TRACE_PATH``, enables and subscribes to the
engine's KV-event stream, and — because the two collectors run in different
OS processes and therefore write separate files (see "Multi-source
recording" in ``docs/TRACE_SPEC.md``) — merges the per-source parts into
the single output trace when the engine exits.

Only the standard library is imported here; the KV-event subscriber (which
needs pyzmq/msgspec, the ``vllm`` extra) is imported lazily and recording
degrades to stats-only if it is unavailable.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from inferlens.schema import TraceMeta
from inferlens.trace_io import BufferedTraceWriter, merge_traces, read_trace

if TYPE_CHECKING:
    from inferlens.collectors.vllm.kv_events import KVEventSubscriber

_Subscription = tuple["KVEventSubscriber", BufferedTraceWriter]

# Duplicated from inferlens.collectors.vllm.stat_logger rather than imported:
# that module lives behind the optional collector package and this one must
# stay stdlib-only.
TRACE_PATH_ENV = "INFERLENS_TRACE_PATH"

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class KVEventsPlan:
    """Where the wrapper's KV-event subscriber should connect."""

    endpoint: str
    replay_endpoint: str | None
    topic: str = ""


def plan_kv_events(command: list[str]) -> tuple[list[str], KVEventsPlan | None]:
    """Decide how KV events flow for this command.

    Returns the (possibly extended) command plus the subscriber plan, or
    ``None`` when there is nothing to subscribe to:

    - The command already carries ``--kv-events-config``: respect it and
      subscribe to its endpoints (wildcard binds become 127.0.0.1).
    - The command is ``vllm serve`` without one: inject a config on free
      localhost ports, with a replay endpoint so gaps are recoverable.
    - Anything else: no injection — we cannot know the command accepts the
      flag — and no subscription.
    """
    config = _existing_kv_config(command)
    if config is not None:
        if not config.get("enable_kv_cache_events") or config.get("publisher") not in (
            None,
            "zmq",
        ):
            return command, None
        replay = config.get("replay_endpoint")
        return command, KVEventsPlan(
            endpoint=_connectable(config.get("endpoint", "tcp://*:5557")),
            replay_endpoint=_connectable(replay) if replay else None,
            topic=config.get("topic", ""),
        )
    if not _is_vllm_serve(command):
        return command, None
    port, replay_port = _free_ports(2)
    # The PUB endpoint must be the wildcard form: vLLM's publisher only
    # *binds* endpoints containing a wildcard (or ipc/inproc) and *connects*
    # otherwise (ZmqEventPublisher._socket_setup) — a concrete address here
    # would leave both sides connecting and no one bound, so no events flow.
    # The replay ROUTER, by contrast, always binds, so it can stay on
    # loopback only.
    injected = {
        "enable_kv_cache_events": True,
        "publisher": "zmq",
        "endpoint": f"tcp://*:{port}",
        "replay_endpoint": f"tcp://127.0.0.1:{replay_port}",
    }
    command = [*command, "--kv-events-config", json.dumps(injected)]
    return command, KVEventsPlan(
        endpoint=f"tcp://127.0.0.1:{port}",
        replay_endpoint=f"tcp://127.0.0.1:{replay_port}",
    )


def _existing_kv_config(command: list[str]) -> dict[str, Any] | None:
    for i, arg in enumerate(command):
        if arg == "--kv-events-config" and i + 1 < len(command):
            value = command[i + 1]
        elif arg.startswith("--kv-events-config="):
            value = arg.split("=", 1)[1]
        else:
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            _logger.warning("could not parse --kv-events-config; not subscribing")
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return None


def _is_vllm_serve(command: list[str]) -> bool:
    return (
        len(command) >= 2 and Path(command[0]).name == "vllm" and command[1] == "serve"
    )


def _connectable(endpoint: str) -> str:
    """Turn a bind endpoint into one a client can connect to."""
    for wildcard in ("//*:", "//0.0.0.0:"):
        if wildcard in endpoint:
            return endpoint.replace(wildcard, "//127.0.0.1:")
    return endpoint


def _free_ports(n: int) -> list[int]:
    # All sockets stay open until every port is picked, so one bind cannot
    # hand back a port a previous one just released.
    socks = [socket.socket() for _ in range(n)]
    try:
        for sock in socks:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in socks]
    finally:
        for sock in socks:
            sock.close()


def _start_subscriber(plan: KVEventsPlan, kv_path: Path) -> _Subscription | None:
    """Start the KV subscriber, or return ``None`` if its deps are missing."""
    try:
        from inferlens.collectors.vllm.kv_events import KVEventSubscriber
    except ImportError:
        print(
            "inferlens record: pyzmq/msgspec not installed "
            "(pip install 'inferlens[vllm]'); recording without KV events",
            file=sys.stderr,
        )
        return None
    writer = BufferedTraceWriter(kv_path)
    subscriber = KVEventSubscriber(
        plan.endpoint,
        writer,
        replay_endpoint=plan.replay_endpoint,
        topic=plan.topic,
    )
    subscriber.start()
    return subscriber, writer


def _mergeable(path: Path) -> bool:
    """True if the part exists and carries the anchor merging requires."""
    if not path.exists():
        return False
    try:
        return any(isinstance(event, TraceMeta) for event in read_trace(path))
    except (OSError, ValueError):
        return False


def run_record(
    output: str | Path,
    command: list[str],
    kv_events: bool = True,
    keep_parts: bool = False,
) -> int:
    """Run ``command`` with collectors attached; merge parts into ``output``.

    Returns the child's exit code (0 if it exited via SIGINT/SIGTERM — that
    is how a recording is normally stopped), or 1 if no trace data was
    produced at all.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    parts_dir = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.parts-", dir=output.parent)
    )
    stats_path = parts_dir / "stats.ilens"
    kv_path = parts_dir / "kv.ilens"

    subscription = None
    if kv_events:
        command, plan = plan_kv_events(command)
        if plan is not None:
            subscription = _start_subscriber(plan, kv_path)

    try:
        proc = subprocess.Popen(
            command, env={**os.environ, TRACE_PATH_ENV: str(stats_path)}
        )
    except FileNotFoundError:
        print(f"inferlens record: command not found: {command[0]}", file=sys.stderr)
        _stop_subscription(subscription)
        shutil.rmtree(parts_dir, ignore_errors=True)
        return 127

    # Ctrl-C: the terminal delivers SIGINT to the whole foreground process
    # group, so the engine already receives it — the wrapper must only stay
    # alive to merge. Forwarding it too would look like a second Ctrl-C
    # (vLLM's force-shutdown path). SIGTERM is delivered to the wrapper
    # alone, so that one is forwarded.
    previous = {
        signal.SIGINT: signal.signal(signal.SIGINT, signal.SIG_IGN),
        signal.SIGTERM: signal.signal(
            signal.SIGTERM, lambda signum, frame: _send_signal(proc, signum)
        ),
    }
    try:
        returncode = proc.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        _stop_subscription(subscription)

    parts = [path for path in (stats_path, kv_path) if _mergeable(path)]
    if not parts:
        print(
            f"inferlens record: no trace data was produced (is inferlens "
            f"installed in the engine's environment?); parts kept in {parts_dir}",
            file=sys.stderr,
        )
        return returncode or 1
    merge_traces(parts, output)
    if keep_parts:
        print(f"per-source parts kept in {parts_dir}", file=sys.stderr)
    else:
        shutil.rmtree(parts_dir, ignore_errors=True)

    counts = Counter(event.KIND for event in read_trace(output))
    total = sum(counts.values())
    kinds = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))
    print(f"wrote {output}: {total} events from {len(parts)} source(s) ({kinds})")
    # A recording is normally stopped by interrupting/terminating the
    # engine; that is success, not an error, for the *recorder*.
    if returncode < 0 and -returncode in (signal.SIGINT, signal.SIGTERM):
        return 0
    return returncode


def _send_signal(proc: subprocess.Popen[bytes], signum: int) -> None:
    # Racing the child's exit is fine: wait() returns shortly either way.
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(signum)


def _stop_subscription(subscription: _Subscription | None) -> None:
    if subscription is None:
        return
    subscriber, writer = subscription
    subscriber.stop()
    writer.close()
