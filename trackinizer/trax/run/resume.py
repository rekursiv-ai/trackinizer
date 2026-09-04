"""Re-enter a stored session: ``trax agentsession 42 run claude``.

Reads a captured session back out of Trackinizer, writes it as the target
CLI's own native file, and enters the runner pointed at it. The target is
chosen HERE rather than by whatever captured the session, which is the point:
a codex-captured session resumes as claude.

Order is load-bearing. The server is stamped with the minted id BEFORE the run
opens its session, because ``store/session.py::_resume_session`` re-attaches by
finding an existing row whose ``agentsession_cli_session_id`` matches -- without
the stamp the run forks a second AgentSession and the transcript splits in two.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from io import StringIO
from uuid import UUID, uuid4

from trackinizer.client.client import Client
from trackinizer.lib.agent.sessions import (
    claude as claude_ir,
    codex as codex_ir,
)
from trackinizer.lib.agent.types.sessions import (
    ContextClear,
    SessionRecord,
    TurnContext,
)
from trackinizer.lib.custom_json import DictCodec, json_freeze, json_unfreeze
from trackinizer.trax.run.errors import (
    LossyConversionError,
    NotResumableError,
)
from trackinizer.trax.run.materialize import (
    RESUMABLE_TARGETS,
    Materialized,
    identified,
    materialize,
)
from trackinizer.types.streams import Stderr, Stdin, Stdout


__all__ = ["prepare_resume"]


def prepare_resume(
    client: Client, session_id: UUID, target: str, *, lossy: bool = False
) -> Materialized:
    """Stamp the server, materialize the newest part, return where it landed.

    Args:
      client: Talks to the server holding the session.
      session_id: The AgentSession to re-enter.
      target: The CLI to resume AS, independent of what captured it.
      lossy: Whether to accept a conversion that DROPS records the target
        format cannot express. Refused by default: a silently shortened
        transcript is a conversation the model never had, and the caller
        cannot tell from the file that anything is missing.

    Returns:
      written: The file's path and the id ``--resume`` takes.

    Raises:
      NotResumableError: The target cannot be re-entered, or the session has no
        part carrying a native format.
      LossyConversionError: The conversion drops records and ``lossy`` is false.

    """
    if target not in RESUMABLE_TARGETS:
        raise NotResumableError(
            f"{target!r} cannot resume a stored session: it has no stable "
            "per-session id to re-enter with. "
            f"Resumable: {', '.join(sorted(RESUMABLE_TARGETS))}. "
            "The session is still downloadable in its own format."
        )
    parts = client.read_session_parts(session_id)
    # The NEWEST part carrying a native format. A part whose format is empty
    # is an ``sh`` scrape -- searchable, never resumable -- and skipping it
    # here is what lets a mixed session still resume its real transcript.
    native = [part for part in parts if part.format]
    if not native:
        raise NotResumableError(
            f"session {session_id} has no part with a native format, so there "
            "is no transcript to hand back to a CLI"
        )
    part = native[-1]
    records, sealed = _read_part(client, session_id, part.part)
    # MEASURED, not predicted from the format pair: the same two formats can
    # be lossless for one session and lossy for another, depending on which
    # record kinds it actually holds. ``convert`` decides losses the same way.
    if not lossy and (dropped := _undroppable(records, part.format, target)):
        raise LossyConversionError(
            f"resuming a {part.format!r} session as {target!r} drops "
            f"{', '.join(dropped)}; pass --lossy to accept a shortened "
            "transcript"
        )
    # The id is minted here and stamped BEFORE the runner opens its session,
    # so the resumed run re-attaches this row rather than forking a new one.
    minted = uuid4()
    client.set_cli_session_id(session_id, str(minted))
    return materialize(
        target=target,
        records=records,
        # How the SOURCE FILE spelled its bytes. Claude's ascii-escaping
        # convention rides on the context in force; rewriting without it
        # escapes different characters, so the bytes differ even though every
        # record matches.
        encoding=json_freeze(json_unfreeze(DictCodec.coerce(part.metadata))),
        sealed=sealed,
        session_id=minted,
        # What captured it, so the writer knows whether this is a crossing:
        # a provider's reasoning seal and its unparsed lines replay only into
        # the format they were read from.
        source=part.format,
    )


def _undroppable(
    records: Sequence[SessionRecord], source: str, target: str
) -> tuple[str, ...]:
    """Acts writing as ``target`` would lose, measured by rewriting.

    Written and re-read rather than reasoned about: whether a conversion loses
    anything depends on which kinds this session HOLDS, not on the format pair
    alone. The rewrite is thrown away -- only its record population is read.

    Measured on the file that will actually be WRITTEN, identity and all:
    materialization stamps the target's own id first (codex needs a
    ``session_meta`` line, synthesized when the source carried none), and that
    line is what an opening ``ContextClear`` rides out on. Measuring the
    unstamped records named a drop the written file does not have.

    Only ACTS count. State records are DERIVED -- each adapter states settings
    in its own shape, claude repeating an envelope per line where codex
    declares once per turn -- so their counts legitimately differ across a
    crossing while no turn is touched. Counting them made every claude-to-codex
    resume demand ``--lossy`` for a transcript that loses nothing, which is a
    flag meaning the opposite of what it says.

    A session resumed in the format it was captured in converts nothing, so it
    can lose nothing and the round trip is skipped.
    """
    if source == target:
        return ()
    writer = claude_ir if target == "claude" else codex_ir
    out = StringIO()
    writer.denormalize(identified(target, records, uuid4()), out)
    rebuilt = writer.normalize(StringIO(out.getvalue()))
    return tuple(sorted((_acts(records) - _acts(rebuilt)).elements()))


def _acts(records: Iterable[SessionRecord]) -> Counter[str]:
    """Count the records carrying a turn, ignoring derived state.

    ``TurnContext`` and ``ContextClear`` are restated per format rather than
    conveyed, so a differing count is a spelling difference, not a loss.
    """
    return Counter(
        type(record).__name__
        for record in records
        if not isinstance(record, TurnContext | ContextClear)
    )


def _read_part(
    client: Client, session_id: UUID, part: int
) -> tuple[Sequence[SessionRecord], Sequence[str | None]]:
    """Every record of one part, and the ciphertext each carried.

    Read WITH the ciphertext, unlike a viewer: a replay is the one caller that
    needs the sealed half, since the provider validates it.

    The store speaks ``TraxRecord``, which is wider than any CLI dialect. Only
    a part with a NATIVE format reaches here, and a scrape's stream records
    live only in a formatless part -- so the narrowing is a real invariant
    rather than a cast, and it is asserted so a future part that breaks it
    fails here instead of inside the claude writer.
    """
    records: list[SessionRecord] = []
    sealed: list[str | None] = []
    for body in client.read_session_records(session_id, part=part):
        row = body.row(session_id, part)
        record = row.record()
        assert not isinstance(record, Stdin | Stdout | Stderr), (
            f"a native part holds no stream records; {row.kind} came from one"
        )
        records.append(record)
        sealed.append(row.ciphertext)
    return records, sealed
