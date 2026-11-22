"""The tools wire contract: what a tool connection offer may carry."""

from __future__ import annotations

from uuid import UUID, uuid4

import asyncio
import uuid

import pydantic
import pytest

from trackinizer.wire.wire_tools import (
    FRAME_BYTES,
    MUX_FRAME_BYTES,
    MUX_STREAMS,
    MuxStream,
    ToolMux,
    ToolOffer,
)


def test_a_tool_offer_reads_past_a_field_a_newer_server_added() -> None:
    connection_id = uuid.uuid4()
    offer = ToolOffer.model_validate(
        {"connection_id": str(connection_id), "added_later": 1},
    )
    assert offer == ToolOffer(connection_id=connection_id)


def test_a_tool_offer_without_its_connection_is_refused() -> None:
    with pytest.raises(pydantic.ValidationError):
        ToolOffer.model_validate({})


@pytest.mark.asyncio
async def test_a_slow_stream_does_not_block_its_neighbor() -> None:
    to_relay: asyncio.Queue[bytes | None] = asyncio.Queue()
    to_host: asyncio.Queue[bytes | None] = asyncio.Queue()
    relay, host = ToolMux(to_host.put), ToolMux(to_relay.put)
    accepted: asyncio.Queue[MuxStream] = asyncio.Queue()
    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        _ = tasks.create_task(relay.run(to_relay.get))
        _ = tasks.create_task(host.run(to_host.get, accept=accepted.put_nowait))
        slow = await relay.open(uuid4())
        peer = await accepted.get()
        fast = await relay.open(uuid4())
        other = await accepted.get()
        await slow.send(b"first")
        blocked = tasks.create_task(slow.send(b"second"))
        await fast.send(b"independent")
        assert await other.read() == b"independent"
        assert not blocked.done()
        assert await peer.read() == b"first"
        await blocked
        await slow.close()
        assert await peer.read() == b"second"
        assert await peer.read() is None
        await other.send(b"reply")
        assert await fast.read() == b"reply"
        await to_relay.put(None)
        await to_host.put(None)


@pytest.mark.asyncio
async def test_cancelled_open_releases_peer_slot_without_closing_neighbors() -> None:
    to_host: asyncio.Queue[bytes | None] = asyncio.Queue()
    to_relay: asyncio.Queue[bytes | None] = asyncio.Queue()
    accepted: asyncio.Queue[MuxStream] = asyncio.Queue()
    cancelled_id = uuid4()
    pending = asyncio.Event()

    async def send_to_host(frame: bytes) -> None:
        to_host.put_nowait(frame)
        if frame == b"O" + cancelled_id.bytes:
            await pending.wait()

    relay, host = ToolMux(send_to_host), ToolMux(to_relay.put)
    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        _ = tasks.create_task(relay.run(to_relay.get))
        _ = tasks.create_task(host.run(to_host.get, accept=accepted.put_nowait))
        neighbor = await relay.open(uuid4())
        peer = await accepted.get()
        for _ in range(MUX_STREAMS - 2):
            await relay.open(uuid4())
            await accepted.get()
        opening = tasks.create_task(relay.open(cancelled_id))
        abandoned = await accepted.get()
        assert abandoned.id == cancelled_id
        assert not host.has_capacity

        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening

        # Receiving this frame proves the host processed any preceding CLOSE.
        await neighbor.send(b"still open")
        assert await peer.read() == b"still open"
        assert host.has_capacity, "cancelled OPEN left a slot occupied on the peer"
        assert await abandoned.read() is None
        await peer.send(b"reply")
        assert await neighbor.read() == b"reply"
        to_relay.put_nowait(None)
        to_host.put_nowait(None)


