"""Every shell form the table accepts or refuses, asserted one case per row.

:mod:`shell_results` documents a table of matched forms and one rejection rule
-- every part must be literal text. Those are promises, and a promise without a
test is a comment. This file holds one case per row and one per rejection
CLASS, so a form that stops working fails here rather than silently annotating
nothing.

Cases were taken from a census of 92,870 distinct commands across 10,770
captured session files, so the forms are the ones agents write rather than the
ones a parser makes easy.
"""

from __future__ import annotations

import pytest

from trackinizer.lib.agent.sessions.shell_results import (
    lift_shell_result,
    rewrite_shell_source,
)
from trackinizer.lib.agent.sessions.udiff import render_udiff
from trackinizer.lib.agent.types.sessions import (
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    ShellCommandResult,
    Splice,
)


def _lift(script: str, *, stdout: str = "", exit_code: int = 0) -> object:
    """Lift one script as a provider would have recorded it."""
    return lift_shell_result(
        ShellCommandResult(
            call_id="c1",
            command=("/bin/bash", "-lc", script),
            stdout=stdout,
            exit_code=exit_code,
        )
    )


@pytest.mark.parametrize(
    ("script", "path"),
    [
        ("cat a.txt", "a.txt"),
        ("/bin/cat a.txt", "a.txt"),
        ("head a.txt", "a.txt"),
        ("head -n 5 a.txt", "a.txt"),
        ("head -5 a.txt", "a.txt"),
        ("tail a.txt", "a.txt"),
        ("tail -n 5 a.txt", "a.txt"),
        # The historical spelling, and the one the corpus actually uses.
        ("tail -80 docs/BUGS17.md", "docs/BUGS17.md"),
        ("sed -n 1p a.txt", "a.txt"),
        ("sed -n '1,220p' a.txt", "a.txt"),
        ("sed -n '84p;101p' a.txt", "a.txt"),
        ("sed -n '/^def /,/^class /p' a.txt", "a.txt"),
        ("nl -ba a.txt", "a.txt"),
        # A cd prefix writes no file, but it DOES decide which file was read:
        # a literal destination composes, because that is the path the command
        # resolved. An expanded one names a directory this reader cannot see,
        # so its operand stays as written.
        ("cd /w && cat a.txt", "/w/a.txt"),
        ("cd $HOME && cat a.txt", "a.txt"),
        # A tilde the reader cannot expand. Not spelled ``.``: the export
        # rewrites that literal to ``.``, which IS a directory this reader
        # composes, so the fixture would stop exercising the expanded case.
        ("cd ~/src && sed -n '1,10p' a.txt", "a.txt"),
    ],
    ids=[
        "cat",
        "cat-absolute",
        "head-bare",
        "head-n",
        "head-count",
        "tail-bare",
        "tail-n",
        "tail-count",
        "sed-line",
        "sed-span",
        "sed-scattered",
        "sed-regex",
        "nl",
        "cd-chain",
        "cd-expanded",
        "cd-tilde",
    ],
)
def test_a_read_row_lifts(script: str, path: str) -> None:
    lifted = _lift(script, stdout="body\n")

    assert isinstance(lifted, FileReadResult)
    assert lifted.path == path
    assert lifted.content == "body\n"


@pytest.mark.parametrize(
    ("script", "path", "content"),
    [
        ("echo hi > a.txt", "a.txt", "hi\n"),
        ("printf hi > a.txt", "a.txt", "hi"),
        ("cat > a.txt << 'EOF'\nbeta\nEOF\n", "a.txt", "beta\n"),
        ("cat > a.txt << 'EOF'\nEOF\n", "a.txt", ""),
        ("cd /w && cat > a.txt << 'EOF'\nbeta\nEOF\n", "/w/a.txt", "beta\n"),
        # ``>|`` overrides noclobber and still truncates.
        ("echo hi >| a.txt", "a.txt", "hi\n"),
    ],
    ids=["echo", "printf", "heredoc", "heredoc-empty", "cd-heredoc", "noclobber"],
)
def test_a_write_row_lifts(script: str, path: str, content: str) -> None:
    lifted = _lift(script)

    assert isinstance(lifted, FileWriteResult)
    assert lifted.path == path
    assert lifted.content == content


