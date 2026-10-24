"""A user's preferences: every key, its type, its default, and what it means.

They are kept on the user's row (``users.preferences``), so they follow the user to
any browser or machine, and an agent the user works with can change them. The
stored object holds only the keys the user set; a read fills in the defaults.
Each field's description is the comment Settings shows beside the key.

A write replaces a top-level key whole. Every collection and text is bounded: the
row is read on every profile fetch, so no key may grow without limit.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import to_json
from treekle import from_plain

from trackinizer.wire.wire_science_chat import ChatProvider


if TYPE_CHECKING:
    from collections.abc import Mapping

    import uuid

    from pydantic.config import JsonDict

    from trackinizer.lib.postgres import Conn


ASSISTANT_KEYS: Final = frozenset(
    {
        "theme",
        "skin",
        "chat_side",
        "app_bar",
        "highlight_mentions",
        "terminal_font_size",
        "terminal_tab_chords",
        "terminal_placement",
        "home",
        "chat_provider",
        "chat_tool_calls",
    },
)
"""The preferences a canvas's assistant may read and change for its owner: the
UI's, each marked ``SETTING`` below. Every other key (the user's own data) stays
the user's alone."""

SETTING: Final[JsonDict] = {"x-setting": True}
"""Marks a field as one of the UI's settings: Settings shows it in the
preferences document, with its description as the comment above it."""


_Id = Annotated[str, StringConstraints(max_length=100)]
_Name = Annotated[str, StringConstraints(max_length=200)]
_Text = Annotated[str, StringConstraints(max_length=100_000)]


def _instant(text: str) -> datetime:
    """Read ``text`` as an ISO time with an offset."""
    time = datetime.fromisoformat(text)
    if time.tzinfo is None:
        raise ValueError("is a time with no offset")
    return time


def _time(text: str) -> str:
    """Return ``text`` if it is an ISO time with an offset, as the browser writes."""
    _instant(text)
    return text


_Time = Annotated[
    str,
    StringConstraints(max_length=40),
    AfterValidator(_time),
]


def _json_within(value: JsonValue, *, max_bytes: int = 100_000) -> JsonValue:
    """Return ``value`` if its JSON text is at most ``max_bytes`` long."""
    if len(to_json(value)) > max_bytes:
        raise ValueError(f"is larger than {max_bytes} bytes")
    return value


class _Strict(BaseModel):
    """A part of a preference: strict, and no key besides its own."""

    # Strict: a stored or sent value is the type itself, never coerced (``"yes"`` is no bool).
    model_config = ConfigDict(extra="forbid", strict=True)


class ViewFilter(_Strict):
    """One filter of a saved view's list query."""

    field: _Id
    op: Literal["is", "ne", "re", "nre", "lt", "le", "gt", "ge", "isnull", "notnull"]
    value: Annotated[str, StringConstraints(max_length=2_000)]


class ViewRequest(_Strict):
    """The ``GET /api/inquiries`` query a saved view shows."""

    kinds: Annotated[list[_Id], Field(max_length=50)]
    filters: Annotated[list[ViewFilter], Field(max_length=50)]


class SavedView(_Strict):
    """A named list query."""

    id: _Id
    name: _Name
    request: ViewRequest


class ConsoleLive(_Strict):
    """A Console view that follows the feed as it grows."""

    live: Literal[True]


class ConsoleWindow(_Strict):
    """A Console view of a window of history, open at an end that is None."""

    live: Literal[False]
    since: _Time | None
    until: _Time | None

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> Self:
        if (
            self.since is not None
            and self.until is not None
            and _instant(self.until) < _instant(self.since)
        ):
            raise ValueError("ends before it starts")
        return self


class ConsoleView(_Strict):
    """A Console view: what it picks, its level and its place in time, under a name."""

    id: _Id
    name: _Name
    pinned: bool
    created: _Time
    agents: Annotated[list[_Name], Field(max_length=100)]
    rooms: Annotated[list[_Name], Field(max_length=100)]
    clis: Annotated[list[_Name], Field(max_length=100)]
    level: Literal[1, 2, 3, 4]
    range: ConsoleLive | ConsoleWindow


class Person(_Strict):
    """A person or an agent added by hand, under the name the pickers show."""

    name: _Name
    type: Literal["person", "agent"]


class ReadState(_Strict):
    """Which notifications are read."""

    boundary: Annotated[str, StringConstraints(max_length=100)] | None
    marks: Annotated[list[_Id], Field(max_length=5_000)]


