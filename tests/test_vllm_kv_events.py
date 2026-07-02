"""Tests for the vLLM KV-event subscriber (translation + real ZMQ wire)."""

import threading
import time

import pytest

pytest.importorskip("zmq")
msgspec = pytest.importorskip("msgspec")

from inferlens.collectors.vllm import kv_events  # noqa: E402
from inferlens.schema import KVBlockRemoved, KVBlockStored, KVCacheCleared  # noqa: E402

# --- translation (pure, no sockets) -----------------------------------------


def test_translate_block_stored():
    batch = kv_events.KVEventBatch(
        ts=100.0,
        events=[
            kv_events.BlockStored(
                block_hashes=[111, 222],
                parent_block_hash=99,
                token_ids=[1, 2, 3, 4, 5],
                block_size=16,
                medium="GPU",
                group_idx=1,
            )
        ],
    )
    [event] = kv_events.translate_batch(batch, seq=5, ts=1.5)
    assert isinstance(event, KVBlockStored)
    assert event.ts == 1.5
    assert event.seq == 5
    assert event.wall_time_unix == 100.0
    assert event.block_hashes == [111, 222]
    assert event.parent_block_hash == 99
    assert event.num_tokens == 5
    assert event.block_size == 16
    assert event.medium == "GPU"
    assert event.group_idx == 1


def test_translate_block_stored_root_block_has_no_parent():
    batch = kv_events.KVEventBatch(
        ts=1.0,
        events=[
            kv_events.BlockStored(
                block_hashes=[1],
                parent_block_hash=None,
                token_ids=[1],
                block_size=16,
                medium="GPU",
            )
        ],
    )
    [event] = kv_events.translate_batch(batch, seq=0, ts=0.0)
    assert event.parent_block_hash is None


def test_translate_block_stored_hex_encodes_raw_bytes():
    batch = kv_events.KVEventBatch(
        ts=1.0,
        events=[
            kv_events.BlockStored(
                block_hashes=[b"\xde\xad\xbe\xef"],
                parent_block_hash=b"\x01\x02",
                token_ids=[1, 2],
                block_size=16,
                medium="GPU",
            )
        ],
    )
    [event] = kv_events.translate_batch(batch, seq=0, ts=0.0)
    assert event.block_hashes == ["deadbeef"]
    assert event.parent_block_hash == "0102"


def test_translate_block_removed():
    batch = kv_events.KVEventBatch(
        ts=2.0,
        events=[kv_events.BlockRemoved(block_hashes=[7, 8], medium="GPU", group_idx=2)],
    )
    [event] = kv_events.translate_batch(batch, seq=1, ts=0.5)
    assert isinstance(event, KVBlockRemoved)
    assert event.block_hashes == [7, 8]
    assert event.medium == "GPU"
    assert event.group_idx == 2


def test_translate_all_blocks_cleared():
    batch = kv_events.KVEventBatch(ts=3.0, events=[kv_events.AllBlocksCleared()])
    [event] = kv_events.translate_batch(batch, seq=2, ts=0.7)
    assert isinstance(event, KVCacheCleared)
    assert event.ts == 0.7
    assert event.seq == 2
    assert event.wall_time_unix == 3.0


def test_translate_batch_preserves_multi_event_order():
    batch = kv_events.KVEventBatch(
        ts=1.0,
        events=[
            kv_events.AllBlocksCleared(),
            kv_events.BlockRemoved(block_hashes=[1], medium="GPU"),
        ],
    )
    events = kv_events.translate_batch(batch, seq=0, ts=0.0)
    assert [type(e) for e in events] == [KVCacheCleared, KVBlockRemoved]


# --- subscriber (real ZMQ sockets) ------------------------------------------


class _FakeSink:
    def __init__(self):
        self._events = []
        self._lock = threading.Lock()

    def write(self, event):
        with self._lock:
            self._events.append(event)

    def snapshot(self):
        with self._lock:
            return list(self._events)


def _encode_batch(events, ts=1000.0):
    batch = kv_events.KVEventBatch(ts=ts, events=events)
    return msgspec.msgpack.Encoder().encode(batch)


def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def zmq_context():
    import zmq

    return zmq.Context.instance()


