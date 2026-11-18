"""The aiortc peer, against a stand-in module: the library itself need not be installed."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import machinery, util
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import sys

import pytest

from trackinizer.trax import terminal_direct_aiortc
from trackinizer.trax.terminal_direct_aiortc import (
    AiortcPeer,
    aiortc_peer_factory,
)


pytestmark = pytest.mark.asyncio

_CONFIGURATION = MagicMock(name="RTCConfiguration")
_DESCRIPTION = MagicMock(name="RTCSessionDescription")
_PEER = MagicMock(name="RTCPeerConnection")


def _server(urls: str) -> str:
    return f"server:{urls}"


_ICE_SERVER = MagicMock(name="RTCIceServer", side_effect=_server)

# One stand-in for the whole process: the peer's module binds `aiortc` lazily, once.
_LIBRARY = ModuleType("aiortc")
_LIBRARY.__spec__ = machinery.ModuleSpec("aiortc", None)
_LIBRARY.__dict__.update(
    RTCConfiguration=_CONFIGURATION,
    RTCIceServer=_ICE_SERVER,
    RTCPeerConnection=_PEER,
    RTCSessionDescription=_DESCRIPTION,
)


@dataclass(slots=True, kw_only=True)
class _Made:
    """The one connection the stand-in makes, and what a test reads off it."""

    connection: MagicMock
    handlers: dict[str, Callable[..., None]]
    set_remote: AsyncMock
    set_local: AsyncMock
    close: AsyncMock


def _recording(handlers: dict[str, Callable[..., None]]) -> MagicMock:
    """Return an ``on`` that keeps each handler it is given, by event."""

    def record(event: str, handler: Callable[..., None]) -> None:
        handlers[event] = handler

    return MagicMock(side_effect=record)


@pytest.fixture(autouse=True)
def stand_in(monkeypatch: pytest.MonkeyPatch) -> _Made:
    """Install the stand-in with fresh mocks; return the connection it will make."""
    monkeypatch.setitem(sys.modules, "aiortc", _LIBRARY)
    for each in (_CONFIGURATION, _DESCRIPTION, _ICE_SERVER, _PEER):
        each.reset_mock()
    handlers: dict[str, Callable[..., None]] = {}
    set_remote, set_local, close = AsyncMock(), AsyncMock(), AsyncMock()
    connection = MagicMock(
        name="connection",
        on=_recording(handlers),
        setRemoteDescription=set_remote,
        setLocalDescription=set_local,
        createAnswer=AsyncMock(return_value="created-answer"),
        localDescription=SimpleNamespace(sdp="answer-sdp"),
        close=close,
        connectionState="new",
    )
    _PEER.return_value = connection
    return _Made(
        connection=connection,
        handlers=handlers,
        set_remote=set_remote,
        set_local=set_local,
        close=close,
    )


async def _accepted(
    made: _Made,
    ice_servers: tuple[str, ...] = (),
) -> tuple[AiortcPeer, list[bytes], list[str]]:
    peer = AiortcPeer()
    got: list[bytes] = []
    closed: list[str] = []
    answer = await peer.accept(
        "offer-sdp",
        ice_servers=ice_servers,
        on_message=got.append,
        on_close=lambda: closed.append("closed"),
    )
    assert answer == "answer-sdp"
    made.set_local.assert_awaited_once_with("created-answer")
    return peer, got, closed


async def test_the_offer_is_answered_with_exactly_the_servers_it_was_given(
    stand_in: _Made,
) -> None:
    await _accepted(stand_in, ("stun:a.test:3478", "stun:b.test"))

    _CONFIGURATION.assert_called_once_with(
        iceServers=["server:stun:a.test:3478", "server:stun:b.test"],
    )
    _DESCRIPTION.assert_called_once_with(sdp="offer-sdp", type="offer")
    stand_in.set_remote.assert_awaited_once()


async def test_no_server_is_an_empty_list_never_the_librarys_own_default(
    stand_in: _Made,
) -> None:
    await _accepted(stand_in, ())

    _CONFIGURATION.assert_called_once_with(iceServers=[])


async def test_the_channel_the_browser_opens_carries_binary_messages_and_its_close(
    stand_in: _Made,
) -> None:
    peer, got, closed = await _accepted(stand_in)
    handlers: dict[str, Callable[..., None]] = {}
    send = MagicMock()
    channel = MagicMock(name="channel", on=_recording(handlers), send=send)
    channel.readyState = "open"
    assert not peer.is_open

    stand_in.handlers["datachannel"](channel)
    handlers["message"](b"\x02")
    handlers["message"]("text is not a frame")
    handlers["close"]()
    peer.send(b"out")

    assert (peer.is_open, got, closed) == (True, [b"\x02"], ["closed"])
    send.assert_called_once_with(b"out")
    channel.readyState = "closed"
    assert not peer.is_open


@pytest.mark.parametrize("state", ["failed", "closed"])
async def test_a_failed_or_closed_connection_reports_the_close_once_it_changes(
    stand_in: _Made,
    state: str,
) -> None:
    _, _, closed = await _accepted(stand_in)

    stand_in.connection.connectionState = "connecting"
    stand_in.handlers["connectionstatechange"]()
    assert closed == []
    stand_in.connection.connectionState = state
    stand_in.handlers["connectionstatechange"]()

    assert closed == ["closed"]


async def test_closing_closes_the_connection(stand_in: _Made) -> None:
    peer, _, _ = await _accepted(stand_in)

    await peer.close()
    await AiortcPeer().close()

    stand_in.close.assert_awaited_once_with()


async def test_sending_before_the_browser_opens_a_channel_is_an_error() -> None:
    with pytest.raises(RuntimeError, match=r"^the data channel is not open$"):
        AiortcPeer().send(b"x")


async def test_a_factory_exists_only_when_the_library_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> None:
        del name

    assert aiortc_peer_factory() is AiortcPeer
    monkeypatch.setattr(util, "find_spec", missing)
    assert terminal_direct_aiortc.aiortc_peer_factory() is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
