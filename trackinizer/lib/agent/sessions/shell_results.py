r"""Lift unambiguous shell executions into provider-neutral file results.

An agent that has file tools still reaches for the shell, so a transcript
records the same three acts twice: once as a typed tool call, once as a
command. This module recognizes the second kind, so a session names what was
READ, WRITTEN, and EDITED however the agent spelled it.

THE FORMS THIS MODULE MATCHES -- there are no others:

===========  ==================================  =========================
operation    form                                becomes
===========  ==================================  =========================
read         ``cat [--] F``                      :class:`FileReadResult`
read         ``head [-n N|-N|--lines=N] F``      :class:`FileReadResult`
read         ``tail [-n N|-N|--lines=N] F``      :class:`FileReadResult`
read         ``sed -n 'N,Mp' F``                 :class:`FileReadResult`
read         ``nl -ba F``                        :class:`FileReadResult`
write        ``cat > F`` (``<<``/``<<-`` body)   :class:`FileWriteResult`
write        ``echo ... > F``                    :class:`FileWriteResult`
write        ``printf ... > F``                  :class:`FileWriteResult`
edit         ``cat >> F``                        :class:`FileEditResult`
edit         ``echo ... >> F``                   :class:`FileEditResult`
edit         ``printf ... >> F``                 :class:`FileEditResult`
edit         ``tee [-a|--append] F``             :class:`FileEditResult`
edit         ``sed -i ... F``                    :class:`FileEditResult`
edit         ``perl -i ... F``                   :class:`FileEditResult`
edit         ``patch ... F``                     :class:`FileEditResult`
===========  ==================================  =========================

A count spelling is the utility's own: ``head -20 f`` and ``head -n 20 f``
name one act, and agents write both. A SIGN is not a spelling -- it changes
WHICH lines print: ``tail -n +5`` starts at line 5 and ``head -n -5`` drops
the last five, so each states the window coreutils actually printed. ``>|``
writes like ``>`` -- it only overrides ``noclobber`` -- and ``--`` ends the
options, which is the one way to name a file whose name begins with a dash.

``tee`` is an EDIT even without ``-a``, and even though it truncates: its
content arrives on a pipe, which is not a form this module matches, so the
file's new bytes are unknown either way. A :class:`FileWriteResult` would have
to state them, and stating ``""`` would claim the command emptied the file.

Only an append states the bytes it added; the other edits state a path and
state no ``edits``, because their result was never printed.

A ``cd`` may precede any of them (``cd repo && cat f``). It changes no file,
but it does change which file a relative operand names, so a LITERAL
destination composes into the reported path (``cd sub && cat a.txt`` reads
``sub/a.txt``) and an expanded one leaves the operand as written. Codex's
``apply_patch`` tool is the same three acts under one name and is read by the
codex adapter, not by this module.

WHAT IS REJECTED, and the one rule behind it: **every part must be literal
text in the transcript.** A path or a body the shell would expand -- ``$VAR``,
``` `cmd` ```, ``*.py``, ``{a,b}`` -- names a file, or holds bytes, that
depend on a machine this reader cannot see. Such a command is a dynamic
program, not a file operation, and is left as a
:class:`ShellCommandResult`. So is anything else: a pipeline, a second
command, a redirect this table does not list. Nothing is guessed.

Two consequences of that rule worth stating outright, because both look like
omissions:

* ``printf`` interprets its own format (``%s``, ``\\n``), so only a format
  needing no interpretation is taken. ``printf 'a\\nb' > f`` writes two lines,
  and reporting the literal string as the content would be wrong.
* An unquoted heredoc (``<< EOF``) expands its body; only the quoted form
  (``<< 'EOF'``) writes what the transcript shows. Its ``<<-`` variant strips
  leading TABS from the body and the terminator, which is how an indented
  heredoc is written, so the body recorded is the dedented one bash writes.

A command that merely happens to redirect -- ``python3 -c ... > f``,
``git show > f`` -- is NOT a write. It is a program whose output landed in a
file, and typing it by that side effect would name the act wrongly (axiom 9:
a result is typed by what the tool DID).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal, Protocol, cast

import re
import shlex

from trackinizer.lib.agent.sessions.udiff import parse_udiff
from trackinizer.lib.agent.types.sessions import (
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    ShellCommandResult,
    Splice,
)
from trackinizer.lib.custom_json import (
    MutableJSONValue,
    json_freeze,
    json_unfreeze,
)


if TYPE_CHECKING:
    import bashlex
else:
    from wrapt import lazy_import

    bashlex = lazy_import("bashlex")


__all__ = ["lift_shell_result", "rewrite_shell_source", "shell_result_for_replay"]


type FileResult = FileReadResult | FileWriteResult | FileEditResult
type Act = Literal["read", "write", "append", "rewrite", "patched"]
"""What one matched command did to its file.

