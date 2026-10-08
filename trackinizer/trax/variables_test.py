"""Tests for ``trax env``: list, set, secret input rules, and delete."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import io
import traceback

import httpx2
import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.trax import cli
from trackinizer.trax.variables import ENV_WORD, Variables
from trackinizer.wire.wire_variables import MAX_VALUE_BYTES, Variable


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


_CANARY = "hunter2-do-not-print"


def _variable(name: str, value: str | None, *, secret: bool = False) -> Variable:
    return Variable(
        layer="org",
        owner="",
        name=name,
        secret=secret,
        value=value,
        updated_by="scout",
        updated=datetime(2026, 10, 6, tzinfo=UTC),
    )


class _StubClient:
    """Records variable calls; raises ``error`` from a mutating call when set."""

    def __init__(
        self,
        variables: Sequence[Variable] = (),
        *,
        error: ClientError | None = None,
    ) -> None:
        self.variables = list(variables)
        self.error = error
        self.calls: list[tuple[str, str, str, bool]] = []

    def list_variables(self) -> list[Variable]:
        self.calls.append(("list", "", "", False))
        if self.error is not None:
            raise self.error
        return self.variables

    def put_variable(self, name: str, *, value: str, secret: bool) -> None:
        self.calls.append(("put", name, value, secret))
        if self.error is not None:
            raise self.error

    def delete_variable(self, name: str) -> None:
        self.calls.append(("delete", name, "", False))
        if self.error is not None:
            raise self.error


def _run(argv: Sequence[str], client: _StubClient) -> None:
    cli.parse_and_run([ENV_WORD, *argv], client_factory=lambda: cast(Client, client))


def _stdin_bytes(monkeypatch: pytest.MonkeyPatch, data: bytes) -> io.TextIOWrapper:
    stdin = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", stdin)
    return stdin


def _stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    _stdin_bytes(monkeypatch, data=text.encode())


def _refusal(argv: Sequence[str], client: _StubClient) -> ClientError:
    with pytest.raises(ClientError) as err:
        _run(argv, client=client)
    return err.value


def test_verb_word_is_one_constant() -> None:
    assert Variables.names == (ENV_WORD,)
    assert Variables in cli.DISPATCHERS


def test_bare_env_lists_plain_values_and_masks_secrets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _StubClient(
        [
            _variable("REGION", "eu-west"),
            # A server that wrongly sent a value must still not see it printed.
            _variable("API_TOKEN", _CANARY, secret=True),
            _variable("NOTE", "a\nb"),
        ],
    )
    _run([], client=client)
    out = capsys.readouterr().out
    assert _CANARY not in out
    assert out.splitlines() == [
        "REGION     eu-west",
        "API_TOKEN  (secret)",
        "NOTE       a\\nb",
    ]
    assert client.calls == [("list", "", "", False)]


def test_bare_env_with_no_variables_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run([], client=_StubClient())
    assert capsys.readouterr().out == "(no variables)\n"


def test_set_plain_value_from_the_command_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _StubClient()
    _run(["REGION", "TO", "eu-west"], client=client)
    assert client.calls == [("put", "REGION", "eu-west", False)]
    assert capsys.readouterr().out == "set: env REGION\n"


def test_set_plain_value_keeps_whitespace_and_option_like_text() -> None:
    client = _StubClient()
    _run(["FLAGS", "to", "--verbose -x "], client=client)
    assert client.calls == [("put", "FLAGS", "--verbose -x ", False)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("v\n", "v"),
        ("v\r\n", "v"),
        ("v\n\n", "v\n"),
        ("v\r\n\r\n", "v\r\n"),
        (" v ", " v "),
        ("v\r", "v\r"),
        ("a\nb\n", "a\nb"),
    ],
)
def test_stdin_value_loses_exactly_one_trailing_newline(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    expected: str,
) -> None:
    client = _StubClient()
    _stdin(monkeypatch, text=text)
    _run(["REGION", "to", "-"], client=client)
    assert client.calls == [("put", "REGION", expected, False)]


def test_file_value_loses_exactly_one_trailing_newline(tmp_path: Path) -> None:
    source = tmp_path / "value.txt"
    source.write_bytes(b"line one\nline two\r\n\r\n")
    client = _StubClient()
    _run(["BODY", "to", f"@{source}"], client=client)
    assert client.calls == [("put", "BODY", "line one\nline two\r\n", False)]


def test_secret_value_from_stdin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _StubClient()
    _stdin(monkeypatch, text=f"{_CANARY}\n")
    _run(["Secret", "API_TOKEN", "To", "-"], client=client)
    assert client.calls == [("put", "API_TOKEN", _CANARY, True)]
    captured = capsys.readouterr()
    assert captured.out == "set: env API_TOKEN (secret)\n"
    assert _CANARY not in captured.out + captured.err


def test_secret_value_from_a_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "token"
    source.write_text(f"{_CANARY}\r\n")
    client = _StubClient()
    _run(["secret", "API_TOKEN", "to", f"@{source}"], client=client)
    assert client.calls == [("put", "API_TOKEN", _CANARY, True)]
    captured = capsys.readouterr()
    assert _CANARY not in captured.out + captured.err


def test_literal_secret_value_is_refused_before_any_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _StubClient()
    err = _refusal(["secret", "API_TOKEN", "to", _CANARY], client=client)
    assert str(err) == (
        "a secret's value comes from stdin (-) or a file (@FILE), "
        "never the command line"
    )
    assert client.calls == []
    captured = capsys.readouterr()
    assert _CANARY not in captured.out + captured.err
    assert _CANARY not in "".join(traceback.format_exception(err))


@pytest.mark.parametrize(
    "argv",
    [
        ["1BAD", "to", "v"],
        ["has-dash", "to", "v"],
        ["a b", "to", "v"],
        ["x" * 129, "to", "v"],
        ["secret", "1BAD", "to", "-"],
        ["1BAD", "del"],
        # ``$`` in the pattern also matches before a final newline.
        ["REGION\n", "to", "v"],
        ["REGION\n", "del"],
        ["secret", "API_TOKEN\n", "to", "-"],
    ],
)
def test_invalid_name_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    client = _StubClient()
    stdin = _stdin_bytes(monkeypatch, data=b"v\n")
    err = _refusal(argv, client=client)
    assert "invalid variable name" in str(err)
    assert client.calls == []
    assert stdin.tell() == 0


def test_the_maximum_name_length_is_accepted() -> None:
    client = _StubClient()
    _run(["x" * 128, "to", "v"], client=client)
    assert client.calls == [("put", "x" * 128, "v", False)]


@pytest.mark.parametrize("text", ["", "\n", "\r\n"])
def test_empty_stdin_value_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    client = _StubClient()
    _stdin(monkeypatch, text=text)
    err = _refusal(["secret", "API_TOKEN", "to", "-"], client=client)
    assert "empty value" in str(err)
    assert client.calls == []


def test_empty_literal_and_empty_file_are_refused(tmp_path: Path) -> None:
    source = tmp_path / "empty"
    source.write_text("")
    client = _StubClient()
    assert "empty value" in str(_refusal(["REGION", "to", ""], client=client))
    assert "empty value" in str(_refusal(["REGION", "to", f"@{source}"], client=client))
    assert client.calls == []


@pytest.mark.parametrize(
    "argv_tail",
    [["secret", "API_TOKEN", "to", "-"], ["R", "to", "-"]],
)
def test_non_utf8_stdin_is_refused_without_echoing_a_byte(
    monkeypatch: pytest.MonkeyPatch,
    argv_tail: list[str],
) -> None:
    client = _StubClient()
    _stdin_bytes(monkeypatch, data=b"\xff" + _CANARY.encode() + b"\n")
    err = _refusal(argv_tail, client=client)
    assert "not valid UTF-8" in str(err)
    assert _CANARY not in "".join(traceback.format_exception(err))
    assert "\\udcff" not in "".join(traceback.format_exception(err))
    assert err.__context__ is None
    assert client.calls == []


def test_non_utf8_file_is_refused_without_echoing_a_byte(tmp_path: Path) -> None:
    source = tmp_path / "token"
    source.write_bytes(b"\xff" + _CANARY.encode())
    client = _StubClient()
    err = _refusal(["secret", "API_TOKEN", "to", f"@{source}"], client=client)
    assert "not valid UTF-8" in str(err)
    assert "0xff" not in "".join(traceback.format_exception(err))
    assert err.__context__ is None
    assert client.calls == []


def test_a_mistyped_literal_secret_is_not_echoed_as_a_missing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _StubClient()
    err = _refusal(["secret", "PASS", "to", "@Xy9-hunter2"], client=client)
    assert "cannot read the secret file" in str(err)
    assert "Xy9-hunter2" not in "".join(traceback.format_exception(err))
    assert err.__context__ is None
    assert client.calls == []


def test_unreadable_file_is_refused_before_any_request(tmp_path: Path) -> None:
    client = _StubClient()
    err = _refusal(["REGION", "to", f"@{tmp_path / 'missing'}"], client=client)
    assert "cannot read" in str(err)
    assert client.calls == []
    assert "@ value requires a path" in str(
        _refusal(["REGION", "to", "@"], client=client),
    )


def test_oversized_value_is_refused_without_echoing_it() -> None:
    client = _StubClient()
    value = "é" * (MAX_VALUE_BYTES // 2 + 1)
    err = _refusal(["REGION", "to", value], client=client)
    assert str(MAX_VALUE_BYTES) in str(err)
    assert value not in str(err)
    assert client.calls == []
    _run(["REGION", "to", "é" * (MAX_VALUE_BYTES // 2)], client=client)
    assert len(client.calls) == 1


def test_delete_removes_the_variable(capsys: pytest.CaptureFixture[str]) -> None:
    client = _StubClient()
    _run(["REGION", "DEL"], client=client)
    assert client.calls == [("delete", "REGION", "", False)]
    assert capsys.readouterr().out == "deleted: env REGION\n"


def test_delete_of_an_absent_variable_names_it() -> None:
    client = _StubClient(error=ClientError("DELETE ... -> 404", status_code=404))
    assert (
        str(_refusal(["REGION", "del"], client=client)) == "variable 'REGION' not found"
    )


def test_a_variable_named_secret_is_still_addressable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``secret`` is a keyword only before ``NAME to SOURCE``; else it is a NAME."""
    client = _StubClient()
    _run(["secret", "to", "plain"], client=client)
    _run(["secret", "del"], client=client)
    assert client.calls == [
        ("put", "secret", "plain", False),
        ("delete", "secret", "", False),
    ]
    capsys.readouterr()


