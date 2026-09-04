"""Write a stored session back out as a CLI's own native file, to resume it.

``trax agentsession 42 run claude`` hands a captured transcript back to a CLI
as something that CLI wrote. That is what the IR's byte-exact round-trip buys:
the file is not a rendering for a human, it is the provider's own format, and a
provider is entitled to reject a transcript it did not write.

Three things make the written file THIS machine's rather than the captured
one's:

- The session id is minted FRESH. Reusing the captured one would collide with
  the original locally, and across a crossing it would name an id the target
  CLI never issued.
- The path derives from the LOCAL machine's layout, which each adapter owns:
  claude encodes the working directory in its project directory name, codex
  shards by date. Resuming in a different directory is expected.
- The id INSIDE the file is rewritten to the minted uuid, so the file's
  contents and its name agree. WHERE that id lives is per-format, which is
  what :class:`_Target` names.

Ciphertext is spliced here, at materialization: it lives in its own table
precisely so retention can drop it, and a session whose sealed reasoning is
gone cannot be replayed to the provider (:class:`CiphertextDroppedError`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TextIO
from uuid import UUID, uuid4

import json
import shutil
import subprocess

from trackinizer.lib.agent.sessions import (
    claude as claude_ir,
    codex as codex_ir,
)
from trackinizer.lib.agent.types.sessions import (
    IncompleteRecord,
    SessionRecord,
    Thinking,
    TurnContext,
)
from trackinizer.lib.custom_json import JSON, DictCodec, json_freeze, json_unfreeze
from trackinizer.trax.run.adapters.claude import ClaudeAdapter
from trackinizer.trax.run.adapters.codex import CodexAdapter
from trackinizer.trax.run.errors import (
    CiphertextDroppedError,
    NotResumableError,
)


__all__ = [
    "RESUMABLE_TARGETS",
    "Materialized",
    "identified",
    "materialize",
    "materialize_claude",
]


RESUMABLE_TARGETS: frozenset[str] = frozenset({"claude", "codex"})
"""The CLIs a stored session can be handed back to.

A target qualifies when it NAMES a session by an id its CLI accepts on the
command line, and both of these do: ``claude --resume <uuid>`` finds a
transcript by file stem, and ``codex resume <SESSION_ID>`` finds a rollout by
the uuid its own filename ends with (verified against the installed CLI and a
captured rollout, whose launch line repeats that uuid as ``payload.id``).

Gemini and ``sh`` do not. Gemini rewrites one document in place and offers no
resume-by-id; a scrape has no native log at all. Both stay DOWNLOADABLE in
their own format -- what they lack is a way to be re-entered.

Because the target is chosen at RESUME time rather than by what captured the
session, every pairing works: a codex capture resumes as claude and a claude
capture resumes as codex, subject to the loss the conversion measures.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class Materialized:
    """A session written to disk, ready for its CLI to resume."""

    path: Path
    """Where the file was written, in the layout its own CLI reads."""

    cli_session_id: UUID
    """The freshly minted id: the file's name, the id stated INSIDE it, and
    the argument the CLI's resume flag takes are all this value."""


def materialize(
    *,
    target: str,
    records: Sequence[SessionRecord],
    encoding: JSON,
    sealed: Sequence[str | None] = (),
    session_id: UUID | None = None,
    source: str | None = None,
) -> Materialized:
    """Write ``records`` as ``target``'s own file, ready to be resumed.

    Args:
      target: Which CLI's format to write. Must be in
        :data:`RESUMABLE_TARGETS`.
      records: The part's records, in ``idx`` order.
      encoding: How the SOURCE FILE spelled its bytes, which is what lets the
        rewrite reproduce its escaping convention. Restated as a leading
        ``TurnContext``, because that is where the IR states it: the writer
        reads the last one in force, and a stored part's records begin at its
        own ``idx`` 0 rather than at the file's opening context.
      sealed: Each record's ciphertext, positionally aligned with ``records``
        (``None`` where a record has none). Empty means none was fetched,
        which is only correct for a session that stored none.
      session_id: The id to mint into the file; generated when omitted. Taken
        as an argument so the caller can stamp the server BEFORE writing --
        without that stamp a resumed run forks a second AgentSession.
      source: Which CLI captured ``records``. Names a CROSSING when it differs
        from ``target``, which is what decides whether a provider-sealed or
        unparsed record can be replayed; see :func:`_crossed`. Omitted means
        records built by hand, which cross nothing.

    Returns:
      written: The path and the id that names it.

    Raises:
      NotResumableError: ``target`` names no CLI this can write for.
      CiphertextDroppedError: A ``Thinking`` record needs sealed bytes that are
        not present.

    """
    spec = _TARGETS.get(target)
    if spec is None:
        raise NotResumableError(
            f"{target!r} cannot be materialized: no writer names a session it "
            f"could re-enter. Resumable: {', '.join(sorted(_TARGETS))}."
        )
    minted = session_id or uuid4()
    spliced = identified(
        target,
        [
            TurnContext(encoding=encoding),
            *_crossed(_spliced(records, sealed), source, target),
        ],
        minted,
    )
    # The adapter names the directory, rather than this module re-deriving each
    # CLI's layout: two spellings of one rule drift, and the one that drifts
    # writes a file into a directory the CLI never reads.
    scope = spec.scope()
    scope.mkdir(parents=True, exist_ok=True)
    path = scope / spec.filename(minted)
    with path.open("w", encoding="utf-8") as handle:
        spec.write(spliced, handle, minted)
    # AFTER the write: an index entry naming a file that does not exist offers
    # the operator a session whose selection fails.
    spec.announce(path, minted)
    return Materialized(path=path, cli_session_id=minted)