@pytest.mark.asyncio
async def test_reader_stop_during_open_preserves_cancellation() -> None:
    stream_id = uuid4()
    started = asyncio.Event()
    pending = asyncio.Event()
    incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
    sent: list[bytes] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)
        started.set()
        await pending.wait()

    mux = ToolMux(send)
    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        running = tasks.create_task(mux.run(incoming.get))
        opening = tasks.create_task(mux.open(stream_id))
        await started.wait()
        # DATA plus the EOF from stop() fills the opening stream's inbox.
        incoming.put_nowait(b"D" + stream_id.bytes + b"early data")
        incoming.put_nowait(None)
        await running
        assert not mux.has_capacity

        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert sent == [b"O" + stream_id.bytes]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True], ids=["cancel", "stop"])
async def test_open_waiting_on_a_writer_sends_no_frames(*, stop: bool) -> None:
    active_id, queued_id = uuid4(), uuid4()
    started = asyncio.Event()
    release = asyncio.Event()
    waiting = asyncio.Event()
    sent: list[bytes] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)
        if frame == b"O" + active_id.bytes:
            started.set()
            await release.wait()

    mux = ToolMux(send)

    async def open_queued() -> None:
        # Catch inside the task so the task group doesn't cancel its siblings.
        error = ConnectionError if stop else asyncio.CancelledError
        waiting.set()
        with pytest.raises(error):
            await mux.open(queued_id)

    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        _ = tasks.create_task(mux.open(active_id))
        await started.wait()
        queued = tasks.create_task(open_queued())
        await waiting.wait()
        if stop:
            mux.stop()
            release.set()
        else:
            queued.cancel()
        await queued
        assert sent == [b"O" + active_id.bytes]
        if not stop:
            release.set()
            reopened = await mux.open(queued_id)
            assert reopened.id == queued_id


@pytest.mark.asyncio
async def test_peer_close_wakes_blocked_writer_and_preserves_pending_data() -> None:
    incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
    outgoing: asyncio.Queue[bytes] = asyncio.Queue()
    mux = ToolMux(outgoing.put)
    stream_id = uuid4()
    stream = await mux.open(stream_id)
    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        _ = tasks.create_task(mux.run(incoming.get))
        await stream.send(b"first")

        async def send_again() -> None:
            with pytest.raises(ConnectionError):
                await stream.send(b"second")

        blocked = tasks.create_task(send_again())
        await asyncio.sleep(0)
        assert not blocked.done()
        incoming.put_nowait(b"D" + stream_id.bytes + b"last")
        incoming.put_nowait(b"C" + stream_id.bytes)
        await stream.wait_closed()
        await blocked
        assert [data async for data in stream] == [b"last"]
        assert await stream.read() is None
        await stream.close()
        assert outgoing.get_nowait() == b"O" + stream_id.bytes
        assert outgoing.get_nowait() == b"D" + stream_id.bytes + b"first"
        assert outgoing.empty()
        incoming.put_nowait(None)


