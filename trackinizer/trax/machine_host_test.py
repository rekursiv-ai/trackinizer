"""Tests for the machine host loop, against a scripted client."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import stat
import uuid

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.machine_host import (
    HOST_CLIS,
    connect,
    host_facts,
)
from trackinizer.wire.wire_machine_host import (
    HEARTBEAT_SEC,
    HeartbeatResponse,
    JoinRequest,
    JoinResponse,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.wire.wire_machines import Facts


_MACHINE_ID: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_CREDENTIAL: Final = "trax_machine_" + "1" * 32 + "_" + "C" * 43
_TOKEN: Final = "enr_" + "0" * 32 + "_" + "S" * 43
_NOW: Final = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

_REVOKED: Final = ClientError("-> 410", status_code=410)


class _Host:
    """A scripted server: each heartbeat takes the next outcome in turn."""

    def __init__(
        self,
        *,
        beats: Sequence[ClientError | None] = (_REVOKED,),
        join_error: ClientError | None = None,
    ) -> None:
        self.beats = list(beats)
        self.join_error = join_error
        self.calls: list[tuple[object, ...]] = []
        self.keys: list[str] = []
        self.closed = 0

    def __call__(self, key: str) -> _Host:
        self.keys.append(key)
        return self

    def join_machine(
        self,
        name: str,
        *,
        token: str,
        instance: uuid.UUID,
        host_version: str,
        facts: Facts,
    ) -> JoinResponse:
        self.calls.append(("join", name, token, instance, host_version, facts))
        if self.join_error is not None:
            raise self.join_error
        return JoinResponse(machine_id=_MACHINE_ID, credential=_CREDENTIAL)

    def heartbeat_machine(
        self,
        machine_id: uuid.UUID,
        *,
        instance: uuid.UUID,
        host_version: str,
        facts: Facts | None = None,
    ) -> HeartbeatResponse:
        self.calls.append(("beat", machine_id, instance, host_version, facts))
        outcome = self.beats.pop(0)
        if outcome is not None:
            raise outcome
        return HeartbeatResponse(server_time=_NOW)

    def close(self) -> None:
        self.closed += 1


def _save() -> None:
    """Save box-1's credential, as a first ``connect`` would have."""
    path = state_dir() / "rekursiv-ai" / "trax" / "machines" / "box-1.credential"
    path.parent.mkdir(parents=True)
    path.write_text(
        JoinResponse(machine_id=_MACHINE_ID, credential=_CREDENTIAL).model_dump_json(),
    )


def _connect(host: _Host, *, stdin: str = f"{_TOKEN}\n") -> list[float]:
    """Connect box-1 with ``stdin`` as its first line, returning each wait."""
    slept: list[float] = []
    connect(
        "box-1",
        client_for=host,
        read_token=lambda: stdin,
        sleep=slept.append,
    )
    return slept


