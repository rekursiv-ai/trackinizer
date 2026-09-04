"""Decompose a unified diff into splices, and render them back.

Both providers state file edits as diff text -- codex writes a per-path
``unified_diff``, claude a ``structuredPatch`` of the same lines -- while
:class:`~trackinizer.lib.agent.types.sessions.FileEditResult` states them as
:class:`~trackinizer.lib.agent.types.sessions.Splice` records. This is the one
conversion between those, so a diff read here and written back reproduces the
provider's own bytes.

One splice per CHANGED RUN, not per hunk: a hunk may hold several runs
separated by unchanged lines, and folding them together would attach context to
a change it does not belong to. The unchanged text rides on the splice as
``lead`` and ``trail`` -- both sides, because a run ending in context loses
that line otherwise, which was 10 of 71 captured codex diffs.
"""

from __future__ import annotations

from collections.abc import (
    Sequence,
    Set as AbstractSet,
)
from dataclasses import replace
from typing import Final

import re

from trackinizer.lib.agent.types.sessions import Splice


__all__ = ["parse_udiff", "render_udiff"]


_NO_NEWLINE: Final = "\\ No newline at end of file"
"""Git's annotation that the line before it carried no terminator."""


def parse_udiff(diff: str) -> tuple[Splice, ...]:
    """Decompose unified diff text into the replacements it describes.

    Args:
      diff: Unified diff body, hunk headers included.

    Returns:
      edits: One splice per changed run, in the order the diff printed them.

    """
    out: list[Splice] = []
    lead: list[str] = []
    removed: list[str] = []
    added: list[str] = []
    trail: list[str] = []
    start: int | None = None
    # Where the NEXT old-file line sits, tracked as the diff is walked. The
    # header names the hunk's first line, and every context or removed line
    # after it advances one -- so a run's own start is knowable, which the
    # ``@@`` numbers alone never were.
    at: int | None = None
    # Which sides ended without a newline, as git's own annotation states it.
    bare: set[str] = set()
    # A final line without its newline is still a line: captured codex patches
    # end that way, and dropping it discarded that change entirely -- a ``+A``
    # vanished and the edit replayed as a pure deletion.
    terminated = diff.endswith("\n")
    lines = _lines(diff)
    for index, line in enumerate(lines):
        header = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
        if header is not None:
            _close(
                out,
                lead=lead,
                removed=removed,
                added=added,
                trail=trail,
                start=start,
                bare=bare,
            )
            lead = [line]
            removed = []
            added = []
            trail = []
            bare = set()
            at = int(header.group(1))
            start = None
            continue
        if line.startswith(_NO_NEWLINE):
            # Metadata ABOUT the line before it, not content: git writes it to
            # say that line carried no terminator. Treating it as context split
            # one replacement in two and gave both sides a newline the file
            # lacks.
            #
            # Attributed by the PRECEDING line's own marker, not by which side
            # the run has accumulated: git writes the annotation after a
            # trailing CONTEXT line whenever the file's unchanged last line has
            # no terminator (``git diff -U1`` on a file ending ``ctx``), and
            # reading that as the added side moved it one line early on
            # rewrite.
            prior = lines[index - 1] if index else ""
            bare.add(
                "after"
                if prior.startswith("+")
                else "before"
                if prior.startswith("-")
                else "trail"
                if removed or added
                else "lead"
            )
            continue
        if line.startswith(("-", "+")):
            if trail:
                # Context already closed the previous run, so this line opens
                # a new splice whose lead is that context.
                carried = trail
                _close(
                    out,
                    lead=lead,
                    removed=removed,
                    added=added,
                    trail=[],
                    start=start,
                    bare=bare,
                )
                lead = carried
                removed = []
                added = []
                trail = []
                bare = set()
                start = None
            if start is None:
                start = at
            if line.startswith("-"):
                removed.append(line[1:])
                at = at + 1 if at is not None else None
            else:
                added.append(line[1:])
            continue
        (trail if removed or added else lead).append(line)
        at = at + 1 if at is not None else None
    _close(
        out,
        lead=lead,
        removed=removed,
        added=added,
        trail=trail,
        start=start,
        bare=bare,
    )
    if out and not terminated and not diff.endswith(_NO_NEWLINE):
        # The last line had no newline, so the last splice's last field must
        # not claim one -- otherwise rendering adds a byte the patch lacked.
        #
        # Unless the annotation is what ends the diff: the missing terminator
        # was already stated, and stripping again would take a real newline.
        out[-1] = _unterminate(out[-1])
    return tuple(out)


