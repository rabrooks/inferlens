"""vLLM stat-logger plugin (work in progress).

vLLM discovers :class:`InferLensStatLogger` through the
``vllm.stat_logger_plugins`` entry-point group and instantiates it per
engine. vLLM requires the plugin to be a genuine ``StatLoggerBase``
subclass (duck typing is rejected with a ``TypeError`` at plugin load),
but this module must stay importable without vLLM installed — so the class
is built lazily via module ``__getattr__``: vLLM is imported only when
vLLM itself resolves the entry point inside a running engine process.

The logger is inert unless ``INFERLENS_TRACE_PATH`` is set, so installing
inferlens never changes vLLM's behavior on its own.
"""

from __future__ import annotations

import os
from typing import Any

TRACE_PATH_ENV = "INFERLENS_TRACE_PATH"


def _build_stat_logger_class() -> type:
    """Build the ``StatLoggerBase`` subclass, importing vLLM only now."""
    from vllm.v1.metrics.loggers import StatLoggerBase

    class InferLensStatLogger(StatLoggerBase):
        """Stat-logger plugin translating vLLM stats into trace events.

        vLLM calls the class itself as a per-engine factory:
        ``InferLensStatLogger(vllm_config, engine_index)``.
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
            """Called once per EngineCoreOutputs batch (~ per scheduler step).

            Runs on the engine's asyncio event loop: it must never block on
            I/O, and an escaping exception fails all in-flight requests.
            Either argument may be None (empty batches / non-scheduler
            steps).
            """
            if not self._enabled:
                return
            # TODO: translate SchedulerStats/IterationStats into
            # EngineSnapshot and RequestFinished events.

        def log_engine_initialized(self) -> None:
            """Called by vLLM once the engine is ready."""

        def log(self) -> None:
            """Called by vLLM on the periodic logging interval (~10s)."""

    return InferLensStatLogger


def __getattr__(name: str) -> Any:
    if name == "InferLensStatLogger":
        cls = _build_stat_logger_class()
        # Cache so repeated lookups (and monkeypatching in tests) all see
        # the same class object.
        globals()[name] = cls
        return cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
