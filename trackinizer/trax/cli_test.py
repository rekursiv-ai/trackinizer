from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import argparse
import subprocess
import sys

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax import cli, profile
from trackinizer.trax.conftest import FakeClient, run
from trackinizer.trax.profile import Profile


@pytest.mark.cli_python_subprocess
def test_cli_import_does_not_load_metric_wire_modules() -> None:
    """Importing the CLI must leave the metric wire modules unloaded.

    They build pydantic models at import (~44ms for the pair), which every
    ``trax`` invocation would pay -- ``trax issue`` included -- for a grammar
    branch only the metric verbs reach. ``parser`` and ``verbs`` bind them
    through ``lazy_import``; a plain module-level ``from ... import X`` in
    either would silently restore the cost AND defeat the matching lazy bind
    in ``client.client``, which cannot help once the real module is loaded.
    Asserted in a subprocess because this test session has long since imported
    the world.
    """
    probe = (
        "import sys;"
        "import trackinizer.trax.cli;"
        "print(any(m.startswith('trackinizer.wire.wire_metrics')"
        " for m in sys.modules))"
    )
    result = subprocess.run(  # noqa: S603 -- fixed interpreter, literal probe.
        [sys.executable, "-c", probe],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "False", (
        "importing trax.cli pulled in a metric wire module; "
        "check for an eager import in parser.py or verbs.py"
    )


@pytest.mark.cli_python_subprocess
def test_module_entrypoint_is_directly_executable() -> None:
    result = subprocess.run(  # noqa: S603 -- test executes a fixed local entrypoint.
        [str(Path(__file__).with_name("__main__.py")), "help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Usage: trax COMMAND [ARGS] [OPTIONS]" in result.stdout
    assert "field is" not in result.stdout
    assert "field to value" in Path(__file__).with_name("__main__.py").read_text()
    assert not result.stderr


def test_profile_flag_overrides_trackinizer_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile.save_profile("prod", Profile(url="http://prod:9000", author="alice"))
    monkeypatch.setenv("TRACKINIZER_URL", "http://env:8766")
    client = cli.connect(argparse.Namespace(profile="prod", host=None, port=None))
    try:
        assert str(client.base_url) == "http://prod:9000"
        assert client.author == "alice"
    finally:
        cli.close_clients()


class TestClientSharing:
    """A long-lived process must not build a connection pool per invocation.

    The daemon calls ``parse_and_run`` once per request. A client cached
    inside that call would open a fresh ``httpx`` pool -- and a fresh TCP
    handshake -- every time, discarding the keep-alive the transport exists
    to provide, and would accumulate open sockets for the daemon's whole life.
    """

    def test_reuses_one_client_across_invocations(self) -> None:
        profile.save_profile("prod", Profile(url="http://prod:9000"))
        args = argparse.Namespace(profile="prod", host=None, port=None)
        try:
            clients = [cli.connect(args) for _ in range(5)]

            assert len({id(client) for client in clients}) == 1, (
                "each invocation built its own Client; under the daemon that "
                "is a new connection pool and a leaked socket per request"
            )
        finally:
            cli.close_clients()

    def test_separates_clients_by_resolved_target(self) -> None:
        """Two profiles are two servers; they must not share a connection."""
        profile.save_profile("a", Profile(url="http://a:9000"))
        profile.save_profile("b", Profile(url="http://b:9000"))
        try:
            first = cli.connect(argparse.Namespace(profile="a", host=None, port=None))
            second = cli.connect(argparse.Namespace(profile="b", host=None, port=None))

            assert first is not second
        finally:
            cli.close_clients()

    def test_a_profile_rewrite_invalidates_the_cached_client(self) -> None:
        """A cached client still carries the OLD token after a token change."""
        profile.save_profile("prod", Profile(url="http://prod:9000", api_key="old"))
        args = argparse.Namespace(profile="prod", host=None, port=None)
        try:
            before = cli.connect(args)
            profile.save_profile("prod", Profile(url="http://prod:9000", api_key="new"))
            after = cli.connect(args)

            assert before is not after
            assert after.api_key == "new"
        finally:
            cli.close_clients()


def test_global_flag_not_peeled_from_field_value() -> None:
    """A global flag spelling AFTER the verb is a field value, not peeled.

    ``_peel_top_flags`` must only consume connection/display flags from the
    pre-verb prefix; ``issue 7 title to --show-ids`` carries ``--show-ids`` as
    the title's VALUE, so the flag stays in ``leftover`` and ``show_ids`` is
    not set (TRAX-CLI-001).
    """
    top, leftover = cli._peel_top_flags(["issue", "7", "title", "to", "--show-ids"])
    assert top.show_ids is False
    assert leftover == ["issue", "7", "title", "to", "--show-ids"]


def test_global_flag_peeled_before_verb() -> None:
    """A global flag BEFORE the verb is still peeled (the prefix is its home)."""
    top, leftover = cli._peel_top_flags(
        ["--show-ids", "issue", "7", "title", "to", "x"]
    )
    assert top.show_ids is True
    assert leftover == ["issue", "7", "title", "to", "x"]


def test_global_value_flag_before_verb_consumes_its_value() -> None:
    """``--host H`` before the verb peels both flag and value off the prefix."""
    top, leftover = cli._peel_top_flags(
        ["--host", "example", "issue", "title", "to", "x"]
    )
    assert top.host == "example"
    assert leftover == ["issue", "title", "to", "x"]


def test_main_delegates_empty_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def record(argv: Sequence[str], **_: object) -> None:
        calls.append(list(argv))

    monkeypatch.setattr(cli, "parse_and_run", record)
    cli.main([])

    assert calls == [[]]


def test_main_formats_client_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def raise_err(argv: object, **_: object) -> None:
        del argv
        raise ClientError("offline")

    monkeypatch.setattr(cli, "parse_and_run", raise_err)
    with pytest.raises(SystemExit) as err:
        cli.main(["profile"])
    assert err.value.code == 2
    assert "offline" in capsys.readouterr().err


def test_empty_argv_lists_all_subjects(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run([], client)
    out = capsys.readouterr().out
    assert "Issue#1" in out
    assert "Experiment#2" in out
    assert "Belief#3" in out


def test_unknown_verb(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="unknown verb"):
        run(["wtf"], client)


def test_kind_help_shows_local_forms(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["issue", "help"], client)
    out = capsys.readouterr().out
    assert "ROW:" in out
    assert "FIELD:" in out
    assert "LIST:" in out
    assert "RELATION:" in out
    assert "EDGE:" in out
    assert "agent-cost" in out
    assert "resource-cost" in out
    assert "--yes" not in out
    assert "--del" not in out
    # The ``result`` list field was removed with WebSearch.results; help must
    # not advertise it as a list field. (``webresult`` the kind, and prose like
    # "result of the experiment" in the outcome help, are fine.)
    assert "``result``" not in out, "stale `result` list field in help"
    assert "on ``websearch``" not in out
    assert not client.calls


def test_kind_help_distinguishes_seq_and_value_list_fields(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LIST legend tags ref-kind fields ``<SEQ>`` and string fields ``<VALUE>``.

    Pins the SEQ-vs-VALUE contract surfaced by the legend so the next help-text
    edit doesn't silently regress the distinction. ``codechange`` consumes a row
    seq via ``ref_kind`` resolution; ``label`` is free text. The
    hint paragraph also has to keep pointing users at the EDGE form when the
    embedded list isn't valid on the current row kind -- that pointer is the
    only escape hatch the help offers, and it's easy to lose in a rewrite.
    """
    run(["issue", "help"], client)
    out = capsys.readouterr().out
    list_block, _, _ = out.partition("RELATION:")
    _, _, list_block = list_block.partition("LIST:")
    # Three-column legend: name, value shape, one-line help.
    assert "codechange  <SEQ>" in list_block
    assert "label       <VALUE>" in list_block
    # Hint paragraph keeps the SEQ example and the EDGE-form escape hatch
    # (line-wrapping makes substring asserts brittle; collapse whitespace).
    flat = " ".join(list_block.split())
    assert "codechange add 7" in flat
    assert "produced codechange sha to <SHA>" in flat


def test_kind_row_help_concretizes_seq(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``trax issue 1 help`` substitutes the typed seq into help slots."""
    run(["issue", "1", "help"], client)
    out = capsys.readouterr().out
    assert "trax issue 1 " in out
    assert "trax issue SEQ" not in out
    assert not client.calls


def test_kind_field_help_shows_local_forms(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["issue", "1", "title", "help"], client)
    out = capsys.readouterr().out
    assert "Usage: trax issue 1 title [to VALUE]" in out
    assert "Bare FIELD projects it" in out
    assert not client.calls


def test_kind_field_trailing_help_shows_local_forms(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Help is a tail-action verb; '--help' is not infused anywhere."""
    run(["issue", "1", "title", "help"], client)
    out = capsys.readouterr().out
    assert "Usage: trax issue 1 title [to VALUE]" in out
    assert "Bare FIELD projects it" in out
    assert not client.calls


def test_leading_dash_help_aliases_help_verb(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--help`` is only an alias at the leading position."""
    run(["issue", "--help"], client)
    out = capsys.readouterr().out
    assert "trax issue" in out
    assert not client.calls


def test_create_hides_uuid_by_default(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``created:`` omits the UUID unless ``--show-ids`` is set."""
    cli.parse_and_run(
        ["issue", "title", "to", "hi"],
        client_factory=lambda: cast(Any, client),
    )
    out = capsys.readouterr().out
    assert out.strip() == "created: Issue#1"


def test_create_shows_uuid_with_flag(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--show-ids`` surfaces the UUID on ``created:`` lines."""
    cli.parse_and_run(
        ["--show-ids", "issue", "title", "to", "hi"],
        client_factory=lambda: cast(Any, client),
    )
    out = capsys.readouterr().out
    assert out.startswith("created: Issue#1 ")
    assert str(client.target_id) in out


def test_help_topic_unknown_verb_raises(
    client: FakeClient,
) -> None:
    """``trax help <unknown>`` raises ``ClientError``."""
    with pytest.raises(ClientError, match="unknown verb"):
        cli.parse_and_run(
            ["help", "notaverb"],
            client_factory=lambda: cast(Any, client),
        )


def test_main_handles_client_error_with_exit_2(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cli.main`` traps ``ClientError`` and exits with code 2."""
    monkeypatch.setattr(
        "sys.argv",
        ["trax", "issue", "notakindorseq"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("trax: ")


def test_main_with_explicit_argv(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``cli.main`` accepts an explicit argv override."""
    cli.main(["help"])
    out = capsys.readouterr().out
    assert "Usage: trax COMMAND" in out


def test_leading_dash_help_with_no_other_args(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``trax --help`` (no other args) prints top-level help."""
    cli.parse_and_run(["--help"], client_factory=lambda: cast(Any, client))
    out = capsys.readouterr().out
    assert "Usage: trax COMMAND" in out


def test_kind_edit_leaf_help_shows_local_forms(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["belief", "3", "judgement", "to", "proven", "help"], client)
    out = capsys.readouterr().out
    assert "Usage: trax belief 3 judgement to VALUE" in out
    assert "Mutates the selected field" in out
    assert not client.calls


def test_standalone_command_help_shows_local_forms(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["recent", "help"], client)
    out = capsys.readouterr().out
    assert "Usage: trax recent [OPTIONS]" in out
    assert "Examples:" in out
    assert not client.calls


def test_run_shim_resolves_client_from_active_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``trax run`` hands a profile-resolving client factory to the runner."""
    profile.save_profile("rprof", Profile(url="http://rhost:9100", author="bob"))
    profile.switch_profile("rprof")
    captured: dict[str, object] = {}

    def fake_run_main(argv: Sequence[str], *, client_factory: object = None) -> int:
        captured["argv"] = list(argv)
        # The factory resolves the active profile only when invoked, mirroring
        # the sync path; ``--no-sync`` never calls it.
        captured["client"] = cast(Any, client_factory)()
        return 0

    monkeypatch.setattr("trackinizer.trax.run.session.main", fake_run_main)
    cli.parse_and_run(["run", "claude", "--", "hi"])

    assert captured["argv"] == ["claude", "--", "hi"]
    resolved = cast(Any, captured["client"])
    try:
        assert str(resolved.base_url) == "http://rhost:9100"
        assert resolved.author == "bob"
    finally:
        resolved.close()


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