@pytest.mark.parametrize(
    ("script", "path", "udiff"),
    [
        ("echo hi >> a.txt", "a.txt", "+hi\n"),
        ("cat >> a.txt << 'EOF'\nbeta\nEOF\n", "a.txt", "+beta\n"),
        ("cat >> a.txt << 'EOF'\none\ntwo\nEOF\n", "a.txt", "+one\n+two\n"),
        # ``printf`` writes no trailing newline -- verified against /bin/bash,
        # which leaves ``x\nhi`` -- so the rendered line carries none either.
        ("printf hi >> a.txt", "a.txt", "+hi"),
        # An inline patch carries the diff itself.
        (
            "patch a.txt << 'EOF'\n-old\n+new\nEOF\n",
            "a.txt",
            "-old\n+new\n",
        ),
        # A rewrite states no diff: the file's new bytes were never printed.
        ("sed -i s/a/b/ a.txt", "a.txt", None),
        ("sed -i 's/a/b/g' a.txt", "a.txt", None),
        ("perl -pi -e s/a/b/ a.txt", "a.txt", None),
        ("perl -i -pe 's/a/b/' a.txt", "a.txt", None),
        ("patch a.txt", "a.txt", None),
        ("tee a.txt", "a.txt", None),
        ("tee -a a.txt", "a.txt", None),
        ("tee --append a.txt", "a.txt", None),
    ],
    ids=[
        "echo-append",
        "heredoc-append",
        "heredoc-append-multiline",
        "printf-append",
        "patch-inline",
        "sed-i",
        "sed-i-quoted",
        "perl-pi",
        "perl-i-pe",
        "patch-bare",
        "tee",
        "tee-a",
        "tee-append-long",
    ],
)
def test_an_edit_row_lifts(script: str, path: str, udiff: str | None) -> None:
    lifted = _lift(script)

    assert isinstance(lifted, FileEditResult)
    assert lifted.path == path
    # The diff IS the splices, so it is rendered rather than stored; a row
    # that states no replacement renders nothing.
    assert (render_udiff(lifted.edits) or None) == udiff


@pytest.mark.parametrize(
    "script",
    [
        # THE RULE: a part the shell would expand names a file, or holds
        # bytes, that depend on a machine this reader cannot see.
        "cat $FILE",
        'cat "$1"',
        "cat `which python`",
        "cat $(ls | head -1)",
        "cat *.txt",
        "cat {a,b}.txt",
        'sed -n "${line}p" a.txt',
        "echo $HOME > a.txt",
        "cat > a.txt << EOF\n$HOME\nEOF\n",
        # printf interprets its own format, so the transcript's bytes are not
        # the file's bytes.
        "printf '%s' body > a.txt",
        "printf 'a\\nb' > a.txt",
        "echo -n body > a.txt",
        # Not one command: a pipeline's data came from another stage, a
        # non-cd chain could have written the file first, and ``;`` proves
        # nothing about the command before it.
        "cat a.txt | head -n 1",
        "nl -ba a.txt | sed -n '10,20p'",
        "cd /w; cat a.txt",
        "rm a.txt && cat a.txt",
        "cat a.txt && cat b.txt",
        "(cd /w && cat a.txt)",
        # A form outside the table.
        "cat a.txt b.txt",
        "cat -n a.txt",
        "cat -- -",
        "nl -ln a.txt",
        "head -c 500 a.txt",
        "sed -n '1,5p;6w other.txt' a.txt",
        "sed 's/a/b/' a.txt",
        "sed -i s/a/b/ a.txt b.txt",
        "sed -i.bak s/a/b/ a.txt",
        "perl -i.bak -pe s/a/b/ a.txt",
        "patch a.txt < changes.diff",
        "tee a.txt b.txt",
        "tee -p a.txt",
        # Another program that merely redirects is not a write.
        "python3 -c 'print(1)' > a.txt",
        "git show HEAD:a.txt > a.txt",
        "uv run pytest > a.txt",
        # A utility from somewhere unexpected is a different program.
        "/opt/tools/cat a.txt",
        "./cat a.txt",
        # An unterminated heredoc.
        "cat > a.txt << 'EOF'\nbeta\n",
        # A second command after the terminator.
        "cat > a.txt << 'EOF'\nbeta\nEOF\nrm a.txt\n",
    ],
)
def test_a_refused_form_lifts_nothing(script: str) -> None:
    assert _lift(script, stdout="body\n") is None


@pytest.mark.parametrize(
    ("script", "before", "after"),
    [
        ("echo hi >> a.txt", "", "hi\n"),
        ("cat >> a.txt << 'EOF'\nbeta\nEOF\n", "", "beta\n"),
        # ``printf`` writes no trailing newline, and the splice states the
        # bytes that REACHED the file -- verified against /bin/bash, which
        # leaves ``x\nhi``. Terminating it claimed a byte the command never
        # wrote, which a consumer applying the splice would then insert.
        ("printf hi >> a.txt", "", "hi"),
    ],
    ids=["echo", "heredoc", "printf"],
)
def test_an_append_states_a_splice_that_replaces_nothing(
    script: str, before: str, after: str
) -> None:
    """An append inserts, which the shape spells as an empty ``before``.

    No special case for insertion: the same two fields carry it, and the
    ``after`` is exactly the bytes that reached the file -- ``printf``
    contributing no trailing newline.
    """
    lifted = _lift(script)

    assert isinstance(lifted, FileEditResult)
    assert lifted.edits == (Splice(before=before, after=after),)