The last three are all edits, split by the EVIDENCE each leaves behind, since
that decides what a record may honestly claim:

* ``append`` states the bytes it added, which become a one-sided diff.
* ``patched`` carries a diff outright -- ``patch F << 'EOF'`` holds it inline.
* ``rewrite`` states only the command that transformed the file, because the
  result was never printed: ``sed -i``, ``perl -i``, ``tee``.

Collapsing them would make a record's ``edits`` mean three different things.
"""
type LineRanges = tuple[tuple[int | None, int | None], ...]
"""Which lines a read returned, as ``(start, count)`` pairs, one-based.

Empty is a whole-file read. SEVERAL pairs because one command routinely reads
scattered lines -- ``sed -n '84p;101p;132p' f`` -- and a single span could
only describe that by claiming the gaps were read too. A ``None`` count is a
bound this reader cannot resolve without the file: ``sed -n '20,$p'`` ends at
the last line, ``tail -5`` counts backwards from it.
"""
type MatchedRow = tuple[Act, str, tuple[int, int], str | None, LineRanges]
"""One table row's match: act, path as written, its span, content, ranges."""
type Operation = tuple[Act, str, tuple[int, int], str | None, LineRanges, str | None]
"""What one command did: act, path, path span, content, line ranges, chdir.

The span is the path's offset in the source, which is what lets a replay put
an edited path back without re-rendering the command around it.

``path`` is the file the command RESOLVED -- ``cd sub && cat a.txt`` names
``sub/a.txt`` -- while the span still points at the operand as written. The
chdir is carried alongside so a rewrite can strip it back off; without it a
replay would splice the resolved path into a command that will chdir again.
"""


class _BashNode(Protocol):
    """Expose the bashlex AST fields this classifier reads."""

    kind: str
    parts: list[_BashNode]
    word: str
    type: str
    op: str
    input: int | None
    output: _BashNode
    heredoc: _BashNode | None
    value: str
    pos: tuple[int, int]


class _Bashlex(Protocol):
    """Expose the bashlex entry point this classifier calls."""

    def parse(self, source: str) -> list[_BashNode]: ...


def lift_shell_result(
    result: ShellCommandResult,
    *,
    command: str | None = None,
    succeeded: bool | None = None,
) -> FileResult | None:
    """Return the most-specific file result proved by a shell execution.

    Args:
      result: Provider-neutral shell execution and its output.
      command: Shell source when the provider did not record an argv.
      succeeded: Whether execution succeeded when no exit code was recorded.

    Returns:
      file_result: A specific file result, or None when lifting is ambiguous.

    """
    completed = (
        result.exit_code == 0 if result.exit_code is not None else succeeded is True
    )
    if not completed:
        return None
    script = command if command is not None else _script(result.command)
    operation = _operation(script) if script is not None else None
    if operation is None:
        return None
    kind, path, _, content, ranges, _ = operation
    extra_value = json_unfreeze(result.extra)
    assert isinstance(extra_value, dict)
    extra = extra_value
    replay: dict[str, MutableJSONValue] = {}
    if result.command is not None:
        replay["command"] = json_unfreeze(result.command)
    if kind != "read" and result.stdout:
        replay["stdout"] = result.stdout
    if result.stderr:
        replay["stderr"] = result.stderr
    if result.exit_code is not None:
        replay["exit_code"] = result.exit_code
    extra["$shell"] = replay
    extra_frozen = json_freeze(extra)
    if kind == "read":
        return FileReadResult(
            context_id=result.context_id,
            timestamp=result.timestamp,
            call_id=result.call_id,
            path=path,
            content=result.stdout,
            ranges=ranges,
            extra=extra_frozen,
        )
    assert content is not None
    if kind == "append":
        # The one edit whose new bytes the transcript holds, so it renders as
        # the one-sided diff it is. The file BEFORE the append was never
        # recorded, and a two-sided diff would invent that half.
        return FileEditResult(
            context_id=result.context_id,
            timestamp=result.timestamp,
            call_id=result.call_id,
            path=path,
            # An append replaces nothing, which is a splice with an empty
            # ``before`` -- the shape's own way of saying "pure insertion".
            #
            # Terminated even when the command wrote no trailing newline:
            # ``printf hi >> f`` appends one line, and a splice whose text
            # lacks the newline renders as a line missing its own terminator,
            # which is a claim about DIFF FORMAT rather than about the file.
            # Empty content appended NOTHING -- ``cat >> f << 'EOF'`` with an
            # empty body leaves the file byte for byte, verified against
            # /bin/bash -- so it states no splice at all.
            #
            # The bytes AS WRITTEN, terminator and all: ``printf hi >> f``
            # leaves ``x\nhi``, and terminating it claimed a byte the command
            # never wrote, which a consumer applying the splice would insert.
            edits=(Splice(before="", after=content),) if content else (),
            extra=extra_frozen,
        )
    if kind == "patched":
        # The diff arrived inline, so the record states the change itself.
        return FileEditResult(
            context_id=result.context_id,
            timestamp=result.timestamp,
            call_id=result.call_id,
            path=path,
            edits=parse_udiff(content),
            extra=extra_frozen,
        )
    if kind == "rewrite":
        # An in-place rewrite or a tee: the file's new bytes were never
        # printed, so the record names the file and states no ``edits``
        # rather than claiming content nobody captured. The command that did
        # it rides in ``$shell``, which is what a replay puts back.
        return FileEditResult(
            context_id=result.context_id,
            timestamp=result.timestamp,
            call_id=result.call_id,
            path=path,
            extra=extra_frozen,
        )
    return FileWriteResult(
        context_id=result.context_id,
        timestamp=result.timestamp,
        call_id=result.call_id,
        path=path,
        content=content,
        extra=extra_frozen,
    )