def test_a_first_connect_joins_with_the_stdin_token_and_saves_the_credential(
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = _Host(beats=[None, None, _REVOKED])

    slept = _connect(host)

    join, *beats = host.calls
    assert join[:3] == ("join", "box-1", _TOKEN)
    assert join[5] == host_facts()
    # The join stored the facts, so no heartbeat repeats them.
    assert [beat[4] for beat in beats] == [None, None, None]
    assert {beat[2] for beat in beats} == {join[3]}
    assert host.keys == ["", _CREDENTIAL]
    assert host.closed == 2
    assert slept == [HEARTBEAT_SEC, HEARTBEAT_SEC]
    saved = JoinResponse.model_validate_json(
        (
            state_dir() / "rekursiv-ai" / "trax" / "machines" / "box-1.credential"
        ).read_text(),
    )
    assert (saved.machine_id, saved.credential) == (_MACHINE_ID, _CREDENTIAL)
    assert capsys.readouterr().out == "joined: machine box-1\nrevoked: machine box-1\n"


def test_the_saved_credential_is_readable_by_this_user_only() -> None:
    _connect(_Host())

    path = state_dir() / "rekursiv-ai" / "trax" / "machines" / "box-1.credential"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_a_saved_credential_skips_the_join_and_sends_facts_once() -> None:
    _save()
    host = _Host(beats=[None, None, _REVOKED])

    connect(
        "box-1",
        client_for=host,
        read_token=_no_token,
        sleep=lambda _: None,
    )

    assert [call[0] for call in host.calls] == ["beat"] * 3
    assert [call[4] for call in host.calls] == [host_facts(), None, None]
    assert host.keys == [_CREDENTIAL]


def test_each_process_heartbeats_as_its_own_instance() -> None:
    _save()
    first, second = _Host(), _Host()

    for host in (first, second):
        connect("box-1", client_for=host, read_token=_no_token, sleep=lambda _: None)

    assert first.calls[0][2] != second.calls[0][2]


def test_lost_contact_is_retried_sooner_at_first_and_never_ends_the_host(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _save()
    refused = ClientError("POST /x failed: connection refused")
    down = ClientError("-> 503", status_code=503)
    rival = ClientError("-> 409", status_code=409, code="another_host_connected")
    host = _Host(
        beats=[
            refused,
            down,
            rival,
            refused,
            refused,
            refused,
            None,
            refused,
            _REVOKED,
        ],
    )
    delays: list[float] = []

    connect("box-1", client_for=host, read_token=_no_token, sleep=delays.append)

    assert delays == [2.0, 4.0, 8.0, 15.0, 15.0, 15.0, HEARTBEAT_SEC, 2.0]
    # Facts ride the first heartbeat that lands, not the first one tried.
    assert [call[4] is not None for call in host.calls[:7]] == [True] * 7
    assert host.calls[7][4] is None
    assert capsys.readouterr().err.count("retrying") == 7


def test_hours_offline_never_overflow_the_backoff() -> None:
    _save()
    refused = ClientError("POST /x failed: connection refused")
    host = _Host(beats=[*[refused] * 1_100, _REVOKED])
    delays: list[float] = []

    connect("box-1", client_for=host, read_token=_no_token, sleep=delays.append)

    assert len(delays) == 1_100
    assert set(delays[4:]) == {float(HEARTBEAT_SEC)}


@pytest.mark.parametrize("status", [401, 404, 422])
def test_an_answer_a_retry_cannot_change_ends_the_host(status: int) -> None:
    _save()
    host = _Host(beats=[ClientError(f"-> {status}", status_code=status)])

    with pytest.raises(ClientError) as err:
        connect("box-1", client_for=host, read_token=_no_token, sleep=lambda _: None)

    assert err.value.status_code == status


def test_an_empty_stdin_never_joins() -> None:
    host = _Host()

    with pytest.raises(ClientError, match="enrollment token from stdin"):
        _connect(host, stdin="  \n")

    assert host.calls == []
    assert not (
        state_dir() / "rekursiv-ai" / "trax" / "machines" / "box-1.credential"
    ).exists()


def test_a_refused_join_saves_nothing() -> None:
    host = _Host(join_error=ClientError("-> 401", status_code=401))

    with pytest.raises(ClientError):
        _connect(host)

    assert not (
        state_dir() / "rekursiv-ai" / "trax" / "machines" / "box-1.credential"
    ).exists()
    assert [call[0] for call in host.calls] == ["join"]


def test_a_corrupt_credential_file_is_named_without_quoting_it() -> None:
    path = state_dir() / "rekursiv-ai" / "trax" / "machines" / "box-1.credential"
    path.parent.mkdir(parents=True)
    path.write_text(f'{{"credential": "{_CREDENTIAL}"}}')

    with pytest.raises(ClientError) as err:
        connect("box-1", client_for=_Host(), read_token=_no_token, sleep=lambda _: None)

    assert str(path) in str(err.value)
    assert _CREDENTIAL not in str(err.value)
    assert err.value.__cause__ is None


def test_host_facts_fit_the_join_contract() -> None:
    facts = host_facts()

    assert set(facts) == {"os", "arch", "cpus", "memory_mb", "user", "clis"}
    cpus, memory_mb, clis = facts["cpus"], facts["memory_mb"], facts["clis"]
    assert isinstance(cpus, int)
    assert isinstance(memory_mb, int)
    assert isinstance(clis, list)
    assert (cpus > 0, memory_mb > 0, set(clis) <= set(HOST_CLIS)) == (True,) * 3
    joined = JoinRequest(
        name="box-1",
        token=_TOKEN,
        instance=uuid.uuid4(),
        host_version="",
        facts=facts,
    )
    assert joined.facts == facts


def _no_token() -> str:
    raise AssertionError("a saved credential must not read a token")


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
