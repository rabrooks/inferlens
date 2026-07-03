"""vLLM stat-logger plugin.

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

import atexit
import logging
import os
import time
from typing import Any

from inferlens.collectors.vllm import translate
from inferlens.schema import TraceMeta
from inferlens.trace_io import BufferedTraceWriter

TRACE_PATH_ENV = "INFERLENS_TRACE_PATH"

_logger = logging.getLogger(__name__)


def _build_stat_logger_class() -> type:
    """Build the ``StatLoggerBase`` subclass, importing vLLM only now."""
    import vllm
    from vllm.v1.metrics.loggers import StatLoggerBase

    class InferLensStatLogger(StatLoggerBase):
        """Stat-logger plugin translating vLLM stats into trace events.

        vLLM calls the class itself as a per-engine factory:
        ``InferLensStatLogger(vllm_config, engine_index)``. vLLM does not
        catch exceptions from this constructor (unlike entry-point *load*
        failures, which it skips), so a bad trace path must disable the
        plugin rather than raise and take the engine down with it. vLLM
        also never calls a shutdown hook on stat loggers, so the writer's
        flush-and-close is registered via ``atexit`` instead.
        """

        def __init__(self, vllm_config: Any, engine_index: int = 0) -> None:
            self._enabled = False
            trace_path = os.environ.get(TRACE_PATH_ENV)
            if trace_path is None:
                return
            try:
                self._writer = BufferedTraceWriter(trace_path)
                atexit.register(self._writer.close)
                self._writer.write(
                    TraceMeta(
                        engine="vllm",
                        engine_version=vllm.__version__,
                        model=vllm_config.model_config.model,
                        wall_time_unix=time.time(),
                        monotonic_time=time.monotonic(),
                        extra={"engine_index": engine_index},
                    )
                )
            except Exception:
                _logger.exception(
                    "InferLens stat-logger failed to start; disabling for this engine"
                )
                return
            self._enabled = True

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
            try:
                ts = time.monotonic()
                snapshot = translate.engine_snapshot(
                    scheduler_stats, iteration_stats, ts
                )
                if snapshot is not None:
                    self._writer.write(snapshot)
                for finished in translate.request_finished_events(iteration_stats, ts):
                    self._writer.write(finished)
            except Exception:
                self._enabled = False
                _logger.exception(
                    "InferLens stat-logger translation failed; disabling "
                    "for the rest of this engine's lifetime"
                )

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
