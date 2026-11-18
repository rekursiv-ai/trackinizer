"""Re-enter a stored session: ``trax agentsession 42 run claude``.

Reads every part of a captured session, writes the conversation as the target CLI's
own native file, and returns where it landed. The target is chosen HERE rather than by
whatever captured the session, which is the point: a codex-captured session resumes as
claude, and a session whose parts were written by bot, claude and codex resumes
as any one of them.

The run re-enters the SAME AgentSession (``Materialized.attach``) and uploads only what
the CLI writes after the loaded file prefix (``Materialized.prefix_bytes``). The chat
keeps its own ``chat:<conv>`` identity, so nothing stamps the CLI's id onto the row:
attaching by id replaces the correlation that the old stamp existed for.

A merged session loads its window (``client/context_window.py``): what the last model
was looking at, from the last compaction of any provider, within the target's budget.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from functools import partial
from io import StringIO
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from treekle import from_plain, immutable

from trackinizer.client.context_window import cli_records, weight, window
from trackinizer.client.conversation import (
    Segment,
    read_part,
    read_segments,
)
from trackinizer.lib.agent.sessions import (
    claude,
    codex,
)
from trackinizer.lib.agent.types.sessions import (
    ContextClear,
    SessionRecord,
    TurnContext,
)
from trackinizer.trax.run.errors import (
    LossyConversionError,
    NotResumableError,
)
from trackinizer.trax.run.materialize import (
    RESUMABLE_TARGETS,
    Materialized,
    materialize,
    prepared,
)
from trackinizer.trax.run.truncate import was_truncated, without_note
from trackinizer.wire.refs import UuidRef


if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from trackinizer.client.client import Client
    from trackinizer.wire.wire_session_ir import PartBody


__all__ = ["Resumed", "prepare_resume"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Resumed(Materialized):
    """A session written for its CLI, and how much of the conversation it holds."""

    loaded_records: int
    """How many records of the conversation the file holds."""

    estimated_tokens: int
    """What they weigh, estimated as UTF-8 bytes over four."""

    skipped: int
    """How many stored records are not loaded: before the last compaction or
    ``/clear``, or over the target's budget."""

    compact_after_load: bool
    """Whether the load is past what the target holds comfortably, so the run asks it
    to compact what it loaded before anything else is typed."""


def prepare_resume(
    client: Client,
    session_id: UUID,
    target: str,
    *,
    lossy: bool = False,
) -> Resumed:
    """Write the session's conversation as ``target``'s own file, ready to resume.

    Args:
      client: Talks to the server holding the session.
      session_id: The AgentSession to re-enter.
      target: The CLI to resume AS, independent of what captured it.
      lossy: Whether to accept a conversion that DROPS records the target format
        cannot express, or a part holding records capture truncated to fit an upload.
        Refused by default: a silently shortened transcript is a conversation the
        model never had, and the caller cannot tell from the file that anything is
        missing.

    Returns:
      written: The file's path, the id ``--resume`` takes, the AgentSession to attach
        to, the size of the loaded prefix, and how much of the conversation it holds.

    Raises:
      NotResumableError: The target cannot be re-entered, or the session has no part
        carrying a native format.
      LossyConversionError: The conversion drops records, or a part holds truncated
        ones, and ``lossy`` is false.

    """
    if target not in RESUMABLE_TARGETS:
        raise NotResumableError(
            f"{target!r} cannot resume a stored session: it has no stable "
            "per-session id to re-enter with. "
            f"Resumable: {', '.join(sorted(RESUMABLE_TARGETS))}. "
            "The session is still downloadable in its own format.",
        )
    # A part whose format is empty is an ``sh`` scrape: searchable, never resumable.
    native = [part for part in client.read_session_parts(session_id) if part.format]
    if not native:
        raise NotResumableError(
            f"session {session_id} has no part with a native format, so there "
            "is no transcript to hand back to a CLI",
        )
    if len(native) == 1 and native[0].format == target:
        return _same_format(client, session_id, target, native[0], lossy=lossy)
    return _merged(client, session_id, target, lossy=lossy)


