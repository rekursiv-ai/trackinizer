"""A lost consuming GET response stays recoverable across transport retries."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import time

import httpx2
import pytest

from trackinizer.client import client
from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.wire.wire_sessions import DrainInboundResponse, InboundDrainItem
from trackinizer.wire.wire_terminals import TerminalWait


class _Server:
    def __init__(self, *, lost: int, terminal: bool) -> None:
        self.lost = lost
        self.terminal = terminal
        self.queued = True
        self.keys: list[str | None] = []
        self.receipts: dict[str, DrainInboundResponse | TerminalWait] = {}
        self.terminal_id = uuid4()

    def answer(self, request: httpx2.Request) -> httpx2.Response:
        key = request.headers.get("Idempotency-Key")
        self.keys.append(key)
        response = self.receipts.get(key or "")
        if response is None and self.queued:
            response = (
                TerminalWait(
                    terminal_id=self.terminal_id,
                    opener="a@x.y",
                    rows=24,
                    cols=80,
                )
                if self.terminal
                else DrainInboundResponse(messages=[InboundDrainItem(text="keep me")])
            )
            self.queued = False
            if key is not None:
                self.receipts[key] = response
        if len(self.keys) <= self.lost:
            raise httpx2.ReadError("response lost")
        if response is not None:
            return httpx2.Response(200, json=response.model_dump(mode="json"))
        return httpx2.Response(200, json=None if self.terminal else {"messages": []})


def _poll(transport: Client, *, terminal: bool) -> str:
    if terminal:
        found = transport.wait_terminals(UUID(int=1), instance=UUID(int=2), wait_sec=0)
        return "" if found is None else str(found.terminal_id)
    return "".join(item.text for item in transport.drain_inbound(UUID(int=1)))


def _no_sleep(seconds: float) -> None:
    del seconds


@pytest.mark.parametrize("terminal", [False, True], ids=["inbound", "terminal-wait"])
@pytest.mark.parametrize("lost", [1, 3], ids=["one-response", "all-attempts"])
def test_consuming_get_retains_its_delivery_identity_until_success(
    monkeypatch: pytest.MonkeyPatch,
    *,
    terminal: bool,
    lost: int,
) -> None:
    monkeypatch.setattr(
        client,
        "time",
        SimpleNamespace(monotonic=time.monotonic, sleep=_no_sleep),
    )
    server = _Server(lost=lost, terminal=terminal)
    transport = Client("http://delivery.test")
    transport._http.close()
    transport._http = httpx2.Client(
        base_url=transport.base_url,
        transport=httpx2.MockTransport(server.answer),
    )
    with transport:
        if lost == 3:
            with pytest.raises(ClientError):
                _poll(transport, terminal=terminal)
        assert _poll(transport, terminal=terminal) == (
            str(server.terminal_id) if terminal else "keep me"
        )
        assert server.keys[0] is not None
        assert len(set(server.keys)) == 1
        _poll(transport, terminal=terminal)
        assert server.keys[-1] != server.keys[0]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