def test_an_empty_append_adds_no_line() -> None:
    r"""``cat >> f << 'EOF'`` with an empty body appends nothing.

    Verified against /bin/bash: a file holding ``x\n`` still holds exactly
    ``x\n`` afterwards. The append branch terminates its content
    unconditionally, so an empty body became ``"\n"`` -- a splice claiming a
    blank line was added to a file the command never touched.
    """
    lifted = _lift("cat >> a.txt << 'EOF'\nEOF\n")

    assert isinstance(lifted, FileEditResult)
    assert lifted.path == "a.txt"
    assert lifted.edits == ()


@pytest.mark.parametrize(
    "script",
    [
        "sed -i s/a/b/ a.txt",
        "perl -pi -e s/a/b/ a.txt",
        "tee a.txt",
        "tee -a a.txt",
        "patch a.txt",
    ],
    ids=["sed-i", "perl-i", "tee", "tee-a", "patch"],
)
def test_a_rewrite_states_no_splice_it_cannot_know(script: str) -> None:
    """A rewrite never printed its result, so it names no before or after.

    Inventing one would be worse than the empty tuple: a consumer cannot tell
    a fabricated splice from an observed one.
    """
    lifted = _lift(script)

    assert isinstance(lifted, FileEditResult)
    assert lifted.edits == ()
    assert render_udiff(lifted.edits) == ""


@pytest.mark.parametrize(
    ("script", "rewritten"),
    [
        # The operand is found by POSITION: looking it up by VALUE returns the
        # utility when the file is named after it, and the replay then rewrote
        # the executable instead of the path -- turning ``tee tee`` into
        # ``renamed.txt tee``, which is a different program.
        ("tee tee", "tee renamed.txt"),
        ("tee -a tee", "tee -a renamed.txt"),
        ("cat cat", "cat renamed.txt"),
        ("sed -i s/a/b/ s/a/b/", "sed -i s/a/b/ renamed.txt"),
        # ``-e`` names the script by flag, and the file that follows may hold
        # the same text. Excluding files by VALUE dropped this edit entirely.
        ("sed -i -e s/a/b/ s/a/b/", "sed -i -e s/a/b/ renamed.txt"),
    ],
    ids=["tee", "tee-a", "cat", "sed-i", "sed-i-e"],
)
def test_a_file_named_after_its_utility_rewrites_the_operand(
    script: str, rewritten: str
) -> None:
    """A path equal to another word must still be replaced where it SITS."""
    assert (
        rewrite_shell_source(script, FileEditResult(call_id="c1", path="renamed.txt"))
        == rewritten
    )


@pytest.mark.parametrize(
    "script", ["sed -n '0p' a.txt", "sed -n '0,3p' a.txt"], ids=["single", "span"]
)
def test_a_sed_address_of_line_zero_is_no_read(script: str) -> None:
    """``sed -n '0p'`` is an error, not a read: files start at line 1.

    Verified against /bin/sed, which exits 1 with "invalid usage of line
    address 0". Reporting a range starting at zero would describe lines no
    file has, and :func:`rewrite_shell_source` reaches this classifier with no
    exit code to filter it.
    """
    assert _lift(script, stdout="") is None


def test_a_reversed_sed_address_reads_one_line() -> None:
    """``sed -n '40,20p'`` prints line 40 only, verified against /bin/sed.

    A second address before the first does not run backwards: sed prints the
    start line and stops. Reporting a zero-line range claimed the read
    returned nothing, which is the opposite of what the file shows.
    """
    lifted = _lift("sed -n '40,20p' a.txt", stdout="line40\n")

    assert isinstance(lifted, FileReadResult)
    assert lifted.ranges == ((40, 1),)


def test_a_write_lifts_from_a_heredoc_body_only() -> None:
    """``cat > F`` needs a heredoc: bare stdin came from a pipe.

    The table's write row is the heredoc form. A ``cat > f`` with nothing
    feeding it states a path whose content this reader never saw, so it is not
    the write the row describes.
    """
    assert _lift("cat > a.txt") is None


@pytest.mark.parametrize(
    ("exit_code", "succeeded"),
    [(1, None), (2, None), (127, None), (1, True)],
    ids=["failed", "usage-error", "not-found", "claimed-success"],
)
def test_a_failed_command_lifts_nothing(exit_code: int, succeeded: bool | None) -> None:
    """A command that failed changed nothing, whatever it was going to do."""
    shell = ShellCommandResult(
        call_id="c1",
        command=("/bin/bash", "-lc", "cat a.txt"),
        stdout="",
        exit_code=exit_code,
    )

    assert lift_shell_result(shell, succeeded=succeeded) is None


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
