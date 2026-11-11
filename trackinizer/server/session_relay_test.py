"""The session relay's declared calls, each of which raises until the relay is built."""

from __future__ import annotations

from uuid import UUID

import pytest

from trackinizer.server.session_relay import NotLiveError, SessionRelay
from trackinizer.wire.wire_session_relay import NewInput


def test_the_relay_takes_no_input_until_it_is_built() -> None:
    relay = SessionRelay()

    with pytest.raises(NotImplementedError) as raised:
        _ = relay.send_input(
            UUID(int=1),
            NewInput(kind="Halt", payload={}),
            author="ada@example.com",
        )

    assert str(raised.value) == "The session relay is not built yet."


def test_an_input_nobody_serves_is_its_own_error() -> None:
    assert issubclass(NotLiveError, Exception)
    assert not issubclass(NotLiveError, NotImplementedError)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