def shell_result_for_replay(result: FileResult) -> ShellCommandResult | None:
    """Recover a lifted shell execution for provider-native replay.

    Args:
      result: A provider-neutral file result.

    Returns:
      shell_result: Its source shell execution, or None when it was native.

    """
    extra = dict(json_unfreeze(result.extra))
    replay_value = extra.pop("$shell", None)
    if not isinstance(replay_value, Mapping):
        return None
    replay = cast(Mapping[str, object], replay_value)
    command_value = replay.get("command")
    command = None
    if isinstance(command_value, list):
        command_values = cast(list[object], command_value)
        parts = tuple(part for part in command_values if isinstance(part, str))
        command = parts if len(parts) == len(command_values) else None
    command = _stencil_command(command, result)
    stdout = (
        result.content or ""
        if isinstance(result, FileReadResult)
        else replay.get("stdout", "")
    )
    stderr = replay.get("stderr", "")
    exit_code = replay.get("exit_code")
    return ShellCommandResult(
        context_id=result.context_id,
        timestamp=result.timestamp,
        call_id=result.call_id,
        command=command,
        stdout=stdout if isinstance(stdout, str) else "",
        stderr=stderr if isinstance(stderr, str) else "",
        exit_code=exit_code if isinstance(exit_code, int) else None,
        extra=json_freeze(extra),
    )


def rewrite_shell_source(command: str, result: FileResult) -> str:
    """Rewrite semantic file fields into a lifted shell command.

    Args:
      command: Original shell source.
      result: Current provider-neutral file result.

    Returns:
      rewritten: Shell source reflecting representable semantic edits.

    """
    operation = _operation(command)
    if operation is None:
        return command
    kind, original_path, span, original_content, _, _ = operation
    # The path is the one field EVERY row carries, and each row reported where
    # it sits, so renaming is one splice regardless of which utility ran.
    renamed = (
        command
        if result.path is None or result.path == original_path
        else command[: span[0]]
        + shlex.quote(_operand(command, result.path))
        + command[span[1] :]
    )
    if kind != "write" or not isinstance(result, FileWriteResult):
        # Only a write states the file's whole contents. An append states what
        # it added and a rewrite states nothing, so neither can be rebuilt into
        # a different command without inventing the rest of the file.
        return renamed
    if result.content is None or result.content == original_content:
        return renamed
    # The content changed, so the command that produced it no longer describes
    # the file. ``printf %s`` writes the new bytes literally, whatever utility
    # the original used, and the redirect keeps the original's direction.
    found = _simple_command(renamed)
    assert found is not None
    tree, _ = found
    redirect = next(part for part in tree.parts if part.kind == "redirect")
    rewritten = (
        f"/usr/bin/printf %s {shlex.quote(result.content)} "
        f"{redirect.type} {shlex.quote(_operand(command, result.path or original_path))}"
    )
    return renamed[: tree.pos[0]] + rewritten + renamed[tree.pos[1] :]


def _script(command: tuple[str, ...] | None) -> str | None:
    """Return shell source from an argv without executing it."""
    found = _shell_source(command)
    return found[0] if found is not None else None


def _shell_source(command: tuple[str, ...] | None) -> tuple[str, int | None] | None:
    """Return shell source and its argv index, or direct argv as source."""
    if not command:
        return None
    shells = {
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
    }
    if command[0] not in shells:
        return (shlex.join(command), None)
    for index, argument in enumerate(command[1:], 1):
        is_command_flag = argument == "-c" or (
            argument.startswith("-")
            and not argument.startswith("--")
            and "c" in argument[1:]
        )
        if is_command_flag and index + 2 == len(command):
            return (command[index + 1], index + 1)
    return None


