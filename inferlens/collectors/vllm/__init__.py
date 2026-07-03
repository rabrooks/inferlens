"""vLLM collector: stat-logger plugin and KV-event ZMQ subscriber.

The stat-logger entry point targets ``stat_logger.py`` directly. There is
deliberately no eager re-export here: importing this package must work
without vLLM installed, and re-exporting :class:`InferLensStatLogger`
would trigger its lazy vLLM import.
"""
