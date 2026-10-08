"""Tests for ``trax machine``: list, show, set, label and delete."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import io

import httpx2
import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.trax import cli
from trackinizer.trax.machines import MACHINE_WORD, Machines
from trackinizer.wire.wire_machines import (
    MAX_HOW_CHARS,
    NAME_PATTERN,
    RESERVED_NAMES,
    Machine,
)


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _machine(
    name: str,
    *,
    role: str = "",
    how: str = "",
    labels: Sequence[str] = (),
) -> Machine:
    return Machine(
        name=name,
        role=role,
        how=how,
        labels=list(labels),
        updated_by="alice",
        updated=datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
    )


class _StubClient:
    """Records machine calls; raises ``error`` from any call when set."""

    def __init__(
        self,
        machines: Sequence[Machine] = (),
        *,
        error: ClientError | None = None,
    ) -> None:
        self.machines = list(machines)
        self.error = error
        self.calls: list[tuple[object, ...]] = []

    def list_machines(self) -> list[Machine]:
        self.calls.append(("list",))
        if self.error is not None:
            raise self.error
        return self.machines

    def get_machine(self, name: str) -> Machine:
        self.calls.append(("get", name))
        if self.error is not None:
            raise self.error
        return next(m for m in self.machines if m.name == name)

    def put_machine(
        self,
        name: str,
        *,
        role: str | None = None,
        how: str | None = None,
    ) -> None:
        self.calls.append(("put", name, role, how))
        if self.error is not None:
            raise self.error

    def change_machine_labels(
        self,
        name: str,
        *,
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
    ) -> None:
        self.calls.append(("labels", name, tuple(add), tuple(remove)))
        if self.error is not None:
            raise self.error

    def delete_machine(self, name: str) -> None:
        self.calls.append(("delete", name))
        if self.error is not None:
            raise self.error


def _run(argv: Sequence[str], client: _StubClient) -> None:
    cli.parse_and_run(
        [MACHINE_WORD, *argv],
        client_factory=lambda: cast(Client, client),
    )


def _refusal(argv: Sequence[str], client: _StubClient) -> ClientError:
    with pytest.raises(ClientError) as err:
        _run(argv, client=client)
    return err.value


def _stdin(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    monkeypatch.setattr(
        "sys.stdin",
        io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"),
    )


def test_verb_word_is_one_constant() -> None:
    assert Machines.names == (MACHINE_WORD,)
    assert MACHINE_WORD == "machine"
    assert cli.DISPATCHERS[-1] is Machines


def test_bare_machine_lists_name_role_labels_and_how(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _StubClient(
        [
            _machine("gpu-box", role="dev", how="ssh gpu-box", labels=["gpu", "a100"]),
            _machine("cpu-1"),
        ],
    )
    _run([], client=client)
    assert capsys.readouterr().out.splitlines() == [
        "gpu-box  dev  gpu, a100  ssh gpu-box",
        "cpu-1    -    -          -",
    ]
    assert client.calls == [("list",)]


def test_list_cuts_how_to_one_short_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _StubClient(
        [
            _machine("a", how="first\nsecond"),
            _machine("b", how="x" * 200),
        ],
    )
    _run([], client=client)
    first, second = capsys.readouterr().out.splitlines()
    assert first == "a  -  -  first\\nsecond"
    assert second == f"b  -  -  {'x' * 57}..."


def test_list_and_show_keep_a_label_on_one_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _StubClient([_machine("a", labels=["x\ny", "z\r"]), _machine("b")])
    _run([], client=client)
    _run(["a"], client=client)
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == ["a  -  x\\ny, z\\r  -", f"b  -  {'-':<9}  -"]
    assert "labels:     x\\ny, z\\r" in lines


def test_bare_machine_with_none_registered_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run([], client=_StubClient())
    assert capsys.readouterr().out == "(no machines)\n"


def test_show_prints_every_field(capsys: pytest.CaptureFixture[str]) -> None:
    client = _StubClient(
        [_machine("gpu-box", role="dev", how="a\nb", labels=["gpu", "a100"])],
    )
    _run(["gpu-box"], client=client)
    assert capsys.readouterr().out.splitlines() == [
        "machine:    gpu-box",
        "role:       dev",
        "labels:     gpu, a100",
        "how:        a\\nb",
        "updated by: alice",
        "updated:    2026-10-06T12:00:00+00:00",
    ]
    assert client.calls == [("get", "gpu-box")]


def test_show_of_a_bare_machine_marks_what_is_unset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(["cpu-1"], client=_StubClient([_machine("cpu-1")]))
    out = capsys.readouterr().out.splitlines()
    assert out[1:4] == [
        "role:       (none)",
        "labels:     (none)",
        "how:        (none)",
    ]


def test_a_bare_field_prints_its_value_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _StubClient([_machine("gpu-box", role="dev", how="a\nb")])
    _run(["gpu-box", "ROLE"], client=client)
    _run(["gpu-box", "how"], client=client)
    assert capsys.readouterr().out == "dev\na\nb\n"
    assert client.calls == [("get", "gpu-box")] * 2


def test_set_role_creates_the_machine(capsys: pytest.CaptureFixture[str]) -> None:
    client = _StubClient()
    _run(["gpu-box", "role", "TO", "dev"], client=client)
    assert client.calls == [("put", "gpu-box", "dev", None)]
    assert capsys.readouterr().out == "set: machine gpu-box role\n"


def test_set_how_sends_only_how(capsys: pytest.CaptureFixture[str]) -> None:
    client = _StubClient()
    _run(["gpu-box", "how", "to", "ssh gpu-box; cd /work"], client=client)
    assert client.calls == [("put", "gpu-box", None, "ssh gpu-box; cd /work")]
    assert capsys.readouterr().out == "set: machine gpu-box how\n"


def test_an_empty_literal_clears_a_field() -> None:
    client = _StubClient()
    _run(["gpu-box", "role", "to", ""], client=client)
    _run(["gpu-box", "how", "to", ""], client=client)
    assert client.calls == [
        ("put", "gpu-box", "", None),
        ("put", "gpu-box", None, ""),
    ]


def test_how_from_stdin_loses_exactly_one_trailing_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StubClient()
    _stdin(monkeypatch, b"ssh gpu-box\r\n\n")
    _run(["gpu-box", "how", "to", "-"], client=client)
    assert client.calls == [("put", "gpu-box", None, "ssh gpu-box\r\n")]


def test_how_from_a_file_is_read_relative_to_the_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "how.txt").write_text("ssh gpu-box\n")
    monkeypatch.chdir(tmp_path)
    client = _StubClient()
    _run(["gpu-box", "how", "to", "@how.txt"], client=client)
    assert client.calls == [("put", "gpu-box", None, "ssh gpu-box")]


def test_an_empty_stdin_or_file_is_refused_instead_of_clearing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "empty.txt").write_text("")
    monkeypatch.chdir(tmp_path)
    client = _StubClient()
    _stdin(monkeypatch, b"")
    assert "empty value" in str(_refusal(["gpu-box", "how", "to", "-"], client=client))
    err = _refusal(["gpu-box", "how", "to", "@empty.txt"], client=client)
    assert "empty value" in str(err)
    assert client.calls == []


@pytest.mark.parametrize(
    ("source", "message"),
    [("@", "@ value requires a path"), ("@missing.txt", "cannot read @missing.txt")],
)
def test_an_unreadable_source_is_refused_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    message: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _StubClient()
    assert message in str(_refusal(["gpu-box", "how", "to", source], client=client))
    assert client.calls == []


def test_non_utf8_input_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StubClient()
    _stdin(monkeypatch, b"\xff\xfe")
    err = _refusal(["gpu-box", "how", "to", "-"], client=client)
    assert str(err) == "the value is not valid UTF-8"
    assert client.calls == []


def test_label_add_and_del_send_one_label_each(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _StubClient()
    _run(["gpu-box", "label", "add", "gpu"], client=client)
    _run(["gpu-box", "LABEL", "DEL", "a100"], client=client)
    assert client.calls == [
        ("labels", "gpu-box", ("gpu",), ()),
        ("labels", "gpu-box", (), ("a100",)),
    ]
    assert capsys.readouterr().out.splitlines() == [
        "set: machine gpu-box label add gpu",
        "set: machine gpu-box label del a100",
    ]


def test_delete_unregisters_the_machine(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _StubClient()
    _run(["gpu-box", "DEL"], client=client)
    assert client.calls == [("delete", "gpu-box")]
    assert capsys.readouterr().out == "deleted: machine gpu-box\n"


@pytest.mark.parametrize("name", sorted(RESERVED_NAMES))
def test_a_reserved_name_is_refused_before_any_request(name: str) -> None:
    client = _StubClient()
    for argv in (
        [name],
        [name, "role"],
        [name, "role", "to", "dev"],
        [name, "label", "add", "gpu"],
        [name, "del"],
    ):
        assert str(_refusal(argv, client=client)) == (
            f"machine name {name!r} is reserved"
        )
    assert client.calls == []


@pytest.mark.parametrize(
    "name",
    ["GPU", "a_b", "a/b", "a b", "x" * 64, "gpu\n"],
    ids=["upper", "underscore", "slash", "space", "long", "newline"],
)
def test_a_malformed_name_is_refused_before_any_request(name: str) -> None:
    client = _StubClient()
    err = _refusal([name, "role", "to", "dev"], client=client)
    assert str(err) == f"invalid machine name {name!r}; expected {NAME_PATTERN}"
    assert client.calls == []


def test_the_longest_name_is_accepted() -> None:
    client = _StubClient()
    _run(["a" * 63, "del"], client=client)
    assert client.calls == [("delete", "a" * 63)]


@pytest.mark.parametrize(
    "argv",
    [
        ["gpu-box", "colour"],
        ["gpu-box", "role", "to"],
        ["gpu-box", "role", "dev"],
        ["gpu-box", "role", "to", "a", "b"],
        ["gpu-box", "label"],
        ["gpu-box", "label", "add"],
        ["gpu-box", "label", "set", "gpu"],
        ["gpu-box", "label", "add", "gpu", "extra"],
        ["gpu-box", "del", "extra"],
        ["gpu-box", "labels"],
    ],
)
def test_a_malformed_command_is_refused_before_any_request(argv: list[str]) -> None:
    client = _StubClient()
    err = _refusal(argv, client=client)
    assert str(err).startswith("usage: trax machine")
    assert client.calls == []


@pytest.mark.parametrize(
    ("argv", "role"),
    [
        ([], "writer"),
        (["gpu-box"], "writer"),
        (["gpu-box", "role"], "writer"),
        (["gpu-box", "role", "to", "dev"], "admin"),
        (["gpu-box", "label", "add", "gpu"], "admin"),
        (["gpu-box", "del"], "admin"),
    ],
)
def test_forbidden_names_the_role_the_action_needs(
    argv: list[str],
    role: str,
) -> None:
    client = _StubClient(error=ClientError("GET ... -> 403", status_code=403))
    assert str(_refusal(argv, client=client)) == f"needs the {role} role"


@pytest.mark.parametrize(
    "argv",
    [
        ["gpu-box"],
        ["gpu-box", "how"],
        ["gpu-box", "label", "del", "gpu"],
        ["gpu-box", "del"],
    ],
)
def test_an_absent_machine_is_named(argv: list[str]) -> None:
    client = _StubClient(error=ClientError("... -> 404", status_code=404))
    assert str(_refusal(argv, client=client)) == "no machine gpu-box"


def test_a_set_never_reports_a_missing_machine() -> None:
    # A set creates the machine, so a 404 means the server lacks the route.
    client = _StubClient(error=ClientError("PUT /x -> 404: Not Found", status_code=404))
    assert str(_refusal(["gpu-box", "role", "to", "dev"], client=client)) == (
        "PUT /x -> 404: Not Found"
    )
    assert str(_refusal(["gpu-box", "how", "to", "ssh"], client=client)) == (
        "PUT /x -> 404: Not Found"
    )


def test_other_failures_pass_through() -> None:
    client = _StubClient(error=ClientError("PUT /x -> 503: down", status_code=503))
    assert str(_refusal(["gpu-box", "role", "to", "dev"], client=client)) == (
        "PUT /x -> 503: down"
    )
    client = _StubClient(error=ClientError("PUT /x failed: refused"))
    assert "refused" in str(_refusal(["gpu-box", "del"], client=client))


@pytest.mark.parametrize(
    "argv",
    [
        ["gpu-box", "role", "to", "Dev"],
        ["gpu-box", "how", "to", "x" * (MAX_HOW_CHARS + 1)],
    ],
    ids=["role", "how"],
)
def test_a_value_the_server_would_refuse_never_reaches_it(argv: list[str]) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(204)

    with Client("https://server") as client:
        client._http.close()
        client._http = httpx2.Client(
            base_url=client.base_url,
            transport=httpx2.MockTransport(handler),
        )
        with pytest.raises(ClientError, match=r"^machine 'gpu-box' "):
            cli.parse_and_run(
                [MACHINE_WORD, *argv],
                client_factory=lambda: client,
            )
    assert seen == []


def test_machine_help_lists_every_form(capsys: pytest.CaptureFixture[str]) -> None:
    client = _StubClient()
    _run(["help"], client=client)
    out = capsys.readouterr().out
    for form in (
        f"trax {MACHINE_WORD} NAME role to ROLE",
        f"trax {MACHINE_WORD} NAME how to -",
        f"trax {MACHINE_WORD} NAME how to @FILE",
        f"trax {MACHINE_WORD} NAME label add LABEL",
        f"trax {MACHINE_WORD} NAME label del LABEL",
        f"trax {MACHINE_WORD} NAME del",
    ):
        assert form in out
    assert client.calls == []


@pytest.mark.parametrize("word", ["help", "-h", "--help"])
def test_a_trailing_help_word_shows_help_instead_of_setting_it(
    capsys: pytest.CaptureFixture[str],
    word: str,
) -> None:
    client = _StubClient()
    _run(["gpu-box", "how", "to", word], client=client)
    assert "Usage: trax machine" in capsys.readouterr().out
    assert client.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["help"],
        ["gpu-box", "label", "add", "help"],
        ["gpu-box", "label", "del", "-h"],
        ["gpu-box", "label", "add", "--help"],
    ],
)
def test_a_trailing_help_word_is_never_a_name_or_label(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    client = _StubClient([_machine("help")])
    _run(argv, client=client)
    assert "Usage: trax machine" in capsys.readouterr().out
    assert client.calls == []


def test_top_level_help_names_the_verb(capsys: pytest.CaptureFixture[str]) -> None:
    cli.parse_and_run(["help", MACHINE_WORD])
    assert f"Usage: trax {MACHINE_WORD}" in capsys.readouterr().out


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
