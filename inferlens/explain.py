"""Render an :class:`~inferlens.analysis.Analysis` for ``inferlens explain``.

Two outputs: a terminal report for people, and JSON for tools and agents.
The JSON shape is documented in ``docs/explain.md``; it is versioned by
``explain_version`` independently of the trace schema.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from inferlens.analysis import Analysis, LatencyStats, RequestAttribution

EXPLAIN_VERSION = "0.1"


def to_json(analysis: Analysis, worst: int) -> dict[str, Any]:
    """Return the JSON-serializable report.

    Per-request attributions are limited to the ``worst`` slowest; the
    per-bin series is omitted (it is an input to the analysis, not a
    finding).
    """
    doc = dataclasses.asdict(analysis)
    doc.pop("bins")
    doc["attributions"] = [dataclasses.asdict(a) for a in analysis.worst(worst)]
    for incident, raw in zip(analysis.incidents, doc["incidents"], strict=True):
        raw["duration_s"] = incident.duration_s
    return {"explain_version": EXPLAIN_VERSION, **doc}


def render_text(analysis: Analysis, worst: int, request_id: str | None = None) -> str:
    """Return the human-readable report."""
    a = analysis
    lines: list[str] = []
    who = " ".join(x for x in (a.engine, a.engine_version) if x) or "unknown engine"
    lines.append(f"Trace: {who}, {a.model or 'unknown model'}, {a.span_s:.1f} s")
    lines.append(f"Requests: {a.requests.count}  {_stats(a.requests)}")
    if a.calm.count:
        lines.append(f"  calm      ({a.calm.count:>4})  {_stats(a.calm)}")
    if a.affected.count:
        lines.append(f"  in incidents ({a.affected.count})  {_stats(a.affected)}")

    if request_id is not None:
        found = a.request(request_id)
        lines.append("")
        if found is None:
            lines.append(f"Request {request_id!r} not found in this trace.")
        else:
            lines.extend(_request_detail(found, a))
        return "\n".join(lines) + "\n"

    lines.append("")
    if a.incidents:
        lines.append(f"Incidents ({len(a.incidents)}), {a.bin_s:g} s resolution:")
        for i in a.incidents:
            kind = "KV-bound" if i.kind == "kv_bound" else "queue-bound"
            slow = f", p95 {i.slowdown_p95:.1f}x calm" if i.slowdown_p95 else ""
            lines.append(
                f"  #{i.id}  t={i.start_s:.0f}-{i.end_s:.0f} s ({i.duration_s:.0f} s)  "
                f"{kind}: KV peak {i.kv_usage_max:.0%}, {i.preemptions} preemptions, "
                f"{i.evicted_blocks} blocks evicted, queue peak {i.waiting_max}"
            )
            lines.append(
                f"        {i.affected.count} requests overlapped: "
                f"{_stats(i.affected)}{slow}"
            )
    else:
        lines.append("Incidents: none")

    for d in a.diagnoses:
        lines.append("")
        lines.append(f"Diagnosis: {d.summary}")
        for s in d.suggestions:
            lines.append(f"  - {s}")

    slowest = a.worst(worst)
    if slowest:
        lines.append("")
        lines.append(f"Slowest {len(slowest)} requests (seconds; phases as reported):")
        lines.append(
            "  request                      e2e  queued prefill  decode  unattr  "
            "preempt  incidents  mostly"
        )
        for r in slowest:
            pre = "-" if r.num_preemptions is None else str(r.num_preemptions)
            inc = ",".join(f"#{i}" for i in r.incidents) or "-"
            lines.append(
                f"  {_short(r.request_id):26s} {r.e2e_s:6.2f}  {r.queued_s:6.2f}  "
                f"{r.prefill_s:6.2f}  {r.decode_s:6.2f}  {r.unattributed_s:6.2f}  "
                f"{pre:>7s}  {inc:9s}  {r.dominant}"
            )
        lines.append("  Use --request ID for one request's full breakdown.")

    if a.caveats:
        lines.append("")
        lines.append("Caveats:")
        lines.extend(f"  - {c}" for c in a.caveats)
    return "\n".join(lines) + "\n"


def _request_detail(r: RequestAttribution, a: Analysis) -> list[str]:
    lines = [
        f"Request {r.request_id}  ({r.finish_reason})",
        f"  arrived t={r.arrival_s:.2f} s, finished t={r.finish_s:.2f} s, "
        f"e2e {r.e2e_s:.3f} s",
        f"  tokens: {r.prompt_tokens} prompt ({r.cached_tokens} from prefix cache), "
        f"{r.generation_tokens} generated",
        "  where the time went (as reported):",
    ]
    for name, value in (
        ("queued", r.queued_s),
        ("prefill", r.prefill_s),
        ("decode", r.decode_s),
        ("unattributed", r.unattributed_s),
    ):
        share = value / r.e2e_s if r.e2e_s else 0.0
        lines.append(f"    {name:13s} {value:7.3f} s  {share:5.0%}")
    if r.num_preemptions is not None:
        lines.append(f"  preempted {r.num_preemptions} time(s)")
    if r.queued_vs_calm is not None:
        lines.append(f"  queue time {r.queued_vs_calm:.1f}x the calm median")
    elif a.calm_queued_p50_s is not None:
        lines.append(
            f"  queue time {r.queued_s:.3f} s vs a calm median of "
            f"{a.calm_queued_p50_s:.3f} s"
        )
    if r.tpot_vs_calm is not None:
        lines.append(f"  per-token decode time {r.tpot_vs_calm:.1f}x the calm median")
    for inc_id in r.incidents:
        inc = a.incidents[inc_id - 1]
        lines.append(
            f"  overlapped incident #{inc.id} (t={inc.start_s:.0f}-{inc.end_s:.0f} s, "
            f"KV peak {inc.kv_usage_max:.0%}, {inc.preemptions} preemptions)"
        )
    if not r.incidents:
        lines.append("  overlapped no incident")
    return lines


def _stats(s: LatencyStats) -> str:
    if not s.count or s.p50_s is None or s.p95_s is None or s.max_s is None:
        return "no latency data"
    return f"e2e p50 {s.p50_s:.2f} s, p95 {s.p95_s:.2f} s, max {s.max_s:.2f} s"


def _short(request_id: str) -> str:
    return (
        request_id
        if len(request_id) <= 26
        else request_id[:12] + "…" + request_id[-13:]
    )