def _operation(script: str) -> Operation | None:
    """Classify one shell command as a file read, write, or edit.

    Each branch below is one row of the table in this module's docstring, in
    the same order. A command matching no row is not a file operation.

    Args:
      script: Shell source of one command.

    Returns:
      operation: What it did, to which path, where that path sits in the
        source, and the content it wrote -- or ``None`` when no row matches.
        The path is composed against a static ``cd`` destination, so it names
        the file the command actually resolved.

    """
    quoted = _quoted_heredoc(script)
    found = _simple_command(quoted[0] if quoted is not None else script)
    if found is None:
        return None
    tree, chdir = found
    matched = _matched_row(
        cast(Sequence[_BashNode], tree.parts),
        quoted=quoted,
    )
    if matched is None:
        return None
    act, path, span, content, ranges = matched
    # A relative operand names a file under the ``cd`` destination, not under
    # the session's own directory: ``cd sub && cat a.txt`` read ``sub/a.txt``,
    # and reporting the bare operand named a different file. An absolute one
    # resolves against the root whatever ``cd`` ran.
    resolved = (
        path if chdir is None or path.startswith("/") else f"{chdir.rstrip('/')}/{path}"
    )
    return (act, resolved, span, content, ranges, chdir)


def _operand(command: str, path: str) -> str:
    """Return a resolved path back as the operand its command should carry.

    The command will chdir again on replay, so splicing the resolved path in
    would resolve the destination twice -- ``cd sub && cat sub/a.txt``.
    """
    found = _operation(command)
    chdir = found[5] if found is not None else None
    return path if chdir is None else path.removeprefix(f"{chdir.rstrip('/')}/")


def _matched_row(
    parts: Sequence[_BashNode], *, quoted: tuple[str, str] | None
) -> MatchedRow | None:
    """Return the table row one parsed command matches, path as written."""
    if any(part.kind not in {"word", "redirect"} for part in parts):
        return None
    # ROW: cat > F, with a heredoc body.
    heredoc = _heredoc_write(parts, literal=quoted is not None)
    if heredoc is not None:
        return heredoc
    # ROW: patch F << 'EOF'. The heredoc IS the diff, which makes this the one
    # rewrite whose change the transcript states outright.
    inline = _patch_heredoc(parts, body=quoted[1] if quoted is not None else None)
    if inline is not None:
        return inline
    word_nodes = [part for part in parts if part.kind == "word"]
    words = [_static_word(part) for part in word_nodes]
    # THE RULE: every word must be literal text. One the shell would expand
    # makes this a dynamic program whose file this reader cannot name.
    if not words or any(word is None for word in words):
        return None
    argv = cast(list[str], words)
    utility = _standard_utility(argv[0])
    if utility is None:
        return None
    redirects = [part for part in parts if part.kind == "redirect"]
    # ROWS: echo > F, printf > F -- and their >> forms, which append.
    if utility in {"echo", "printf"}:
        target = _single_output(redirects)
        content = _write_content(utility, argv[1:])
        if target is None or content is None:
            return None
        operation, path, span = target
        return (operation, path, span, content, ())
    # ROWS: tee F, tee -a F. Its content is the PIPE's, which no transcript
    # records, so only a command whose stdin this reader can see would give
    # one -- and a bare ``tee`` has none. The path is still knowable.
    if utility == "tee":
        return _tee_operation(argv, word_nodes)
    # ROWS: sed -i F, perl -i F. Both rewrite in place, so the file after is
    # not in the transcript and the record carries the SCRIPT that produced
    # it rather than a content this reader would have to invent.
    if utility in {"sed", "perl"} and _in_place(argv[1:]):
        return _in_place_operation(argv, word_nodes)
    # ROW: patch F.
    if utility == "patch":
        return _patch_operation(argv, word_nodes, redirects)
    if redirects:
        # Every remaining row is a READ, and a read writes nothing.
        return None
    # ROW: cat F. The whole file, so no range bounds it.
    if utility == "cat":
        # ``--`` ends the options, so the word after it is the file WHATEVER it
        # looks like. That is the only way to name a file beginning with a
        # dash, and a reader that refuses the form cannot express one.
        at = 2 if len(argv) == 3 and argv[1] == "--" else 1
        if len(argv) != at + 1 or (at == 1 and argv[1].startswith("-")):
            return None
        return ("read", argv[at], word_nodes[at].pos, None, ())
    # ROWS: head [-n N] F, tail [-n N] F.
    if utility in {"head", "tail"}:
        return _line_reader_operation(utility, argv, word_nodes)
    # ROW: nl -ba F.
    if utility == "nl":
        return _nl_operation(argv, word_nodes)
    # ROW: sed -n 'N,Mp' F.
    if utility == "sed":
        return _sed_operation(argv, word_nodes)
    return None


