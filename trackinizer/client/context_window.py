"""The window a switch loads: what the last model was looking at, within a budget.

A chat's parts are read in time order and cut at the last record that opened a window
by leaving history out: a compaction's :class:`ContextClear`, which carries a summary,
from claude, codex or the bot's sagent alike; or a claude part begun with ``/clear``.
What is loaded is that window's opening plus every record after it, so a switch never
loads what the last model had already compacted away. :func:`window` is the one place
that decides the cut, for the agent runner's resume as claude, codex or sagent and for
the bot's own history alike.

Then a per-target :class:`Budget` applies. At or under ``fit_bytes`` the window loads
as is; up to ``max_bytes`` it loads whole and the target is asked to compact it with its
own compactor; past that the oldest whole turns are left out. A result or line over
:data:`CUT_ABOVE` bytes keeps its head and tail. Whatever is left out is counted by a
note the window opens with, which names the ``trax agentsession N records`` command
that reads it.

No summarizer lives here: a summary is only ever the one a provider's own compaction
wrote.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import json

from treekle import immutable, mutable

from trackinizer.client.conversation import (
    Placed,
    Segment,
    neutral,
    placed,
    projected,
)
from trackinizer.sagent.session.types import (
    AgentStatusResult,
    AgentToAgentMessage,
    AssistantMessage,
    ContextClear,
    ContextCompaction,
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    SessionRecord,
    ShellCommandResult,
    ToolCall,
    ToolResult,
    UncategorizedToolResult,
    UserMessage,
    WebFetchResult,
    WebSearchResults,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


__all__ = [
    "BUDGETS",
    "CUT_ABOVE",
    "Budget",
    "Loaded",
    "Opening",
    "Window",
    "cli_records",
    "cut_text",
    "weight",
    "window",
]


CUT_ABOVE: Final = 16_000
"""The most bytes one result or line is loaded with; a longer one keeps its head and
tail, so no single record is too big for a target to compact."""

_HEAD: Final = 10_000
_TAIL: Final = 4_000

# What claude's own compaction flags its summary line with. The writer emits both keys
# (``claude_orders.user_order``), and claude builds the model's context from such a line.
_COMPACT_SUMMARY: Final = immutable(
    {"isCompactSummary": True, "isVisibleInTranscriptOnly": True},
)

# The line a claude ``/clear`` starts its new transcript with, before any answer.
_CLEAR_COMMAND: Final = "<command-name>/clear</command-name>"

# The records whose words are one ``content`` field.
_WORDED: Final = (
    UserMessage,
    AssistantMessage,
    AgentToAgentMessage,
    UncategorizedToolResult,
    FileReadResult,
    FileWriteResult,
    WebFetchResult,
    AgentStatusResult,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Budget:
    """How much of a window one target loads.

    Attributes:
      fit_bytes: The most loaded as is.
      max_bytes: The most ever loaded. Past ``fit_bytes`` the target is asked to
        compact what it loaded; past this, the oldest whole turns are left out.
      max_lines: The most lines loaded, for a reader that counts lines; 0 counts none.

    """

    fit_bytes: int
    max_bytes: int
    max_lines: int = 0


BUDGETS: Final[Mapping[str, Budget]] = MappingProxyType(
    {
        # About 400k and 700k estimated tokens, of claude's 1M window.
        "claude": Budget(fit_bytes=1_600_000, max_bytes=2_800_000),
        # About 120k and 200k, of codex's 272k (258k usable).
        "codex": Budget(fit_bytes=480_000, max_bytes=800_000),
        # sagent's window is its model's, 272k or 1M, and it compacts before any call
        # that model cannot hold (``Compactor.should_run``), so it is never asked to
        # here; the cap, claude's, only bounds what is read.
        "sagent": Budget(fit_bytes=2_800_000, max_bytes=2_800_000),
        # About 60k of the bot's 258k, whose own compactor takes the rest.
        "assistant": Budget(fit_bytes=240_000, max_bytes=240_000, max_lines=500),
    },
)
"""Each target's budget, by the name its loader passes."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Opening:
    """Where a window opens: a provider's compaction, or a claude ``/clear``.

    Attributes:
      provider: The provider whose part holds it.
      at: The opening clear, or for a ``/clear`` the first record of its part.
      clear: The clear the window opens with, its summary and kept history whole.
      leader: The compaction event before the clear, which codex writes back with it.
      discard: Whether the window opened with ``/clear``, which carries no summary.

    """

    provider: str
    at: Placed
    clear: ContextClear
    leader: ContextCompaction | None = None
    discard: bool = False

    @property
    def sealed(self) -> bool:
        """Whether the summary is codex's ciphertext, which only codex can read."""
        return (
            self.provider == "codex"
            and self.leader is not None
            and "$message" not in self.leader.extra
        )

    @property
    def summary(self) -> str:
        """The summary another model can read, or empty."""
        return "" if self.sealed else self.clear.summary or ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Window:
    """What the last model was looking at, across every part of a chat.

    Attributes:
      opening: Where the window opens, or None when nothing was ever cut.
      history: The opening's kept turns, provider-neutral, oldest first.
      records: Every record after the opening, provider-neutral, oldest first.
      skipped: How many stored records come before the opening.
      last_skipped: The ``(part, idx)`` of the newest record before the opening.

    """

    opening: Opening | None
    history: list[Placed]
    records: list[Placed]
    skipped: int
    last_skipped: tuple[int, int] | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Loaded:
    """A window as one target loads it.

    Attributes:
      records: What the target's writer takes, oldest first.
      loaded_records: How many of the window's records are in it.
      estimated_tokens: Its weight in tokens, estimated as bytes over four.
      skipped: How many stored records are not in it.
      compact_after_load: Whether it is past the target's ``fit_bytes``, so the target
        is asked to compact it once loaded.

    """

    records: list[SessionRecord]
    loaded_records: int
    estimated_tokens: int
    skipped: int
    compact_after_load: bool