@pytest.mark.parametrize(
    "argv",
    [
        ["REGION"],
        ["REGION", "to"],
        ["REGION", "eu"],
        ["REGION", "to", "a", "b"],
        ["secret", "API_TOKEN", "del"],
        ["secret", "API_TOKEN"],
        ["secret", "API_TOKEN", "to"],
        ["secret", "API_TOKEN", "to", "-", "extra"],
        ["REGION", "del", "extra"],
    ],
)
def test_malformed_command_is_refused_before_any_request(argv: list[str]) -> None:
    client = _StubClient()
    err = _refusal(argv, client=client)
    assert "usage: trax env" in str(err)
    assert client.calls == []


def test_forbidden_means_the_admin_role_is_needed() -> None:
    forbidden = ClientError("PUT ... -> 403: {...}", status_code=403)
    for argv in (["REGION", "to", "v"], ["REGION", "del"]):
        client = _StubClient(error=forbidden)
        assert str(_refusal(argv, client=client)) == "needs the admin role"


def test_listing_forbidden_means_the_writer_role_is_needed() -> None:
    client = _StubClient(error=ClientError("GET ... -> 403", status_code=403))
    assert str(_refusal([], client=client)) == "needs the writer role"


@pytest.mark.parametrize("status", [409, 503])
def test_conflict_and_unavailable_pass_the_server_detail(status: int) -> None:
    detail = '{"detail": "secret backend is not configured"}'
    client = _StubClient(
        error=ClientError(f"PUT /x -> {status}: {detail}", status_code=status),
    )
    err = _refusal(["REGION", "to", "v"], client=client)
    assert "secret backend is not configured" in str(err)