def _quoted_heredoc(script: str) -> tuple[str, str] | None:
    """Return ``script`` with a QUOTED heredoc delimiter bared, and the body.

    bashlex cannot parse ``<< 'EOF'`` at all -- it reports the document as
    unterminated -- and that is precisely the form worth lifting, since a
    quoted delimiter is the one that suppresses expansion and so writes its
    body verbatim. Unquoting it for the parser is safe because the delimiter
    itself is never content; the body is taken from the original text.
    """
    match = re.search(r"(<<-?)\s*(['\"])(\w+)\2\s*?\n", script)
    if match is None:
        return None
    delimiter = match.group(3)
    # ``<<-`` strips leading TABS from every body line AND from the terminator,
    # which is why the terminator cannot be matched literally: an indented
    # ``\tEOF`` never equals ``EOF``, and the whole write was refused. Tabs
    # only -- bash leaves spaces alone. Verified against /bin/bash for a body
    # indented one tab and one indented two.
    dedent = match.group(1) == "<<-"
    # The terminator is a whole LINE equal to the delimiter, so the body is
    # found by line rather than by substring: partitioning on the text would
    # miss an EMPTY body, whose terminator is the very first line.
    lines = [
        line.lstrip("\t") if dedent else line
        for line in script[match.end() :].split("\n")
    ]
    if delimiter not in lines:
        return None
    at = lines.index(delimiter)
    if any(rest.strip() for rest in lines[at + 1 :]):
        # Anything after the terminator is a second command whose effect on
        # the file this classifier has not modelled.
        return None
    body = "".join(f"{line}\n" for line in lines[:at])
    return (f"{script[: match.start()]}<< {delimiter}\n{body}{delimiter}\n", body)


def _heredoc_write(parts: Sequence[_BashNode], *, literal: bool) -> MatchedRow | None:
    """Return the write a ``cat > path << EOF`` performs, when it is static.

    Agents author files this way constantly; 4.6% of 1351 captured claude
    commands carry a heredoc. Only a QUOTED delimiter lifts: an unquoted one
    expands ``$var`` and backticks inside the body, so the bytes on disk are
    not the bytes in the transcript and a replay would write the wrong file.
    """
    redirects = [part for part in parts if part.kind == "redirect"]
    documents = [part for part in redirects if part.type in {"<<", "<<-"}]
    if len(documents) != 1 or not literal:
        return None
    words = [part for part in parts if part.kind == "word"]
    argv = [_static_word(part) for part in words]
    if len(argv) != 1 or argv[0] is None:
        return None
    if _standard_utility(argv[0]) != "cat":
        return None
    target = _single_output([part for part in redirects if part not in documents])
    if target is None:
        return None
    operation, path, span = target
    delimiter = _static_word(documents[0].output)
    body = documents[0].heredoc
    content = body.value if body is not None else None
    if delimiter is None or not isinstance(content, str):
        return None
    # bashlex reports the body WITH its terminator appended and no trailing
    # newline: ``beta\nEOF`` for a file holding ``beta\n``. Verified against
    # /bin/bash for empty, single-line, multi-line, and blank-line bodies.
    if not content.endswith(delimiter):
        return None
    return (operation, path, span, content[: -len(delimiter)], ())


def _standard_utility(executable: str) -> str | None:
    """Return the utility an executable names, bare or by absolute path.

    A BARE name counts. It is not certain -- ``cat`` could be a shell function
    or something earlier in ``PATH`` -- but requiring ``/bin/cat`` matched
    nothing agents actually write: across 359 captured codex shell results and
    588 claude ones it lifted ZERO, and on codex that one rule accounted for
    31.5% of rejections by itself. A classifier that recognizes no real
    command annotates nothing, and shadowing a coreutils with a function that
    does something else was never observed.

    Only the leaf is read, so ``/usr/bin/sed`` and ``sed`` are one utility; a
    path ending in the name but living elsewhere -- ``/opt/tools/cat`` -- is a
    different program and stays excluded.
    """
    directory, _, name = executable.rpartition("/")
    if directory not in {"", "/bin", "/usr/bin", "/usr/local/bin"}:
        return None
    utilities = (
        "cat",
        "echo",
        "head",
        "nl",
        "patch",
        "perl",
        "printf",
        "sed",
        "tail",
        "tee",
    )
    return name if name in utilities else None