def window(segments: Sequence[Segment]) -> Window:
    """Cut a chat's parts at the last window opening, across every part.

    A compaction cuts: its :class:`ContextClear` carries a summary. So does a claude
    part whose user lines, before any answer, include the ``/clear`` command. A clear
    with no summary never cuts, however it is marked: codex sets
    ``cleared_session_id`` on a FORK from the session it forked, and cutting there
    would drop every earlier part of every other provider.

    Args:
      segments: The parts, from ``read_segments``.

    Returns:
      window: The opening, its kept history and the records after it, and what was
        left out before it.

    """
    order = placed(segments)
    opening, start, resume = _window_opening(order, segments)
    history = (
        []
        if opening is None
        else neutral(
            [replace(opening.at, record=record) for record in opening.clear.history],
        )
    )
    return Window(
        opening=opening,
        history=history,
        records=neutral(
            order[resume:],
            answerer=next(
                (
                    each.provider
                    for each in reversed(history)
                    if isinstance(each.record, AssistantMessage | ToolCall)
                ),
                "",
            ),
        ),
        skipped=start,
        last_skipped=(order[start - 1].part, order[start - 1].idx) if start else None,
    )


def cli_records(
    seen: Window,
    target: str,
    *,
    seq: Callable[[], int],
    budget: Budget | None = None,
) -> Loaded:
    """Load a window as ``target``'s own records, within its budget.

    The window opens with what the target can read of the summary. Codex resuming its
    own compaction gets it natively: the event and the clear, which its writer turns
    into one ``compacted`` line, sealed summary and kept turns included. Any other
    target gets one user turn holding the readable summary, flagged as claude flags its
    own when the target is claude, then an acknowledgement; a sealed summary is named
    and never copied, and its kept turns follow as the oldest turns.

    Args:
      seen: The window, from :func:`window`.
      target: The CLI the records are for, ``claude``, ``codex`` or ``sagent``.
      seq: Returns the session's seq, which the note names; called only when
        something is left out.
      budget: How much to load; the target's entry in :data:`BUDGETS` by default.

    Returns:
      loaded: The records and what they hold.

    """
    limits = budget or BUDGETS[target]
    opening = seen.opening
    native = target == "codex" and opening is not None and opening.provider == target
    if opening is not None and native:
        carried = list(opening.clear.history)
        summary = opening.clear.summary or ""
    else:
        carried = [each.record for each in seen.history]
        summary = "" if opening is None else opening.summary
    fit = _fitted(
        summary_bytes=len(summary.encode()),
        carried=[_shortened(record) for record in carried],
        body=[replace(each, record=_shortened(each.record)) for each in seen.records],
        limits=limits,
    )
    skipped = seen.skipped + fit.dropped
    notes: list[str] = []
    # The newest record left out: one the budget dropped, else a kept turn of the
    # opening, which is stored at the opening, else the last one before it.
    last = (
        fit.last_dropped
        or (
            (opening.at.part, opening.at.idx)
            if opening is not None and fit.dropped
            else None
        )
        or seen.last_skipped
    )
    if last is not None:
        notes.append(_note(seen, dropped=fit.dropped, seq=seq(), last=last))
    if opening is not None and opening.sealed and not native:
        notes.append(
            "[trax] codex's summary of the earlier conversation is sealed; its kept "
            "turns follow.",
        )
    stamp = _stamp(seen)
    head: list[SessionRecord] = []
    if opening is not None and native:
        head.extend(
            [
                *([opening.leader] if opening.leader is not None else []),
                replace(opening.clear, history=tuple(fit.carried)),
            ],
        )
    elif summary and opening is not None:
        notes.append(
            f"Summary of the conversation before this (by {opening.provider}):\n"
            f"{summary}",
        )
    if notes:
        flagged = bool(summary) and not native and target == "claude"
        head.extend(
            [
                UserMessage(
                    timestamp=stamp,
                    content="\n".join(notes),
                    extra=_COMPACT_SUMMARY if flagged else immutable({}),
                ),
                # Its own turn, so the writer cannot fold the next person's line into
                # the flagged summary, where claude would show it to nobody.
                AssistantMessage(timestamp=stamp, content="Understood."),
            ],
        )
    kept = [*([] if native else fit.carried), *(each.record for each in fit.body)]
    return Loaded(
        records=projected(kept, target, opening=head),
        loaded_records=len(fit.carried) + len(fit.body),
        estimated_tokens=fit.weight // 4,
        skipped=skipped,
        compact_after_load=fit.compact,
    )