class WorkspacesPreference(_Strict):
    """The user's workspace bar state."""

    order: Annotated[list[_Id], Field(max_length=500)] = []
    pinned: Annotated[list[_Id], Field(max_length=500)] = []
    hidden: Annotated[list[_Id], Field(max_length=500)] = []
    last_canvas: Annotated[dict[_Id, _Id], Field(max_length=500)] = Field(
        default=dict[_Id, _Id](),
        description=(
            "The canvas you opened last in each workspace, by workspace id: "
            "switching to the workspace opens it again, in any browser."
        ),
    )


class FieldDraft(_Strict):
    """The unsent text of one field's editor, and the value editing began from."""

    text: _Text
    base: JsonValue

    _bounded = field_validator("base")(_json_within)


class RelationTarget(_Strict):
    """An inquiry a relation of a new inquiry points at."""

    id: _Id
    kind: _Id
    seq: int
    title: _Name | None = None


class DraftRelation(_Strict):
    """One relation picked on the create form: the option's key, and its target."""

    option: _Id
    target: RelationTarget


class CreateDraft(_Strict):
    """The create form's unsent inquiry."""

    kind: _Id
    title: _Text
    description: _Text
    fields: Annotated[dict[_Id, _Text], Field(max_length=50)]
    chips: Annotated[dict[_Id, JsonValue], Field(max_length=50)]
    relations: Annotated[list[DraftRelation], Field(max_length=100)]

    _bounded = field_validator("chips")(_json_within)


class Preferences(_Strict):
    """Every preference, the user's choice or its default."""

    theme: Literal["dark", "light", "system"] = Field(
        default="dark",
        description="The app's colours: dark, light, or whichever the system uses.",
        json_schema_extra=SETTING,
    )
    skin: Literal[
        "default",
        "retro",
        "scifi",
        "dos",
        "console",
        "matrix",
        "hacker",
        "minimal",
        "brutal",
    ] = Field(
        default="default",
        description=(
            "The app's look: default, or one of the fixed skins, which ignore theme."
        ),
        json_schema_extra=SETTING,
    )
    chat_side: Literal["left", "right"] = Field(
        default="left",
        description="The side of the workbench Chat stands on; the sidebar takes the other.",
        json_schema_extra=SETTING,
    )
    app_bar: Literal["top", "left"] = Field(
        default="top",
        description=(
            "Where the app bar stands: along the top, or down the left side, where it "
            "resizes and folds to its icons. A phone keeps it along the top."
        ),
        json_schema_extra=SETTING,
    )
    app_bar_width: int = Field(
        default=220,
        ge=160,
        le=400,
        description="The app bar's width in px when it stands on the left, unfolded.",
    )
    app_bar_folded: bool = Field(
        default=False,
        description="The app bar on the left folded to its icons.",
    )
    highlight_mentions: bool = Field(
        default=True,
        description="Light up the rows the assistant's answers mention.",
        json_schema_extra=SETTING,
    )
    terminal_font_size: str = Field(
        default="13px",
        pattern=r"^[1-9][0-9]?(\.[0-9])?(px|pt)$",
        description=("The browser terminal's font size, in px or pt: 13px, or 16pt."),
        json_schema_extra=SETTING,
    )
    terminal_tab_chords: bool = Field(
        default=False,
        description=(
            "Ctrl+T, Ctrl+W and Ctrl+Tab open, close and switch terminal tabs "
            "instead of reaching the shell (Ctrl+Shift+T, Ctrl+Shift+W and "
            "Ctrl+Shift+PageUp/PageDown always do)."
        ),
        json_schema_extra=SETTING,
    )
    terminal_placement: Literal["bottom", "side"] = Field(
        default="bottom",
        description=(
            "Where the terminal opens beside the page: under it, the page's width, "
            "or at its side."
        ),
        json_schema_extra=SETTING,
    )
    home: Literal["graph", "empty"] = Field(
        default="graph",
        description=(
            "What the canvas's Home button and a new canvas tab show: "
            "the graph, or an empty canvas."
        ),
        json_schema_extra=SETTING,
    )
    chat_provider: ChatProvider = Field(
        default="assistant",
        description=(
            "The provider a new chat starts on: the assistant, or claude or codex run on "
            "a machine of yours."
        ),
        json_schema_extra=SETTING,
    )
    chat_tool_calls: bool = Field(
        default=False,
        description=(
            "Show each tool call an agent makes in Chat as a card between the lines: "
            "what it ran, and what came back."
        ),
        json_schema_extra=SETTING,
    )
    stars: Annotated[list[_Id], Field(max_length=2_000)] = Field(
        default=list[_Id](),
        description="The inquiries you starred, by id: your bookmarks, never a label.",
    )
    views: Annotated[list[SavedView], Field(max_length=200)] = Field(
        default=list[SavedView](),
        description="Your saved views: a name and the list query each one shows.",
    )
    console_views: Annotated[list[ConsoleView], Field(max_length=200)] = Field(
        default=list[ConsoleView](),
        description="Your Console views: a name, what each picks, its level and its range.",
    )
    aliases: Annotated[list[_Name], Field(max_length=50)] = Field(
        default=list[_Name](),
        description="The names besides your email that mean you in owner and subscriber filters.",
    )
    people: Annotated[dict[_Name, Person], Field(max_length=1_000)] = Field(
        default=dict[_Name, Person](),
        description="Names for people and agents you added by hand, by owner or subscriber value.",
    )
    read: ReadState = Field(
        default_factory=lambda: ReadState(boundary=None, marks=[]),
        description="The notifications you have read: a boundary time, and changes marked one by one after it.",
    )
    collapsed: Annotated[list[_Id], Field(max_length=500)] = Field(
        default=list[_Id](),
        description="The sections you folded, by key.",
    )
    dismissed: Annotated[list[_Id], Field(max_length=200)] = Field(
        default=list[_Id](),
        description="The notes you dismissed, by key: each stays hidden wherever it would show.",
    )
    lens: _Id | None = Field(
        default=None,
        description="The detail lens you used last; none until you pick one.",
    )
    drafts: Annotated[dict[_Id, FieldDraft], Field(max_length=200)] = Field(
        default=dict[_Id, FieldDraft](),
        description="Unsent edits of a field, by inquiry id and field name, until you send or discard them.",
    )
    create_draft: CreateDraft | None = Field(
        default=None,
        description="The inquiry you started on the create form and have not created.",
    )
    workspaces: WorkspacesPreference = Field(
        default_factory=WorkspacesPreference,
        description=(
            "Your workspace bar order, pins, and hidden items, and the canvas you "
            "opened last in each workspace."
        ),
    )
    settings_artifact_id: _Id | None = Field(
        default=None,
        description="Your personal settings Artifact id, pinned when first opened.",
    )


