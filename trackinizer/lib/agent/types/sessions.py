"""Provider-neutral types for lossless agent sessions.

Axioms:

1. Reversible by construction -- a record holds every field its source line
   carried, and nothing caches the original bytes.
2. ``None`` is unset; an empty string or collection is a value.
3. Every distinct act the model emits is its own record, never nested
   content -- text, thinking, and tool calls are siblings.
4. A message is prose plus its attachments, not a sequence of blocks: no
   captured message interleaves the two.
5. Settings live in a :class:`TurnContext`; a record names the one that
   applied by index, and never copies it.
6. A :class:`TurnContext` is full state, so no record needs its predecessors.
   A STATE record precedes the acts it governs and recurs whenever the state
   changes: settings as a :class:`TurnContext`, the model's context as a
   :class:`ContextClear`. Two independent sequences, neither nested in the
   other; "what applied here" is the last of each before the record. A session
   is therefore its RECORDS and nothing else -- no object wraps them and no
   metadata sits beside them, since a container holding a whole session is the
   materialization axiom 11 forbids.
7. Settings are what was requested; a record reports what was fulfilled.
8. A provider is named per turn, so one session may span several.
9. A tool result is typed by what the tool DID, not by who ran it, so the
   same act crossing providers keeps its meaning.
10. Whatever a record's own fields do not name, ``extra`` holds, so the line
    still rewrites to the bytes it was read from. One source line can become
    several records; only the first of them carries it.
11. An adapter reads and writes in ONE pass, holding neither the stream it
    reads nor the one it writes. ``normalize`` YIELDS each record as its line
    lands and ``denormalize`` consumes an iterable, so nothing -- not even the
    records -- is materialized on the adapter's behalf. Sessions reach 273 MB,
    and ONE non-ASCII character makes CPython widen a whole string to 4 bytes
    per character: a reader that listed its lines cost 1.09 GB before parsing
    began, and a writer that joined its output cost 2.6 GB.

    A live session has no EOF, which is what forces the shape rather than
    merely rewarding it: a reader that returned one value when the stream ended
    would hand a tailer nothing, forever. So a whole-file property is not
    resolved -- it is STATED for the prefix consumed and restated when it
    moves. Claude's escaping convention is a majority over lines that flips
    mid-file, and each :class:`TurnContext` carries what is true so far; the
    last one before a record is the one in force. Nothing is ever final, and
    nothing needs to be.

    Reaching backwards is likewise bounded rather than forbidden: a claude tool
    result sits at most 9 lines after the call it answers, measured over 15,647
    pairs, so the window a writer holds is a constant and not a fraction of the
    file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trackinizer.lib.agent.types.capability import SummaryKind, ThinkingEffort
from trackinizer.lib.custom_json import JSON, JSONValue


__all__ = [
    "AgentStatusResult",
    "AgentToAgentMessage",
    "AnyToolResult",
    "AssistantMessage",
    "Attachment",
    "ContextClear",
    "ContextCompaction",
    "ContextState",
    "FileEditResult",
    "FileReadResult",
    "FileWriteResult",
    "IncompleteRecord",
    "SessionRecord",
    "ShellCommandResult",
    "Splice",
    "SummaryKind",
    "SystemMessage",
    "Thinking",
    "ThinkingEffort",
    "TokenUsage",
    "ToolCall",
    "ToolResult",
    "TranscriptItem",
    "TurnContext",
    "UncategorizedRecord",
    "UncategorizedToolResult",
    "UserMessage",
    "WebFetchResult",
    "WebSearchResult",
    "WebSearchResults",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnContext:
    """The settings in force, stated BEFORE the acts they govern.

    A session opens with one, and writes another whenever anything changes:
    codex states its per-turn settings, claude switches model mid-file, and a
    caller may swap the CLI outright. Axiom 6 makes each one full state, so a
    launch line is simply the first -- there is no separate "session
    declaration" record, because a declaration is settings and settings are
    this.

    The recurrence is the point. "What settings applied" is the last
    ``TurnContext`` before a record, exactly as "what context applied" is the
    last :class:`ContextClear` before it. Two independent sequences, one rule:
    the state record precedes what it applies to.

    Attributes:
      encoding: How the SOURCE FILE spelled its bytes, when it was read from
        one. Not a conversation fact -- it is what lets a rewrite reproduce
        the provider's own bytes, and claude's escaping convention is a
        majority over lines, so a later context restates it and supersedes.
      extra: Every setting no field above names, including the whole launch
        payload a provider declared -- cwd, cli version, git state.

    """

    context_id: int | None = None
    timestamp: str | None = None
    permission: str | None = None
    model: str | None = None
    effort: ThinkingEffort | None = None
    summary_kind: SummaryKind | None = None
    encoding: JSON = field(default_factory=dict[str, JSONValue])
    extra: JSON = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True, slots=True, kw_only=True)
class Attachment:
    """Carry one binary message attachment and its media type."""

    mime_descriptor: str
    data: bytes


@dataclass(frozen=True, slots=True, kw_only=True)
class UserMessage:
    """Represent prose and attachments supplied by the user."""

    context_id: int | None = None
    timestamp: str | None = None
    content: str | None = None
    attachments: tuple[Attachment, ...] = ()
    extra: JSON = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessage:
    """Represent prose and attachments emitted by the assistant."""

    context_id: int | None = None
    timestamp: str | None = None
    content: str | None = None
    attachments: tuple[Attachment, ...] = ()
    extra: JSON = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True, slots=True, kw_only=True)
class Thinking:
    """Represent readable, sealed, or summarized model reasoning."""

    context_id: int | None = None
    timestamp: str | None = None
    content: str | None = None
    encrypted: str | None = None
    summary: str | None = None
    extra: JSON = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCall:
    """Represent one invocation requested by the model."""

    context_id: int | None = None
    timestamp: str | None = None
    call_id: str
    name: str
    arguments: JSON = field(default_factory=dict[str, JSONValue])
    extra: JSON = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolResult:
    """Identify the tool call answered by an observation."""

    context_id: int | None = None
    timestamp: str | None = None
    call_id: str
    extra: JSON = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True, slots=True, kw_only=True)
class UncategorizedToolResult(ToolResult):
    """Hold a tool observation whose operation is not represented."""

    content: str | None = None
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ShellCommandResult(ToolResult):
    """Report a completed shell command."""

    command: tuple[str, ...] | None = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FileReadResult(ToolResult):
    """Report content read from a file, whole or by line ranges.

    Attributes:
      path: File that was read.
      content: What the read returned.
      ranges: Which lines came back, as ``(start, count)`` pairs, one-based
        and inclusive of ``start``. Empty means the whole file.

        SEVERAL pairs, not one span: agents routinely read scattered lines in
        a single command -- ``sed -n '84p;101p;132p;155p' f`` and
        ``sed -n '1765,1771p;1847,1853p' f`` both appear in captured sessions
        -- and one span could only describe those by claiming everything
        between them was read too.

        A ``None`` count means the read ran to a bound this record cannot
        resolve to a number: ``sed -n '20,$p'`` ends at the file's last line,
        and ``tail -5`` counts backwards from it, neither of which is knowable
        without the file.

    """

    path: str | None = None
    content: str | None = None
    ranges: tuple[tuple[int | None, int | None], ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class FileWriteResult(ToolResult):
    """Report content written to a file."""

    path: str | None = None
    content: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Splice:
    r"""One replacement an edit performed: what was there, what replaced it.

    Content-anchored, with position optional -- the shape of a
    diff-match-patch ``patch_obj`` rather than an LSP ``TextEdit``, because a
    transcript holds no file to resolve a position against. Providers state
    edits in two vocabularies and this carries both: claude names the text it
    replaced (``oldString``/``newString``) and says nothing about where, while
    a unified diff and codex's ``apply_patch`` name a line span. Neither
    normalizes into the other without the file, so a splice holds whichever
    the provider gave and leaves the rest unset.

    An insert and a delete need no special case: an insert has an empty
    ``before``, a delete an empty ``after``.

    Attributes:
      before: Text that was replaced. Empty for a pure insertion.
      after: Text written in its place. Empty for a pure deletion.
      lead: Unchanged text a diff printed BEFORE the change, including the
        ``@@`` header when the provider wrote one.
      trail: Unchanged text a diff printed AFTER it, up to the next change.

        Both sides are needed, not just the lead: a hunk whose last line is
        context -- ``-old``, ``+new``, then `` return {}`` -- loses that line
        otherwise, which was 10 of 71 captured codex diffs. Together they make
        the decomposition exact: 71 of 71 rebuild byte for byte.

      start: Line the replaced text began on, one-based, when the provider
        said. ``None`` means it located the edit by CONTENT, not position.
      count: Lines the replaced text spanned, when the provider said.
      bare: Which of ``before``/``after``/``trail`` the provider ANNOTATED as
        ending without a newline -- git writes ``\\ No newline at end of file``
        for exactly that. ``trail`` is a member because git annotates a
        trailing CONTEXT line too, whenever the file's unchanged last line has
        no terminator. Distinct from text that merely lacks one: a shell append
        states the bytes it wrote and was never annotated, so inferring the
        annotation from the text would add a line no patch contained.

    """

    before: str | None = None
    after: str | None = None
    lead: str | None = None
    trail: str | None = None
    start: int | None = None
    count: int | None = None
    bare: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True, kw_only=True)
class FileEditResult(ToolResult):
    """Report an edit applied to a file.

    Attributes:
      path: File that was edited.
      edits: The replacements the edit made. Empty when the provider stated
        only that a file changed -- ``sed -i 's/a/b/g'`` is a program, not a
        replacement this record can name, so it reports its path and leaves
        this empty rather than inventing a before and after.

        There is no separate diff field: a diff IS these splices, and
        :func:`~trackinizer.lib.agent.sessions.udiff.render_udiff` writes it back.
        Keeping both let the two disagree, and the writers then had to decide
        which one the provider meant.

    """

    path: str | None = None
    edits: tuple[Splice, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class WebSearchResults(ToolResult):
    """Report the rows returned by a web search."""

    query: str | None = None
    duration_sec: float | None = None
    content: tuple[WebSearchResult, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class WebSearchResult:
    """Represent one result row from a web search."""

    url: str | None = None
    title: str | None = None
    snippet: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class WebFetchResult(ToolResult):
    """Report content fetched from the web."""

    url: str | None = None
    content: str | None = None
    code: int | None = None
    duration_sec: float | None = None
    size: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentStatusResult(ToolResult):
    """Report the state or final output of a delegated agent."""

    agent_id: str | None = None
    agent_kind: str | None = None
    prompt: str | None = None
    content: str | None = None
    model: str | None = None
    state: str | None = None
    tokens: int | None = None
    duration_sec: float | None = None
    tool_calls: int | None = None
    output_file: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemMessage:
    """Represent instructions or status emitted by the harness."""

    context_id: int | None = None
    timestamp: str | None = None
    content: str | None = None
    attachments: tuple[Attachment, ...] = ()
    subtype: str | None = None
    extra: JSON = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenUsage:
    """Report provider token accounting and rate limits."""

    context_id: int | None = None
    timestamp: str | None = None
    info: JSON = field(default_factory=dict[str, JSONValue])
    rate_limits: JSON = field(default_factory=dict[str, JSONValue])
    extra: JSON = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextState:
    """Represent context injected into the model-visible state."""

    context_id: int | None = None
    timestamp: str | None = None
    kind: str = ""
    content: str | None = None
    extra: JSON = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextCompaction:
    """The EVENT: the model's history was replaced by a summary.

    The event and its directions only. What the compaction PRODUCED -- the
    summary, the surviving turns, the instructions the next window starts
    from -- is on the :class:`ContextClear` that follows it, because that is
    the record every context window opens with whatever caused the reset.

    So a session reads::

        SessionMetadata
        ContextClear(system_prompt)
        ...records...
        ContextCompaction          <- it happened, and why
        ContextClear(system_prompt, summary, history)
        ...records...

    That ordering buys one rule for "what was the model looking at": the last
    :class:`ContextClear` plus every record after it. Without the trailing
    clear a consumer needs two rules and a different reconstruction for each.

    Attributes:
      summary: Ignored by readers that follow the grammar above -- kept
        because a provider may state the summary on the event line itself,
        and a value the wire carried has to land somewhere.
      extra: Provider bookkeeping, and the DIRECTIONS where one was given.
        Claude takes free-form guidance (``/compact keep the API``); codex
        refuses it outright. There is no separate call record for it: a
        request with no distinguishable failure is one event, not two.

    """

    context_id: int | None = None
    timestamp: str | None = None
    summary: str | None = None
    extra: JSON = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextClear:
    """A context window opens here, and this is what it opens with.

    EVERY window, whatever caused it: a session begins with one, a ``/clear``
    starts one, and a :class:`ContextCompaction` is followed by one. That
    uniformity is the point -- "what was the model looking at" is the last
    clear plus every record after it, one rule for every provider. A record
    that carried its context only for compactions would need a second rule and
    a consumer that knew which kind of reset it was reading.

    What distinguishes the causes is what the window carries, not which record
    announced it: ``summary`` unset is a discard, ``summary`` set is a
    distillation of what came before.

    Attributes:
      cleared_session_id: The session this one replaced, when it replaced one.
        ``None`` opens a conversation rather than continuing it.
      system_prompt: The instructions the fresh context begins with. Restated
        on every window rather than referenced, so each is self-describing and
        no consumer scans backwards to assemble one.
      summary: What the window carries forward IN PLACE of the history it
        replaced. ``None`` where nothing was distilled -- a fresh session, or
        a clear that simply discarded.
      history: The records the fresh context carries. Codex states them as
        stripped copies rather than the originals, so they are held here
        rather than re-emitted as ordinary records -- the stripped form is not
        recoverable from the turns they came from.

    """

    context_id: int | None = None
    timestamp: str | None = None
    cleared_session_id: str | None = None
    system_prompt: str | None = None
    summary: str | None = None
    history: tuple[SessionRecord, ...] = ()
    extra: JSON = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentToAgentMessage:
    """Represent a message exchanged between delegated agents."""

    context_id: int | None = None
    timestamp: str | None = None
    content: str | None = None
    attachments: tuple[Attachment, ...] = ()
    sender: str | None = None
    recipient: str | None = None
    extra: JSON = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True, slots=True, kw_only=True)
class UncategorizedRecord:
    """Preserve a provider record with no neutral representation."""

    context_id: int | None = None
    timestamp: str | None = None
    kind: str
    payload: JSON = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True, slots=True, kw_only=True)
class IncompleteRecord:
    """Preserve one line that could not be structurally normalized."""

    text: str


type AnyToolResult = (
    ShellCommandResult
    | FileReadResult
    | FileWriteResult
    | FileEditResult
    | WebSearchResults
    | WebFetchResult
    | AgentStatusResult
    | UncategorizedToolResult
)
type TranscriptItem = (
    UserMessage | AssistantMessage | Thinking | ToolCall | AnyToolResult
)
type SessionRecord = (
    TranscriptItem
    | SystemMessage
    | TokenUsage
    | ContextState
    | ContextCompaction
    | ContextClear
    | AgentToAgentMessage
    | TurnContext
    | UncategorizedRecord
    | IncompleteRecord
)