def weight(record: SessionRecord) -> int:
    """Return how many UTF-8 bytes of ``record`` a target reads.

    Args:
      record: Any record.

    Returns:
      weight: The bytes of its text, summary, call arguments and result fields.

    """
    return sum(len(text.encode()) for text in _texts(record))


def cut_text(text: str) -> str:
    """Return ``text`` with its middle cut, when it is over :data:`CUT_ABOVE` bytes.

    Args:
      text: A result or a line.

    Returns:
      text: Unchanged when short enough; otherwise its first 10,000 bytes, a marker
        naming how many were cut, and its last 4,000.

    """
    raw = text.encode()
    if len(raw) <= CUT_ABOVE:
        return text
    head = raw[:_HEAD].decode(errors="ignore")
    tail = raw[-_TAIL:].decode(errors="ignore")
    return f"{head}[... {len(raw) - _HEAD - _TAIL} bytes cut ...]{tail}"


@dataclass(frozen=True, slots=True, kw_only=True)
class _Fit:
    """What of a window fits a budget, and what was left out."""

    carried: list[SessionRecord]
    body: list[Placed]
    dropped: int
    last_dropped: tuple[int, int] | None
    weight: int
    compact: bool


# The last opening wins, whichever part holds it. Its compaction event sits just before
# the clear, on the same source line, so it is left out with what came before.
def _window_opening(
    order: Sequence[Placed],
    segments: Sequence[Segment],
) -> tuple[Opening | None, int, int]:
    """Return the window's opening, where what is left out ends, and where it resumes."""
    cleared = {
        segment.part
        for segment in segments
        if segment.format == "claude" and _opens_with_clear(segment.records)
    }
    found: tuple[Opening | None, int, int] = (None, 0, 0)
    seen_parts: set[int] = set()
    for at, each in enumerate(order):
        first = each.part not in seen_parts
        seen_parts.add(each.part)
        record = each.record
        if isinstance(record, ContextClear) and record.summary not in {None, ""}:
            before = order[at - 1] if at else None
            leader = (
                before.record
                if before is not None
                and before.part == each.part
                and isinstance(before.record, ContextCompaction)
                else None
            )
            opening = Opening(
                provider=each.provider,
                at=each,
                clear=record,
                leader=leader,
            )
            found = (opening, at - (leader is not None), at + 1)
        elif first and each.part in cleared:
            opening = Opening(
                provider=each.provider,
                at=each,
                clear=ContextClear(timestamp=each.moment.isoformat()),
                discard=True,
            )
            found = (opening, at, at)
    return found


def _opens_with_clear(records: Sequence[SessionRecord]) -> bool:
    """Return whether a claude part was begun by ``/clear``, before any answer."""
    for record in records:
        if isinstance(record, AssistantMessage | ToolCall):
            return False
        if isinstance(record, UserMessage) and _CLEAR_COMMAND in (record.content or ""):
            return True
    return False


def _fitted(
    *,
    summary_bytes: int,
    carried: list[SessionRecord],
    body: list[Placed],
    limits: Budget,
) -> _Fit:
    """Keep what fits: the opening first, then the newest whole turns."""
    total = summary_bytes + sum(map(weight, carried)) + _weigh(body)
    if total <= limits.max_bytes:
        return _Fit(
            carried=carried,
            body=body,
            dropped=0,
            last_dropped=None,
            weight=total,
            compact=total > limits.fit_bytes,
        )
    room = limits.max_bytes - summary_bytes
    kept_carried = _newest(carried, room=room)
    room -= sum(map(weight, kept_carried))
    kept_body = _turns(body, room=room)
    kept_ids = {id(each) for each in kept_body}
    newest_dropped = next(
        (each for each in reversed(body) if id(each) not in kept_ids),
        None,
    )
    return _Fit(
        carried=kept_carried,
        body=kept_body,
        dropped=len(carried) - len(kept_carried) + len(body) - len(kept_body),
        last_dropped=None
        if newest_dropped is None
        else (newest_dropped.part, newest_dropped.idx),
        weight=summary_bytes + sum(map(weight, kept_carried)) + _weigh(kept_body),
        compact=True,
    )


