"""Trace analysis: incident windows, latency attribution, and diagnosis.

Answers "why was it slow?" from a trace alone, without a UI. Shared by
``inferlens explain`` and (later) trace diffing and planner calibration,
so everything here is plain data in, plain data out — stdlib only.

What the analysis can and cannot claim is bounded by what engines expose
(``docs/upstream-gaps.md``): per-request phase times are taken *as
reported* and do not tile end-to-end latency (the remainder is reported
as ``unattributed``, never redistributed); there is no per-step batch
composition, so a request is linked to an incident by *time overlap*, not
by proof that it ran in the affected steps. Diagnoses are heuristics that
point at what to look at — they never tune anything.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

from inferlens.placement import Clock, split_meta
from inferlens.schema import (
    CollectorGap,
    EngineSnapshot,
    KVBlockRemoved,
    RequestFinished,
    TraceEvent,
)

DEFAULT_BIN_S = 1.0
# KV usage at or above this fraction counts as saturated. Not 1.0: vLLM's
# usage float rarely reads exactly full while the allocator is starving.
KV_SATURATED = 0.95
# Calm gaps of up to this many bins inside an incident are bridged, so one
# storm with a momentary lull is reported once, not as several incidents.
MERGE_GAP_BINS = 2
# Below this many calm requests, "vs calm" comparisons are noise.
MIN_CALM_REQUESTS = 5
# Calm requests often queue for microseconds; a ratio against that is
# meaningless (millions of x), so below this floor only absolutes are shown.
MIN_RATIO_BASELINE_S = 0.01


@dataclass(slots=True)
class Bin:
    """Engine state aggregated over one time bin (``t`` = bin start, s)."""

    t: float
    kv_usage_max: float = 0.0
    running_max: int = 0
    waiting_max: int = 0
    preemptions: int = 0
    evicted_blocks: int = 0
    snapshots: int = 0

    @property
    def kv_saturated(self) -> bool:
        """Whether KV usage reached the saturation threshold."""
        return self.kv_usage_max >= KV_SATURATED

    @property
    def queue_backed_up(self) -> bool:
        """Whether the waiting queue was at least as long as the batch."""
        return self.waiting_max >= max(2, self.running_max)

    @property
    def stressed(self) -> bool:
        """Whether this bin shows any incident signal."""
        return self.kv_saturated or self.preemptions > 0 or self.queue_backed_up


@dataclass(slots=True)
class LatencyStats:
    """Distribution summary of end-to-end latency (seconds)."""

    count: int
    p50_s: float | None
    p95_s: float | None
    max_s: float | None

    @classmethod
    def of(cls, values: list[float]) -> LatencyStats:
        """Summarize ``values`` (empty gives ``None`` statistics)."""
        if not values:
            return cls(0, None, None, None)
        ordered = sorted(values)
        return cls(
            len(ordered),
            _percentile(ordered, 50),
            _percentile(ordered, 95),
            ordered[-1],
        )


@dataclass(slots=True)
class Incident:
    """A contiguous window of engine stress, with its impact on requests.

    ``start_s``/``end_s`` are seconds since trace start. ``kind`` is the
    heuristic classification: ``kv_bound`` (KV cache saturated or requests
    preempted — capacity ran out), or ``queue_bound`` (requests queued
    behind a full batch without KV pressure — compute or admission limits).
    """

    id: int
    start_s: float
    end_s: float
    kind: str
    kv_usage_max: float
    preemptions: int
    evicted_blocks: int
    waiting_max: int
    affected: LatencyStats
    slowdown_p95: float | None = None

    @property
    def duration_s(self) -> float:
        """Window length in seconds."""
        return self.end_s - self.start_s


@dataclass(slots=True)
class RequestAttribution:
    """Where one request's end-to-end latency went (seconds, as reported).

    ``unattributed_s`` is e2e minus the reported phases: frontend time and
    re-queueing the engine does not attribute to a phase. ``queued_vs_calm``
    and ``tpot_vs_calm`` compare this request's queue time and per-token
    decode time with the calm-period medians (``None`` without a usable
    calm baseline, or when calm requests barely queued at all).
    ``incidents`` lists overlapping incident ids — overlap in time, not
    proof the request ran in the affected steps.
    """

    request_id: str
    arrival_s: float
    finish_s: float
    e2e_s: float
    queued_s: float
    prefill_s: float
    decode_s: float
    unattributed_s: float
    dominant: str
    num_preemptions: int | None
    prompt_tokens: int
    cached_tokens: int
    generation_tokens: int
    finish_reason: str
    incidents: list[int] = field(default_factory=list)
    queued_vs_calm: float | None = None
    tpot_vs_calm: float | None = None


@dataclass(slots=True)
class Diagnosis:
    """A heuristic finding plus what to look at — never an automatic change."""

    kind: str
    summary: str
    suggestions: list[str]


@dataclass(slots=True)
class Analysis:
    """Everything ``inferlens explain`` reports about one trace."""

    engine: str | None
    engine_version: str | None
    model: str | None
    span_s: float
    bin_s: float
    requests: LatencyStats
    calm: LatencyStats
    affected: LatencyStats
    incidents: list[Incident]
    attributions: list[RequestAttribution]
    diagnoses: list[Diagnosis]
    gaps: int
    caveats: list[str]
    calm_queued_p50_s: float | None = None
    calm_tpot_p50_s: float | None = None
    bins: list[Bin] = field(default_factory=list, repr=False)

    def worst(self, n: int) -> list[RequestAttribution]:
        """The ``n`` slowest requests by end-to-end latency."""
        return sorted(self.attributions, key=lambda a: a.e2e_s, reverse=True)[:n]

    def request(self, request_id: str) -> RequestAttribution | None:
        """Look up one request's attribution by id."""
        return next((a for a in self.attributions if a.request_id == request_id), None)


