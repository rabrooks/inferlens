"""Smoke test: verify vLLM discovers and calls the InferLens stat logger.

Runs a tiny model on vLLM's CPU backend via ``AsyncLLM`` (stat-logger
plugins load in async mode only) and counts the callbacks vLLM makes into
``InferLensStatLogger``. The class is monkeypatched in-process, which
observes the real calls because stat loggers run in the frontend process.

Useful as a fast end-to-end check that the plugin wiring survives vLLM
upgrades. Requires vLLM installed (CPU build is fine) and ~2 GiB free RAM;
downloads ``facebook/opt-125m`` on first run.

Usage::

    python examples/vllm_plugin_smoke.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.sampling_params import SamplingParams
from vllm.v1.engine.async_llm import AsyncLLM

import inferlens.collectors.vllm.stat_logger as stat_logger

calls = {"init": 0, "record": 0, "sched_stats": 0, "iter_stats": 0, "log_init": 0}


def _patch_logger() -> None:
    """Wrap the plugin class so every callback increments a counter."""
    cls = stat_logger.InferLensStatLogger
    orig_init = cls.__init__
    orig_record = cls.record
    orig_log_init = cls.log_engine_initialized

    def patched_init(self: Any, vllm_config: Any, engine_index: int = 0) -> None:
        calls["init"] += 1
        print(f"[smoke] plugin instantiated (engine_index={engine_index})", flush=True)
        orig_init(self, vllm_config, engine_index)

    def patched_record(
        self: Any,
        scheduler_stats: Any,
        iteration_stats: Any,
        mm_cache_stats: Any = None,
        engine_idx: int = 0,
    ) -> None:
        calls["record"] += 1
        if scheduler_stats is not None:
            calls["sched_stats"] += 1
        if iteration_stats is not None:
            calls["iter_stats"] += 1
        orig_record(self, scheduler_stats, iteration_stats, mm_cache_stats, engine_idx)

    def patched_log_init(self: Any) -> None:
        calls["log_init"] += 1
        orig_log_init(self)

    cls.__init__ = patched_init  # type: ignore[method-assign]
    cls.record = patched_record  # type: ignore[method-assign]
    cls.log_engine_initialized = patched_log_init  # type: ignore[method-assign]


async def _generate() -> None:
    engine = AsyncLLM.from_engine_args(
        AsyncEngineArgs(
            model="facebook/opt-125m",
            enforce_eager=True,
            max_model_len=256,
            dtype="float32",
            # On the CPU backend this reserves a fraction of system RAM
            # (despite the name); the 0.92 default wants ~15 GiB.
            gpu_memory_utilization=0.15,
        )
    )
    sampling = SamplingParams(max_tokens=32)

    async def run_one(i: int) -> Any:
        final = None
        async for out in engine.generate(
            "The capital of France is", sampling, request_id=f"smoke-{i}"
        ):
            final = out
        return final

    outs = await asyncio.gather(*[run_one(i) for i in range(3)])
    for o in outs:
        print("[smoke] output:", o.outputs[0].text[:60].replace("\n", " "), flush=True)
    engine.shutdown()


def main() -> None:
    # Must be set before the engine is constructed: the plugin reads it in
    # __init__. The trace file lands next to this script (*.ilens* is
    # gitignored).
    os.environ["INFERLENS_TRACE_PATH"] = str(Path(__file__).parent / "smoke.ilens")
    _patch_logger()
    asyncio.run(_generate())

    print(f"[smoke] RESULTS: {calls}", flush=True)
    assert calls["init"] >= 1, "plugin was never instantiated"
    assert calls["log_init"] >= 1, "log_engine_initialized never called"
    assert calls["record"] > 0, "record() was never called"
    assert calls["sched_stats"] > 0, "never saw a non-None SchedulerStats"
    assert calls["iter_stats"] > 0, "never saw a non-None IterationStats"
    print("[smoke] SUCCESS: InferLens plugin received callbacks", flush=True)


if __name__ == "__main__":
    # The __main__ guard is load-bearing: vLLM spawns the EngineCore process,
    # which re-imports this module.
    main()