@pytest.mark.asyncio
async def test_stream_capacity_recovers_after_close_and_failed_open() -> None:
    sent: list[bytes] = []
    failed_open = BrokenPipeError()
    failed_close = OSError()
    rejected = uuid4()

    async def send(frame: bytes) -> None:
        if frame == b"O" + rejected.bytes and not sent:
            sent.append(frame)
            raise failed_open
        sent.append(frame)
        if frame == b"C" + rejected.bytes:
            raise failed_close

    mux = ToolMux(send)
    with pytest.raises(BrokenPipeError) as failure:
        _ = await mux.open(rejected)
    assert failure.value is failed_open
    assert sent == [b"O" + rejected.bytes, b"C" + rejected.bytes]

    streams = [await mux.open(uuid4()) for _ in range(MUX_STREAMS - 1)]
    with pytest.raises(ValueError, match=r".+"):
        _ = await mux.open(streams[0].id)
    assert mux.has_capacity
    last = await mux.open(rejected)
    assert not mux.has_capacity
    with pytest.raises(ValueError, match=r".+"):
        _ = await mux.open(uuid4())
    assert len(sent) == MUX_STREAMS + 2

    await last.close()
    await last.close()
    assert sent.count(b"C" + rejected.bytes) == 2
    assert await last.read() is None
    assert mux.has_capacity
    _ = await mux.open(rejected)
    assert not mux.has_capacity
    mux.stop()
    assert not mux.has_capacity
    assert all([await stream.read() is None for stream in streams])


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_locally", [True, False], ids=["stop", "cancel"])
async def test_shutdown_drains_reader_and_fences_socket(*, stop_locally: bool) -> None:
    outgoing: asyncio.Queue[bytes] = asyncio.Queue()
    mux = ToolMux(outgoing.put)
    stream_id = uuid4()
    stream = await mux.open(stream_id)
    started = asyncio.Event()
    cleaned = asyncio.Event()
    pending: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def read() -> bytes | None:
        try:
            started.set()
            return await pending.get()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        runner = tasks.create_task(mux.run(read))
        await started.wait()
        if stop_locally:
            mux.stop()
            await runner
        else:
            runner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await runner
        assert cleaned.is_set()
        await stream.wait_closed()
        assert not mux.has_capacity
        with pytest.raises(ConnectionError, match=r"^the daemon tools socket closed$"):
            await mux.send(b"D", stream_id, b"too late")
        assert outgoing.get_nowait() == b"O" + stream_id.bytes
        assert outgoing.empty()

        async def unexpected_read() -> bytes | None:
            pytest.fail("a stopped mux must not read the socket again")

        await mux.run(unexpected_read)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frame", "allow_open", "invalid_part"),
    [
        pytest.param(b"D" + bytes(15), True, "length", id="short-header"),
        pytest.param(b"D" + bytes(MUX_FRAME_BYTES), True, "length", id="oversize"),
        pytest.param(b"O" + bytes(16), False, "kind", id="open-on-relay"),
        pytest.param(
            b"O" + bytes(16) + b"payload",
            True,
            "kind",
            id="open-with-payload",
        ),
        pytest.param(b"D" + bytes(16), True, "kind", id="empty-data"),
        pytest.param(
            b"A" + bytes(16) + b"payload",
            True,
            "kind",
            id="credit-with-payload",
        ),
        pytest.param(
            b"C" + bytes(16) + b"payload",
            True,
            "kind",
            id="close-with-payload",
        ),
        pytest.param(b"?" + bytes(16), True, "kind", id="unknown-tag"),
    ],
)
async def test_malformed_envelope_shuts_down_socket(
    frame: bytes,
    invalid_part: str,
    *,
    allow_open: bool,
) -> None:
    incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
    outgoing: asyncio.Queue[bytes] = asyncio.Queue()
    mux = ToolMux(outgoing.put)
    stream = await mux.open(uuid4())
    accepted: list[MuxStream] = []
    incoming.put_nowait(frame)
    incoming.put_nowait(None)
    async with asyncio.timeout(2):
        with pytest.raises(
            ValueError,
            match=rf"^invalid daemon tool frame {invalid_part}$",
        ):
            await mux.run(incoming.get, accept=accepted.append if allow_open else None)
        assert not accepted
        assert not mux.has_capacity
        assert await stream.read() is None


@pytest.mark.asyncio
async def test_receiver_rejects_data_beyond_credit_without_losing_first_frame() -> None:
    incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
    outgoing: asyncio.Queue[bytes] = asyncio.Queue()
    mux = ToolMux(outgoing.put)
    stream_id = UUID(int=1)
    payload = b"x" * FRAME_BYTES
    for frame in (
        b"O" + stream_id.bytes,
        b"D" + uuid4().bytes + b"stale",
        b"D" + stream_id.bytes + payload,
        b"D" + stream_id.bytes + b"excess",
        None,
    ):
        incoming.put_nowait(frame)
    accepted: list[MuxStream] = []
    async with asyncio.timeout(2):
        with pytest.raises(
            ValueError,
            match=r"^data exceeds the tool stream's credit$",
        ):
            await mux.run(incoming.get, accept=accepted.append)
        (stream,) = accepted
        assert stream.id == stream_id
        assert await stream.read() == payload
        assert await stream.read() is None
        assert outgoing.empty()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