def analyze(events: Iterable[TraceEvent], bin_s: float = DEFAULT_BIN_S) -> Analysis:
    """Analyze a (single or merged) trace.

    Args:
        events: Events as yielded by :func:`inferlens.trace_io.read_trace`.
        bin_s: Width of the engine-state time bins, in seconds. Incident
            boundaries are quantized to this.

    Returns:
        The full :class:`Analysis`.

    Raises:
        ValueError: If ``bin_s`` is not positive.
    """
    if not bin_s > 0:
        raise ValueError(f"bin_s must be positive, got {bin_s!r}")
    meta, body = split_meta(events)
    clock = Clock.from_meta(meta)
    placed = [(clock.place(e), e) for e in body]
    if not placed:
        return _empty(meta, bin_s)

    # t=0 is the first recorded event; requests' arrivals can precede it,
    # which is fine (negative arrival_s).
    t0 = min(t for t, _ in placed)
    span = max(t for t, _ in placed) - t0
    bins = _bins(placed, t0, bin_s)
    windows = _windows(bins, bin_s)

    requests = [
        (t - t0, e)
        for t, e in placed
        if isinstance(e, RequestFinished) and e.e2e_latency_s > 0
    ]
    overlaps: list[list[int]] = []
    for finish, r in requests:
        arrival = finish - r.e2e_latency_s
        overlaps.append(
            [
                i + 1
                for i, (ws, we, _) in enumerate(windows)
                if arrival < we and finish > ws
            ]
        )
    calm_reqs = [r for (_, r), ov in zip(requests, overlaps, strict=True) if not ov]
    affected_reqs = [r for (_, r), ov in zip(requests, overlaps, strict=True) if ov]
    calm = LatencyStats.of([r.e2e_latency_s for r in calm_reqs])
    have_calm = calm.count >= MIN_CALM_REQUESTS

    incidents = []
    for i, (ws, we, wbins) in enumerate(windows):
        hit = [r for (_, r), ov in zip(requests, overlaps, strict=True) if i + 1 in ov]
        affected = LatencyStats.of([r.e2e_latency_s for r in hit])
        kv_bound = any(b.kv_saturated or b.preemptions for b in wbins)
        incidents.append(
            Incident(
                id=i + 1,
                start_s=ws,
                end_s=we,
                kind="kv_bound" if kv_bound else "queue_bound",
                kv_usage_max=max(b.kv_usage_max for b in wbins),
                preemptions=sum(b.preemptions for b in wbins),
                evicted_blocks=sum(b.evicted_blocks for b in wbins),
                waiting_max=max(b.waiting_max for b in wbins),
                affected=affected,
                slowdown_p95=_ratio(affected.p95_s, calm.p95_s) if have_calm else None,
            )
        )

    calm_queued = _median([r.queued_time_s for r in calm_reqs]) if have_calm else None
    calm_tpot = (
        _median([t for r in calm_reqs if (t := _tpot(r)) is not None])
        if have_calm
        else None
    )
    attributions = [
        _attribute(finish, r, ov, calm_queued, calm_tpot)
        for (finish, r), ov in zip(requests, overlaps, strict=True)
    ]

    gaps = sum(isinstance(e, CollectorGap) for _, e in placed)
    return Analysis(
        engine=meta.engine if meta else None,
        engine_version=meta.engine_version if meta else None,
        model=meta.model if meta else None,
        span_s=span,
        bin_s=bin_s,
        requests=LatencyStats.of([r.e2e_latency_s for _, r in requests]),
        calm=calm,
        affected=LatencyStats.of([r.e2e_latency_s for r in affected_reqs]),
        incidents=incidents,
        attributions=attributions,
        diagnoses=_diagnose(incidents, attributions, meta.engine if meta else None),
        gaps=gaps,
        caveats=_caveats(gaps, have_calm, attributions),
        calm_queued_p50_s=calm_queued,
        calm_tpot_p50_s=calm_tpot,
        bins=bins,
    )


