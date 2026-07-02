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
from typing import Any, Protocol

import msgspec
import zmq

from inferlens.schema import KVBlockRemoved, KVBlockStored, KVCacheCleared, TraceEvent

_logger = logging.getLogger(__name__)

ExternalBlockHash = bytes | int


class EventBatch(
    msgspec.Struct,
    array_like=True,
    omit_defaults=True,
    gc=False,
):
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
    block_hashes: list[ExternalBlockHash]
    parent_block_hash: ExternalBlockHash | None
    token_ids: list[int]
    block_size: int
    medium: str | None
    group_idx: int = 0


class BlockRemoved(KVCacheEvent):
    block_hashes: list[ExternalBlockHash]
    medium: str | None
    group_idx: int = 0


class AllBlocksCleared(KVCacheEvent):
    pass


class KVEventBatch(EventBatch):
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


class EventSink(Protocol):
    """What :class:`KVEventSubscriber` needs from a trace writer."""

    def write(self, event: TraceEvent) -> None: ...


class KVEventSubscriber:
    """Background thread that turns vLLM's KV-event stream into trace events.

    Mirrors vLLM's reference subscriber: a SUB socket for the live stream,
    plus an optional DEALER replay socket to recover events missed on a
    sequence-number gap (dropped-on-full PUB, a slow subscriber, a restart).
    Never raises out of the thread — a single bad payload is logged and
    skipped rather than killing the subscription.
    """

    def __init__(
        self,
        endpoint: str,
        sink: EventSink,
        replay_endpoint: str | None = None,
        topic: str = "",
        poll_timeout_ms: int = 100,
        replay_timeout_ms: int = 200,
    ) -> None:
        self._endpoint = endpoint
        self._replay_endpoint = replay_endpoint
        self._topic = topic
        self._sink = sink
        self._poll_timeout_ms = poll_timeout_ms
        self._replay_timeout_ms = replay_timeout_ms
        self._decoder = msgspec.msgpack.Decoder(type=KVEventBatch)
        self._ctx = zmq.Context.instance()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        """Start the subscriber thread."""
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the subscriber thread to stop and wait for it to exit."""
        self._stop.set()
        self._thread.join(timeout=timeout)

    def __enter__(self) -> KVEventSubscriber:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
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
                if not sub.poll(self._poll_timeout_ms):
                    continue
                _topic, seq_bytes, payload = sub.recv_multipart()
                seq = int.from_bytes(seq_bytes, "big", signed=True)

                if replay is not None and last_seq >= 0 and seq > last_seq + 1:
                    assert poller is not None
                    last_seq = self._replay_gap(replay, poller, last_seq, seq)
                if seq <= last_seq:
                    continue  # already delivered via replay, or a duplicate

                self._decode_and_emit(payload, seq)
                last_seq = seq
        finally:
            sub.close(linger=0)
            if replay is not None:
                replay.close(linger=0)

    def _replay_gap(
        self, replay: zmq.Socket, poller: zmq.Poller, last_seq: int, current_seq: int
    ) -> int:
        _logger.warning(
            "KV event gap: missed seq %d..%d, requesting replay",
            last_seq + 1,
            current_seq - 1,
        )
        replay.send_multipart((b"", (last_seq + 1).to_bytes(8, "big")))
        while poller.poll(timeout=self._replay_timeout_ms):
            _empty, seq_bytes, payload = replay.recv_multipart()
            if not payload:
                break  # end-of-replay marker
            replay_seq = int.from_bytes(seq_bytes, "big", signed=True)
            if replay_seq > last_seq:
                self._decode_and_emit(payload, replay_seq)
                last_seq = replay_seq
            if replay_seq >= current_seq - 1:
                break
        return last_seq

    def _decode_and_emit(self, payload: bytes, seq: int) -> None:
        try:
            batch = self._decoder.decode(payload)
            ts = time.monotonic()
            for event in translate_batch(batch, seq, ts):
                self._sink.write(event)
        except Exception:
            _logger.exception("dropping undecodable KV event batch (seq=%d)", seq)
