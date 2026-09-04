"""Join session files into the one conversation a person actually had.

A CLI splits a long conversation across files, and neither the transcript nor
the IR records the seam -- the session simply stops in one file and resumes in
another. Fusing puts the seam back as the record that already means it: a
:class:`ContextClear`, which is what opens every context window whatever caused
the reset. A part that carried a summary across the seam states it there, and
one that opened fresh does not.

Unfusing is the inverse: split at those records and every part is the record
stream it came from. That is the invariant this module is built around --
``unfuse(fuse(parts)) == parts`` -- so a fused session is a lossless view, not
a lossy merge.

Streams on BOTH sides, never sequences (axiom 11): a part is an iterable and
both functions yield, so joining a 273 MB conversation holds the seam records
rather than a copy of the files.

What a boundary states is decided from a bounded PREFIX of the part it
introduces, because the grammar bounds it: a summary that crossed the seam is
written as that file's FIRST user turn, so once the model has spoken no summary
is coming. The prefix is buffered, the boundary emitted, and the rest of the
part streams straight through behind it. Measured on the captured fixtures, the
deepest that prefix ran was 11 records of a 137-record file.

Unfusing yields one ITERATOR per part, consumed in order: a part is the records
between two seams, so the third file cannot be handed back before the first two
have gone past. Abandoning a part still walks it -- that is what asking for the
next one costs -- but it is walked, not held.

Which file follows which is the provider's own bookkeeping, and it lives in
the residual the adapters kept: codex names ``forked_from_id`` on its launch
settings, claude marks the carried-over summary with ``isCompactSummary``.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence

from trackinizer.lib.agent.types.sessions import (
    ContextClear,
    SessionRecord,
    TurnContext,
    UserMessage,
)
from trackinizer.lib.custom_json import (
    DictCodec,
    StrCodec,
    json_freeze,
    json_unfreeze,
)


__all__ = ["fuse", "names_of", "unfuse"]


def fuse(
    parts: Iterable[Iterable[SessionRecord]],
    names: Sequence[str] = (),
    *,
    seam: str = "$seam",
) -> Iterator[SessionRecord]:
    """Join record streams into one, a boundary record at every seam.

    Args:
      parts: Each file's records, in the order they were written. Iterated
        lazily, so a part is walked once and never held.
      names: What each part was called on disk, so unfusing can put the files
        back where they came from. Optional: a caller that only wants the
        conversation does not need them.
      seam: Residual key marking a boundary THIS function inserted. A part may
        contain a reset of its own -- codex compacts in place -- and splitting
        there would invent a file that never existed.

    Yields:
      record: Every part's records in order, separated by the
        :class:`ContextClear` naming each seam.

    """
    for index, part in enumerate(parts):
        stream = iter(part)
        if not index:
            if names:
                # The root's own name has nowhere else to ride: there is no
                # seam before the first part, so it rides that part's opening
                # context.
                yield from _named(stream, names[0], seam=seam)
                continue
            yield from stream
            continue
        prefix, boundary = _boundary(
            stream, names[index] if index < len(names) else "", seam=seam
        )
        yield boundary
        yield from prefix
        yield from stream


def _named(
    part: Iterator[SessionRecord], name: str, *, seam: str
) -> Iterator[SessionRecord]:
    """Yield a part with its file name on the settings it opens with."""
    for index, record in enumerate(part):
        if index or not isinstance(record, TurnContext):
            yield record
            continue
        extra = dict(json_unfreeze(record.extra))
        extra[seam] = name
        yield _restated(record, extra)


def unfuse(
    records: Iterable[SessionRecord], *, seam: str = "$seam"
) -> Iterator[Iterator[SessionRecord]]:
    """Split a fused stream back into the files it was joined from.

    Yields one ITERATOR per part, and they are consumed IN ORDER: a part is
    the records between two seams, so nothing of the third file exists until
    the first two have gone past. A caller that abandons a part still walks
    it -- asking for the next one is what walks it -- but it is walked rather
    than held.

    Args:
      records: A fused record stream carrying boundary records.
      seam: Residual key marking a boundary :func:`fuse` inserted. Must match
        what fusing used, or a part carrying its own reset splits into a file
        that never existed.

    Yields:
      part: One source file's records, in the order they were written.

    """
    stream = iter(records)
    # A one-slot buffer, because the boundary that ENDS a part is only seen by
    # trying to read past it: the record belongs to the next part's decision,
    # not to this one's output.
    pending: list[SessionRecord] = []
    ended = False
    root = True
    while not ended:
        part = _until_seam(stream, pending, seam=seam, root=root)
        yield part
        # Drained here rather than trusted to the caller: the next part starts
        # where this one stopped, and a caller that took two records and moved
        # on would otherwise splice this file's tail onto the next.
        for _ in part:
            pass
        ended = not pending
        pending.clear()
        root = False


def _until_seam(
    stream: Iterator[SessionRecord],
    pending: list[SessionRecord],
    *,
    seam: str,
    root: bool,
) -> Iterator[SessionRecord]:
    """Yield one part's records, stopping at the boundary that ends it."""
    for record in stream:
        if isinstance(record, ContextClear) and seam in record.extra:
            # Kept, so the loop above can tell "a seam followed" from "the
            # stream ended" -- one more part exists in the first case only.
            pending.append(record)
            return
        yield _unnamed(record, seam=seam) if root else record


def _unnamed(record: SessionRecord, *, seam: str) -> SessionRecord:
    """Return the root's opening settings without the name fusing added."""
    if not isinstance(record, TurnContext) or seam not in record.extra:
        return record
    return _restated(
        record,
        {
            key: value
            for key, value in dict(json_unfreeze(record.extra)).items()
            if key != seam
        },
    )