def test_subscriber_receives_live_events(zmq_context):
    import zmq

    pub = zmq_context.socket(zmq.PUB)
    port = pub.bind_to_random_port("tcp://127.0.0.1")
    sink = _FakeSink()
    subscriber = kv_events.KVEventSubscriber(f"tcp://127.0.0.1:{port}", sink)
    subscriber.start()
    try:
        payload = _encode_batch(
            [
                kv_events.BlockStored(
                    block_hashes=[123],
                    parent_block_hash=None,
                    token_ids=[1, 2, 3, 4],
                    block_size=16,
                    medium="GPU",
                )
            ],
            ts=42.0,
        )

        def publish_until_received():
            # Defeats ZMQ's "slow joiner" problem: the subscription may not
            # have propagated to the publisher yet when the first message
            # is sent, silently dropping it.
            pub.send_multipart((b"", (0).to_bytes(8, "big"), payload))
            return len(sink.snapshot()) > 0

        assert _wait_until(publish_until_received)
    finally:
        subscriber.stop()
        pub.close(linger=0)

    [event] = sink.snapshot()
    assert isinstance(event, KVBlockStored)
    assert event.seq == 0
    assert event.wall_time_unix == 42.0
    assert event.num_tokens == 4
    assert event.block_hashes == [123]


def test_subscriber_recovers_gap_via_replay(zmq_context):
    import zmq

    pub = zmq_context.socket(zmq.PUB)
    pub_port = pub.bind_to_random_port("tcp://127.0.0.1")
    router = zmq_context.socket(zmq.ROUTER)
    router_port = router.bind_to_random_port("tcp://127.0.0.1")

    seq0_payload = _encode_batch([kv_events.AllBlocksCleared()], ts=1.0)
    seq1_payload = _encode_batch(
        [kv_events.BlockRemoved(block_hashes=[7], medium="GPU")], ts=2.0
    )
    seq2_payload = _encode_batch(
        [
            kv_events.BlockStored(
                block_hashes=[9],
                parent_block_hash=7,
                token_ids=[1] * 16,
                block_size=16,
                medium="GPU",
            )
        ],
        ts=3.0,
    )
    buffered = {0: seq0_payload, 1: seq1_payload, 2: seq2_payload}

    # Stand-in for vLLM's ZmqEventPublisher._service_replay: on request,
    # sends one multipart reply per buffered item >= start_seq, then an
    # end-of-replay marker (empty final frame).
    stop_replay = threading.Event()

    def replay_server():
        while not stop_replay.is_set():
            if router.poll(50):
                frame = router.recv_multipart()
                if len(frame) != 3:
                    continue
                client_id, _empty, start_seq_bytes = frame
                start_seq = int.from_bytes(start_seq_bytes, "big")
                for seq, payload in buffered.items():
                    if seq >= start_seq:
                        router.send_multipart(
                            (client_id, b"", seq.to_bytes(8, "big"), payload)
                        )
                end_seq = (-1).to_bytes(8, "big", signed=True)
                router.send_multipart((client_id, b"", end_seq, b""))

    replay_thread = threading.Thread(target=replay_server, daemon=True)
    replay_thread.start()

    sink = _FakeSink()
    subscriber = kv_events.KVEventSubscriber(
        f"tcp://127.0.0.1:{pub_port}",
        sink,
        replay_endpoint=f"tcp://127.0.0.1:{router_port}",
    )
    subscriber.start()
    try:

        def publish_seq0_until_received():
            pub.send_multipart((b"", (0).to_bytes(8, "big"), seq0_payload))
            return len(sink.snapshot()) > 0

        assert _wait_until(publish_seq0_until_received)

        # Skip seq 1 on the live stream entirely: only the replay buffer has
        # it, so this only passes if replay recovery actually works.
        pub.send_multipart((b"", (2).to_bytes(8, "big"), seq2_payload))

        assert _wait_until(lambda: len(sink.snapshot()) >= 3)
    finally:
        subscriber.stop()
        stop_replay.set()
        replay_thread.join(timeout=2.0)
        pub.close(linger=0)
        router.close(linger=0)

    events = sink.snapshot()
    assert [e.seq for e in events] == [0, 1, 2]
    assert [type(e) for e in events] == [
        KVCacheCleared,
        KVBlockRemoved,
        KVBlockStored,
    ]
