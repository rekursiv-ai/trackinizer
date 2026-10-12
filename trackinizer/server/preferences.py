"""A user's preferences: every key, its type, its default, and what it means.

They are kept on the user's row (``users.preferences``), so they follow the user to
any browser or machine, and an agent the user works with can change them. The
stored object holds only the keys the user set; a read fills in the defaults.
Each field's description is the comment Settings shows beside the key.

A write replaces a top-level key whole. Every collection and text is bounded: the
row is read on every profile fetch, so no key may grow without limit.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)
from pydantic_core import to_json
from treekle import from_plain


_Id = Annotated[str, StringConstraints(max_length=100)]
_Name = Annotated[str, StringConstraints(max_length=200)]
_Text = Annotated[str, StringConstraints(max_length=100_000)]


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


class Person(_Strict):
    """A person or an agent added by hand, under the name the pickers show."""

    name: _Name
    type: Literal["person", "agent"]


class ReadState(_Strict):
    """Which notifications are read."""

    boundary: Annotated[str, StringConstraints(max_length=100)] | None
    marks: Annotated[list[_Id], Field(max_length=5_000)]


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
    )
    chat_style: Literal["default", "terminal"] = Field(
        default="default",
        description="How Chat is drawn: the app's own look, or as a terminal.",
    )
    chat_side: Literal["left", "right"] = Field(
        default="left",
        description="The side of the workbench Chat stands on; the sidebar takes the other.",
    )
    highlight_mentions: bool = Field(
        default=True,
        description="Light up the rows the assistant's answers mention.",
    )
    stars: Annotated[list[_Id], Field(max_length=2_000)] = Field(
        default=[],
        description="The inquiries you starred, by id: your bookmarks, never a label.",
    )
    views: Annotated[list[SavedView], Field(max_length=200)] = Field(
        default=[],
        description="Your saved views: a name and the list query each one shows.",
    )
    aliases: Annotated[list[_Name], Field(max_length=50)] = Field(
        default=[],
        description="The names besides your email that mean you in owner and subscriber filters.",
    )
    people: Annotated[dict[_Name, Person], Field(max_length=1_000)] = Field(
        default={},
        description="Names for people and agents you added by hand, by owner or subscriber value.",
    )
    read: ReadState = Field(
        default_factory=lambda: ReadState(boundary=None, marks=[]),
        description="The notifications you have read: a boundary time, and changes marked one by one after it.",
    )
    collapsed: Annotated[list[_Id], Field(max_length=500)] = Field(
        default=[],
        description="The sections you folded, by key.",
    )
    lens: _Id | None = Field(
        default=None,
        description="The detail lens you used last; none until you pick one.",
    )
    drafts: Annotated[dict[_Id, FieldDraft], Field(max_length=200)] = Field(
        default={},
        description="Unsent edits of a field, by inquiry id and field name, until you send or discard them.",
    )
    create_draft: CreateDraft | None = Field(
        default=None,
        description="The inquiry you started on the create form and have not created.",
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