def identified(
    target: str, records: Sequence[SessionRecord], session_id: UUID
) -> list[SessionRecord]:
    """State ``session_id`` the way ``target``'s own format states identity.

    Public because the LOSS GATE has to measure the same records the writer
    will emit: stamping identity is what gives codex its ``session_meta``
    line, and an opening ``ContextClear`` has no line to ride out on without
    it. A gate that rewrote the unstamped records reported a drop the written
    file did not have.

    Args:
      target: Which CLI's format will be written.
      records: The records about to be written, state records included.
      session_id: The id the file will be named by.

    Returns:
      records: The same records, with identity restated for ``target``.

    """
    spec = _TARGETS.get(target)
    return list(records) if spec is None else spec.identify(records, session_id)


def materialize_claude(
    *,
    records: Sequence[SessionRecord],
    encoding: JSON,
    sealed: Sequence[str | None] = (),
    session_id: UUID | None = None,
) -> Materialized:
    """Write ``records`` as a claude transcript the CLI can resume.

    Args:
      records: The part's records, in ``idx`` order.
      encoding: How the source file spelled its bytes.
      sealed: Each record's ciphertext, positionally aligned with ``records``.
      session_id: The id to mint into the file; generated when omitted.

    Returns:
      written: The path and the id that names it.

    """
    return materialize(
        target="claude",
        records=records,
        encoding=encoding,
        sealed=sealed,
        session_id=session_id,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Target:
    """How one CLI names, places, and states the identity of a session file.

    Four fields because those are the four things that differ. The RECORDS are
    format-neutral by construction -- that is what the IR is for -- so a target
    is only ever the seam between them and one provider's filesystem.
    """

    scope: Callable[[], Path]
    """Directory this CLI reads its sessions from, asked of its adapter."""

    filename: Callable[[UUID], str]
    """What the file must be called for the CLI to find it by id."""

    identify: Callable[[Sequence[SessionRecord], UUID], list[SessionRecord]]
    """Rewrite the id the records STATE, so the file agrees with its name."""

    write: Callable[[Sequence[SessionRecord], TextIO, UUID], None]
    """The format's own writer, told which id the file will be named by.

    The id is not decoration for it: claude SYNTHESIZES the identity keys a
    foreign session lacks, and given no id of its own it falls back to a fixed
    namespace uuid. A transcript then declared two sessions -- the synthesized
    lines one, the file another -- and claude, which filters its transcript by
    ``sessionId``, opened the session showing none of the conversation.
    """

    announce: Callable[[Path, UUID], None]
    """Register the file wherever the CLI LOOKS UP a session by id.

    Codex resumes from ``session_index.jsonl``, so a rollout that is only on
    disk does not exist to it. Claude finds a transcript by scanning its
    project directory and needs nothing.
    """


def _claude_scope() -> Path:
    """Claude's project directory for the local cwd."""
    scope = ClaudeAdapter().session_scope()
    assert scope is not None, "the claude adapter always derives a project directory"
    return scope


def _codex_scope() -> Path:
    """Today's rollout directory, which is how codex shards its sessions."""
    root = next(iter(CodexAdapter().session_dirs()))
    today = datetime.now(UTC)
    return root / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}"