def _simple_command(script: str) -> tuple[_BashNode, str | None] | None:
    """Parse the one file-touching command a script runs, failing closed.

    A bare command, or the last stage of a ``cd``-prefixed chain: agents write
    ``cd repo && cat file`` constantly -- 58.3% of 1351 captured claude
    commands are ``cd``-prefixed -- and rejecting the shape left the majority
    of real reads unlifted. Only ``cd`` may precede, because it is the one
    utility that changes nothing but the directory the read resolves against;
    any other leading command could have WRITTEN the file being read, which
    would make the observed content not the file's prior state.

    Returns:
      command: The node whose parts name the file operation.
      chdir: The ``cd`` destination when it is literal text, ``None`` when
        there was no ``cd`` or its destination expands.

    """
    try:
        parser = cast(_Bashlex, bashlex)
        trees = parser.parse(script)
    except Exception:  # noqa: BLE001 -- bashlex exposes several parse failures.
        return None
    if len(trees) != 1:
        return None
    if trees[0].kind == "command":
        return (trees[0], None)
    if trees[0].kind != "list":
        # A pipeline's output is its LAST stage's, and a compound hides its
        # redirections, so neither says what one utility did to one file.
        return None
    parts = cast(Sequence[_BashNode], trees[0].parts)
    commands = [part for part in parts if part.kind == "command"]
    operators = [part for part in parts if part.kind == "operator"]
    if len(commands) != 2 or len(operators) != 1:
        return None
    if all(operator.op != "&&" for operator in operators):
        # ``;`` runs the second command whatever the first did, so a zero exit
        # code proves only that the READ succeeded, not the ``cd`` before it --
        # and a failed ``cd`` resolves the path somewhere else entirely.
        return None
    leading = cast(Sequence[_BashNode], commands[0].parts)
    if any(part.kind != "word" for part in leading):
        return None
    if len(leading) != 2 or leading[0].word != "cd":
        return None
    # An EXPANDED destination still lifts: ``cd $HOME && cat f`` is the
    # majority shape, and refusing it would lose most real reads. The operand
    # then stays as the command wrote it -- unresolved rather than resolved
    # against a directory this reader cannot see.
    return (commands[1], _static_word(leading[1]))


def _static_word(node: _BashNode) -> str | None:
    """Return a word only when the shell performs no expansion."""
    if node.parts or node.word == "-":
        return None
    if any(character in node.word for character in "*?["):
        return None
    if "{" in node.word and "}" in node.word:
        return None
    return node.word


def _single_output(
    redirects: Sequence[_BashNode],
) -> tuple[Act, str, tuple[int, int]] | None:
    """Return one stdout redirection: whether it truncates, and its target.

    ``>`` replaces the file and ``>>`` adds to it, which is the difference
    between a write and an append -- the same distinction the table draws
    between ``echo > F`` and ``echo >> F``.
    """
    if len(redirects) != 1:
        return None
    redirect = redirects[0]
    if redirect.input not in {None, 1}:
        # A redirect of some other descriptor -- ``2> log`` -- leaves the
        # file this command wrote to unnamed.
        return None
    if redirect.type in {">", ">|"}:
        act: Act = "write"
    elif redirect.type == ">>":
        act = "append"
    else:
        return None
    path = _static_word(redirect.output)
    return (act, path, redirect.output.pos) if path is not None else None


def _write_content(executable: str, arguments: Sequence[str]) -> str | None:
    """Return content only for literal write forms with stable semantics."""
    if executable == "printf":
        if len(arguments) != 1 or any(mark in arguments[0] for mark in ("%", "\\")):
            return None
        return arguments[0]
    if arguments and arguments[0].startswith("-"):
        return None
    if any("\\" in argument for argument in arguments):
        return None
    return " ".join(arguments) + "\n"


def _tee_operation(
    argv: Sequence[str], nodes: Sequence[_BashNode]
) -> MatchedRow | None:
    """Return what ``tee`` did. TABLE ROWS: ``tee F`` and ``tee -a F``.

    Always a ``rewrite``, never a ``write``, even without ``-a``: tee's content
    is whatever was piped into it, and a pipeline is not a form this module
    matches, so the bytes are unknown either way. A ``write`` would have to
    state them, and stating ``""`` would claim the command emptied the file.
    """
    flags = [word for word in argv[1:] if word.startswith("-")]
    # Carried WITH its index, never looked up by value afterwards: a file named
    # after its own utility -- ``tee tee`` -- makes ``argv.index`` return the
    # executable, and the replay then rewrote that instead of the path,
    # turning the command into a different program.
    operands = [
        (word, index)
        for index, word in enumerate(argv[1:], 1)
        if not word.startswith("-")
    ]
    if len(operands) != 1 or any(flag not in {"-a", "--append"} for flag in flags):
        # Several files, or a flag that changes what tee does to them.
        return None
    path, at = operands[0]
    return ("rewrite", path, nodes[at].pos, "", ())


def _in_place(arguments: Sequence[str]) -> bool:
    """Whether a sed/perl invocation rewrites its file rather than printing.

    ``-i`` may carry a backup suffix (``-i.bak``) and may be bundled with
    other letters (``perl -pi -e``), so the flag is recognized by its ``i``
    rather than by equality.
    """
    for word in arguments:
        if word == "--in-place":
            return True
        if word.startswith("--") or not word.startswith("-"):
            continue
        if "." in word or "=" in word:
            # A SUFFIX form -- ``-i.bak``, ``--in-place=.bak`` -- also writes a
            # backup, so the command touched a second file this record has no
            # field to name. Reporting only the edit would hide the other one.
            continue
        if "i" in word[1:]:
            return True
    return False