@pytest.mark.parametrize("status", [403, 409, 422, 500, 503])
def test_a_secret_value_is_in_no_output_or_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    """Even a server that echoes the request body leaks nothing."""
    echoed = ClientError(
        f"PUT /api/variables/API_TOKEN -> {status}: "
        f'{{"detail": [{{"input": "{_CANARY}"}}]}}',
        status_code=status,
    )
    client = _StubClient(error=echoed)
    _stdin(monkeypatch, text=f"{_CANARY}\n")
    err = _refusal(["secret", "API_TOKEN", "to", "-"], client=client)
    assert err.__cause__ is None
    assert err.__context__ is None
    assert _CANARY not in str(err) + repr(err)
    assert _CANARY not in "".join(traceback.format_exception(err))
    captured = capsys.readouterr()
    assert _CANARY not in captured.out + captured.err


@pytest.mark.parametrize("status", [400, 409, 422, 500, 503])
def test_a_failure_that_may_echo_the_body_names_only_its_status(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """A truncated echo cannot be redacted by matching, so none of it is shown."""
    client = _StubClient(
        error=ClientError(f"PUT /x -> {status}: {_CANARY[:8]}", status_code=status),
    )
    _stdin(monkeypatch, text=_CANARY)
    err = _refusal(["secret", "API_TOKEN", "to", "-"], client=client)
    assert str(err) == f"server refused the request (HTTP {status})"


@pytest.mark.parametrize(
    "secret",
    [
        'pa"ss-word-1',
        "back\\slash-pass1",
        "line1\nline2-pass",
        "tab\tsep-pass1",
        "ctl\x01x-pass-1",
        "S" * 1_000 + "Q" * 3_000,
    ],
    ids=["quote", "backslash", "newline", "tab", "control", "truncated"],
)
@pytest.mark.parametrize("status", [409, 503])
def test_a_secret_is_not_leaked_by_an_escaped_or_truncated_echo(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    secret: str,
) -> None:
    """The real client JSON-escapes and truncates what a server echoes back."""
    monkeypatch.setattr("time.sleep", Mock())
    _stdin(monkeypatch, text=secret + "\n")

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(status, json={"detail": f"conflict storing {secret}"})

    with Client("https://server") as client:
        client._http.close()
        client._http = httpx2.Client(
            base_url=client.base_url,
            transport=httpx2.MockTransport(handler),
        )
        with pytest.raises(ClientError) as err:
            cli.parse_and_run(
                [ENV_WORD, "secret", "API_TOKEN", "to", "-"],
                client_factory=lambda: client,
            )
    text = "".join(traceback.format_exception(err.value))
    assert str(err.value) == f"server refused the request (HTTP {status})"
    assert secret[:8] not in text
    assert err.value.__context__ is None


def test_a_transport_failure_passes_through() -> None:
    client = _StubClient(error=ClientError("PUT /x failed: refused"))
    assert "refused" in str(_refusal(["REGION", "to", "v"], client=client))


def test_env_help_lists_every_form(capsys: pytest.CaptureFixture[str]) -> None:
    client = _StubClient()
    _run(["help"], client=client)
    out = capsys.readouterr().out
    for form in (
        f"trax {ENV_WORD} NAME to VALUE",
        f"trax {ENV_WORD} secret NAME to -",
        f"trax {ENV_WORD} secret NAME to @FILE",
        f"trax {ENV_WORD} NAME del",
    ):
        assert form in out
    assert client.calls == []


def test_env_usage_lists_only_the_forms_the_parser_accepts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(["help"], client=_StubClient())
    usage = capsys.readouterr().out.splitlines()[0]
    forms = "[NAME to VALUE|-|@FILE | secret NAME to -|@FILE | NAME del]"
    assert usage == f"Usage: trax env {forms}"
    err = _refusal(["secret", "API_TOKEN", "del"], client=_StubClient())
    assert str(err) == f"usage: trax env {forms}"


@pytest.mark.parametrize("word", ["help", "-h", "--help"])
def test_a_trailing_help_word_shows_help_instead_of_setting_it(
    capsys: pytest.CaptureFixture[str],
    word: str,
) -> None:
    client = _StubClient()
    _run(["MODE", "to", word], client=client)
    assert "Usage: trax env" in capsys.readouterr().out
    assert "set such a value with @FILE" in Variables.help_text()
    assert client.calls == []


def test_top_level_help_names_the_verb(capsys: pytest.CaptureFixture[str]) -> None:
    cli.parse_and_run(["help", ENV_WORD])
    assert f"Usage: trax {ENV_WORD}" in capsys.readouterr().out


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