def _newest(records: Sequence[SessionRecord], *, room: int) -> list[SessionRecord]:
    """Return the newest records whose weight stays within ``room``."""
    used = 0
    start = len(records)
    while start and used + weight(records[start - 1]) <= room:
        used += weight(records[start - 1])
        start -= 1
    return list(records[start:])


# A turn is a person's line and everything up to the next one, so a line is never loaded
# without the answer it got. The newest turn is kept even when it alone is over.
def _turns(body: Sequence[Placed], *, room: int) -> list[Placed]:
    """Return the newest whole turns within ``room``, and the newest turn always."""
    turns: list[list[Placed]] = []
    for each in body:
        if not turns or isinstance(each.record, AgentToAgentMessage):
            turns.append([])
        turns[-1].append(each)
    kept: list[list[Placed]] = []
    used = 0
    for turn in reversed(turns):
        if used + _weigh(turn) > room:
            break
        kept.append(turn)
        used += _weigh(turn)
    if not kept and turns:
        kept.append(_trimmed(turns[-1], room=room))
    return [each for turn in reversed(kept) for each in turn]


# A newest turn that alone is over the budget still says what was asked and answered,
# with as many of its newest calls as fit beside them.
def _trimmed(turn: Sequence[Placed], *, room: int) -> list[Placed]:
    """Return a turn's person line, last answer and newest call/result pairs in room."""
    person = turn[0] if isinstance(turn[0].record, AgentToAgentMessage) else None
    answer = next(
        (each for each in reversed(turn) if isinstance(each.record, AssistantMessage)),
        None,
    )
    sure = [each for each in (person, answer) if each is not None]
    keep = {id(each) for each in sure}
    used = _weigh(sure)
    results = {
        each.record.call_id: each
        for each in turn
        if isinstance(each.record, ToolResult)
    }
    for each in reversed(turn):
        if not isinstance(each.record, ToolCall):
            continue
        pair = [each]
        if each.record.call_id in results:
            pair.append(results[each.record.call_id])
        if used + _weigh(pair) > room:
            break
        used += _weigh(pair)
        keep.update(id(member) for member in pair)
    return [each for each in turn if id(each) in keep]


def _weigh(records: Sequence[Placed]) -> int:
    """Return the weight of placed records."""
    return sum(weight(each.record) for each in records)


def _note(
    seen: Window,
    *,
    dropped: int,
    seq: int,
    last: tuple[int, int],
) -> str:
    """Return the line that counts what was left out and names how to read it."""
    opening = seen.opening
    before = (
        "cleared with /clear"
        if opening is not None and opening.discard
        else "before the last compaction"
    )
    return (
        f"[trax] {seen.skipped + dropped} earlier records are not loaded "
        f"({seen.skipped} {before}, {dropped} over the size budget). "
        f"Read them: trax agentsession {seq} records --to {last[0]}:{last[1]}"
    )


def _stamp(seen: Window) -> str | None:
    """Return when the window opens: its opening, or its first record."""
    if seen.opening is not None:
        return seen.opening.at.moment.isoformat()
    return seen.records[0].moment.isoformat() if seen.records else None


def _texts(record: SessionRecord) -> list[str]:
    """Return the text of ``record`` that a target reads."""
    if isinstance(record, ToolCall):
        return [record.name, json.dumps(mutable(record.arguments), sort_keys=True)]
    if isinstance(record, ShellCommandResult):
        return [record.stdout, record.stderr]
    if isinstance(record, FileEditResult):
        return [
            text or "" for edit in record.edits for text in (edit.before, edit.after)
        ]
    if isinstance(record, WebSearchResults):
        return [
            text or ""
            for row in record.content
            for text in (row.url, row.title, row.snippet)
        ]
    if isinstance(record, ContextClear):
        return [record.summary or ""]
    if isinstance(record, _WORDED):
        return [record.content or ""]
    return []


def _shortened(record: SessionRecord) -> SessionRecord:
    """Return ``record`` with each text over :data:`CUT_ABOVE` bytes cut."""
    if isinstance(record, ShellCommandResult):
        return replace(
            record,
            stdout=cut_text(record.stdout),
            stderr=cut_text(record.stderr),
        )
    if isinstance(record, _WORDED) and record.content is not None:
        return replace(record, content=cut_text(record.content))
    return record