# A single part the target already speaks is replayed as it was captured: the provider's
# own file, with its own sealed reasoning, is the one thing a provider is sure to accept.
def _same_format(
    client: Client,
    session_id: UUID,
    target: str,
    part: PartBody,
    *,
    lossy: bool,
) -> Resumed:
    """Replay one same-format part as it was captured."""
    # Read WITH the ciphertext, unlike a viewer: a replay is the one caller that needs the
    # sealed half, since the provider validates it.
    records, sealed = read_part(client, session_id, part.part, plaintext_only=False)
    if not lossy and (cut := sum(map(was_truncated, records))):
        raise LossyConversionError(
            f"{cut} record(s) of session {session_id} were truncated at capture "
            "to fit an upload; pass --lossy to accept a shortened transcript",
        )
    # The note is Trackinizer's, not the CLI's: a writer replays an uncategorized
    # record's payload verbatim, note and all.
    records = [without_note(record) for record in records]
    # MEASURED, not predicted from the format pair: the same two formats can be lossless
    # for one session and lossy for another, depending on which record kinds it actually
    # holds. ``convert`` decides losses the same way.
    encoding = immutable(from_plain(part.metadata, dict[str, object]))
    minted = uuid4()
    ready = prepared(
        target=target,
        records=records,
        encoding=encoding,
        sealed=sealed,
        source=part.format,
        session_id=minted,
    )
    if not lossy and (
        dropped := _undroppable(records, part.format, target, ready=ready)
    ):
        raise LossyConversionError(
            f"resuming a {part.format!r} session as {target!r} drops "
            f"{', '.join(dropped)}; pass --lossy to accept a shortened "
            "transcript",
        )
    written = materialize(
        target=target,
        records=records,
        # How the SOURCE FILE spelled its bytes. Claude's ascii-escaping convention
        # rides on the context in force; rewriting without it escapes different
        # characters, so the bytes differ even though every record matches.
        encoding=encoding,
        sealed=sealed,
        session_id=minted,
        # What captured it, so the writer knows whether this is a crossing: a
        # provider's reasoning seal and its unparsed lines replay only into the format
        # they were read from.
        source=part.format,
        attach=session_id,
    )
    # The file is whole, but its own CLI reads only from its last compaction on, which
    # is what the window counts.
    seen = window(
        [Segment(part=part.part, name=part.name, format=part.format, records=records)],
    )
    held = [each.record for each in (*seen.history, *seen.records)]
    summary = "" if seen.opening is None else seen.opening.clear.summary or ""
    return _resumed(
        written,
        loaded_records=len(held),
        estimated_tokens=(len(summary.encode()) + sum(map(weight, held))) // 4,
        skipped=seen.skipped,
        compact_after_load=False,
    )


