# Recording overhead

`inferlens record` is built to stay off the engine's critical path. The
stat-logger plugin hands each iteration's snapshot to a background writer
thread (a non-blocking, bounded queue), and the KV-event subscriber runs in
the wrapper process rather than the engine's event loop, so the serving loop
never blocks on trace I/O. This page reports a measured A/B confirming that.

**Summary:** recording adds no measurable throughput cost and no measurable
median-latency cost; tail latency moves by at most one reporting tick
(≤10 ms, ~1%). No trace events were dropped. Overhead sits at or below the
measurement noise floor.

## Method

- **GPU / engine:** NVIDIA A100-SXM4-40GB; vLLM 0.24.0 serving
  `Qwen/Qwen2.5-1.5B-Instruct`, `--max-model-len 4096`, default (full) KV
  cache — no cache throttling, so the engine runs in a clean steady state.
- **Load:** a steady, deliberately **non-saturating** workload —
  ~10 req/s Poisson arrivals with a mixed short/long prompt distribution
  (`examples/workload.py`), fixed seed, 150 s per run. Overhead is easiest to
  observe when the engine is *not* the bottleneck; at saturation the GPU
  dominates wall-clock and any host-side cost is hidden.
- **Design:** 3 baseline + 3 recorded runs, **interleaved**
  (baseline, recorded, baseline, …) to cancel thermal and noisy-neighbor
  drift. Both arms use identical `vllm serve` arguments. *Baseline* is plain
  `vllm serve` with inferlens installed but inactive
  (`INFERLENS_TRACE_PATH` unset — the plugin is inert). *Recorded* is the
  same command under `inferlens record`, which additionally activates the
  stat-logger plugin and subscribes to the engine's KV-event stream.

## Results

Per run (workload-reported throughput and latency; vLLM's own average
generation throughput in the last column):

| Run | tag | ok | throughput (req/s) | p50 (s) | p95 (s) | avg gen (tok/s) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | baseline | 1532 | 10.213 | 0.59 | 0.62 | 1466.8¹ |
| 1 | recorded | 1533 | 10.220 | 0.59 | 0.62 | 1580.5 |
| 2 | baseline | 1533 | 10.220 | 0.59 | 0.61 | 1594.3 |
| 2 | recorded | 1532 | 10.213 | 0.59 | 0.62 | 1576.1 |
| 3 | baseline | 1534 | 10.227 | 0.59 | 0.61 | 1581.7 |
| 3 | recorded | 1532 | 10.213 | 0.59 | 0.62 | 1591.8 |

¹ First baseline run reflects cold-start; excluded from the average below.

| Metric | Baseline | Recorded | Delta |
| --- | --- | --- | --- |
| Throughput (req/s) | 10.220 | 10.215 | **−0.05%** |
| Latency p50 (s) | 0.59 | 0.59 | **0.0%** |
| Latency p95 (s) | 0.613 | 0.620 | +0.007 s (~1%) |
| Avg gen throughput (tok/s)² | 1588 | 1583 | −0.3% |
| Dropped trace events | — | 0 | — |
| Request errors | 0 | 0 | — |

² Baseline mean excludes the cold-start run.

## Findings

- **Throughput is unchanged** — 10.22 req/s in both arms (−0.05%, i.e. a
  one-request difference over 150 s).
- **Median latency is unchanged** — p50 0.59 s in every run.
- **Tail latency moves by at most one reporting tick** — p95 0.61→0.62 s.
  The measurement resolves latency to 0.01 s, so this ≤10 ms difference is at
  the edge of what the setup can distinguish.
- **No trace events were dropped** in any recorded run, and the recorded
  runs logged no queue-full warnings — the bounded writer queue kept up.

The buffered-writer design means the per-iteration cost the engine pays is a
single enqueue, which does not grow with request size or batch size. On
heavier models or loads the ratio only improves (longer iterations, same
fixed enqueue), so this fast-small-model / moderate-load regime is close to a
worst case for the *proportional* overhead.

## Reproduce

```bash
# recorded arm
inferlens record -o run.ilens.gz -- \
  vllm serve Qwen/Qwen2.5-1.5B-Instruct --max-model-len 4096

# baseline arm (inferlens installed but inactive)
vllm serve Qwen/Qwen2.5-1.5B-Instruct --max-model-len 4096

# same workload against each (fixed seed, non-saturating):
python examples/workload.py --duration 150 --calm-rps 10 --burst-rps 10 \
  --burst-every 99999 --long-frac 0.5 --long-prefix-sentences 40 \
  --long-tail-words 200 --max-tokens 128 --seed 42
```

Interleave the arms and take the mean; compare workload throughput and
p50/p95, and check the recorded run's stderr for dropped-event warnings.
