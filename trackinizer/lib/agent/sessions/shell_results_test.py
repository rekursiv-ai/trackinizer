"""Tests for provider-neutral shell-result lifting."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from trackinizer.lib.agent.sessions import shell_results
from trackinizer.lib.agent.sessions.shell_results import (
    lift_shell_result,
    shell_result_for_replay,
)
from trackinizer.lib.agent.sessions.udiff import render_udiff
from trackinizer.lib.agent.types.sessions import (
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    ShellCommandResult,
)


if TYPE_CHECKING:
    from trackinizer.lib.agent.sessions.shell_results import _BashNode


class _Node:
    def __init__(
        self,
        *,
        kind: str = "word",
        word: str = "",
        parts: list[_BashNode] | None = None,
        type: str = "",
        input: int | None = None,
        output: _BashNode | None = None,
        heredoc: _BashNode | None = None,
        value: str = "",
        pos: tuple[int, int] = (0, 0),
        op: str = "",
    ) -> None:
        self.kind = kind
        self.word = word
        self.parts = parts or []
        self.type = type
        self.input = input
        self.output = output or self
        self.heredoc = heredoc
        self.value = value
        self.pos = pos
        self.op = op


@pytest.mark.parametrize(
    ("script", "stdout", "expected", "written", "path"),
    [
        ("/bin/cat a.txt", "body\n", FileReadResult, None, "a.txt"),
        ("/usr/bin/head -n 2 a.txt", "first\nsecond\n", FileReadResult, None, "a.txt"),
        ("/bin/sed -n 1p a.txt", "first\n", FileReadResult, None, "a.txt"),
        ("/usr/bin/printf body > a.txt", "", FileWriteResult, "body", "a.txt"),
        ("/bin/echo more > a.txt", "", FileWriteResult, "more\n", "a.txt"),
        # A ``cd`` chain is the majority shape, not an edge case: 58.3% of
        # 1351 captured claude commands are cd-prefixed. A literal destination
        # composes, because that IS the file the command resolved.
        ("cd /w && /bin/cat a.txt", "body\n", FileReadResult, None, "/w/a.txt"),
        (
            "cd /w && /usr/bin/printf body > a.txt",
            "",
            FileWriteResult,
            "body",
            "/w/a.txt",
        ),
        # An expanded ``cd`` destination still lifts: the path reported is the
        # operand as written, so the directory never reaches the result.
        ("cd $HOME && /bin/cat a.txt", "body\n", FileReadResult, None, "a.txt"),
        ("/usr/bin/tail -n 1 a.txt", "second\n", FileReadResult, None, "a.txt"),
        ("/usr/bin/nl -ba a.txt", "     1\tbody\n", FileReadResult, None, "a.txt"),
        # Heredoc bodies verified against /bin/bash: empty, one line, and
        # several, plus a blank line inside one.
        (
            "/bin/cat > a.txt << 'EOF'\nbeta\nEOF\n",
            "",
            FileWriteResult,
            "beta\n",
            "a.txt",
        ),
        (
            "/bin/cat > a.txt << 'EOF'\nbeta\ngamma\nEOF\n",
            "",
            FileWriteResult,
            "beta\ngamma\n",
            "a.txt",
        ),
        ("/bin/cat > a.txt << 'EOF'\nEOF\n", "", FileWriteResult, "", "a.txt"),
        (
            "/bin/cat > a.txt << 'EOF'\nbeta\n\nEOF\n",
            "",
            FileWriteResult,
            "beta\n\n",
            "a.txt",
        ),
    ],
    ids=[
        "cat",
        "head",
        "sed-read",
        "printf",
        "echo",
        "cd-cat",
        "cd-printf",
        "cd-expanded",
        "tail",
        "nl",
        "heredoc",
        "heredoc-multiline",
        "heredoc-empty",
        "heredoc-blank-line",
    ],
)
def test_a_static_file_operation_lifts_and_replays(
    script: str,
    stdout: str,
    expected: type[FileReadResult | FileWriteResult | FileEditResult],
    written: str | None,
    path: str,
) -> None:
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", script),
        stdout=stdout,
        exit_code=0,
        extra={"provider": "native"},
    )

    lifted = lift_shell_result(shell)

    assert type(lifted) is expected
    assert lifted is not None
    assert lifted.path == path
    if isinstance(lifted, FileReadResult):
        assert lifted.content == stdout
    if isinstance(lifted, FileWriteResult):
        assert lifted.content == written
    assert shell_result_for_replay(lifted) == shell


@pytest.mark.parametrize(
    "script",
    [
        "cat a.txt b.txt",
        "cat a.txt | head -n 1",
        'cat "$FILE"',
        "printf body > a.txt 2> errors.txt",
        "sed -f commands.sed a.txt",
        "grep pattern a.txt",
        "sed 'w out.txt' a.txt",
        "sed 'r other.txt' a.txt",
        "sed 'e touch-side-effect' a.txt",
        "sed -i.bak s/old/new/ a.txt",
        "cat --help a.txt",
        "tail --pid 999999",
        "tail -s 1",
        "sed -l 80 p",
        "/opt/tools/cat a.txt",
        "cat --help",
        "head -n2",
        "tail --version",
        "sed -n 1p --help",
        "cat *.txt",
        "cat {a,b}.txt",
        "echo body > *.txt",
        "cat -- -",
        "head -- -",
        "tail -- -",
        "cat -n a.txt",
        # ``;`` runs the read whatever the cd did, so a zero exit proves only
        # that the read ran -- against a directory this reader cannot name.
        "cd /w; /bin/cat a.txt",
        # Only ``cd`` may precede: any other command could have WRITTEN the
        # file, making the observed content not its prior state.
        "/bin/rm a.txt && /bin/cat a.txt",
        "/bin/cat a.txt && /bin/cat b.txt",
        "cd /w /x && /bin/cat a.txt",
        # An UNQUOTED delimiter expands the body, so the bytes on disk are not
        # the bytes in the transcript.
        "/bin/cat > a.txt << EOF\n$HOME\nEOF\n",
        # A second command after the terminator is unmodelled.
        "/bin/cat > a.txt << 'EOF'\nbeta\nEOF\n/bin/rm a.txt\n",
        # ``patch`` reading a diff FILE describes an edit whose content this
        # reader never saw.
        "/usr/bin/patch a.txt < changes.diff",
        # One script over several files names no single edited path.
        "/bin/sed -i s/a/b/ a.txt b.txt",
        # ``tee`` writing several files, and a flag that changes what it does.
        "/usr/bin/tee a.txt b.txt",
        "/usr/bin/tee -p a.txt",
    ],
)
def test_an_ambiguous_or_unsupported_operation_returns_none(script: str) -> None:
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", script),
        exit_code=0,
    )

    assert lift_shell_result(shell) is None


@pytest.mark.parametrize(
    "script",
    [
        "echo -n body > a.txt",
        # A LITERAL backslash pair survives word parsing, so the content
        # would have to encode it; a single ``\c`` is consumed by the shell
        # and lifts correctly (bash writes ``c``, which is what we record).
        r"echo \\c > a.txt",
        "printf %s body > a.txt",
        r"printf \\n > a.txt",
    ],
    ids=[
        "echo-option",
        "echo-literal-backslash",
        "printf-format",
        "printf-literal-backslash",
    ],
)
def test_an_operation_without_complete_neutral_semantics_returns_none(
    script: str,
) -> None:
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", script),
        exit_code=0,
    )

    assert lift_shell_result(shell) is None


@pytest.mark.parametrize(
    ("script", "udiff"),
    [
        ("/bin/echo more >> a.txt", "+more\n"),
        ("/bin/cat >> a.txt << 'EOF'\nadded\nEOF\n", "+added\n"),
        (
            "/bin/cat >> a.txt << 'EOF'\none\ntwo\nEOF\n",
            "+one\n+two\n",
        ),
    ],
    ids=["echo", "heredoc", "heredoc-multiline"],
)
def test_an_append_lifts_to_the_lines_it_added(script: str, udiff: str) -> None:
    """``>>`` adds to a file, which is an edit rather than a write.

    The diff is one-sided because that is all the transcript holds: the file
    before the append was never recorded, so the lines added are the only
    honest rendering. Verified against ``/bin/bash`` for each form.
    """
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", script),
        exit_code=0,
    )

    lifted = lift_shell_result(shell)

    assert isinstance(lifted, FileEditResult)
    assert lifted.path == "a.txt"
    assert render_udiff(lifted.edits) == udiff


@pytest.mark.parametrize(
    ("script", "ranges"),
    [
        ("sed -n '20,40p' a.txt", ((20, 21),)),
        ("sed -n '7p' a.txt", ((7, 1),)),
        # Scattered lines in ONE command, which is why ranges is a tuple: a
        # single span could only describe this by claiming the gaps were read.
        ("sed -n '84p;101p;132p' a.txt", ((84, 1), (101, 1), (132, 1))),
        ("sed -n '1765,1771p;1847,1853p' a.txt", ((1765, 7), (1847, 7))),
        # ``$`` is the file's last line, so the count is unknowable here.
        ("sed -n '20,$p' a.txt", ((20, None),)),
        # A regex address names lines only the file itself could resolve.
        ("sed -n '/^def /,/^class /p' a.txt", ((None, None),)),
        ("head -20 a.txt", ((1, 20),)),
        ("head -n 20 a.txt", ((1, 20),)),
        # An unstated count is coreutils' default of 10, not the whole file.
        ("head a.txt", ((1, 10),)),
        ("tail a.txt", ((None, 10),)),
        # ``tail`` counts backwards from the end, so it states a count only.
        ("tail -5 a.txt", ((None, 5),)),
        ("tail -n 5 a.txt", ((None, 5),)),
        # A whole-file read bounds nothing.
        ("cat a.txt", ()),
        ("nl -ba a.txt", ()),
    ],
    ids=[
        "sed-span",
        "sed-single",
        "sed-scattered",
        "sed-two-spans",
        "sed-to-end",
        "sed-regex",
        "head-bare",
        "head-n",
        "head-default",
        "tail-default",
        "tail-bare",
        "tail-n",
        "cat",
        "nl",
    ],
)
def test_a_read_reports_the_lines_it_returned(
    script: str,
    ranges: tuple[tuple[int | None, int | None], ...],
) -> None:
    """A bounded read states WHICH lines came back, not just the file.

    Without it every partial read claims the file's whole content was
    returned, which is wrong for the most common read agents write.
    """
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", script),
        stdout="x\n",
        exit_code=0,
    )

    lifted = lift_shell_result(shell)

    assert isinstance(lifted, FileReadResult)
    assert lifted.ranges == ranges


@pytest.mark.parametrize(
    ("script", "ranges"),
    [
        # ``+N`` is a FROM-line address, not a count: measured against
        # /usr/bin/tail on an 8-line file, ``tail -n +5`` printed lines 5-8.
        # Reading it as a count reported the last 5 lines, which is a
        # different four-line window.
        ("tail -n +5 a.txt", ((5, None),)),
        # ``+0`` and ``+1`` both print the whole file -- line 0 is not a line.
        ("tail -n +0 a.txt", ((1, None),)),
        # ``-N`` on head drops the last N lines, so the read starts at line 1
        # and ends somewhere only the file's length names. Measured: ``head -n
        # -5`` on 8 lines printed lines 1-3.
        ("head -n -5 a.txt", ((1, None),)),
        # ``-N`` on tail is the count form it already had.
        ("tail -n -5 a.txt", ((None, 5),)),
    ],
    ids=["tail-from", "tail-from-zero", "head-drop", "tail-negative"],
)
def test_a_signed_count_names_the_lines_that_utility_prints(
    script: str,
    ranges: tuple[tuple[int | None, int | None], ...],
) -> None:
    """A sign changes WHICH lines come back, not merely how many.

    Both signed forms were read as plain counts, so ``tail -n +5`` claimed the
    file's last five lines and ``head -n -5`` claimed five lines from an
    unknown start -- neither of which the command printed.
    """
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", script),
        stdout="x\n",
        exit_code=0,
    )

    lifted = lift_shell_result(shell)

    assert isinstance(lifted, FileReadResult)
    assert lifted.ranges == ranges


def test_a_tab_stripped_heredoc_lifts_to_the_body_bash_writes() -> None:
    r"""``<<-`` strips leading TABS from the body and its terminator.

    Agents indent a heredoc inside a shell function or an ``if``, and the
    ``-`` form exists for exactly that. The terminator was matched literally,
    so an indented ``\tEOF`` never equalled ``EOF`` and the whole write was
    refused. Verified against /bin/bash: the file holds ``body\n``.
    """
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", "/bin/cat > a.txt <<- 'EOF'\n\tbody\n\tEOF\n"),
        exit_code=0,
    )

    lifted = lift_shell_result(shell)

    assert isinstance(lifted, FileWriteResult)
    assert lifted.path == "a.txt"
    assert lifted.content == "body\n"


def test_a_static_chdir_composes_the_path_the_read_resolved() -> None:
    """``cd sub && cat a.txt`` read ``sub/a.txt``, and says so.

    Reporting the bare operand named a file in the session's own directory --
    a different file, and often one that does not exist. The destination is
    composed only when it is literal text; ``cd $HOME`` names a directory this
    reader cannot see, so its operand stays as the command wrote it.
    """
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", "cd sub && /bin/cat a.txt"),
        stdout="body\n",
        exit_code=0,
    )

    lifted = lift_shell_result(shell)

    assert isinstance(lifted, FileReadResult)
    assert lifted.path == "sub/a.txt"
    assert shell_result_for_replay(lifted) == shell


def test_a_chdir_leaves_an_absolute_operand_alone() -> None:
    """An absolute path resolves against the root, whatever ``cd`` ran."""
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", "cd sub && /bin/cat /etc/hosts"),
        stdout="body\n",
        exit_code=0,
    )

    lifted = lift_shell_result(shell)

    assert isinstance(lifted, FileReadResult)
    assert lifted.path == "/etc/hosts"


def test_a_sed_script_that_does_more_than_print_is_not_a_read() -> None:
    """One non-printing clause refuses the whole script, never just itself."""
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", "sed -n '1,5p;6w other.txt' a.txt"),
        stdout="x\n",
        exit_code=0,
    )

    assert lift_shell_result(shell) is None


def test_a_bare_utility_name_lifts() -> None:
    """Agents write ``cat a.txt``, never ``/bin/cat a.txt``.

    Requiring the absolute path was correct about PATH but wrong about the
    corpus: it lifted ZERO of 359 captured codex shell results and zero of 588
    claude ones. A bare name is taken as the coreutils it names.
    """
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", "cat a.txt"),
        stdout="body\n",
        exit_code=0,
    )

    lifted = lift_shell_result(shell)

    assert isinstance(lifted, FileReadResult)
    assert lifted.path == "a.txt"


def test_a_utility_from_an_unexpected_directory_returns_none() -> None:
    """A path ending in the name but living elsewhere is another program."""
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", "/opt/tools/cat a.txt"),
        stdout="shadow\n",
        exit_code=0,
    )

    assert lift_shell_result(shell) is None


@pytest.mark.parametrize(
    ("script", "written"),
    [
        ("/bin/echo \\c > a.txt", "c\n"),
        ("/usr/bin/printf \\n > a.txt", "n"),
    ],
    ids=["echo", "printf"],
)
def test_a_shell_consumed_escape_lifts_to_what_the_shell_wrote(
    script: str,
    written: str,
) -> None:
    """The guard rejects a LITERAL backslash, not one the shell already ate.

    Verified against ``/bin/bash -lc``: both forms write exactly ``written``.
    The neighbouring rejection cases spell doubled backslashes, so nothing
    covered the single-backslash form the guard is phrased against.
    """
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", script),
        exit_code=0,
    )

    lifted = lift_shell_result(shell)

    assert isinstance(lifted, FileWriteResult)
    assert lifted.content == written


def test_a_failed_file_operation_returns_none() -> None:
    shell = ShellCommandResult(
        call_id="c1",
        command=("cat", "a.txt"),
        stderr="cat: a.txt: No such file\n",
        exit_code=1,
    )

    assert lift_shell_result(shell) is None


def test_an_explicit_success_cannot_override_a_recorded_failure() -> None:
    shell = ShellCommandResult(
        call_id="c1",
        command=("cat", "missing.txt"),
        exit_code=1,
    )

    assert lift_shell_result(shell, succeeded=True) is None


def test_provider_shell_source_can_supply_a_missing_argv() -> None:
    shell = ShellCommandResult(call_id="c1", stdout="body\n")

    lifted = lift_shell_result(shell, command="/bin/cat a.txt", succeeded=True)

    assert isinstance(lifted, FileReadResult)
    assert lifted.path == "a.txt"
    assert shell_result_for_replay(lifted) == shell


def test_replay_stencils_an_edited_path_into_the_original_command() -> None:
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", "/bin/cat old.txt"),
        stdout="body\n",
        exit_code=0,
    )
    lifted = lift_shell_result(shell)
    assert isinstance(lifted, FileReadResult)

    replay = shell_result_for_replay(replace(lifted, path="new name.txt"))

    assert replay is not None
    assert replay.command == ("/bin/bash", "-lc", "/bin/cat 'new name.txt'")


def test_replay_stencils_edited_write_content_into_the_original_command() -> None:
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", "/usr/bin/printf body > a.txt"),
        exit_code=0,
    )
    lifted = lift_shell_result(shell)
    assert isinstance(lifted, FileWriteResult)

    replay = shell_result_for_replay(replace(lifted, content="new"))

    assert replay is not None
    assert replay.command == (
        "/bin/bash",
        "-lc",
        "/usr/bin/printf %s new > a.txt",
    )


def test_replay_rewrites_the_path_not_a_trailing_flag() -> None:
    """A path followed by a flag is still the word a rename replaces.

    ``tee F -a`` puts the flag last, and rewriting ``command[-1]`` replaced it
    -- turning an append into a truncate while leaving the file named ``F``.
    The same value-versus-position defect the matchers carry ``(word, index)``
    pairs to avoid.
    """
    shell = ShellCommandResult(call_id="c1", command=("tee", "F", "-a"), exit_code=0)
    lifted = lift_shell_result(shell)
    assert isinstance(lifted, FileEditResult)

    replay = shell_result_for_replay(replace(lifted, path="NEW.txt"))

    assert replay is not None
    assert replay.command == ("tee", "NEW.txt", "-a")


def test_replay_uses_the_operand_position_when_argv_values_repeat() -> None:
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/cat", "/bin/cat"),
        stdout="body",
        exit_code=0,
    )
    lifted = lift_shell_result(shell)
    assert isinstance(lifted, FileReadResult)

    replay = shell_result_for_replay(replace(lifted, path="new"))

    assert replay is not None
    assert replay.command == ("/bin/cat", "new")


def test_replay_renames_the_operand_not_an_earlier_word_with_its_text() -> None:
    # The script and the file carry the same text, so a lookup by value
    # rewrote the script and left the file named as before.
    shell = ShellCommandResult(
        call_id="c1",
        command=("sed", "-i", "s/a/b/", "s/a/b/"),
        exit_code=0,
    )
    lifted = lift_shell_result(shell)
    assert isinstance(lifted, FileEditResult)

    replay = shell_result_for_replay(replace(lifted, path="x"))

    assert replay is not None
    assert replay.command == ("sed", "-i", "s/a/b/", "x")


def test_an_edited_quoted_heredoc_write_rewrites_its_content() -> None:
    script = "cat > f << 'EOF'\nold\nEOF\n"
    result = FileWriteResult(call_id="c1", path="f", content="new\n")

    assert shell_results.rewrite_shell_source(script, result) == (
        "/usr/bin/printf %s 'new\n' > f\n"
    )


def test_replay_preserves_stderr_from_the_shell_result() -> None:
    shell = ShellCommandResult(
        call_id="c1",
        command=("cat", "a.txt"),
        stderr="warning",
        exit_code=0,
    )
    lifted = lift_shell_result(shell)
    assert isinstance(lifted, FileReadResult)

    replay = shell_result_for_replay(lifted)

    assert replay is not None
    assert replay.stderr == "warning"


def test_rewrite_of_an_unrecognized_command_is_unchanged() -> None:
    result = FileReadResult(call_id="c1", path="a.txt", content="body")

    assert (
        shell_results.rewrite_shell_source("unknown a.txt", result) == "unknown a.txt"
    )


def _other_utility(executable: str) -> str:
    del executable
    return "other"


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("cd sub && echo old >| a.txt", "cd sub && /usr/bin/printf %s new >| a.txt"),
        (
            "cd sub && cat > a.txt <<- 'EOF'\n\told\n\tEOF\n",
            "cd sub && /usr/bin/printf %s new > a.txt\n",
        ),
    ],
    ids=["chdir", "tab-heredoc"],
)
def test_a_rewrite_splices_into_the_original_spans(script: str, expected: str) -> None:
    # Both halves of the old reparse: a heredoc's body and terminator are
    # replaced with the command, and the chdir prefix and trailer survive.
    result = FileWriteResult(call_id="c1", path="sub/a.txt", content="new")

    assert shell_results.rewrite_shell_source(script, result) == expected


def test_rewrite_shell_source_changes_only_a_write_content() -> None:
    result = FileWriteResult(call_id="c1", path="a.txt", content="new")

    assert shell_results.rewrite_shell_source("echo old > a.txt", result) == (
        "/usr/bin/printf %s new > a.txt"
    )


@pytest.mark.parametrize(
    "script",
    [
        "cat < a.txt",
        "cat a.txt | head -n 1",
        "cd /w; cat a.txt",
        "cd /w /x && cat a.txt",
        "cat a.txt && cat b.txt",
        "cat",
        "cat -n a.txt",
        "head -n nope a.txt",
        "head -n 2 -a.txt",
        "sed -n '0p' a.txt",
        "sed -n '' a.txt",
        "sed -n '1w out.txt' a.txt",
        "nl -ba -a.txt",
        "patch a.diff b.txt",
        "sed -i s/a/b/",
        "sed -i s/a/b/ a.txt b.txt",
        "cat > a.txt 2> errors.txt",
        "printf body > a.txt 2> errors.txt",
        "tee a.txt b.txt",
        "tee -p a.txt",
    ],
)
def test_unsupported_shell_shapes_are_not_lifted(script: str) -> None:
    shell = ShellCommandResult(
        call_id="c1",
        command=("bash", "-lc", script),
        exit_code=0,
    )

    assert lift_shell_result(shell) is None


def test_patch_heredoc_carries_the_inline_diff() -> None:
    shell = ShellCommandResult(
        call_id="c1",
        command=(
            "bash",
            "-lc",
            "/usr/bin/patch a.txt << 'EOF'\n@@ -1 +1 @@\n-a\n+b\nEOF\n",
        ),
        exit_code=0,
    )

    lifted = lift_shell_result(shell)

    assert isinstance(lifted, FileEditResult)
    assert render_udiff(lifted.edits) == "@@ -1 +1 @@\n-a\n+b\n"


@pytest.mark.parametrize(
    "script",
    [
        "bash -x echo",
        "echo hi > a.txt 2> e.txt",
        "echo hi < a.txt",
        "sed --in-place s/a/b/ a.txt",
        "sed -i.bak s/a/b/ a.txt",
        "sed -i -e s/a/b/",
        "patch a.txt b.txt",
        "head -n nope a.txt",
        "head -n 2 -a.txt",
        "sed -n '$p' a.txt",
        "sed -n '1,0p' a.txt",
    ],
)
def test_rewriting_unsupported_shapes_preserves_the_source(script: str) -> None:
    result = FileReadResult(call_id="c1", path="a.txt", content="body")

    assert shell_results.rewrite_shell_source(script, result) == script


def test_rewrite_stencils_a_shell_command_with_an_edited_path() -> None:
    result = FileReadResult(call_id="c1", path="new.txt", content="body")

    assert shell_results.rewrite_shell_source("cat old.txt", result) == "cat new.txt"
    assert shell_results._stencil_command(
        ("bash", "-lc", "unknown"),
        result,
    ) == ("bash", "-lc", "unknown")


def test_shell_source_rejects_a_shell_without_a_c_flag() -> None:
    assert shell_results._shell_source(("bash", "-x", "echo")) is None
    assert shell_results._shell_source(None) is None


def test_unknown_utility_is_not_a_file_operation() -> None:
    node = _Node(kind="word", word="unknown", parts=[], pos=(0, 7))
    assert shell_results._matched_row([node], quoted=None) is None
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(shell_results, "_standard_utility", _other_utility)
        assert shell_results._matched_row([node], quoted=None) is None


def test_matched_row_rejects_non_command_nodes() -> None:
    node = _Node(kind="operator", word="", parts=[], pos=(0, 1))
    assert shell_results._matched_row([node], quoted=None) is None


def test_simple_command_rejects_compounds_and_invalid_cd_prefixes() -> None:
    assert shell_results._simple_command("cd /w && cat a && cat b") is None
    assert shell_results._simple_command("cd >log && cat a") is None
    assert shell_results._simple_command("a;b") is None


def test_quoted_heredoc_requires_a_terminator() -> None:
    assert shell_results._quoted_heredoc("cat > a.txt << 'EOF'\nbody\n") is None


def test_heredoc_write_rejects_non_cat_and_invalid_body() -> None:
    redirect = _Node(
        kind="redirect",
        type=">",
        input=None,
        output=_Node(pos=(0, 1)),
    )
    word = _Node(kind="word", word="echo", parts=[], pos=(0, 4))

    assert shell_results._heredoc_write([word, redirect], literal=True) is None
    assert shell_results._heredoc_write([word], literal=True) is None


def test_heredoc_write_rejects_missing_delimiter_and_body() -> None:
    output = _Node(kind="word", word="a", parts=[], pos=(0, 1))
    document = _Node(
        kind="redirect",
        type="<<",
        input=None,
        output=output,
        heredoc=None,
    )
    cat = _Node(kind="word", word="cat", parts=[], pos=(0, 3))
    assert shell_results._heredoc_write([cat, document], literal=True) is None
    document.heredoc = _Node(value="body")
    document.output.word = "EOF"
    assert shell_results._heredoc_write([cat, document], literal=True) is None

    echo = _Node(kind="word", word="echo", parts=[], pos=(0, 4))
    assert shell_results._heredoc_write([echo, document], literal=True) is None
    document.output.word = "-"
    target = _Node(
        kind="redirect",
        type=">",
        input=None,
        output=_Node(kind="word", word="a", parts=[], pos=(0, 1)),
    )
    assert shell_results._heredoc_write([cat, target, document], literal=True) is None
    document.output.word = "EOF"
    assert shell_results._heredoc_write([cat, target, document], literal=True) is None


def test_single_output_rejects_other_descriptors_and_redirects() -> None:
    output = _Node(kind="word", word="a", parts=[], pos=(0, 1))
    descriptor = _Node(input=2, type=">", output=output)
    unknown = _Node(input=None, type="<", output=output)

    assert shell_results._single_output([descriptor]) is None
    assert shell_results._single_output([unknown]) is None


def test_patch_helpers_reject_invalid_operands() -> None:
    word = _Node(kind="word", word="patch", parts=[], pos=(0, 5))
    path = _Node(kind="word", word="-x", parts=[], pos=(6, 8))
    target = _Node(kind="word", word="a", parts=[], pos=(6, 7))

    assert shell_results._patch_heredoc([word], body="diff") is None
    assert shell_results._patch_heredoc([word, path], body="diff") is None
    assert shell_results._patch_operation(["patch", "a", "b"], [word], []) is None
    assert shell_results._patch_operation(["patch", "a"], [word, target], []) == (
        "rewrite",
        "a",
        (6, 7),
        "",
        (),
    )


def test_line_reader_supports_lines_equals_and_rejects_dash_paths() -> None:
    path = _Node(pos=(0, 1))
    count = _Node(pos=(0, 1))
    nodes = [_Node(pos=(0, 1)) for _ in range(3)]
    assert shell_results._line_reader_operation(
        "head",
        ["head", "--lines=2", "a"],
        nodes,
    ) == ("read", "a", nodes[2].pos, None, ((1, 2),))
    assert (
        shell_results._line_reader_operation(
            "head",
            ["head", "-x"],
            [path, count],
        )
        is None
    )
    assert shell_results._line_reader_operation(
        "head",
        ["head", "a"],
        [path, count],
    ) == ("read", "a", count.pos, None, ((1, 10),))
    assert shell_results._simple_command("") is None

    class Parser:
        def parse(self, _: str) -> list[object]:
            return []

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(shell_results, "bashlex", Parser())
        assert shell_results._simple_command("anything") is None


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
