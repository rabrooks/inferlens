r"""Bursty workload generator for producing interesting traces.

Drives an OpenAI-compatible completions endpoint with a two-phase arrival
process — a calm Poisson trickle punctuated by heavy bursts — and a
long/short prompt mix. Long prompts share a common prefix (exercising the
prefix cache) but diverge afterwards, so under KV pressure the burst phases
force queue growth, preemptions, and evictions: exactly the incidents a
trace should make visible. Pair it with a small KV budget, e.g.::

    inferlens record -o trace.ilens.gz -- vllm serve \
        Qwen/Qwen2.5-1.5B-Instruct --gpu-memory-utilization 0.3
    python examples/workload.py --duration 120

Standard library only — no client dependencies.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

# ~10 tokens per sentence with common tokenizers; prompt lengths below are
# sentence counts, not exact token counts.
_SENTENCE = "The quick brown fox jumps over the lazy dog near the riverbank. "


class _Stats:
    """Thread-safe request outcome counters."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sent = 0
        self.ok = 0
        self.errors = 0
        self.latencies_s: list[float] = []

    def record(self, latency_s: float | None) -> None:
        with self.lock:
            if latency_s is None:
                self.errors += 1
            else:
                self.ok += 1
                self.latencies_s.append(latency_s)


def _default_model(base_url: str) -> str:
    with urllib.request.urlopen(f"{base_url}/v1/models", timeout=10) as resp:
        return json.load(resp)["data"][0]["id"]


def _build_prompt(rng: random.Random, args: argparse.Namespace, i: int) -> str:
    """Mix long shared-prefix prompts with short unique ones.

    The long prompts' shared prefix populates the prefix cache; the unique
    tail (seeded per request) keeps their KV blocks distinct beyond it, so
    enough of them in flight exhausts the cache and triggers preemption.
    """
    if rng.random() < args.long_frac:
        shared = _SENTENCE * args.long_prefix_sentences
        unique = f"Report {i}: " + " ".join(
            rng.choice(("alpha", "beta", "gamma", "delta", "epsilon"))
            for _ in range(args.long_tail_words)
        )
        return shared + unique
    return f"Question {i}: what follows the number {rng.randrange(10_000)}?"


def _send_one(args: argparse.Namespace, prompt: str, stats: _Stats) -> None:
    payload = json.dumps(
        {"model": args.model, "prompt": prompt, "max_tokens": args.max_tokens}
    ).encode()
    request = urllib.request.Request(
        f"{args.base_url}/v1/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as resp:
            json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        stats.record(None)
        return
    stats.record(time.monotonic() - start)


def _current_rate(args: argparse.Namespace, elapsed_s: float) -> float:
    """Two-phase arrival rate: calm, with periodic bursts."""
    in_burst = elapsed_s % args.burst_every < args.burst_len
    return args.burst_rps if in_burst else args.calm_rps


def run(args: argparse.Namespace) -> int:
    """Generate load until the duration elapses; return an exit code."""
    if args.model is None:
        args.model = _default_model(args.base_url)
        print(f"targeting model {args.model!r}")
    rng = random.Random(args.seed)
    stats = _Stats()
    workers: list[threading.Thread] = []
    start = time.monotonic()
    i = 0
    while (elapsed := time.monotonic() - start) < args.duration:
        # Poisson arrivals: exponential gaps at the phase's current rate.
        time.sleep(rng.expovariate(_current_rate(args, elapsed)))
        prompt = _build_prompt(rng, args, i)
        worker = threading.Thread(
            target=_send_one, args=(args, prompt, stats), daemon=True
        )
        worker.start()
        workers.append(worker)
        stats.sent += 1
        i += 1
        if i % 25 == 0:
            in_flight = stats.sent - stats.ok - stats.errors
            print(
                f"[{elapsed:6.1f}s] sent={stats.sent} ok={stats.ok} "
                f"errors={stats.errors} in_flight={in_flight}"
            )
    print("duration reached; waiting for in-flight requests")
    for worker in workers:
        worker.join(timeout=args.timeout)

    print(f"sent={stats.sent} ok={stats.ok} errors={stats.errors}")
    if stats.latencies_s:
        latencies = sorted(stats.latencies_s)
        p50 = statistics.median(latencies)
        p95 = latencies[int(0.95 * (len(latencies) - 1))]
        print(f"latency p50={p50:.2f}s p95={p95:.2f}s max={latencies[-1]:.2f}s")
    return 1 if stats.ok == 0 else 0


def main() -> int:
    """Parse arguments and run the workload."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default=None, help="default: first /v1/models entry")
    parser.add_argument("--duration", type=float, default=120.0, help="seconds")
    parser.add_argument("--calm-rps", type=float, default=0.5)
    parser.add_argument("--burst-rps", type=float, default=8.0)
    parser.add_argument("--burst-every", type=float, default=30.0, help="seconds")
    parser.add_argument("--burst-len", type=float, default=8.0, help="seconds")
    parser.add_argument("--long-frac", type=float, default=0.5)
    parser.add_argument("--long-prefix-sentences", type=int, default=40)
    parser.add_argument("--long-tail-words", type=int, default=200)
    parser.add_argument("--max-tokens", type=int, default=96)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--seed", type=int, default=0)
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
