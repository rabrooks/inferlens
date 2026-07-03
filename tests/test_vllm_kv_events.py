"""Tests for the vLLM KV-event subscriber (translation + real ZMQ wire)."""

import threading
import time

import pytest

pytest.importorskip("zmq")
msgspec = pytest.importorskip("msgspec")

from inferlens.collectors.vllm import kv_events  # noqa: E402
from inferlens.schema import (  # noqa: E402
    CollectorGap,
    KVBlockRemoved,
    KVBlockStored,
    KVCacheCleared,
    TraceMeta,
)

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

    def events(self):
        """Everything but the trace_meta anchor written at start()."""
        return [e for e in self.snapshot() if not isinstance(e, TraceMeta)]


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
            return len(sink.events()) > 0

        assert _wait_until(publish_until_received)
    finally:
        subscriber.stop()
        pub.close(linger=0)

    [event] = sink.events()
    assert isinstance(event, KVBlockStored)
    assert event.seq == 0
    assert event.wall_time_unix == 42.0
    assert event.num_tokens == 4
    assert event.block_hashes == [123]


def _replay_server(router, buffered, stop):
    """Stand-in for vLLM's ``ZmqEventPublisher._service_replay``.

    On request, sends one multipart reply per buffered batch >= start_seq,
    then an end-of-replay marker (empty final frame). ``buffered`` is read
    live on each request, so tests may grow it as they publish.
    """
    while not stop.is_set():
        if router.poll(50):
            frame = router.recv_multipart()
            if len(frame) != 3:
                continue
            client_id, _empty, start_seq_bytes = frame
            start_seq = int.from_bytes(start_seq_bytes, "big")
            for seq, payload in sorted(buffered.items()):
                if seq >= start_seq:
                    router.send_multipart(
                        (client_id, b"", seq.to_bytes(8, "big"), payload)
                    )
            end_seq = (-1).to_bytes(8, "big", signed=True)
            router.send_multipart((client_id, b"", end_seq, b""))


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

    stop_replay = threading.Event()
    replay_thread = threading.Thread(
        target=_replay_server, args=(router, buffered, stop_replay), daemon=True
    )
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
            return len(sink.events()) > 0

        assert _wait_until(publish_seq0_until_received)

        # Skip seq 1 on the live stream entirely: only the replay buffer has
        # it, so this only passes if replay recovery actually works.
        pub.send_multipart((b"", (2).to_bytes(8, "big"), seq2_payload))

        assert _wait_until(lambda: len(sink.events()) >= 3)
    finally:
        subscriber.stop()
        stop_replay.set()
        replay_thread.join(timeout=2.0)
        pub.close(linger=0)
        router.close(linger=0)

    events = sink.events()
    assert [e.seq for e in events] == [0, 1, 2]
    assert [type(e) for e in events] == [
        KVCacheCleared,
        KVBlockRemoved,
        KVBlockStored,
    ]


def test_subscriber_recovers_repeated_gaps(zmq_context):
    """Regression: a first replay must not poison the next one.

    vLLM replays *every* buffered batch plus an end marker; a replay that
    stops reading once its own gap is filled leaves frames on the socket
    that a later replay would mistake for its own response.
    """
    import zmq

    pub = zmq_context.socket(zmq.PUB)
    pub_port = pub.bind_to_random_port("tcp://127.0.0.1")
    router = zmq_context.socket(zmq.ROUTER)
    router_port = router.bind_to_random_port("tcp://127.0.0.1")

    payloads = {
        seq: _encode_batch([kv_events.AllBlocksCleared()], ts=float(seq))
        for seq in range(6)
    }
    # Grown by publish(): like vLLM's publisher, the replay buffer holds
    # only batches published so far — buffering future batches would let
    # one replay's leftovers accidentally satisfy the next gap.
    buffered = {}

    stop_replay = threading.Event()
    replay_thread = threading.Thread(
        target=_replay_server, args=(router, buffered, stop_replay), daemon=True
    )
    replay_thread.start()

    sink = _FakeSink()
    subscriber = kv_events.KVEventSubscriber(
        f"tcp://127.0.0.1:{pub_port}",
        sink,
        replay_endpoint=f"tcp://127.0.0.1:{router_port}",
    )
    subscriber.start()

    def publish(seq):
        # Publishing seq N means vLLM already buffered batches 0..N, even
        # the ones our SUB socket never sees.
        for missed in range(seq + 1):
            buffered.setdefault(missed, payloads[missed])
        pub.send_multipart((b"", seq.to_bytes(8, "big"), payloads[seq]))

    def received_seqs():
        return {e.seq for e in sink.events()}

    try:

        def publish_seq0_until_received():
            publish(0)
            return 0 in received_seqs()

        assert _wait_until(publish_seq0_until_received)

        publish(2)  # first gap: seq 1 exists only in the replay buffer
        assert _wait_until(lambda: {0, 1, 2} <= received_seqs())

        publish(5)  # second gap: seqs 3 and 4 exist only in the replay buffer
        assert _wait_until(lambda: {3, 4, 5} <= received_seqs())
    finally:
        subscriber.stop()
        stop_replay.set()
        replay_thread.join(timeout=2.0)
        pub.close(linger=0)
        router.close(linger=0)

    assert sorted(received_seqs()) == [0, 1, 2, 3, 4, 5]