def _empty(meta: object, bin_s: float) -> Analysis:
    none = LatencyStats.of([])
    return Analysis(
        engine=getattr(meta, "engine", None),
        engine_version=getattr(meta, "engine_version", None),
        model=getattr(meta, "model", None),
        span_s=0.0,
        bin_s=bin_s,
        requests=none,
        calm=none,
        affected=none,
        incidents=[],
        attributions=[],
        diagnoses=[],
        gaps=0,
        caveats=["trace has no events"],
    )


def _bins(placed: list[tuple[float, TraceEvent]], t0: float, bin_s: float) -> list[Bin]:
    n = max(1, math.floor((max(t for t, _ in placed) - t0) / bin_s) + 1)
    bins = [Bin(t=i * bin_s) for i in range(n)]
    for t, e in placed:
        b = bins[min(n - 1, max(0, math.floor((t - t0) / bin_s)))]
        if isinstance(e, EngineSnapshot):
            b.snapshots += 1
            b.kv_usage_max = max(b.kv_usage_max, e.kv_cache_usage)
            b.running_max = max(b.running_max, e.num_running_reqs)
            b.waiting_max = max(b.waiting_max, e.num_waiting_reqs)
            b.preemptions += e.num_preempted_reqs
        elif isinstance(e, KVBlockRemoved):
            b.evicted_blocks += len(e.block_hashes)
    return bins


def _windows(bins: list[Bin], bin_s: float) -> list[tuple[float, float, list[Bin]]]:
    """Group stressed bins into windows, bridging short calm gaps."""
    spans: list[list[int]] = []  # [first, last] bin index per window
    for i, b in enumerate(bins):
        if not b.stressed:
            continue
        if spans and i - spans[-1][1] - 1 <= MERGE_GAP_BINS:
            spans[-1][1] = i  # bridge the lull: those calm bins join the window
        else:
            spans.append([i, i])
    return [
        (bins[first].t, bins[last].t + bin_s, bins[first : last + 1])
        for first, last in spans
    ]


def _attribute(
    finish: float,
    r: RequestFinished,
    incidents: list[int],
    calm_queued: float | None,
    calm_tpot: float | None,
) -> RequestAttribution:
    unattributed = r.e2e_latency_s - (
        r.queued_time_s + r.prefill_time_s + r.decode_time_s
    )
    parts = {
        "queued": r.queued_time_s,
        "prefill": r.prefill_time_s,
        "decode": r.decode_time_s,
        "unattributed": unattributed,
    }
    tpot = _tpot(r)
    return RequestAttribution(
        request_id=r.request_id,
        arrival_s=finish - r.e2e_latency_s,
        finish_s=finish,
        e2e_s=r.e2e_latency_s,
        queued_s=r.queued_time_s,
        prefill_s=r.prefill_time_s,
        decode_s=r.decode_time_s,
        unattributed_s=unattributed,
        dominant=max(parts, key=lambda k: parts[k]),
        num_preemptions=r.num_preemptions,
        prompt_tokens=r.num_prompt_tokens,
        cached_tokens=r.num_cached_tokens,
        generation_tokens=r.num_generation_tokens,
        finish_reason=r.finish_reason,
        incidents=incidents,
        queued_vs_calm=_ratio(r.queued_time_s, calm_queued, MIN_RATIO_BASELINE_S),
        tpot_vs_calm=_ratio(tpot, calm_tpot),
    )


