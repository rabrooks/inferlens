"""vLLM KV-event ZMQ subscriber.

vLLM publishes KV cache block lifecycle events (block store/evict, cache
reset) over a ZMQ PUB socket — an out-of-process channel, separate from the
in-process :mod:`inferlens.collectors.vllm.stat_logger` callback, meant to
be consumed by external subscribers (see ``--kv-events-config`` and
``docs/vllm-internals.md`` §6). This module is that subscriber.

The wire schema (``msgspec`` structs, msgpack-encoded, 3-frame ZMQ
multipart) is copied from vLLM rather than imported, mirroring vLLM's own
reference subscriber
(``examples/features/kv_events/kv_events_subscriber.py``): decoding the
wire format needs only ``pyzmq``/``msgspec`` (this collector's declared
``vllm`` extra), not the vLLM package itself. Fields this collector doesn't
use (``lora_id``, ``extra_keys``, ``kv_cache_spec_kind``, ...) are omitted;
``msgspec`` ignores encoder-side fields a decode-side struct doesn't
declare. Pinned to vLLM ``63fcce4de`` — re-check
``vllm/distributed/kv_events.py`` on version bumps.
"""

from __future__ import annotations

import logging
import threading
import time
from types import TracebackType
from typing import Any

import msgspec
import zmq

from inferlens.schema import (
    CollectorGap,
    KVBlockRemoved,
    KVBlockStored,
    KVCacheCleared,
    TraceEvent,
    TraceMeta,
)
from inferlens.trace_io import EventSink

_logger = logging.getLogger(__name__)

# `source` value on the CollectorGap events this collector emits.
GAP_SOURCE = "vllm_kv_events"

ExternalBlockHash = bytes | int


# The struct class names below ARE the wire format: ``tag=True`` makes
# msgspec use the class name as each event's tag on decode, so they must
# match vLLM's class names exactly. Do not rename them to fit project style.
class EventBatch(
    msgspec.Struct,
    array_like=True,
    omit_defaults=True,
    gc=False,
):
    """Envelope for one published batch of KV cache events."""

    ts: float
    events: list[Any]


class KVCacheEvent(
    msgspec.Struct,
    omit_defaults=True,
    gc=False,
    tag=True,
):
    """Base class for all KV cache-related events."""


class BlockStored(KVCacheEvent):
    """A chain of KV blocks was stored."""

    block_hashes: list[ExternalBlockHash]
    parent_block_hash: ExternalBlockHash | None
    token_ids: list[int]
    block_size: int
    medium: str | None
    group_idx: int = 0


class BlockRemoved(KVCacheEvent):
    """KV blocks were evicted from the cache."""

    block_hashes: list[ExternalBlockHash]
    medium: str | None
    group_idx: int = 0


class AllBlocksCleared(KVCacheEvent):
    """The whole prefix cache was reset."""


class KVEventBatch(EventBatch):
    """``EventBatch`` narrowed to the concrete KV event types."""

    events: list[BlockStored | BlockRemoved | AllBlocksCleared]


def _hash_repr(block_hash: ExternalBlockHash) -> int | str:
    """JSON can't carry raw bytes: hex-encode them, pass ints through."""
    return block_hash if isinstance(block_hash, int) else block_hash.hex()


def translate_batch(batch: KVEventBatch, seq: int, ts: float) -> list[TraceEvent]:
    """Translate one decoded KV-event batch into trace events.

    ``ts`` should be the subscriber's own monotonic receive time, not
    ``batch.ts`` (vLLM's wall clock) — see the clock-model note in
    ``docs/TRACE_SPEC.md``.
    """
    return [_translate_event(event, seq, batch.ts, ts) for event in batch.events]


def _translate_event(
    event: BlockStored | BlockRemoved | AllBlocksCleared,
    seq: int,
    wall_time_unix: float,
    ts: float,
) -> TraceEvent:
    if isinstance(event, BlockStored):
        return KVBlockStored(
            ts=ts,
            seq=seq,
            wall_time_unix=wall_time_unix,
            block_hashes=[_hash_repr(h) for h in event.block_hashes],
            parent_block_hash=(
                _hash_repr(event.parent_block_hash)
                if event.parent_block_hash is not None
                else None
            ),
            num_tokens=len(event.token_ids),
            block_size=event.block_size,
            medium=event.medium,
            group_idx=event.group_idx,
        )
    if isinstance(event, BlockRemoved):
        return KVBlockRemoved(
            ts=ts,
            seq=seq,
            wall_time_unix=wall_time_unix,
            block_hashes=[_hash_repr(h) for h in event.block_hashes],
            medium=event.medium,
            group_idx=event.group_idx,
        )
    return KVCacheCleared(ts=ts, seq=seq, wall_time_unix=wall_time_unix)


