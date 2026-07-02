"""vLLM collector (work in progress).

vLLM discovers :class:`InferLensStatLogger` through the
``vllm.stat_logger_plugins`` entry-point group and instantiates it per engine.
The logger is inert unless ``INFERLENS_TRACE_PATH`` is set, so installing
inferlens never changes vLLM's behavior on its own.

This module must not import ``vllm`` at import time: the entry point is
resolved inside a running vLLM process, and everything else in inferlens has
to work without vLLM installed.
"""

from __future__ import annotations

import os
from typing import Any

TRACE_PATH_ENV = "INFERLENS_TRACE_PATH"


class InferLensStatLogger:
    """Stat-logger plugin translating vLLM stats into trace events.

    Duck-typed against ``vllm.v1.metrics.loggers.StatLoggerBase``; vLLM calls
    the class itself as a factory: ``InferLensStatLogger(vllm_config,
    engine_index)``.
    """

    def __init__(self, vllm_config: Any, engine_index: int = 0) -> None:
        self._trace_path = os.environ.get(TRACE_PATH_ENV)
        self._enabled = self._trace_path is not None
        # TODO: open a TraceWriter and emit TraceMeta from vllm_config.

    def record(
        self,
        scheduler_stats: Any,
        iteration_stats: Any,
        mm_cache_stats: Any = None,
        engine_idx: int = 0,
    ) -> None:
        """Called by vLLM every logging step with fresh stats."""
        if not self._enabled:
            return
        # TODO: translate SchedulerStats/IterationStats into EngineSnapshot
        # and RequestFinished events.

    def log_engine_initialized(self) -> None:
        """Called by vLLM once the engine is ready."""

    def log(self) -> None:
        """Called by vLLM on the periodic logging interval."""
