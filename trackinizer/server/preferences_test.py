"""Tests for ``server/preferences.py``: reading a user's stored preferences."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import uuid

from pydantic import JsonValue

import pydantic
import pytest

from trackinizer.server.preferences import (
    ASSISTANT_KEYS,
    SETTING,
    Preferences,
    stored_preferences,
    write_preferences,
)


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


_DEFAULTS: dict[str, JsonValue] = {
    "theme": "dark",
    "skin": "default",
    "scanlines": True,
    "terminal_colors": {
        "text": "#00ff00",
        "background": "#000000",
        "bold": None,
        "palette": [
            "#171421",
            "#c01c28",
            "#26a269",
            "#a2734c",
            "#12488b",
            "#a347ba",
            "#2aa1b3",
            "#d0cfcc",
            "#5e5c64",
            "#f66151",
            "#33d17a",
            "#e9ad0c",
            "#2a7bde",
            "#c061cb",
            "#33c7de",
            "#ffffff",
        ],
        "bold_is_bright": True,
    },
    "chat_side": "left",
    "app_bar": "top",
    "app_bar_width": 220,
    "app_bar_folded": False,
    "highlight_mentions": True,
    "terminal_font_size": "13px",
    "terminal_tab_chords": False,
    "terminal_placement": "bottom",
    "home": "graph",
    "chat_provider": "assistant",
    "chat_tool_calls": True,
    "stars": [],
    "views": [],
    "console_views": [],
    "aliases": [],
    "people": {},
    "read": {"boundary": None, "marks": []},
    "collapsed": [],
    "dismissed": [],
    "lens": None,
    "drafts": {},
    "create_draft": None,
    "workspaces": {"order": [], "pinned": [], "hidden": [], "last_canvas": {}},
    "settings_artifact_id": None,
}

_VIEW_REQUEST: dict[str, JsonValue] = {
    "kinds": ["Issue"],
    "filters": [{"field": "owner", "op": "re", "value": "^(ada)$"}],
}

_VIEW: dict[str, JsonValue] = {"id": "v1", "name": "Mine", "request": _VIEW_REQUEST}

_CONSOLE_VIEW: dict[str, JsonValue] = {
    "id": "c1",
    "name": "Atlas live",
    "pinned": False,
    "created": "2026-10-01T09:00:00.000Z",
    "agents": ["atlas-*"],
    "rooms": [],
    "clis": ["claude"],
    "level": 2,
    "range": {"live": True},
}

_WINDOW: dict[str, JsonValue] = {
    "live": False,
    "since": "2026-10-01T09:00:00.000Z",
    "until": None,
}

_RELATION: dict[str, JsonValue] = {
    "option": "narrows:out",
    "target": {"id": "i1", "kind": "Issue", "seq": 7},
}

_CREATE_DRAFT: dict[str, JsonValue] = {
    "kind": "Issue",
    "title": "T",
    "description": "",
    "fields": {"confidence": "0.5"},
    "chips": {"priority": 20, "labels": ["a"], "owner": None},
    "relations": [_RELATION],
}


def test_nothing_stored_reads_as_the_defaults() -> None:
    assert stored_preferences(None).model_dump() == _DEFAULTS


def test_the_keys_set_override_their_defaults_and_unknown_ones_are_dropped() -> None:
    read = stored_preferences(
        {"theme": "system", "highlight_mentions": False, "retired": "x"},
    )
    assert read.model_dump() == {
        **_DEFAULTS,
        "theme": "system",
        "highlight_mentions": False,
    }


def test_a_value_its_key_does_not_take_is_refused() -> None:
    with pytest.raises(pydantic.ValidationError):
        Preferences.model_validate({"chat_side": "middle"})


@pytest.mark.parametrize(
    "change",
    [{"app_bar": "bottom"}, {"app_bar_width": 159}, {"app_bar_width": 401}],
)
def test_the_app_bar_stands_top_or_left_at_a_width_it_can_hold(
    change: dict[str, JsonValue],
) -> None:
    left = Preferences.model_validate(
        {"app_bar": "left", "app_bar_width": 400, "app_bar_folded": True},
    )
    assert (left.app_bar, left.app_bar_width, left.app_bar_folded) == (
        "left",
        400,
        True,
    )
    with pytest.raises(pydantic.ValidationError):
        Preferences.model_validate(change)


def test_a_skin_is_one_of_the_named_looks() -> None:
    assert Preferences.model_validate({"skin": "dos"}).skin == "dos"
    with pytest.raises(pydantic.ValidationError):
        Preferences.model_validate({"skin": "cyber"})


def test_terminal_colors_are_gnome_green_on_black_until_changed_and_hold_a_terminals_shape() -> (
    None
):
    colors = stored_preferences(None).terminal_colors
    assert (colors.text, colors.background, colors.bold) == ("#00ff00", "#000000", None)
    assert (colors.palette[1], colors.palette[14], colors.bold_is_bright) == (
        "#c01c28",
        "#33c7de",
        True,
    )
    assert "terminal_colors" in ASSISTANT_KEYS
    whole = colors.model_dump()
    tango = ["#2e3436", *["#cc0000"] * 15]
    chosen = Preferences.model_validate(
        {"terminal_colors": {**whole, "palette": tango, "bold": "#ffffff"}},
    ).terminal_colors
    assert (chosen.palette, chosen.bold, chosen.text) == (tango, "#ffffff", "#00ff00")
    for wrong in (
        {**whole, "palette": tango[:15]},
        {**whole, "palette": [*tango, "#000000"]},
        {**whole, "text": "green"},
        {**whole, "background": "#FFFFFF"},
        {**whole, "bold": "#fff"},
        {**whole, "cursor": "#ffffff"},
        {"palette": tango},
    ):
        with pytest.raises(pydantic.ValidationError):
            Preferences.model_validate({"terminal_colors": wrong})


def test_terminal_colors_default_is_a_copy_each_time() -> None:
    first = stored_preferences(None).terminal_colors
    first.palette[0] = "#ffffff"
    assert stored_preferences(None).terminal_colors.palette[0] == "#171421"


def test_scanlines_are_on_until_the_user_turns_them_off() -> None:
    assert stored_preferences(None).scanlines is True
    assert stored_preferences({"scanlines": False}).scanlines is False
    assert "scanlines" in ASSISTANT_KEYS
    with pytest.raises(pydantic.ValidationError):
        Preferences.model_validate({"scanlines": "off"})


def test_workspace_preferences_keep_order_pin_visibility_and_last_canvas() -> None:
    saved = {
        "order": ["22222222-2222-2222-2222-222222222222"],
        "pinned": ["22222222-2222-2222-2222-222222222222"],
        "hidden": ["33333333-3333-3333-3333-333333333333"],
        "last_canvas": {
            "22222222-2222-2222-2222-222222222222": (
                "44444444-4444-4444-4444-444444444444"
            ),
        },
    }
    assert (
        Preferences.model_validate({"workspaces": saved}).workspaces.model_dump()
        == saved
    )


def test_the_assistants_keys_are_exactly_the_settings_the_document_shows() -> None:
    marked = {
        name
        for name, field in Preferences.model_fields.items()
        if field.json_schema_extra == SETTING
    }
    assert marked == ASSISTANT_KEYS


def test_a_new_chat_starts_on_the_assistant_unless_the_user_picked_a_cli() -> None:
    assert stored_preferences(None).chat_provider == "assistant"
    assert stored_preferences({"chat_provider": "codex"}).chat_provider == "codex"
    with pytest.raises(pydantic.ValidationError):
        Preferences.model_validate({"chat_provider": "gemini"})


@pytest.mark.asyncio
async def test_a_write_sets_the_chat_provider_a_new_chat_starts_on() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"preferences": {"chat_provider": "claude"}})
    written = await write_preferences(
        cast("Conn", conn),
        user_id=_USER,
        change={"chat_provider": "claude"},
    )
    assert written is not None
    assert written.chat_provider == "claude"
    conn.fetchrow.assert_awaited_once_with(
        "UPDATE users SET preferences = "
        "(coalesce(preferences, '{}'::jsonb) || $1::jsonb) - $2::text[] "
        "WHERE id = $3 RETURNING preferences",
        {"chat_provider": "claude"},
        ["chat_style"],
        _USER,
    )
    with pytest.raises(ValueError, match="chat_provider: "):
        await write_preferences(
            cast("Conn", conn),
            user_id=_USER,
            change={"chat_provider": "gemini"},
        )


def test_tool_calls_in_chat_are_on_until_the_user_turns_them_off() -> None:
    assert stored_preferences(None).chat_tool_calls is True
    assert stored_preferences({"chat_tool_calls": False}).chat_tool_calls is False
    assert "chat_tool_calls" in ASSISTANT_KEYS
    with pytest.raises(pydantic.ValidationError):
        Preferences.model_validate({"chat_tool_calls": "yes"})


@pytest.mark.asyncio
async def test_a_write_turns_tool_calls_in_chat_off_and_back_to_the_default() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"preferences": {"chat_tool_calls": False}})
    written = await write_preferences(
        cast("Conn", conn),
        user_id=_USER,
        change={"chat_tool_calls": False},
    )
    assert written is not None
    assert written.chat_tool_calls is False
    conn.fetchrow.assert_awaited_once_with(
        "UPDATE users SET preferences = "
        "(coalesce(preferences, '{}'::jsonb) || $1::jsonb) - $2::text[] "
        "WHERE id = $3 RETURNING preferences",
        {"chat_tool_calls": False},
        ["chat_style"],
        _USER,
    )
    conn.fetchrow = AsyncMock(return_value={"preferences": {"theme": "light"}})
    reset = await write_preferences(
        cast("Conn", conn),
        user_id=_USER,
        change={"theme": "light", "chat_tool_calls": None},
    )
    assert reset is not None
    assert reset.chat_tool_calls is True
    conn.fetchrow.assert_awaited_once_with(
        "UPDATE users SET preferences = "
        "(coalesce(preferences, '{}'::jsonb) || $1::jsonb) - $2::text[] "
        "WHERE id = $3 RETURNING preferences",
        {"theme": "light"},
        ["chat_tool_calls", "chat_style"],
        _USER,
    )


@pytest.mark.parametrize("size", ["13px", "16pt", "9.5px", "99pt"])
def test_a_terminal_font_size_is_px_or_pt(size: str) -> None:
    assert (
        Preferences.model_validate({"terminal_font_size": size}).terminal_font_size
        == size
    )


@pytest.mark.parametrize("size", ["0px", "100px", "16em", "16", "1.25px", "16 pt"])
def test_any_other_terminal_font_size_is_refused(size: str) -> None:
    with pytest.raises(pydantic.ValidationError):
        Preferences.model_validate({"terminal_font_size": size})


_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.mark.asyncio
async def test_a_write_sets_and_resets_keys_in_one_statement() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"preferences": {"theme": "light"}})

    written = await write_preferences(
        cast("Conn", conn),
        user_id=_USER,
        change={"theme": "light", "chat_side": None},
    )

    assert written == Preferences(theme="light")
    conn.fetchrow.assert_awaited_once_with(
        "UPDATE users SET preferences = "
        "(coalesce(preferences, '{}'::jsonb) || $1::jsonb) - $2::text[] "
        "WHERE id = $3 RETURNING preferences",
        {"theme": "light"},
        ["chat_side", "chat_style"],
        _USER,
    )


@pytest.mark.asyncio
async def test_a_write_for_a_user_who_is_gone_is_none() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    assert (
        await write_preferences(
            cast("Conn", conn),
            user_id=_USER,
            change={"theme": "light"},
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"colour": "red", "theme": None}, "No such preference: colour"),
        ({"theme": "blue"}, "theme: "),
        (
            {"chat_side": "right", "highlight_mentions": "yes"},
            "highlight_mentions: ",
        ),
    ],
)
async def test_a_key_or_value_the_preferences_do_not_take_writes_nothing(
    change: dict[str, object],
    message: str,
) -> None:
    conn = AsyncMock()
    with pytest.raises(ValueError, match=message):
        await write_preferences(cast("Conn", conn), user_id=_USER, change=change)
    assert not conn.fetchrow.called


def test_the_user_data_keys_round_trip_whole() -> None:
    kept: dict[str, JsonValue] = {
        "stars": ["a", "b"],
        "views": [_VIEW],
        "console_views": [
            _CONSOLE_VIEW,
            {**_CONSOLE_VIEW, "id": "c2", "range": _WINDOW},
        ],
        "aliases": ["ada"],
        "people": {"dan": {"name": "Dan", "type": "person"}},
        "read": {"boundary": "2026-10-01T00:00:00Z", "marks": ["c1"]},
        "collapsed": ["open"],
        "lens": "graph",
        "drafts": {"i1:description": {"text": "hi", "base": {"a": [1, None]}}},
        "create_draft": {
            **_CREATE_DRAFT,
            "relations": [
                {
                    "option": "narrows:out",
                    "target": {"id": "i1", "kind": "Issue", "seq": 7, "title": None},
                },
            ],
        },
    }
    assert stored_preferences(kept).model_dump(mode="json", include=set(kept)) == kept


def test_a_draft_names_its_base_even_when_unset() -> None:
    with pytest.raises(pydantic.ValidationError):
        Preferences.model_validate({"drafts": {"i1:title": {"text": "x"}}})
    read = Preferences.model_validate(
        {"drafts": {"i1:title": {"text": "x", "base": None}}},
    )
    assert read.drafts["i1:title"].base is None


@pytest.mark.parametrize(
    "window",
    [
        {"live": False, "since": None, "until": None},
        {"live": False, "since": None, "until": "2026-10-01T09:00:00Z"},
        {
            "live": False,
            "since": "2026-10-01T09:00:00Z",
            "until": "2026-10-01T09:00:00Z",
        },
        {
            "live": False,
            "since": "2026-10-01T10:00:00+02:00",
            "until": "2026-10-01T09:00:00Z",
        },
    ],
)
def test_a_console_window_may_be_open_or_end_where_it_starts(
    window: dict[str, JsonValue],
) -> None:
    kept = Preferences.model_validate(
        {"console_views": [{**_CONSOLE_VIEW, "range": window}]},
    )
    assert kept.console_views[0].range.model_dump() == window


def _key_of(change: dict[str, JsonValue]) -> str:
    return next(iter(change))


@pytest.mark.parametrize(
    "change",
    [
        {"stars": ["x"] * 2_001},
        {"stars": ["x" * 101]},
        {"views": [_VIEW] * 201},
        {"views": [{**_VIEW, "name": "n" * 201}]},
        {
            "views": [
                {
                    **_VIEW,
                    "request": {
                        **_VIEW_REQUEST,
                        "filters": [{"field": "f", "op": "like", "value": ""}],
                    },
                },
            ],
        },
        {"views": [{**_VIEW, "extra": 1}]},
        {"console_views": [_CONSOLE_VIEW] * 201},
        {"console_views": [{**_CONSOLE_VIEW, "name": "n" * 201}]},
        {"console_views": [{**_CONSOLE_VIEW, "agents": ["a"] * 101}]},
        {"console_views": [{**_CONSOLE_VIEW, "rooms": ["r" * 201]}]},
        {"console_views": [{**_CONSOLE_VIEW, "level": 5}]},
        {"console_views": [{**_CONSOLE_VIEW, "created": "yesterday"}]},
        {"console_views": [{**_CONSOLE_VIEW, "created": "2026-10-01T09:00:00"}]},
        {"console_views": [{**_CONSOLE_VIEW, "extra": 1}]},
        {"console_views": [{**_CONSOLE_VIEW, "range": {"live": True, "since": None}}]},
        {"console_views": [{**_CONSOLE_VIEW, "range": {**_WINDOW, "since": "soon"}}]},
        {
            "console_views": [
                {
                    **_CONSOLE_VIEW,
                    "range": {
                        "live": False,
                        "since": "2026-10-01T10:00:00Z",
                        "until": "2026-10-01T09:00:00Z",
                    },
                },
            ],
        },
        {"aliases": ["a"] * 51},
        {"aliases": ["a" * 201]},
        {"people": {f"p{n}": {"name": "P", "type": "person"} for n in range(1_001)}},
        {"people": {"p": {"name": "P", "type": "robot"}}},
        {"read": {"boundary": None, "marks": ["m"] * 5_001}},
        {"read": {"boundary": None}},
        {"collapsed": ["c"] * 501},
        {"drafts": {f"d{n}": {"text": "", "base": None} for n in range(201)}},
        {"drafts": {"d": {"text": "x" * 100_001, "base": None}}},
        {"drafts": {"d": {"text": "", "base": "x" * 100_001}}},
        {"create_draft": {**_CREATE_DRAFT, "title": "x" * 100_001}},
        {"create_draft": {**_CREATE_DRAFT, "chips": {"labels": ["x" * 100_001]}}},
        {
            "create_draft": {
                **_CREATE_DRAFT,
                "relations": [_RELATION] * 101,
            },
        },
        {"create_draft": {**_CREATE_DRAFT, "kind": None}},
        {"lens": 3},
    ],
    ids=_key_of,
)
def test_a_value_past_its_bound_or_type_is_refused(
    change: dict[str, JsonValue],
) -> None:
    with pytest.raises(pydantic.ValidationError):
        Preferences.model_validate(change)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