def _in_place_operation(
    argv: Sequence[str], nodes: Sequence[_BashNode]
) -> MatchedRow | None:
    """Return the edit an in-place rewrite performed.

    TABLE ROWS: ``sed -i ... F`` and ``perl -i ... F``.

    The file's new bytes are not in the transcript -- the command printed
    nothing -- so the record states its path and no content, exactly as
    ``tee`` and ``patch`` do. The command that transformed it rides in
    ``$shell``, which is what a replay puts back.
    """
    operands = [
        (word, index) for index, word in enumerate(argv) if not word.startswith("-")
    ][1:]
    # ``-e SCRIPT`` names the script by flag; otherwise the first operand is
    # the script and the rest are files. Held by INDEX, because a file may
    # carry the same text as the script -- ``sed -i -e s/a/b/ s/a/b/`` -- and
    # excluding operands by value dropped that edit entirely.
    flagged = {
        index + 1
        for index, word in enumerate(argv[:-1])
        if word in {"-e", "--expression"}
    }
    if flagged:
        files = [found for found in operands if found[1] not in flagged]
    elif len(operands) >= 2:
        files = operands[1:]
    else:
        return None
    if len(files) != 1:
        # Several files share one script, so no single path names the edit.
        return None
    path, at = files[0]
    return ("rewrite", path, nodes[at].pos, "", ())


def _patch_heredoc(
    parts: Sequence[_BashNode], *, body: str | None
) -> MatchedRow | None:
    """Return the edit ``patch F << 'EOF'`` applied, diff included.

    TABLE ROW: ``patch ... F`` in its inline form. Unlike every other rewrite,
    the change is right there in the command -- the heredoc holds the diff --
    so the record carries it rather than leaving :attr:`edits` empty.
    """
    if body is None:
        return None
    words = [part for part in parts if part.kind == "word"]
    argv = [_static_word(part) for part in words]
    if any(word is None for word in argv) or len(argv) != 2:
        return None
    found = cast(list[str], argv)
    if _standard_utility(found[0]) != "patch" or found[1].startswith("-"):
        return None
    return ("patched", found[1], words[1].pos, body, ())


def _patch_operation(
    argv: Sequence[str], nodes: Sequence[_BashNode], redirects: Sequence[_BashNode]
) -> MatchedRow | None:
    """Return the edit ``patch`` applied. TABLE ROW: ``patch ... F``.

    The diff arrives on stdin, which the transcript holds only when the
    command spelled it inline; a ``patch < f.diff`` reads a file this reader
    has never seen, so it names no edit it can describe.
    """
    if redirects:
        return None
    operands = [
        (word, index) for index, word in enumerate(argv) if not word.startswith("-")
    ][1:]
    if len(operands) != 1:
        return None
    path, at = operands[0]
    return ("rewrite", path, nodes[at].pos, "", ())


def _nl_operation(argv: Sequence[str], nodes: Sequence[_BashNode]) -> MatchedRow | None:
    """Return the read ``nl -ba F`` performs. TABLE ROW: ``nl -ba F``.

    Only ``-ba``: it numbers every line, so the output's line count matches
    the file's. Any other numbering style skips lines, and the content would
    then be a projection of the file rather than the file.
    """
    if len(argv) != 3 or argv[1] != "-ba" or argv[2].startswith("-"):
        return None
    return ("read", argv[2], nodes[2].pos, None, ())


def _line_reader_operation(
    utility: str, argv: Sequence[str], nodes: Sequence[_BashNode]
) -> MatchedRow | None:
    """Return the bounded read ``head`` or ``tail`` performed.

    TABLE ROWS: ``head [-n N] F`` and ``tail [-n N] F``.

    The count is the READ's line range, so it travels on the record: ``head
    -20 f`` returned lines 1-20, and a record without that says the whole file
    came back. A SIGNED count names a different window entirely, which
    :func:`_line_range` resolves.
    """
    if len(argv) == 2:
        # The default is 10 lines, but stating it would report a bound the
        # command never gave; an unstated count is not a known one.
        if argv[1].startswith("-"):
            return None
        return ("read", argv[1], nodes[1].pos, None, ())
    if len(argv) == 4 and argv[1] in {"-n", "--lines"}:
        count, path_index = argv[2], 3
    elif len(argv) == 3 and (
        argv[1].startswith("-n") or argv[1].startswith("--lines=")
    ):
        count = argv[1].split("=", 1)[-1].removeprefix("-n")
        path_index = 2
    elif len(argv) == 3 and re.fullmatch(r"-\d+", argv[1]):
        # ``tail -80 f``: the historical spelling, and the one agents write --
        # 4 of 4 captured ``tail`` reads in one corpus used it.
        count, path_index = argv[1][1:], 2
    else:
        return None
    if re.fullmatch(r"[+-]?\d+", count) is None:
        return None
    path = argv[path_index]
    if path.startswith("-"):
        return None
    return ("read", path, nodes[path_index].pos, None, (_line_range(utility, count),))


