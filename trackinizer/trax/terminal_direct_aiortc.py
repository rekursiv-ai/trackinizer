"""The aiortc peer for a direct terminal link, present only where aiortc is installed.

aiortc is not a dependency of this package: a user who wants the direct link installs
it into their own tool environment (``uv tool install --with aiortc trackinizer``).
Nothing here imports it until a browser offers a link, and :func:`aiortc_peer_factory`
says whether it is installed without importing it.
"""

from __future__ import annotations

from importlib import util
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable

    import aiortc

    from trackinizer.trax.terminal_direct import PeerFactory
else:
    from wrapt import lazy_import

    aiortc = lazy_import("aiortc")  # 90 ms, and it drags in ffmpeg's libraries.


__all__ = ["AiortcPeer", "aiortc_peer_factory"]


class AiortcPeer:
    """One aiortc peer connection; the browser creates the data channel."""

    def __init__(self) -> None:
        self._connection: aiortc.RTCPeerConnection | None = None
        self._channel: aiortc.RTCDataChannel | None = None

    @property
    def is_open(self) -> bool:
        """Whether the data channel is open."""
        return self._channel is not None and self._channel.readyState == "open"

    async def accept(
        self,
        offer_sdp: str,
        *,
        ice_servers: tuple[str, ...],
        on_message: Callable[[bytes], None],
        on_close: Callable[[], None],
    ) -> str:
        """Take the browser's offer and return the answer, once candidates are gathered.

        Args:
          offer_sdp: The browser's session description.
          ice_servers: STUN URLs; none means host candidates only.
          on_message: Called with each binary message the channel receives.
          on_close: Called when the channel closes or the connection fails.

        Returns:
          sdp: The answer's session description.

        """
        # An empty list is passed as one: with no configuration aiortc falls back to a
        # STUN server of its own choosing, which stalls a LAN-only link for 10 seconds.
        connection = aiortc.RTCPeerConnection(
            aiortc.RTCConfiguration(
                iceServers=[aiortc.RTCIceServer(urls=url) for url in ice_servers],
            ),
        )
        self._connection = connection

        def on_channel(channel: aiortc.RTCDataChannel) -> None:
            self._channel = channel

            def on_data(data: bytes | str) -> None:
                if isinstance(data, bytes):
                    on_message(data)

            _ = channel.on("message", on_data)
            _ = channel.on("close", on_close)

        def on_state() -> None:
            if connection.connectionState in {"failed", "closed"}:
                on_close()

        _ = connection.on("datachannel", on_channel)
        _ = connection.on("connectionstatechange", on_state)
        await connection.setRemoteDescription(
            aiortc.RTCSessionDescription(sdp=offer_sdp, type="offer"),
        )
        await connection.setLocalDescription(await connection.createAnswer())
        return connection.localDescription.sdp

    def send(self, data: bytes) -> None:
        """Send one message on the channel."""
        channel = self._channel
        if channel is None:
            raise RuntimeError("the data channel is not open")
        channel.send(data)

    async def close(self) -> None:
        """Close the connection; closing one that never opened is a no-op."""
        if self._connection is not None:
            await self._connection.close()


def aiortc_peer_factory() -> PeerFactory | None:
    """Return the peer class when aiortc is installed, else ``None``.

    Returns:
      factory: Makes a peer for each offer, or ``None`` to keep every terminal on the
        relay.

    """
    return AiortcPeer if util.find_spec("aiortc") else None
