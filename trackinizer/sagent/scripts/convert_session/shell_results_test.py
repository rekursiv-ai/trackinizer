"""Tests for provider-neutral shell-result lifting."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from trackinizer.sagent.scripts.convert_session import shell_results
from trackinizer.sagent.scripts.convert_session.shell_results import (
    lift_shell_result,
    shell_result_for_replay,
)
from trackinizer.sagent.scripts.convert_session.udiff import parse_udiff, render_udiff
from trackinizer.sagent.session.types import (
    BashResult,
    EditResult,
    LineSpan,
    ReadResult,
    Splice,
    ToolResult,
    WriteResult,
)


if TYPE_CHECKING:
    from treekle import PlainTree

    from trackinizer.sagent.scripts.convert_session.shell_results import _BashNode


def _typed_result[T](result: ToolResult | None, kind: type[T]) -> T:
    """Return ``result.result`` after asserting it is an instance of ``kind``."""
    assert result is not None
    tool_result = result.result
    assert isinstance(tool_result, kind)
    return tool_result


class _Node:
    def __init__(
        self,
        *,
        kind: str = "word",
        word: str = "",
        parts: list[_BashNode] | None = None,
        type: str = "",
        input: int | None = None,
        output: _BashNode | int | str | None = None,
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
        ("/bin/cat a.txt", "body\n", ReadResult, None, "a.txt"),
        ("/usr/bin/head -n 2 a.txt", "first\nsecond\n", ReadResult, None, "a.txt"),
        ("/bin/sed -n 1p a.txt", "first\n", ReadResult, None, "a.txt"),
        ("/usr/bin/printf body > a.txt", "", WriteResult, "body", "a.txt"),
        ("/bin/echo more > a.txt", "", WriteResult, "more\n", "a.txt"),
        # A ``cd`` chain is the majority shape, not an edge case: 58.3% of
        # 1351 captured claude commands are cd-prefixed. A literal destination
        # composes, because that IS the file the command resolved.
        ("cd /w && /bin/cat a.txt", "body\n", ReadResult, None, "/w/a.txt"),
        (
            "cd /w && /usr/bin/printf body > a.txt",
            "",
            WriteResult,
            "body",
            "/w/a.txt",
        ),
        # An expanded ``cd`` destination still lifts: the path reported is the
        # operand as written, so the directory never reaches the result.
        ("cd $HOME && /bin/cat a.txt", "body\n", ReadResult, None, "a.txt"),
        ("/usr/bin/tail -n 1 a.txt", "second\n", ReadResult, None, "a.txt"),
        ("/usr/bin/nl -ba a.txt", "     1\tbody\n", ReadResult, None, "a.txt"),
        # Heredoc bodies verified against /bin/bash: empty, one line, and
        # several, plus a blank line inside one.
        (
            "/bin/cat > a.txt << 'EOF'\nbeta\nEOF\n",
            "",
            WriteResult,
            "beta\n",
            "a.txt",
        ),
        (
            "/bin/cat > a.txt << 'EOF'\nbeta\ngamma\nEOF\n",
            "",
            WriteResult,
            "beta\ngamma\n",
            "a.txt",
        ),
        ("/bin/cat > a.txt << 'EOF'\nEOF\n", "", WriteResult, "", "a.txt"),
        (
            "/bin/cat > a.txt << 'EOF'\nbeta\n\nEOF\n",
            "",
            WriteResult,
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
    expected: type[ReadResult | WriteResult | EditResult],
    written: str | None,
    path: str,
) -> None:
    shell = ToolResult(
        call_id="c1",
        state="done",
        stdout=(stdout,) if stdout else (),
        result=BashResult(command=("/bin/bash", "-lc", script), exit_code=0),
        extra={"provider": "native"},
    )

    lifted = lift_shell_result(shell)

    assert lifted is not None
    tool_result = lifted.result
    assert type(tool_result) is expected
    assert tool_result is not None
    assert tool_result.path == path
    if isinstance(tool_result, ReadResult):
        assert (
            "".join(text for text in lifted.stdout if isinstance(text, str)) == stdout
        )
    if isinstance(tool_result, WriteResult):
        assert tool_result.content == written
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
    shell = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=("/bin/bash", "-lc", script), exit_code=0),
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
    shell = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=("/bin/bash", "-lc", script), exit_code=0),
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
    shell = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=("/bin/bash", "-lc", script), exit_code=0),
    )

    lifted = lift_shell_result(shell)

    assert lifted is not None
    assert _typed_result(lifted, EditResult).path == "a.txt"
    assert (
        render_udiff(tuple(item for item in lifted.stdout if isinstance(item, Splice)))
        == udiff
    )


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
    shell = ToolResult(
        call_id="c1",
        state="done",
        stdout=("x\n",),
        result=BashResult(command=("/bin/bash", "-lc", script), exit_code=0),
    )

    lifted = lift_shell_result(shell)

    assert lifted is not None
    assert isinstance(lifted.result, ReadResult)
    assert (
        tuple(
            (span.start, span.count)
            for span in tuple(
                item for item in lifted.stdout if isinstance(item, LineSpan)
            )
        )
        == ranges
    )


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
    shell = ToolResult(
        call_id="c1",
        state="done",
        stdout=("x\n",),
        result=BashResult(command=("/bin/bash", "-lc", script), exit_code=0),
    )

    lifted = lift_shell_result(shell)

    assert lifted is not None
    assert isinstance(lifted.result, ReadResult)
    assert (
        tuple(
            (span.start, span.count)
            for span in tuple(
                item for item in lifted.stdout if isinstance(item, LineSpan)
            )
        )
        == ranges
    )


def test_a_tab_stripped_heredoc_lifts_to_the_body_bash_writes() -> None:
    r"""``<<-`` strips leading TABS from the body and its terminator.

    Agents indent a heredoc inside a shell function or an ``if``, and the
    ``-`` form exists for exactly that. The terminator was matched literally,
    so an indented ``\tEOF`` never equalled ``EOF`` and the whole write was
    refused. Verified against /bin/bash: the file holds ``body\n``.
    """
    shell = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(
            command=("/bin/bash", "-lc", "/bin/cat > a.txt <<- 'EOF'\n\tbody\n\tEOF\n"),
            exit_code=0,
        ),
    )

    lifted = lift_shell_result(shell)

    tool_result = _typed_result(lifted, WriteResult)
    assert tool_result.path == "a.txt"
    assert tool_result.content == "body\n"


def test_a_static_chdir_composes_the_path_the_read_resolved() -> None:
    """``cd sub && cat a.txt`` read ``sub/a.txt``, and says so.

    Reporting the bare operand named a file in the session's own directory --
    a different file, and often one that does not exist. The destination is
    composed only when it is literal text; ``cd $HOME`` names a directory this
    reader cannot see, so its operand stays as the command wrote it.
    """
    shell = ToolResult(
        call_id="c1",
        state="done",
        stdout=("body\n",),
        result=BashResult(
            command=("/bin/bash", "-lc", "cd sub && /bin/cat a.txt"),
            exit_code=0,
        ),
    )

    lifted = lift_shell_result(shell)

    assert lifted is not None
    assert _typed_result(lifted, ReadResult).path == "sub/a.txt"
    assert shell_result_for_replay(lifted) == shell


def test_a_chdir_leaves_an_absolute_operand_alone() -> None:
    """An absolute path resolves against the root, whatever ``cd`` ran."""
    shell = ToolResult(
        call_id="c1",
        state="done",
        stdout=("body\n",),
        result=BashResult(
            command=("/bin/bash", "-lc", "cd sub && /bin/cat /etc/hosts"),
            exit_code=0,
        ),
    )

    lifted = lift_shell_result(shell)

    assert _typed_result(lifted, ReadResult).path == "/etc/hosts"


def test_a_sed_script_that_does_more_than_print_is_not_a_read() -> None:
    """One non-printing clause refuses the whole script, never just itself."""
    shell = ToolResult(
        call_id="c1",
        state="done",
        stdout=("x\n",),
        result=BashResult(
            command=("/bin/bash", "-lc", "sed -n '1,5p;6w other.txt' a.txt"),
            exit_code=0,
        ),
    )

    assert lift_shell_result(shell) is None


def test_a_bare_utility_name_lifts() -> None:
    """Agents write ``cat a.txt``, never ``/bin/cat a.txt``.

    Requiring the absolute path was correct about PATH but wrong about the
    corpus: it lifted ZERO of 359 captured codex shell results and zero of 588
    claude ones. A bare name is taken as the coreutils it names.
    """
    shell = ToolResult(
        call_id="c1",
        state="done",
        stdout=("body\n",),
        result=BashResult(command=("/bin/bash", "-lc", "cat a.txt"), exit_code=0),
    )

    lifted = lift_shell_result(shell)

    assert _typed_result(lifted, ReadResult).path == "a.txt"


@pytest.mark.parametrize("path", ["XX.txt", "a{b.txt", "a}b.txt"])
def test_a_word_bash_does_not_expand_is_a_literal_path(path: str) -> None:
    """Only a glob character or a ``{...}`` PAIR expands; a lone brace is literal."""
    shell = ToolResult(
        call_id="c1",
        state="done",
        stdout=("body\n",),
        result=BashResult(command=("/bin/bash", "-lc", f"cat {path}"), exit_code=0),
    )

    lifted = lift_shell_result(shell)

    assert _typed_result(lifted, ReadResult).path == path


def test_a_utility_from_an_unexpected_directory_returns_none() -> None:
    """A path ending in the name but living elsewhere is another program."""
    shell = ToolResult(
        call_id="c1",
        state="done",
        stdout=("shadow\n",),
        result=BashResult(
            command=("/bin/bash", "-lc", "/opt/tools/cat a.txt"),
            exit_code=0,
        ),
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
    shell = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=("/bin/bash", "-lc", script), exit_code=0),
    )

    lifted = lift_shell_result(shell)

    assert _typed_result(lifted, WriteResult).content == written


def test_a_failed_file_operation_returns_none() -> None:
    shell = ToolResult(
        call_id="c1",
        state="done",
        stderr=("cat: a.txt: No such file\n",),
        result=BashResult(command=("cat", "a.txt"), exit_code=1),
    )

    assert lift_shell_result(shell) is None


def test_an_explicit_success_cannot_override_a_recorded_failure() -> None:
    shell = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=("cat", "missing.txt"), exit_code=1),
    )

    assert lift_shell_result(shell, succeeded=True) is None


def test_provider_shell_source_can_supply_a_missing_argv() -> None:
    shell = ToolResult(
        call_id="c1",
        state="done",
        stdout=("body\n",),
        result=BashResult(),
    )

    lifted = lift_shell_result(shell, command="/bin/cat a.txt", succeeded=True)

    assert lifted is not None
    assert _typed_result(lifted, ReadResult).path == "a.txt"
    assert shell_result_for_replay(lifted) == shell


def test_replay_stencils_an_edited_path_into_the_original_command() -> None:
    shell = ToolResult(
        call_id="c1",
        state="done",
        stdout=("body\n",),
        result=BashResult(
            command=("/bin/bash", "-lc", "/bin/cat old.txt"),
            exit_code=0,
        ),
    )
    lifted = lift_shell_result(shell)
    assert lifted is not None
    tool_result = _typed_result(lifted, ReadResult)

    edited = replace(lifted, result=replace(tool_result, path="new name.txt"))
    replay = _typed_result(shell_result_for_replay(edited), BashResult)

    assert replay.command == ("/bin/bash", "-lc", "/bin/cat 'new name.txt'")


def test_replay_stencils_edited_write_content_into_the_original_command() -> None:
    shell = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(
            command=("/bin/bash", "-lc", "/usr/bin/printf body > a.txt"),
            exit_code=0,
        ),
    )
    lifted = lift_shell_result(shell)
    assert lifted is not None
    tool_result = _typed_result(lifted, WriteResult)

    edited = replace(lifted, result=replace(tool_result, content="new"))
    replay = _typed_result(shell_result_for_replay(edited), BashResult)

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
    shell = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=("tee", "F", "-a"), exit_code=0),
    )
    lifted = lift_shell_result(shell)
    assert lifted is not None
    tool_result = _typed_result(lifted, EditResult)

    edited = replace(lifted, result=replace(tool_result, path="NEW.txt"))
    replay = _typed_result(shell_result_for_replay(edited), BashResult)

    assert replay.command == ("tee", "NEW.txt", "-a")


def test_replay_uses_the_operand_position_when_argv_values_repeat() -> None:
    shell = ToolResult(
        call_id="c1",
        state="done",
        stdout=("body",),
        result=BashResult(command=("/bin/cat", "/bin/cat"), exit_code=0),
    )
    lifted = lift_shell_result(shell)
    assert lifted is not None
    tool_result = _typed_result(lifted, ReadResult)

    edited = replace(lifted, result=replace(tool_result, path="new"))
    replay = _typed_result(shell_result_for_replay(edited), BashResult)

    assert replay.command == ("/bin/cat", "new")


def test_replay_renames_the_operand_not_an_earlier_word_with_its_text() -> None:
    # The script and the file carry the same text, so a lookup by value
    # rewrote the script and left the file named as before.
    shell = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=("sed", "-i", "s/a/b/", "s/a/b/"), exit_code=0),
    )
    lifted = lift_shell_result(shell)
    assert lifted is not None
    tool_result = _typed_result(lifted, EditResult)

    edited = replace(lifted, result=replace(tool_result, path="x"))
    replay = _typed_result(shell_result_for_replay(edited), BashResult)

    assert replay.command == ("sed", "-i", "s/a/b/", "x")


def test_an_edited_quoted_heredoc_write_rewrites_its_content() -> None:
    script = "cat > f << 'EOF'\nold\nEOF\n"
    result = WriteResult(path="f", content="new\n")

    assert shell_results.rewrite_shell_source(script, result) == (
        "/usr/bin/printf %s 'new\n' > f\n"
    )


def test_a_write_keeps_the_stdout_it_printed_and_a_read_leaves_it_to_content() -> None:
    """A write's own output survives replay; a read's IS the file, so it is not kept.

    Storing a read's stdout in ``$shell`` too would hold the file's bytes twice.
    """
    write = ToolResult(
        call_id="c1",
        state="done",
        stdout=("echoed\n",),
        result=BashResult(command=("/usr/bin/tee", "a.txt"), exit_code=0),
    )
    read = ToolResult(
        call_id="c1",
        state="done",
        stdout=("echoed\n",),
        result=BashResult(command=("/bin/cat", "a.txt"), exit_code=0),
    )
    lifted_write = lift_shell_result(write)
    lifted_read = lift_shell_result(read)
    assert lifted_write is not None
    assert lifted_read is not None

    assert shell_result_for_replay(lifted_write) == write
    assert dict(lifted_read.extra)["$shell"] == {
        "command": ("/bin/cat", "a.txt"),
        "exit_code": 0,
    }
    assert shell_result_for_replay(lifted_read) == read


def test_a_native_file_result_has_no_shell_to_replay() -> None:
    assert (
        shell_result_for_replay(
            ToolResult(call_id="c1", state="done", result=ReadResult(path="a")),
        )
        is None
    )


def test_a_result_that_is_not_a_file_result_has_no_shell_to_replay() -> None:
    shell = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(),
        extra={"$shell": {"command": ["ls"]}},
    )

    assert shell_result_for_replay(shell) is None


def test_replay_preserves_stderr_from_the_shell_result() -> None:
    shell = ToolResult(
        call_id="c1",
        state="done",
        stderr=("warning",),
        result=BashResult(command=("cat", "a.txt"), exit_code=0),
    )
    lifted = lift_shell_result(shell)
    assert lifted is not None
    assert isinstance(lifted.result, ReadResult)

    replay = shell_result_for_replay(lifted)

    assert replay is not None
    assert (replay.stdout, replay.stderr) == ((), ("warning",))


def test_rewrite_of_an_unrecognized_command_is_unchanged() -> None:
    result = ReadResult(path="a.txt")

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
    result = WriteResult(path="sub/a.txt", content="new")

    assert shell_results.rewrite_shell_source(script, result) == expected


def test_rewrite_shell_source_changes_only_a_write_content() -> None:
    result = WriteResult(path="a.txt", content="new")

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
    shell = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=("bash", "-lc", script), exit_code=0),
    )

    assert lift_shell_result(shell) is None


def test_patch_heredoc_carries_the_inline_diff() -> None:
    shell = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(
            command=(
                "bash",
                "-lc",
                "/usr/bin/patch a.txt << 'EOF'\n@@ -1 +1 @@\n-a\n+b\nEOF\n",
            ),
            exit_code=0,
        ),
    )

    lifted = lift_shell_result(shell)

    assert lifted is not None
    assert isinstance(lifted.result, EditResult)
    assert (
        render_udiff(tuple(item for item in lifted.stdout if isinstance(item, Splice)))
        == "@@ -1 +1 @@\n-a\n+b\n"
    )


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
    result = ReadResult(path="a.txt")

    assert shell_results.rewrite_shell_source(script, result) == script


def test_rewrite_stencils_a_shell_command_with_an_edited_path() -> None:
    result = ReadResult(path="new.txt")

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
    output.word = "EOF"
    assert shell_results._heredoc_write([cat, document], literal=True) is None

    echo = _Node(kind="word", word="echo", parts=[], pos=(0, 4))
    assert shell_results._heredoc_write([echo, document], literal=True) is None
    output.word = "-"
    target = _Node(
        kind="redirect",
        type=">",
        input=None,
        output=_Node(kind="word", word="a", parts=[], pos=(0, 1)),
    )
    assert shell_results._heredoc_write([cat, target, document], literal=True) is None
    output.word = "EOF"
    assert shell_results._heredoc_write([cat, target, document], literal=True) is None


def test_single_output_rejects_other_descriptors_and_redirects() -> None:
    output = _Node(kind="word", word="a", parts=[], pos=(0, 1))
    descriptor = _Node(input=2, type=">", output=output)
    unknown = _Node(input=None, type="<", output=output)

    assert shell_results._single_output([descriptor]) is None
    assert shell_results._single_output([unknown]) is None


def test_file_and_document_redirects_require_a_word_operand() -> None:
    cat = _Node(kind="word", word="cat", parts=[], pos=(0, 3))
    target = _Node(
        kind="redirect",
        type=">",
        input=None,
        output=_Node(kind="word", word="a", parts=[], pos=(0, 1)),
    )
    descriptor = _Node(kind="redirect", input=None, type=">", output=2)
    document = _Node(kind="redirect", input=None, type="<<", output=2)

    message = r"^Expected not isinstance\(output, int \| str\)\.$"
    with pytest.raises(TypeError, match=message):
        shell_results._single_output([descriptor])
    with pytest.raises(TypeError, match=message):
        shell_results._heredoc_write([cat, target, document], literal=True)


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
        None,
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


def _lifted(script: str, *, stdout: str = "") -> ToolResult | None:
    """Return what a successful bash execution of ``script`` lifts to."""
    return lift_shell_result(
        ToolResult(
            call_id="c1",
            state="done",
            stdout=(stdout,) if stdout else (),
            result=BashResult(command=("bash", "-lc", script), exit_code=0),
        ),
    )


def test_each_lifted_kind_carries_every_field_of_its_shell_result() -> None:
    """Position, time, call, path and residual all travel; the shell rides along."""

    def extra(script: str, *, stdout: bool) -> dict[str, PlainTree]:
        replay: dict[str, PlainTree] = {"command": ("bash", "-lc", script)}
        if stdout:
            replay["stdout"] = "out"
        return {"k": 1, "$shell": replay | {"stderr": "err", "exit_code": 0}}

    patch = "patch a.txt << 'EOF'\n@@ -1 +1 @@\n-a\n+b\nEOF\n"

    assert lift_shell_result(
        ToolResult(
            context_id=3,
            timestamp="t",
            call_id="c9",
            state="done",
            stdout=("out",),
            stderr=("err",),
            result=BashResult(command=("bash", "-lc", "cat a.txt"), exit_code=0),
            extra={"k": 1},
        ),
    ) == ToolResult(
        context_id=3,
        timestamp="t",
        call_id="c9",
        state="done",
        stdout=("out",),
        result=ReadResult(path="a.txt"),
        extra=extra("cat a.txt", stdout=False),
    )
    assert lift_shell_result(
        ToolResult(
            context_id=3,
            timestamp="t",
            call_id="c9",
            state="done",
            stdout=("out",),
            stderr=("err",),
            result=BashResult(
                command=("bash", "-lc", "printf hi > a.txt"),
                exit_code=0,
            ),
            extra={"k": 1},
        ),
    ) == ToolResult(
        context_id=3,
        timestamp="t",
        call_id="c9",
        state="done",
        stdout=("Wrote a.txt.",),
        result=WriteResult(path="a.txt", content="hi"),
        extra=extra("printf hi > a.txt", stdout=True),
    )
    assert lift_shell_result(
        ToolResult(
            context_id=3,
            timestamp="t",
            call_id="c9",
            state="done",
            stdout=("out",),
            stderr=("err",),
            result=BashResult(command=("bash", "-lc", "echo hi >> a.txt"), exit_code=0),
            extra={"k": 1},
        ),
    ) == ToolResult(
        context_id=3,
        timestamp="t",
        call_id="c9",
        state="done",
        stdout=("Edited a.txt.", Splice(before="", after="hi\n")),
        result=EditResult(path="a.txt"),
        extra=extra("echo hi >> a.txt", stdout=True),
    )
    assert lift_shell_result(
        ToolResult(
            context_id=3,
            timestamp="t",
            call_id="c9",
            state="done",
            stdout=("out",),
            stderr=("err",),
            result=BashResult(command=("bash", "-lc", patch), exit_code=0),
            extra={"k": 1},
        ),
    ) == ToolResult(
        context_id=3,
        timestamp="t",
        call_id="c9",
        state="done",
        stdout=("Edited a.txt.", *parse_udiff("@@ -1 +1 @@\n-a\n+b\n")),
        result=EditResult(path="a.txt"),
        extra=extra(patch, stdout=True),
    )
    assert lift_shell_result(
        ToolResult(
            context_id=3,
            timestamp="t",
            call_id="c9",
            state="done",
            stdout=("out",),
            stderr=("err",),
            result=BashResult(
                command=("bash", "-lc", "sed -i s/a/b/ a.txt"),
                exit_code=0,
            ),
            extra={"k": 1},
        ),
    ) == ToolResult(
        context_id=3,
        timestamp="t",
        call_id="c9",
        state="done",
        stdout=("Edited a.txt.",),
        result=EditResult(path="a.txt"),
        extra=extra("sed -i s/a/b/ a.txt", stdout=True),
    )


@pytest.mark.parametrize(
    ("script", "act"),
    [
        ("cat a.txt", "read"),
        ("printf hi > a.txt", "write"),
        ("echo hi >> a.txt", "append"),
        ("patch a.txt << 'EOF'\n@@ -1 +1 @@\n-a\n+b\nEOF\n", "patched"),
        ("sed -i s/a/b/ a.txt", "rewrite"),
        ("perl -pi -e s/a/b/ a.txt", "rewrite"),
        ("tee a.txt", "rewrite"),
        ("patch a.txt", "rewrite"),
    ],
)
def test_each_row_names_the_act_it_performed(script: str, act: str) -> None:
    operation = shell_results._operation(script)

    assert operation is not None
    assert operation.act == act


def test_an_empty_append_states_no_splice() -> None:
    lifted = _lifted("cat >> a.txt << 'EOF'\nEOF\n")

    assert lifted is not None
    assert isinstance(lifted.result, EditResult)
    assert tuple(item for item in lifted.stdout if isinstance(item, Splice)) == ()


def test_a_result_with_no_command_and_no_script_lifts_nothing() -> None:
    shell = ToolResult(call_id="c1", state="done", result=BashResult(exit_code=0))
    unparsed = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=("bash", "-x", "cat a"), exit_code=0),
    )

    assert lift_shell_result(shell) is None
    assert lift_shell_result(unparsed) is None


def test_a_pending_result_lifts_nothing() -> None:
    shell = ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=("cat", "a.txt"), exit_code=0),
    )
    assert lift_shell_result(shell) is not None

    pending = replace(shell, state="pending", result=None)

    assert lift_shell_result(pending, succeeded=True) is None


def test_replay_rebuilds_every_field_and_reads_bad_output_as_none() -> None:
    read = ToolResult(
        context_id=3,
        timestamp="t",
        call_id="c9",
        state="done",
        result=ReadResult(path="a.txt"),
        extra={"$shell": {"command": ["cat", "a.txt"]}, "k": 1},
    )
    write = ToolResult(
        call_id="c9",
        state="done",
        stdout=("Wrote a.txt.",),
        result=WriteResult(path="a.txt", content="x"),
        extra={"$shell": {"stdout": 5, "stderr": 7, "exit_code": "0"}},
    )
    bare = replace(write, extra={"$shell": {}})

    assert shell_result_for_replay(read) == ToolResult(
        context_id=3,
        timestamp="t",
        call_id="c9",
        state="done",
        result=BashResult(command=("cat", "a.txt")),
        extra={"k": 1},
    )
    assert shell_result_for_replay(write) == ToolResult(
        call_id="c9",
        state="done",
        result=BashResult(),
    )
    assert shell_result_for_replay(bare) == ToolResult(
        call_id="c9",
        state="done",
        result=BashResult(),
    )


@pytest.mark.parametrize(
    "shell",
    [
        "bash",
        "dash",
        "ksh",
        "sh",
        "zsh",
        "/bin/bash",
        "/bin/dash",
        "/bin/ksh",
        "/bin/sh",
        "/bin/zsh",
        "/usr/bin/bash",
        "/usr/bin/dash",
        "/usr/bin/ksh",
        "/usr/bin/sh",
        "/usr/bin/zsh",
    ],
)
def test_each_shell_runs_the_script_its_c_flag_names(shell: str) -> None:
    assert shell_results._shell_source((shell, "-c", "cat a")) == ("cat a", 2)


def test_only_a_short_option_bundles_the_c_flag() -> None:
    assert shell_results._shell_source(("bash", "-lc", "cat a")) == ("cat a", 2)
    assert shell_results._shell_source(("bash", "--rc", "cat a")) is None


@pytest.mark.parametrize(
    ("script", "path"),
    [
        ("sed --in-place s/a/b/ a.txt", "a.txt"),
        ("sed s/a/b/ -i a.txt", "a.txt"),
        ("perl -I. -pi -e s/a/b/ a.txt", "a.txt"),
        ("perl -pi -e s/a/b/ a.txt", "a.txt"),
        ("sed -i -e s/x/y/ -e s/a/b/ a.txt", "a.txt"),
        ("sed -i --expression s/x/y/ --expression s/a/b/ a.txt", "a.txt"),
        ("sed -i a.txt -e s/a/b/", "a.txt"),
        ("patch -p1 a.txt", "a.txt"),
        ("tee --append a.txt", "a.txt"),
        ("/usr/local/bin/tee a.txt", "a.txt"),
    ],
)
def test_each_rewrite_form_names_its_file(script: str, path: str) -> None:
    lifted = _lifted(script)

    assert lifted is not None
    assert (
        _typed_result(lifted, EditResult).path,
        tuple(item for item in lifted.stdout if isinstance(item, Splice)),
    ) == (
        path,
        (),
    )


@pytest.mark.parametrize(
    "script",
    [
        "sed -i=bak s/a/b/ a.txt",
        "sed --quiet s/a/b/ a.txt",
        "cat a.txt > b.txt << 'EOF'\nx\nEOF\n",
        "cat $F a.txt",
        "nl -x a.txt",
        "head -n 5 a b",
        "head --lines==5 a.txt",
        "head --lines=5=6 a.txt",
        "head -5 a b",
        "cd a && cat b;",
        "printf a%b > a.txt",
    ],
)
def test_a_form_one_word_off_a_row_lifts_nothing(script: str) -> None:
    assert _lifted(script) is None


@pytest.mark.parametrize(
    ("script", "ranges"),
    [
        ("head --lines 5 a.txt", ((1, 5),)),
        ("head -n5 a.txt", ((1, 5),)),
        ("sed -n '$p' a.txt", ((None, 1),)),
        ("sed -n '$,$p' a.txt", ((None, None),)),
        ("sed -n '40,20p' a.txt", ((40, 1),)),
    ],
)
def test_each_count_spelling_reads_its_lines(
    script: str,
    ranges: tuple[tuple[int | None, int | None], ...],
) -> None:
    lifted = _lifted(script, stdout="x\n")

    assert lifted is not None
    assert isinstance(lifted.result, ReadResult)
    assert (
        tuple(
            (span.start, span.count)
            for span in tuple(
                item for item in lifted.stdout if isinstance(item, LineSpan)
            )
        )
        == ranges
    )


def test_a_double_dash_ends_cats_options() -> None:
    lifted = _lifted("cat -- -a.txt", stdout="x\n")

    assert _typed_result(lifted, ReadResult).path == "-a.txt"
    assert _lifted("cat x -- a.txt") is None


def test_a_chdir_with_a_trailing_slash_composes_one_separator() -> None:
    lifted = _lifted("cd /w/ && cat a.txt", stdout="x\n")
    result = ReadResult(path="/w/b.txt")

    assert _typed_result(lifted, ReadResult).path == "/w/a.txt"
    assert shell_results.rewrite_shell_source("cd /w/ && cat a.txt", result) == (
        "cd /w/ && cat b.txt"
    )
    # Only the separator is stripped, whatever letter ends the directory.
    ended = _lifted("cd /wX && cat a.txt", stdout="x\n")
    renamed = ReadResult(path="/wX/b.txt")
    assert _typed_result(ended, ReadResult).path == "/wX/a.txt"
    assert shell_results.rewrite_shell_source("cd /wX && cat a.txt", renamed) == (
        "cd /wX && cat b.txt"
    )


def test_a_rewrite_with_no_path_keeps_the_commands_own() -> None:
    result = ReadResult()

    assert shell_results.rewrite_shell_source("cat a.txt", result) == "cat a.txt"


@pytest.mark.parametrize(
    ("script", "content"),
    [
        # Only ``<<-`` strips, and only tabs.
        ("cat > a << 'EOF'\n\tx\nEOF\n", "\tx\n"),
        ("cat > a <<- 'EOF'\n  x\n\tEOF\n", "  x\n"),
        ("cat > a <<- 'EOF'\n\tXY\n\tEOF\n", "XY\n"),
    ],
)
def test_a_heredoc_body_is_dedented_only_as_bash_does(
    script: str,
    content: str,
) -> None:
    lifted = _lifted(script)

    assert _typed_result(lifted, WriteResult).content == content


def test_echo_joins_its_words_with_one_space() -> None:
    lifted = _lifted("echo a b > a.txt")

    assert _typed_result(lifted, WriteResult).content == "a b\n"


def test_a_heredoc_write_with_no_body_node_is_refused() -> None:
    output = _Node(kind="word", word="EOF", parts=[], pos=(0, 3))
    document = _Node(kind="redirect", type="<<", input=None, output=output)
    target = _Node(
        kind="redirect",
        type=">",
        input=None,
        output=_Node(kind="word", word="a", parts=[], pos=(0, 1)),
    )
    cat = _Node(kind="word", word="cat", parts=[], pos=(0, 3))

    assert shell_results._heredoc_write([cat, target, document], literal=True) is None


def test_replay_keeps_a_command_it_cannot_reparse() -> None:

    assert shell_result_for_replay(
        ToolResult(
            call_id="c1",
            state="done",
            result=ReadResult(),
            extra={"$shell": {"command": ["ls"]}},
        ),
    ) == ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=("ls",)),
    )
    assert shell_result_for_replay(
        ToolResult(
            call_id="c1",
            state="done",
            result=ReadResult(path="b.txt"),
            extra={"$shell": {"command": ["ls"]}},
        ),
    ) == ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=("ls",)),
    )
    assert shell_result_for_replay(
        ToolResult(
            call_id="c1",
            state="done",
            result=ReadResult(),
            extra={"$shell": {"command": []}},
        ),
    ) == ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=()),
    )
    assert shell_result_for_replay(
        ToolResult(
            call_id="c1",
            state="done",
            result=ReadResult(),
            extra={"$shell": {"command": ["cat", "a.txt"]}},
        ),
    ) == ToolResult(
        call_id="c1",
        state="done",
        result=BashResult(command=("cat", "a.txt")),
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