def _line_range(utility: str, count: str) -> tuple[int | None, int | None]:
    """Return the lines a signed ``head``/``tail`` count names.

    A sign changes WHICH lines print, not merely how many, so it cannot be
    stripped. Measured against coreutils on an 8-line file:

    * ``tail -n +5`` printed lines 5-8 -- a FROM-line address, running to an
      end this reader cannot number. ``+0`` and ``+1`` both print the whole
      file, since line 0 is not a line.
    * ``head -n -5`` printed lines 1-3 -- it DROPS the last five, so the read
      starts at line 1 and ends where only the file's length says.
    * ``tail -n -5`` and ``tail -n 5`` are the same count from the end.
    """
    lines = int(count.lstrip("+-"))
    if utility == "tail":
        return (max(lines, 1), None) if count.startswith("+") else (None, lines)
    return (1, None) if count.startswith("-") else (1, lines)


def _sed_operation(
    argv: Sequence[str], nodes: Sequence[_BashNode]
) -> MatchedRow | None:
    """Return the bounded read a ``sed -n`` script performed.

    TABLE ROW: ``sed -n 'N,Mp' F``, the single most common read agents write
    -- 181 of 279 captured codex reads. The script IS the line range, so it
    becomes the record's ranges rather than being discarded.

    A script may hold SEVERAL addresses (``'84p;101p;132p'``), which is why
    ranges are a tuple: each clause contributes its own pair, and the gaps
    between them were never read.
    """
    if len(argv) != 4:
        return None
    option, script, path = argv[1:]
    if path.startswith("-") or option != "-n":
        return None
    clauses = [clause for clause in script.split(";") if clause]
    if not clauses:
        return None
    ranges: list[tuple[int | None, int | None]] = []
    for clause in clauses:
        found = _sed_clause_range(clause)
        if found is None:
            # Not a print, or an address this row does not model: the whole
            # script is refused rather than one clause silently dropped.
            return None
        ranges.append(found)
    return ("read", path, nodes[3].pos, None, tuple(ranges))


def _sed_clause_range(clause: str) -> tuple[int | None, int | None] | None:
    """Return the lines one ``sed`` clause prints, or ``None`` if it is no read.

    A print is a read. Any other command -- ``w`` writing a file, ``r``
    splicing one in, ``e`` running a shell command, ``s`` substituting --
    makes the invocation something this row does not describe.
    """
    match = re.fullmatch(r"(\d+|\$)(?:,(\d+|\$))?p", clause)
    if match is not None:
        # Line 0 is not a line: /bin/sed exits 1 with "invalid usage of line
        # address 0", so a script naming it read nothing. The exit code would
        # catch this on a lift, but ``rewrite_shell_source`` reaches here with
        # none.
        if match.group(1) == "0":
            return None
        return _sed_range(match.group(1), match.group(2))
    # ``/start/,/end/p`` and ``/start/,+N p``: a REGEX address, which only the
    # file itself could resolve to line numbers, but which still only prints.
    if re.fullmatch(r"/(?:[^/\\]|\\.)*/(?:,(?:/(?:[^/\\]|\\.)*/|\+\d+))?p", clause):
        return (None, None)
    return None


def _sed_range(first: str, last: str | None) -> tuple[int | None, int | None]:
    """Return the line range a ``sed -n`` address describes.

    ``$`` is the file's last line, whose number depends on the file, so a
    range ending there states its start and leaves the count unknown.
    """
    if first == "$":
        return (None, 1 if last is None else None)
    start = int(first)
    if last is None:
        return (start, 1)
    if last == "$":
        return (start, None)
    # A second address BEFORE the first does not run backwards: sed prints the
    # start line and stops, verified against /bin/sed for ``40,20p`` on a
    # 50-line file. Reporting zero lines claimed the read returned nothing.
    return (start, max(int(last) - start + 1, 1))


def _stencil_command(
    command: tuple[str, ...] | None, result: FileResult
) -> tuple[str, ...] | None:
    """Rewrite changed semantic fields into the original command stencil."""
    if command is None:
        return command
    source = _shell_source(command)
    operation = _operation(source[0]) if source is not None else None
    if source is None or operation is None:
        return command
    if source[1] is None:
        if result.path is None or operation[1] == result.path:
            return command
        # By the word the operation NAMED, not the last one: an argv whose
        # path is followed by a flag -- ``tee F -a`` -- had the flag replaced
        # instead, turning an append into a truncate and leaving the path
        # untouched. The same value-versus-position defect the matchers carry
        # ``(word, index)`` pairs to avoid.
        rewritten = list(command)
        at = next(
            (
                index
                for index, word in enumerate(command)
                if word == operation[1] and index > 0
            ),
            len(command) - 1,
        )
        rewritten[at] = result.path
        return tuple(rewritten)
    rewritten = list(command)
    rewritten[source[1]] = rewrite_shell_source(source[0], result)
    return tuple(rewritten)
