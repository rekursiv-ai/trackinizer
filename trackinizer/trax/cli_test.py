from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import argparse
import subprocess
import sys

import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.trax import cli, connection
from trackinizer.trax.client_cache import Target, close_clients
from trackinizer.trax.conftest import FakeClient, run


if TYPE_CHECKING:
    from collections.abc import Sequence


_CWD: Final = Path(__file__).resolve().parent


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
        [str(_CWD / "__main__.py"), "help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Usage: trax COMMAND [ARGS] [OPTIONS]" in result.stdout
    assert "field is" not in result.stdout
    assert "field to value" in (_CWD / "__main__.py").read_text()
    assert not result.stderr


_KEY: Final = "tok-SENTINEL-93f1c7"
"""A recognisable key, so a leak is found by substring."""


@pytest.fixture(autouse=True)
def no_ambient_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub an exported ``TRACKINIZER_TOKEN`` so it cannot bleed into these tests."""
    monkeypatch.delenv("TRACKINIZER_TOKEN", raising=False)


def _resolve(*, profile_name: str | None = None, port: int | None = None) -> Target:
    return connection.resolve_target(
        argparse.Namespace(profile=profile_name, host=None, port=port),
    )


class TestTokenNeverEchoed:
    """A malformed or good ``TRACKINIZER_TOKEN`` text appears in no output."""

    @pytest.mark.parametrize(
        "token",
        [
            pytest.param(f"{_KEY}\n", id="trailing-newline"),
            pytest.param(f"{_KEY}\r\nX-Evil: 1", id="header-injection"),
            pytest.param(f"{_KEY} tail", id="embedded-space"),
            pytest.param(f"{_KEY}-\u00e9", id="non-ascii"),
            pytest.param("  ", id="blank"),
        ],
    )
    def test_a_malformed_token_is_refused_without_echoing_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        token: str,
    ) -> None:
        """A value httpx2 would reject prints in its traceback, so it never gets there."""
        monkeypatch.setenv("TRACKINIZER_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("TRACKINIZER_TOKEN", token)

        try:
            exit_code = cli.main(["issue"])
        finally:
            close_clients()
        captured = capsys.readouterr()

        assert exit_code == 2
        assert "TRACKINIZER_TOKEN" in captured.err
        assert _KEY not in captured.out + captured.err

    def test_token_text_in_no_error_or_repr(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        shown: list[str] = []
        monkeypatch.setenv("TRACKINIZER_TOKEN", _KEY)

        monkeypatch.setenv("TRACKINIZER_URL", "ftp://bad-scheme")
        with pytest.raises(ClientError) as bad_url:
            _resolve()
        shown += [str(bad_url.value), repr(bad_url.value)]

        monkeypatch.setenv("TRACKINIZER_URL", "http://127.0.0.1:1")
        try:
            client = connection.connect(
                argparse.Namespace(profile=None, host=None, port=None),
            )
            shown += [str(client), repr(client)]
            assert cli.main(["issue"]) == 2
        finally:
            close_clients()
        captured = capsys.readouterr()
        shown += [captured.out, captured.err]

        assert shown
        assert [text for text in shown if _KEY in text] == []


def test_global_flag_not_peeled_from_field_value() -> None:
    """A global flag spelling AFTER the verb is a field value, not peeled.

    ``_peel_top_flags`` must only consume connection/display flags from the
    pre-verb prefix; ``issue 7 title to --show-ids`` carries ``--show-ids`` as
    the title's VALUE, so the flag stays in ``leftover`` and ``show_ids`` is
    not set (TRAX-CLI-001).
    """
    top, leftover = cli._peel_top_flags(["issue", "7", "title", "to", "--show-ids"])
    flags = cast(cli._TopFlags, top)
    assert flags.show_ids is False
    assert leftover == ["issue", "7", "title", "to", "--show-ids"]


def test_global_flag_peeled_before_verb() -> None:
    """A global flag BEFORE the verb is still peeled (the prefix is its home)."""
    top, leftover = cli._peel_top_flags(
        ["--show-ids", "issue", "7", "title", "to", "x"],
    )
    flags = cast(cli._TopFlags, top)
    assert flags.show_ids is True
    assert leftover == ["issue", "7", "title", "to", "x"]


def test_global_value_flag_before_verb_consumes_its_value() -> None:
    """``--host H`` before the verb peels both flag and value off the prefix."""
    top, leftover = cli._peel_top_flags(
        ["--host", "example", "issue", "title", "to", "x"],
    )
    flags = cast(cli._TopFlags, top)
    assert flags.host == "example"
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
    assert cli.main(["profile"]) == 2
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
    """A lone unknown token is still an unknown verb.

    One bare word is a create form, not a filter (a filter needs an operator),
    so the parser declines it and the dispatcher's own error stands.
    """
    with pytest.raises(ClientError, match="unknown verb"):
        run(["wtf"], client)


def test_search_verb_is_gone(client: FakeClient) -> None:
    """``trax search`` no longer dispatches.

    The filter grammar subsumes it (``trax issue title re foo``), so the verb
    was removed rather than extended. ``search`` is not a filter field either,
    so the leading token no longer resolves to anything.
    """
    with pytest.raises(ClientError, match="unknown"):
        run(["search", "retry"], client)


def test_kindless_filter_lists_every_kind(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A filter with no leading kind queries every kind.

    This is what replaces ``trax search``: the leading token is a FIELD, so the
    query widens to all kinds instead of naming one. ``row`` matches the Issue
    fixture and ``experiment row`` matches the Experiment one, so a result from
    two different kinds proves the query was not narrowed to one.
    """
    run(["title", "re", "row"], client)

    out = capsys.readouterr().out
    assert "Issue#1" in out
    assert "Experiment#2" in out


def test_kindless_filter_narrows_to_kinds_owning_the_field(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A kind-specific field kindless narrows to the kinds that own it.

    ``judgement`` exists only on Belief, so ``trax judgement is proven`` is a
    Belief query -- not an error, and not a scan of every kind.
    """
    run(["judgement", "is", "proven"], client)

    assert "Belief#3" in capsys.readouterr().out


def test_verb_beats_a_same_spelled_field(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A verb still wins when a field shares its spelling.

    ``version`` is both a verb and (on no kind today) a plausible field name;
    the dispatcher must keep resolving verbs first, or adding a field could
    silently steal an existing command.
    """
    run(["version"], client)

    assert "unknown filter field" not in capsys.readouterr().out


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
        client_factory=lambda: cast(Client, client),
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
        client_factory=lambda: cast(Client, client),
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
            client_factory=lambda: cast(Client, client),
        )


def test_main_handles_client_error_with_exit_2(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cli.main`` traps ``ClientError`` and returns exit code 2."""
    monkeypatch.setattr(
        "sys.argv",
        ["trax", "issue", "notakindorseq"],
    )
    assert cli.main() == 2
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
    cli.parse_and_run(["--help"], client_factory=lambda: cast(Client, client))
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


@pytest.mark.parametrize(
    ("argv", "form"),
    [
        (["run", "claude", "--", "hi"], "run"),
        (["run", "attach", "chat-1"], "run"),
        (["--profile", "p", "--host", "h", "--port", "9", "RUN", "codex"], "run"),
        (["machine"], "machine"),
        (["MACHINE", "gx10", "connect", "--sandbox"], "machine"),
        (["machine", "gx10", "role", "to", "dev"], "machine"),
        (["job", "submit", "--", "python", "x.py", "--help"], "job"),
        (["job", "status"], "job"),
        (["job", "secret", "HF_TOKEN", "set"], "job"),
        (["help", "Machine"], "machine"),
        (["help", "job"], "job"),
    ],
)
def test_a_form_that_moved_out_of_trax_exits_2_saying_so(
    argv: list[str],
    form: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def no_client() -> Client:
        raise AssertionError("a moved form must not reach the server")

    with pytest.raises(SystemExit) as stopped:
        cli.parse_and_run(argv, client_factory=no_client)

    assert stopped.value.code == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith(f"trax {form} moved out of trax")
    assert err.count("\n") == 1


def test_help_says_what_moved_out_of_trax(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.parse_and_run(["help"])

    out = capsys.readouterr().out
    commands = out.split("Commands:")[1].splitlines()[0].split()
    assert not {"run", "machine", "job"} & set(commands)
    assert "Moved out of trax: job machine run" in out


@pytest.mark.parametrize("verb", ["RUN", "machine", "job"])
def test_a_moved_verb_prints_no_hint_about_traxs_profiles(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    """The hint names trax's profiles, which a command that left trax does not use."""
    monkeypatch.setenv("LC_TRAX_SERVER", "https://elsewhere.test")

    with pytest.raises(SystemExit):
        _ = cli.main(["--host", "h", verb, "status"])

    assert capsys.readouterr().err.startswith(f"trax {verb.lower()} moved out of trax")


def test_any_other_verb_still_prints_the_unmatched_server_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LC_TRAX_SERVER", "https://elsewhere.test")

    _ = cli.main(["profile"])

    assert "this shell came from https://elsewhere.test" in capsys.readouterr().err


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