def _codex_filename(session_id: UUID) -> str:
    """``rollout-<ISO>-<uuid>.jsonl``, the shape codex globs for.

    The whole stem, not the bare uuid: codex lists ``rollout-*`` and
    ``CodexAdapter.matches_session_file`` requires the same prefix, so a file
    named by the id alone is invisible to the CLI and to our own capture.
    """
    return f"rollout-{datetime.now(UTC):%Y-%m-%dT%H-%M-%S}-{session_id}.jsonl"


def _codex_identified(
    records: Sequence[SessionRecord], session_id: UUID
) -> list[SessionRecord]:
    """State ``session_id`` on the launch settings codex declares itself with.

    ONE record, unlike claude's per-line ``sessionId``: codex declares its
    identity once, on the ``session_meta`` line, which the IR carries as the
    ``payload`` residual of the opening :class:`TurnContext`. A rollout whose
    payload still names the captured session resumes one this machine does not
    have.

    ``id`` and ``session_id`` both, because the launch payload states both and
    the CLI reads the pair; ``parent_thread_id`` is dropped rather than
    rewritten, since the thread it forked from is not being materialized.

    The launch record is the one CARRYING a payload, not merely the first: a
    rollout that opens with a blank line states its encoding before it
    declares itself, which is how ``codex.py::denormalize`` finds it too.

    A session captured from ANOTHER CLI carries no launch payload at all --
    claude declares nothing of the kind -- so one is synthesized. Without it
    the writer emits no ``session_meta`` line, and a rollout that never states
    its own id is one the CLI cannot resume however it is named.
    """
    at = _stamp()
    stamped = [
        _codex_declared(record, session_id)
        if isinstance(record, TurnContext) and "payload" in record.extra
        else _codex_stamped(record, at)
        for record in records
    ]
    if any(
        isinstance(record, TurnContext) and "payload" in record.extra
        for record in stamped
    ):
        return stamped
    return [
        _codex_declared(
            TurnContext(encoding=json_freeze({"newline_terminated": True})),
            session_id,
        ),
        *stamped,
    ]


def _codex_stamped(record: SessionRecord, at: str) -> SessionRecord:
    """Give a record a timestamp when it crossed in without one.

    An unstamped line is one codex does NOT replay into the resumed context.
    Measured on three rollouts of one conversation: stamping only the launch
    line resumed the session but answered "Unknown" about its own transcript,
    while stamping every line answered from it. The session opens either way,
    so a resume that only checks the CLI started cannot see the difference.

    Its own stamp is kept when it has one, since the time a turn happened is a
    fact about that turn rather than about this materialization.

    ``getattr``, because ``IncompleteRecord`` declares no ``timestamp`` at all
    -- it is a raw line the reader could not parse, carrying only its text.
    Stamping it unguarded crashed a real resume before anything was written.
    The same reason ``_renamed`` reads the residual that way.
    """
    if getattr(record, "timestamp", "") is not None:
        return record
    return replace(record, timestamp=at)


def _codex_declared(record: TurnContext, session_id: UUID) -> TurnContext:
    """Return the launch settings with this machine's declaration in them.

    A payload stating only the id is REFUSED by the CLI: measured against the
    installed binary, a materialized rollout carrying ``{id, session_id}``
    answered "No saved session found", and the same file resumed once its
    declaration named cwd, originator, cli_version, source, thread_source, and
    model_provider. Codex validates a session by what it declares, so the
    defaults below are the minimum a session it never recorded must state.

    A captured codex rollout already declares all of it, and keeps its own:
    only identity is restated, since the cwd and provider it ran under are
    facts about that session rather than about this machine.
    """
    extra = dict(json_unfreeze(record.extra))
    stated = str(session_id)
    # The launch line's own ordinal, which is how codex numbers a rollout it
    # wrote; a session crossed in from another CLI declares none.
    extra.setdefault("ordinal", 0)
    extra.setdefault("$timestamp", True)
    captured = {
        key: value
        for key, value in json_unfreeze(DictCodec.coerce(extra.get("payload"))).items()
        # The thread this session forked FROM is not being materialized, so
        # naming it would point the CLI at a rollout the machine may not hold.
        if key not in {"id", "session_id", "parent_thread_id"}
    }
    extra["payload"] = (
        {
            "timestamp": _stamp(),
            # The LOCAL cwd, as claude's project directory is derived locally:
            # a resume happens on this machine, in this directory.
            "cwd": str(Path.cwd()),
            "originator": "codex-tui",
            "cli_version": _codex_cli_version(),
            "source": "cli",
            "thread_source": "user",
            "model_provider": "openai",
        }
        | captured
        | {"id": stated, "session_id": stated}
    )
    # The OUTER stamp, and the field rather than the residual: the writer takes
    # the launch line's timestamp from :attr:`TurnContext.timestamp`, so a
    # stamp left in ``extra`` alone emitted ``"timestamp":null``. Measured on
    # two copies of one rollout differing in this field alone, null was refused
    # and the ISO string resumed.
    return replace(
        record, timestamp=record.timestamp or _stamp(), extra=json_freeze(extra)
    )