def _unterminate(splice: Splice) -> Splice:
    """Drop the trailing newline from a splice's last populated field."""
    for name in ("trail", "after", "before", "lead"):
        value = getattr(splice, name)
        if value:
            return replace(splice, **{name: value.removesuffix("\n")})
    return splice


def render_udiff(edits: Sequence[Splice]) -> str:
    """Render splices back as the unified diff text they came from.

    A splice carrying no context and no position -- claude states its edits
    that way -- renders as its bare ``-``/``+`` lines rather than gaining a
    hunk header it never had.

    Args:
      edits: Splices to render.

    Returns:
      diff: Unified diff body.

    """
    out: list[str] = []
    for splice in edits:
        out.append(splice.lead or "")
        # git's annotation is re-emitted only where the source stated one. A
        # splice whose text merely lacks ``\n`` is a DIFFERENT fact -- an
        # append states the bytes it wrote, and no patch ever annotated them --
        # so the flag travels rather than being inferred from the text.
        out.extend(_marked("-", splice.before, terminate="before" in splice.bare))
        out.extend(_marked("+", splice.after, terminate="after" in splice.bare))
        trail = splice.trail or ""
        out.append(
            f"{trail}\n{_NO_NEWLINE}\n" if "trail" in splice.bare and trail else trail
        )
    return "".join(out)


def _marked(mark: str, text: str | None, *, terminate: bool = False) -> list[str]:
    """Return each line of ``text`` under ``mark``, keeping its termination.

    Text whose last line carries no newline renders without one, so a patch
    that ended mid-line rebuilds exactly as the provider wrote it. When more
    diff FOLLOWS that line, the missing terminator is stated the way git does
    -- an annotation -- because the next line has to start somewhere.
    """
    lines = _lines(text)
    if not lines:
        return []
    out = [f"{mark}{line}\n" for line in lines]
    if text is not None and not text.endswith("\n"):
        out[-1] = out[-1].removesuffix("\n")
        if terminate:
            out[-1] += f"\n{_NO_NEWLINE}\n"
    return out


def _lines(text: str | None) -> list[str]:
    """Return text as its lines, terminated or not.

    A final line without its newline is still a line: ``printf hi >> f``
    appends exactly ``hi``, and dropping the last piece unconditionally
    rendered that whole edit as nothing.
    """
    if not text:
        return []
    pieces = text.split("\n")
    return pieces[:-1] if pieces[-1] == "" else pieces


def _close(
    out: list[Splice],
    *,
    lead: Sequence[str],
    removed: Sequence[str],
    added: Sequence[str],
    trail: Sequence[str],
    start: int | None,
    bare: AbstractSet[str],
) -> None:
    """Append the splice being accumulated, when it holds anything.

    ``count`` is the lines the run REPLACED, which is what the field states --
    the ``@@`` header's own count spans the whole hunk, context included, and
    copying it made every splice claim lines it did not touch.
    """
    if not (removed or added or lead or trail):
        return
    before = _joined(removed)
    after = _joined(added)
    trailing = _joined(trail)
    out.append(
        Splice(
            before=before.removesuffix("\n")
            if before is not None and "before" in bare
            else before,
            after=after.removesuffix("\n")
            if after is not None and "after" in bare
            else after,
            lead=_joined(lead),
            trail=trailing.removesuffix("\n")
            if trailing is not None and "trail" in bare
            else trailing,
            start=start if removed or added else None,
            count=len(removed) if (removed or added) else None,
            bare=frozenset(bare & {"before", "after", "trail"}),
        )
    )


def _joined(lines: Sequence[str]) -> str | None:
    """Return newline-terminated text, or ``None`` when there was none."""
    return "".join(f"{line}\n" for line in lines) if lines else None