# Every part is read, not the newest, because a conversation is spread over them: a bot
# part, then the claude part a switch answered in. What is loaded is the window: from
# the last compaction of any part on, within the target's budget, so a long chat never
# hands the target more than it can hold. The merged file is written from the
# provider-neutral conversation, so it is foreign to the target and states that
# encoding, which is what makes claude synthesize the identity it needs.
def _merged(
    client: Client,
    session_id: UUID,
    target: str,
    *,
    lossy: bool,
) -> Resumed:
    """Write the session's window as one conversation the target answers from."""
    segments = read_segments(client, session_id)
    if not lossy and (
        cut := sum(was_truncated(record) for seg in segments for record in seg.records)
    ):
        raise LossyConversionError(
            f"{cut} record(s) of session {session_id} were truncated at capture "
            "to fit an upload; pass --lossy to accept a shortened transcript",
        )
    loaded = cli_records(
        window(
            [
                replace(seg, records=[without_note(record) for record in seg.records])
                for seg in segments
            ],
        ),
        target,
        seq=partial(_seq, client, session_id),
    )
    projected = loaded.records
    encoding = immutable({"newline_terminated": True})
    minted = uuid4()
    ready = prepared(
        target=target,
        records=projected,
        encoding=encoding,
        session_id=minted,
    )
    # The loss gate measures what the target's writer is given, so a person's line counts
    # as the user turn it now is, not as an act the target never had.
    if not lossy and (dropped := _undroppable(projected, None, target, ready=ready)):
        raise LossyConversionError(
            f"resuming this session as {target!r} drops {', '.join(dropped)}; "
            "pass --lossy to accept a shortened transcript",
        )
    return _resumed(
        materialize(
            target=target,
            records=projected,
            encoding=encoding,
            session_id=minted,
            attach=session_id,
        ),
        loaded_records=loaded.loaded_records,
        estimated_tokens=loaded.estimated_tokens,
        skipped=loaded.skipped,
        compact_after_load=loaded.compact_after_load,
    )


def _resumed(
    written: Materialized,
    *,
    loaded_records: int,
    estimated_tokens: int,
    skipped: int,
    compact_after_load: bool,
) -> Resumed:
    """Return the written file with how much of the conversation it holds."""
    return Resumed(
        path=written.path,
        cli_session_id=written.cli_session_id,
        attach=written.attach,
        prefix_bytes=written.prefix_bytes,
        loaded_records=loaded_records,
        estimated_tokens=estimated_tokens,
        skipped=skipped,
        compact_after_load=compact_after_load,
    )


# The note names the session the way a person types it, so it is asked only when the
# note is written: a window that leaves nothing out costs no request.
def _seq(client: Client, session_id: UUID) -> int:
    """Return the session's seq, which ``trax agentsession N records`` takes."""
    _kind, _target_id, view = client.get_inquiry(UuidRef(uuid=session_id))
    return from_plain(from_plain(view["self"], dict[str, object])["seq"], int)


# Written and re-read rather than reasoned about: whether a conversion loses anything
# depends on which kinds this session HOLDS, not on the format pair alone. The rewrite
# is thrown away -- only its record population is read.
#
# Measured on the file that will actually be WRITTEN, identity and all: materialization
# stamps the target's own id first (codex needs a ``session_meta`` line, synthesized
# when the source carried none), and that line is what an opening ``ContextClear`` rides
# out on. Measuring the unstamped records named a drop the written file does not have.
#
# Only ACTS count. State records are DERIVED -- each adapter states settings in its own
# shape, claude repeating an envelope per line where codex declares once per turn -- so
# their counts legitimately differ across a crossing while no turn is touched. Counting
# them made every claude-to-codex resume demand ``--lossy`` for a transcript that loses
# nothing, which is a flag meaning the opposite of what it says.
def _undroppable(
    records: Sequence[SessionRecord],
    source: str | None,
    target: str,
    *,
    ready: Sequence[SessionRecord] | None = None,
) -> tuple[str, ...]:
    """Acts writing as ``target`` would lose, measured by rewriting."""
    if ready is None:
        ready = prepared(
            target=target,
            records=records,
            encoding=immutable({}),
            source=source,
            session_id=uuid4(),
        )
    writer = claude if target == "claude" else codex
    out = StringIO()
    writer.denormalize(ready, out)
    rebuilt = writer.normalize(StringIO(out.getvalue()))
    return tuple(sorted((_acts(records) - _acts(rebuilt)).elements()))


# ``TurnContext`` and ``ContextClear`` are restated per format rather than conveyed, so
# a differing count is a spelling difference, not a loss.
def _acts(records: Iterable[SessionRecord]) -> Counter[str]:
    """Count the records carrying a turn, ignoring derived state."""
    return Counter(
        type(record).__name__
        for record in records
        if not isinstance(record, TurnContext | ContextClear)
    )