def _diagnose(
    incidents: list[Incident],
    attributions: list[RequestAttribution],
    engine: str | None,
) -> list[Diagnosis]:
    vllm = engine == "vllm"
    out = []
    kv = [i for i in incidents if i.kind == "kv_bound"]
    if kv:
        preemptions = sum(i.preemptions for i in kv)
        seconds = sum(i.duration_s for i in kv)
        summary = (
            f"KV-cache bound: {len(kv)} window(s), {seconds:.0f} s total, "
            f"{preemptions} preemptions. Requests were evicted from the batch "
            "and recomputed when the KV cache ran out."
        )
        suggestions = [
            "Give the KV cache more room"
            + (
                " (--gpu-memory-utilization, a smaller --max-model-len, or "
                "--kv-cache-dtype fp8)."
                if vllm
                else "."
            ),
            "Cap concurrency so admitted requests fit"
            + (" (--max-num-seqs)." if vllm else "."),
            "If prompts share prefixes, route them to the same replica so "
            "prefix-cache hits free capacity.",
        ]
        out.append(Diagnosis("kv_bound", summary, suggestions))
    queue = [i for i in incidents if i.kind == "queue_bound"]
    if queue:
        seconds = sum(i.duration_s for i in queue)
        peak = max(i.waiting_max for i in queue)
        summary = (
            f"Queue bound without KV pressure: {len(queue)} window(s), "
            f"{seconds:.0f} s total, queue peak {peak}. The batch was full "
            "while KV capacity was still free — compute or admission limits."
        )
        suggestions = [
            "Check the per-step token budget"
            + (" (--max-num-batched-tokens) and --max-num-seqs." if vllm else "."),
            "If the engine is compute-saturated, add replicas or a faster GPU.",
        ]
        out.append(Diagnosis("queue_bound", summary, suggestions))
    if not incidents and attributions:
        out.append(
            Diagnosis(
                "calm",
                "No incident windows: no KV saturation, preemptions, or queue "
                "backlog. Latency is set by the workload itself.",
                [],
            )
        )
    return out


def _caveats(
    gaps: int, have_calm: bool, attributions: list[RequestAttribution]
) -> list[str]:
    out = [
        "Requests are linked to incidents by time overlap; the engine does not "
        "expose which requests ran in which step.",
    ]
    if gaps:
        out.append(
            f"The trace has {gaps} collector gap(s): some events are missing, "
            "so counts in affected windows may be low."
        )
    if not have_calm:
        out.append(
            f"Fewer than {MIN_CALM_REQUESTS} requests ran outside incidents, so "
            "there is no calm baseline to compare against."
        )
    if attributions and all(a.num_preemptions is None for a in attributions):
        out.append(
            "Per-request preemption counts were not recorded (engine older "
            "than vLLM 0.34); preemptions are known only per step."
        )
    return out


def _tpot(r: RequestFinished) -> float | None:
    """Per-output-token decode time; the first token comes from prefill."""
    if r.num_generation_tokens < 2:
        return None
    return r.decode_time_s / (r.num_generation_tokens - 1)


def _median(values: list[float]) -> float | None:
    return _percentile(sorted(values), 50) if values else None


def _percentile(ordered: list[float], pct: float) -> float:
    """Linear-interpolated percentile of an already sorted, non-empty list."""
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * pct / 100
    lo = math.floor(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _ratio(
    value: float | None, baseline: float | None, floor: float = 0.0
) -> float | None:
    if value is None or baseline is None or baseline <= floor or baseline <= 0:
        return None
    return value / baseline