def _codex_cli_version() -> str:
    """The installed codex's own version, or a plausible floor.

    Read from the binary rather than pinned: the rollout claims to have been
    written by the CLI that will read it, and a version far from the truth is
    a claim the file cannot support. A CLI that cannot be asked leaves the
    floor, which is the version this was verified against.
    """
    binary = shutil.which("codex")
    if binary is None:
        return "0.150.1"
    try:
        found = subprocess.run(  # noqa: S603 -- the resolved codex binary, no shell.
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "0.150.1"
    version = found.stdout.split()
    return version[-1] if found.returncode == 0 and version else "0.150.1"


def _codex_indexed(path: Path, session_id: UUID) -> None:
    """Announce the rollout in the index ``codex resume`` looks it up in.

    Writing the file is HALF the job: measured against the installed CLI, a
    rollout sitting in ``sessions/`` with no ``session_index.jsonl`` entry
    answers "No saved session found with ID ...", while a real rollout resumes
    in an otherwise-empty ``CODEX_HOME`` given only its index line.

    APPENDED, never rewritten: the file is the operator's whole session list,
    and materializing one session must not forget the rest.
    """
    # Beside the sessions ROOT, asked of the adapter rather than walked up from
    # the file: the Y/M/D depth is codex's sharding, and counting parents here
    # would be a second spelling of it.
    del path
    index = next(iter(CodexAdapter().session_dirs())).parent / "session_index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": str(session_id),
        # What the picker shows. Named for its provenance, since a resumed
        # session did not come from a prompt the operator typed here.
        "thread_name": f"trax resume {session_id}",
        "updated_at": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%S.%f000}Z",
    }
    with index.open("a", encoding="utf-8") as handle:
        _ = handle.write(json.dumps(entry, separators=(",", ":")) + "\n")


def _stamp() -> str:
    """Now, in the ISO form codex writes on its own launch line."""
    return f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%S.%f}"[:-3] + "Z"


_TARGETS: Mapping[str, _Target] = MappingProxyType(
    {
        "claude": _Target(
            scope=_claude_scope,
            filename=lambda session_id: f"{session_id}.jsonl",
            identify=lambda records, session_id: [
                _renamed(record, session_id) for record in records
            ],
            write=lambda records, stream, session_id: claude_ir.denormalize(
                records, stream, seed=session_id
            ),
            # Claude scans its project directory, so the file IS the
            # announcement.
            announce=lambda _path, _session_id: None,
        ),
        "codex": _Target(
            scope=_codex_scope,
            filename=_codex_filename,
            identify=_codex_identified,
            # Codex states identity once, on the launch line ``_codex_declared``
            # writes, so its writer needs no id.
            write=lambda records, stream, _session_id: codex_ir.denormalize(
                records, stream
            ),
            announce=_codex_indexed,
        ),
    }
)


def _crossed(
    records: Sequence[SessionRecord], source: str | None, target: str
) -> list[SessionRecord]:
    """Strip what only the CAPTURING provider could replay.

    Two things do not survive a crossing, both measured on a claude session
    resumed as codex:

    - A :class:`Thinking` seal. Reasoning bytes are encrypted BY the provider
      that issued them, so claude's rode into codex's ``encrypted_content``
      and the first request came back ``invalid_encrypted_content -- The
      encrypted content CAIS...AQ== could not be verified``. The readable
      summary crosses; the seal cannot.
    - An :class:`IncompleteRecord` written into a FOREIGN file. It is one
      provider's raw line, replayed verbatim, so in a rollout it landed with no
      envelope -- the resulting line parsed as nothing that format defines.

    The seal is a crossing question; an unstated source is not a crossing,
    since a caller naming no format is building records by hand rather than
    moving them between two CLIs. Same format both sides, the seal replays as
    it was read, which is what keeps the round-trip byte-exact.

    An unparsable record is judged separately, by :func:`_writable`: one that
    cannot be a line is dropped whether or not a crossing is happening.
    """
    crossing = source is not None and source != target
    return [
        replace(record, encrypted=None)
        if crossing and isinstance(record, Thinking)
        else record
        for record in records
        if _writable(record, crossing)
    ]