class KVEventSubscriber:
    """Background thread that turns vLLM's KV-event stream into trace events.

    Mirrors vLLM's reference subscriber: a SUB socket for the live stream,
    plus an optional DEALER replay socket to recover events missed on a
    sequence-number gap (dropped-on-full PUB, a slow subscriber, a restart).

    Failure policy: gaps are never fatal to the recording. A bad message is
    logged and skipped, and any events that could not be recovered (replay
    disabled, or replay timed out short of the gap) are annotated in the
    trace itself as a :class:`~inferlens.schema.CollectorGap` — a trace with
    holes is acceptable, a trace with silent holes is not. Only a socket
    error stops the thread, with an error log.
    """

    def __init__(
        self,
        endpoint: str,
        sink: EventSink,
        replay_endpoint: str | None = None,
        topic: str = "",
        poll_timeout_ms: int = 100,
        replay_timeout_ms: int = 200,
        engine_version: str = "",
        model: str = "",
    ) -> None:
        self._endpoint = endpoint
        self._replay_endpoint = replay_endpoint
        self._topic = topic
        self._sink = sink
        self._poll_timeout_ms = poll_timeout_ms
        self._replay_timeout_ms = replay_timeout_ms
        self._engine_version = engine_version
        self._model = model
        self._decoder = msgspec.msgpack.Decoder(type=KVEventBatch)
        self._ctx = zmq.Context.instance()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        """Write this stream's clock anchor, then start the subscriber thread.

        The subscriber usually runs in a different OS process than the stat
        logger, so its stream needs its own ``trace_meta``
        ``(wall, monotonic)`` anchor to be independently mergeable — see
        "Multi-source recording" in ``docs/TRACE_SPEC.md``. ``kv_ts_source``
        records that ``ts`` values are subscriber receive times, so a future
        engine-side timestamp policy is distinguishable in old traces.
        """
        self._sink.write(
            TraceMeta(
                engine="vllm",
                engine_version=self._engine_version,
                model=self._model,
                wall_time_unix=time.time(),
                monotonic_time=time.monotonic(),
                extra={"source": GAP_SOURCE, "kv_ts_source": "subscriber_receive"},
            )
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the subscriber thread to stop and wait for it to exit."""
        self._stop.set()
        self._thread.join(timeout=timeout)

    def __enter__(self) -> KVEventSubscriber:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def _run(self) -> None:
        sub = self._ctx.socket(zmq.SUB)
        sub.connect(self._endpoint)
        sub.setsockopt_string(zmq.SUBSCRIBE, self._topic)
        replay = None
        poller = None
        if self._replay_endpoint is not None:
            # DEALER, not REQ: a REQ socket's send/recv FSM only allows one
            # recv() per send(), so it can only ever collect the *first*
            # replayed batch — vLLM's own reference subscriber has this bug
            # (verified empirically against pyzmq: a second recv() after one
            # send() raises EFSM). DEALER has no such restriction; matching
            # vLLM's ROUTER-side framing just requires adding back the empty
            # delimiter frame REQ would otherwise supply automatically.
            replay = self._ctx.socket(zmq.DEALER)
            replay.connect(self._replay_endpoint)
            poller = zmq.Poller()
            poller.register(replay, zmq.POLLIN)

        last_seq = -1
        try:
            while not self._stop.is_set():
                try:
                    if not sub.poll(self._poll_timeout_ms):
                        continue
                    frames = sub.recv_multipart()
                    if len(frames) != 3:
                        _logger.warning(
                            "dropping malformed KV event message (%d frames)",
                            len(frames),
                        )
                        continue
                    _topic, seq_bytes, payload = frames
                    seq = int.from_bytes(seq_bytes, "big", signed=True)

                    if last_seq >= 0 and seq > last_seq + 1:
                        if replay is not None:
                            assert poller is not None
                            last_seq = self._replay_gap(replay, poller, last_seq, seq)
                        if seq > last_seq + 1:
                            self._record_gap(last_seq + 1, seq - 1, replay)
                    if seq <= last_seq:
                        continue  # already delivered via replay, or a duplicate

                    self._decode_and_emit(payload, seq)
                    last_seq = seq
                except zmq.ZMQError:
                    _logger.exception("KV event socket error; stopping subscriber")
                    break
                except Exception:
                    _logger.exception("error handling KV event message; continuing")
        finally:
            sub.close(linger=0)
            if replay is not None:
                replay.close(linger=0)

    def _record_gap(
        self, first_seq: int, last_seq: int, replay: zmq.Socket | None
    ) -> None:
        """Annotate unrecoverable missed batches in the trace itself."""
        reason = "replay_disabled" if replay is None else "replay_incomplete"
        _logger.warning(
            "KV event batches seq %d..%d lost (%s)", first_seq, last_seq, reason
        )
        self._sink.write(
            CollectorGap(
                ts=time.monotonic(),
                source=GAP_SOURCE,
                reason=reason,
                first_seq=first_seq,
                last_seq=last_seq,
            )
        )

    def _replay_gap(
        self, replay: zmq.Socket, poller: zmq.Poller, last_seq: int, current_seq: int
    ) -> int:
        _logger.warning(
            "KV event gap: missed seq %d..%d, requesting replay",
            last_seq + 1,
            current_seq - 1,
        )
        # Discard anything a previous timed-out replay left on the socket:
        # stale frames would otherwise be mistaken for this request's reply.
        while replay.poll(0):
            replay.recv_multipart()
        replay.send_multipart((b"", (last_seq + 1).to_bytes(8, "big")))
        # Always consume through the end-of-replay marker, even once the gap
        # is filled: vLLM replays *every* buffered batch >= the requested
        # seq, and frames left unread here would poison the next replay.
        while poller.poll(timeout=self._replay_timeout_ms):
            _empty, seq_bytes, payload = replay.recv_multipart()
            if not payload:
                return last_seq  # end-of-replay marker
            replay_seq = int.from_bytes(seq_bytes, "big", signed=True)
            if replay_seq > last_seq:
                self._decode_and_emit(payload, replay_seq)
                last_seq = replay_seq
        # Timed out short of the end marker; the caller records whatever
        # part of the gap is still missing as a CollectorGap.
        return last_seq

    def _decode_and_emit(self, payload: bytes, seq: int) -> None:
        try:
            batch = self._decoder.decode(payload)
            ts = time.monotonic()
            for event in translate_batch(batch, seq, ts):
                self._sink.write(event)
        except Exception:
            _logger.exception("dropping undecodable KV event batch (seq=%d)", seq)