def stored_preferences(stored: object) -> Preferences:
    """Read a user's stored preferences, the defaults filling every key not set.

    Args:
      stored: The ``users.preferences`` value: an object of the keys set (the
        column's CHECK allows no other JSON), or None.

    Returns:
      preferences: Every key; one stored under a name no longer known is ignored.

    """
    known = {
        key: value
        for key, value in from_plain(stored, dict[str, object], default={}).items()
        if key in Preferences.model_fields
    }
    return Preferences.model_validate(known)


async def write_preferences(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    change: Mapping[str, object],
) -> Preferences | None:
    """Change some of a user's preferences, and put others back to their defaults.

    The keys ``change`` leaves out keep their values. It is one statement, so two
    changes to different keys at once both keep theirs.

    Args:
      conn: The connection to write on.
      user_id: Whose preferences change.
      change: The keys to change: a value sets the key, ``None`` puts it back to
        its default.

    Returns:
      preferences: Every key after the change, or None when the user is gone.

    Raises:
      ValueError: Naming a key no preference has or a value its key does not take;
        nothing is written.

    """
    unknown = sorted(set(change) - set(Preferences.model_fields))
    if unknown:
        raise ValueError(f"No such preference: {', '.join(unknown)}")
    chosen = {key: value for key, value in change.items() if value is not None}
    try:
        checked = Preferences.model_validate(chosen)
    except ValidationError as error:
        problems = (
            f"{'.'.join(map(str, problem['loc']))}: {problem['msg']}"
            for problem in error.errors()
        )
        raise ValueError("; ".join(problems)) from error
    stored = await conn.fetchrow(
        "UPDATE users SET preferences = "
        "(coalesce(preferences, '{}'::jsonb) || $1::jsonb) - $2::text[] "
        "WHERE id = $3 RETURNING preferences",
        checked.model_dump(mode="json", include=set(chosen)),
        # A retired key goes with the next write, so a stored value never lingers.
        [
            *sorted(key for key, value in change.items() if value is None),
            "chat_style",
        ],
        user_id,
    )
    return None if stored is None else stored_preferences(stored["preferences"])