def test_subscriber_annotates_gap_when_replay_disabled(zmq_context):
    """Without replay, missed batches must surface as a collector_gap.

    Holes in a trace are allowed; silent holes are not.
    """
    import zmq

    pub = zmq_context.socket(zmq.PUB)
    port = pub.bind_to_random_port("tcp://127.0.0.1")
    sink = _FakeSink()
    subscriber = kv_events.KVEventSubscriber(f"tcp://127.0.0.1:{port}", sink)
    subscriber.start()
    try:
        payload = _encode_batch([kv_events.AllBlocksCleared()])

        def publish_seq0_until_received():
            pub.send_multipart((b"", (0).to_bytes(8, "big"), payload))
            return len(sink.events()) > 0

        assert _wait_until(publish_seq0_until_received)

        # Batches 1 and 2 are never published to us: an unrecoverable gap.
        pub.send_multipart((b"", (3).to_bytes(8, "big"), payload))
        assert _wait_until(
            lambda: any(isinstance(e, CollectorGap) for e in sink.snapshot())
        )
    finally:
        subscriber.stop()
        pub.close(linger=0)

    [gap] = [e for e in sink.snapshot() if isinstance(e, CollectorGap)]
    assert gap.source == kv_events.GAP_SOURCE
    assert gap.reason == "replay_disabled"
    assert (gap.first_seq, gap.last_seq) == (1, 2)


def test_subscriber_survives_malformed_message(zmq_context):
    """A message with the wrong frame count must not kill the thread."""
    import zmq

    pub = zmq_context.socket(zmq.PUB)
    port = pub.bind_to_random_port("tcp://127.0.0.1")
    sink = _FakeSink()
    subscriber = kv_events.KVEventSubscriber(f"tcp://127.0.0.1:{port}", sink)
    subscriber.start()
    try:
        payload = _encode_batch([kv_events.AllBlocksCleared()])

        def publish_garbage_then_valid():
            pub.send_multipart((b"", b"only-two-frames"))
            pub.send_multipart((b"", (0).to_bytes(8, "big"), payload))
            return len(sink.events()) > 0

        assert _wait_until(publish_garbage_then_valid)
    finally:
        subscriber.stop()
        pub.close(linger=0)

    assert any(isinstance(e, KVCacheCleared) for e in sink.snapshot())


def test_subscriber_writes_trace_meta_anchor_at_start(zmq_context):
    """The stream must be independently mergeable (TRACE_SPEC multi-source)."""
    before_wall, before_mono = time.time(), time.monotonic()
    sink = _FakeSink()
    subscriber = kv_events.KVEventSubscriber(
        "tcp://127.0.0.1:1",  # never published to; only the anchor matters
        sink,
        engine_version="0.23.1",
        model="test-model",
    )
    subscriber.start()
    try:
        [meta] = sink.snapshot()
    finally:
        subscriber.stop()

    assert isinstance(meta, TraceMeta)
    assert meta.engine == "vllm"
    assert meta.engine_version == "0.23.1"
    assert meta.model == "test-model"
    assert before_wall <= meta.wall_time_unix <= time.time()
    assert before_mono <= meta.monotonic_time <= time.monotonic()
    assert meta.extra["source"] == kv_events.GAP_SOURCE
    assert meta.extra["kv_ts_source"] == "subscriber_receive"
