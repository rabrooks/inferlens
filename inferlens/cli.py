"""The ``inferlens`` command-line interface."""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from inferlens import __version__
from inferlens.schema import TraceMeta
from inferlens.trace_io import read_trace


def _cmd_info(args: argparse.Namespace) -> int:
    counts: Counter[str] = Counter()
    meta: TraceMeta | None = None
    first_ts: float | None = None
    last_ts: float | None = None
    try:
        for event in read_trace(args.trace):
            counts[event.KIND] += 1
            if isinstance(event, TraceMeta):
                meta = event
            else:
                ts = event.ts
                first_ts = ts if first_ts is None else min(first_ts, ts)
                last_ts = ts if last_ts is None else max(last_ts, ts)
    except OSError as exc:
        # Covers the missing/unreadable/not-actually-gzip file cases with
        # one friendly line instead of a traceback.
        print(f"error: cannot read {args.trace}: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        # E.g. an unsupported schema major version, or a binary file that
        # isn't valid UTF-8.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not counts:
        print(f"{args.trace}: empty trace", file=sys.stderr)
        return 1

    if meta is not None:
        print(f"engine:  {meta.engine} {meta.engine_version}")
        print(f"model:   {meta.model}")
        print(f"schema:  {meta.schema_version}")
    if first_ts is not None and last_ts is not None:
        print(f"span:    {last_ts - first_ts:.3f}s")
    print("events:")
    for kind, count in sorted(counts.items()):
        print(f"  {kind:20s} {count}")
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    # argparse.REMAINDER keeps the leading "--" separator in the list.
    command = list(args.engine_command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print(
            "error: no command to record; usage: "
            "inferlens record -o trace.ilens.gz -- vllm serve <model> ...",
            file=sys.stderr,
        )
        return 2
    from inferlens.record import run_record

    return run_record(
        args.output,
        command,
        kv_events=not args.no_kv_events,
        keep_parts=args.keep_parts,
    )


def _cmd_not_implemented(command: str) -> int:
    print(f"inferlens {command} is not implemented yet", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="inferlens",
        description="Record and explore LLM inference engine traces.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_record = sub.add_parser(
        "record",
        help="run an engine-serving command with trace collectors attached",
        description=(
            "Wrap an engine-serving command (e.g. `vllm serve <model>`) so its "
            "scheduler stats and KV-cache events are recorded; when the engine "
            "exits (Ctrl-C / SIGTERM), the per-source streams are merged into "
            "one trace file."
        ),
    )
    p_record.add_argument(
        "-o", "--output", required=True, help="trace file to write (.ilens[.gz])"
    )
    p_record.add_argument(
        "--no-kv-events",
        action="store_true",
        help="do not enable/subscribe to the engine's KV-cache event stream",
    )
    p_record.add_argument(
        "--keep-parts",
        action="store_true",
        help="keep the per-source part files next to the output (debugging)",
    )
    # dest is "engine_command", not "command": the subparser already claims
    # args.command for the subcommand name.
    p_record.add_argument(
        "engine_command",
        nargs=argparse.REMAINDER,
        metavar="-- command ...",
        help="the engine-serving command to run",
    )

    p_view = sub.add_parser("view", help="open a trace in the local viewer")
    p_view.add_argument("trace", nargs="?", help="trace file to open")

    p_info = sub.add_parser("info", help="summarize a trace file")
    p_info.add_argument("trace", help="trace file to summarize")

    args = parser.parse_args(argv)
    if args.command == "info":
        return _cmd_info(args)
    if args.command == "record":
        return _cmd_record(args)
    return _cmd_not_implemented(args.command)


if __name__ == "__main__":
    sys.exit(main())
