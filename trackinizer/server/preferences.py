"""A user's preferences: every key, its type, its default, and what it means.

They are kept on the user's row (``users.preferences``), so they follow the user to
any browser or machine, and an agent the user works with can change them. The
stored object holds only the keys the user set; a read fills in the defaults.
Each field's description is the comment Settings shows beside the key.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from treekle import from_plain


class Preferences(BaseModel):
    """Every preference, the user's choice or its default."""

    # Strict: a stored or sent value is the type itself, never coerced (``"yes"`` is no bool).
    model_config = ConfigDict(extra="forbid", strict=True)

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