def _restated(record: TurnContext, extra: Mapping[str, object]) -> TurnContext:
    """Return one context with a different residual and nothing else moved."""
    return TurnContext(
        context_id=record.context_id,
        timestamp=record.timestamp,
        permission=record.permission,
        model=record.model,
        effort=record.effort,
        summary_kind=record.summary_kind,
        encoding=record.encoding,
        extra=json_freeze(dict(extra)),
    )


def _boundary(
    part: Iterator[SessionRecord],
    name: str,
    *,
    seam: str,
    forked_from: str = "forked_from_id",
    summary: str = "isCompactSummary",
) -> tuple[list[SessionRecord], SessionRecord]:
    """Return the prefix read and the record naming what ``part`` resumed.

    A window opens either way, so the boundary IS a clear -- carrying the
    summary when one crossed the seam. It stores nothing else: the part keeps
    its OWN opening records, so unfusing drops the boundary and the file
    reassembles from what it already had.

    BOUNDED, though it reads ahead: claude compacts by writing the earlier
    conversation's summary as the file's FIRST user turn, flagged as such, so
    the first turn settles the question and everything before it is the state
    the file opens with. Measured on the captured fixtures, the deepest that
    ran was 11 records of a 137-record file. The prefix comes back rather than
    being re-read, so the part is walked once.

    Args:
      part: The records of the file that resumed another.
      name: What that file was called on disk.
      seam: Residual key marking a boundary this module inserted.
      forked_from: Residual key codex writes on a rollout's launch settings.
      summary: Residual key claude writes on a carried-over summary turn.

    Returns:
      prefix: The records read to decide, to be emitted after the boundary.
      boundary: The clear that opens the resumed file's window.

    """
    prefix: list[SessionRecord] = []
    opened: str | None = None
    carried: str | None = None
    for record in part:
        prefix.append(record)
        if isinstance(record, TurnContext) and opened is None:
            opened = record.timestamp or ""
        if isinstance(record, UserMessage):
            if forked_from in record.extra or summary in record.extra:
                carried = record.content
            break
    return prefix, ContextClear(
        timestamp=opened or None,
        summary=carried,
        extra=json_freeze({seam: name}),
    )


def names_of(records: Iterable[SessionRecord], *, seam: str = "$seam") -> list[str]:
    """Return the file name each part of a fused stream came from.

    The root's name rides on the settings it opens with; every part after it
    rides on the seam that introduced it.

    Args:
      records: A fused record stream.
      seam: Residual key marking a boundary :func:`fuse` inserted.

    Returns:
      names: One file name per part, in the order they were written.

    """
    out: list[str] = []
    for record in records:
        if isinstance(record, TurnContext) and seam in record.extra and not out:
            out.append(StrCodec.coerce(dict(json_unfreeze(record.extra)).get(seam)))
        elif isinstance(record, ContextClear) and seam in record.extra:
            if not out:
                out.append("")
            out.append(StrCodec.coerce(dict(json_unfreeze(record.extra)).get(seam)))
    return out


def chain(
    parts: Iterable[Sequence[SessionRecord]], *, forked_from: str = "forked_from_id"
) -> list[Sequence[SessionRecord]]:
    """Return parts ordered by which one continues which.

    Codex names the thread a rollout forked from, and 1495 of 1495 captured
    links resolve inside the tree -- so the order is the provider's, not a
    guess from timestamps.

    Args:
      parts: Each file's records.
      forked_from: Launch-settings key naming the thread a rollout continued.

    Returns:
      ordered: Each root followed by the chain that continues it.

    """
    found = list(parts)
    by_id: dict[str, Sequence[SessionRecord]] = {}
    for part in found:
        own = StrCodec.coerce(_declared(part).get("id"))
        if own:
            by_id[own] = part
    # A LIST per parent: a thread may be resumed more than once, and keeping
    # one successor apiece dropped every earlier fork -- 16 of 392 rollouts on
    # one captured day, which rebuilt 84 MB short and reported no error.
    successors: dict[str, list[Sequence[SessionRecord]]] = {}
    roots: list[Sequence[SessionRecord]] = []
    for part in found:
        parent = StrCodec.coerce(_declared(part).get(forked_from))
        if parent and parent in by_id:
            successors.setdefault(parent, []).append(part)
        else:
            roots.append(part)
    ordered: list[Sequence[SessionRecord]] = []
    seen: set[int] = set()
    for root in roots:
        # Depth-first, so a fork's own continuation follows it rather than
        # every sibling being emitted first.
        stack = [root]
        while stack:
            part = stack.pop()
            if id(part) in seen:
                # A cycle in the provider's own links: visiting twice would
                # duplicate the part rather than order it.
                continue
            seen.add(id(part))
            ordered.append(part)
            own = StrCodec.coerce(_declared(part).get("id"))
            stack.extend(reversed(successors.get(own, [])))
    # A component whose links form a CYCLE has no root, so the walk above never
    # started on it -- the ``seen`` guard only stops a re-visit, it cannot reach
    # an unreachable node. Those parts were dropped silently, and a file that
    # forks from itself emptied the list entirely, which the caller then
    # indexed. Emitted in input order, after the rooted chains, so ``chain`` is
    # total.
    ordered.extend(part for part in found if id(part) not in seen)
    return ordered


def _declared(part: Sequence[SessionRecord]) -> dict[str, object]:
    """Return the launch settings a part declared, by its wire key names."""
    for record in part:
        if isinstance(record, TurnContext):
            return DictCodec.coerce(dict(json_unfreeze(record.extra)).get("payload"))
    return {}