def _writable(record: SessionRecord, crossing: bool) -> bool:
    """Whether ``record`` can be written as ONE line of the target's file.

    Only :class:`IncompleteRecord` can fail: every other member is structured,
    and the writer builds its line. This one carries raw bytes, and the writer
    emits them as they stand.

    Two ways they cannot stand. Crossing, they are another format's line, so
    nothing the target defines can read them. And a real capture stored 4498
    characters -- a ``cost-state``, an ``atis-latch`` and a ``turn_context``
    concatenated with no separators -- under a SINGLE record, which is not one
    line in any format: written back into its own rollout it produced a line
    that parsed as none.

    Anything that IS one line replays verbatim, valid or not: a truncated final
    line is exactly what this record exists to preserve, and rewriting the file
    it came from must reproduce it byte for byte.
    """
    if not isinstance(record, IncompleteRecord):
        return True
    if crossing:
        return False
    return _one_line(record.text)


def _one_line(text: str) -> bool:
    """Whether ``text`` is a single line holding at most one JSON value."""
    if "\n" in text.rstrip("\n"):
        return False
    try:
        _, end = json.JSONDecoder().raw_decode(text.strip())
    except ValueError:
        # Unparsable as a whole is the TRUNCATED case this record preserves;
        # one line, so it replays.
        return True
    return not text.strip()[end:].strip()


def _spliced(
    records: Sequence[SessionRecord], sealed: Sequence[str | None]
) -> list[SessionRecord]:
    """Rejoin each record with its ciphertext, or refuse if any is missing.

    Checked BEFORE anything is written: a partially-materialized file left on
    disk would be discovered by the runner's watch as this run's own capture.
    """
    out: list[SessionRecord] = []
    for idx, record in enumerate(records):
        bytes_ = sealed[idx] if idx < len(sealed) else None
        if not isinstance(record, Thinking):
            out.append(record)
            continue
        if bytes_ is not None:
            out.append(replace(record, encrypted=bytes_))
            continue
        # No bytes came back, and the stored record always reads ``encrypted``
        # back as ``""`` (they were split into ``session_ciphertext``). What
        # distinguishes the two cases is READABLE reasoning: claude writes a
        # plaintext ``content`` block, which replays as it stands; a sealed
        # one has none, so an empty field with no bytes is retention having
        # dropped them. ``summary`` does NOT distinguish -- codex writes one
        # ALONGSIDE its ciphertext, so requiring it absent never fires.
        #
        # The record stays searchable, which is what the split buys; what it
        # can no longer do is go back to the provider, which validates it.
        if not record.encrypted and not record.content:
            raise CiphertextDroppedError(
                f"record {idx} is sealed reasoning whose ciphertext is no "
                "longer stored; the provider rejects a transcript with an "
                "empty 'encrypted' field, so this session cannot be resumed"
            )
        out.append(record)
    return out


def _renamed(record: SessionRecord, session_id: UUID) -> SessionRecord:
    """Rewrite the ``sessionId`` a record carries in its provider residual.

    Every record, because identity is not in the IR at all: claude repeats
    ``sessionId`` on every line, and the writer only fills one in where the
    record does not already state it (``claude.py::_line_defaults``) --
    deliberately, since replaying a captured line verbatim is what makes the
    round-trip byte-exact. A captured record therefore carries the ORIGINAL
    id, and a file rewritten without this step names the session it came from
    while its filename names the new one: ``--resume`` then hands the CLI a
    transcript that disagrees with the id it was asked for.

    Only that one key is touched. Everything else in the residual -- key
    order, nulls, the uuid chain -- is what the byte-exact rewrite rests on.

    Two record classes carry no ``extra`` at all (``UncategorizedRecord``
    holds its whole line under ``payload``; ``IncompleteRecord`` is raw text),
    so they pass through: neither states a ``sessionId`` the writer replays.
    """
    # ``getattr`` because two members declare no ``extra`` at all. Narrowed
    # through ``DictCodec`` rather than an isinstance check: the attribute is
    # untyped, and the codec takes ``object`` and returns a typed mapping.
    #
    # Not thawed first: the values are re-frozen unchanged, so unfreezing the
    # whole residual only to freeze it again would walk every nested structure
    # twice for one replaced key.
    residual = DictCodec.coerce(getattr(record, "extra", None))
    if "sessionId" not in residual:
        return record
    return replace(
        record,
        extra=json_freeze({**residual, "sessionId": str(session_id)}),
    )
