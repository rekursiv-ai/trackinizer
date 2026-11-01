"""A user's preferences: the defaults, a change, a reset, and who sees them.

The whole app answers each request over PGlite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from treekle import from_plain

import pytest

from trackinizer.server.api.conftest import (
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.api.session_test_support import (
    OTHER_EMAIL,
    OTHER_KEY_ID,
    OTHER_USER_ID,
    seed_accounts,
)


if TYPE_CHECKING:
    import uuid

    import httpx2

    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
]


_DEFAULTS: dict[str, object] = {
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
    "assistant_prompt": "lead",
    "cli_folder": "",
    "chat_tool_calls": True,
    "autofix": True,
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
    "workspaces": {
        "order": [],
        "hidden": [],
        "colors": {},
        "expanded": [],
        "tabs": [],
        "last_canvas": {},
    },
    "settings_artifact_id": None,
}


async def test_preferences_are_the_defaults_until_set(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)

    assert (await _profile(client))["preferences"] == _DEFAULTS
    assert await _stored_preferences(store, TEST_USER_ID) is None


async def test_a_change_sets_its_keys_and_keeps_the_rest(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)

    first = await client.patch("/api/me/preferences", json={"theme": "light"})
    second = await client.patch("/api/me/preferences", json={"chat_side": "right"})

    assert first.status_code == 200, first.text
    assert second.json() == {**_DEFAULTS, "theme": "light", "chat_side": "right"}
    assert (await _profile(client))["preferences"] == second.json()
    # Only what the user set is stored; the defaults stay out.
    assert await _stored_preferences(store, TEST_USER_ID) == {
        "theme": "light",
        "chat_side": "right",
    }


async def test_a_retired_key_a_user_still_has_stored_is_ignored_then_dropped(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    async with store.engine.acquire() as conn:
        await conn.execute(
            'UPDATE users SET preferences = \'{"chat_style": "terminal"}\'::jsonb '
            "WHERE id = $1",
            TEST_USER_ID,
        )

    assert (await _profile(client))["preferences"] == _DEFAULTS
    changed = await client.patch("/api/me/preferences", json={"theme": "light"})

    assert changed.status_code == 200, changed.text
    assert "chat_style" not in from_plain(changed.json(), dict[str, object])
    assert await _stored_preferences(store, TEST_USER_ID) == {"theme": "light"}


async def test_null_puts_a_key_back_to_its_default(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    await client.patch(
        "/api/me/preferences",
        json={"theme": "light", "chat_side": "right"},
    )

    reset = await client.patch("/api/me/preferences", json={"theme": None})

    assert reset.json() == {**_DEFAULTS, "chat_side": "right"}
    assert await _stored_preferences(store, TEST_USER_ID) == {"chat_side": "right"}


async def test_an_unknown_key_or_a_bad_value_writes_nothing(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    await client.patch("/api/me/preferences", json={"theme": "light"})

    unknown = await client.patch(
        "/api/me/preferences",
        json={"colour": "red", "chat_side": "right"},
    )
    bad = await client.patch(
        "/api/me/preferences",
        json={"theme": "blue", "chat_side": "right"},
    )

    assert unknown.status_code == 422
    assert "colour" in unknown.text
    assert bad.status_code == 422
    assert "theme" in bad.text
    assert await _stored_preferences(store, TEST_USER_ID) == {"theme": "light"}


async def test_the_users_own_data_is_kept_whole_per_key_and_follows_them(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    draft = {"text": "Words", "base": None}
    created: dict[str, object] = {
        "kind": "Issue",
        "title": "Half",
        "description": "",
        "fields": {},
        "chips": {"priority": 20},
        "relations": [
            {
                "option": "narrows:out",
                "target": {"id": "i1", "kind": "Issue", "seq": 1},
            },
        ],
    }
    await client.patch(
        "/api/me/preferences",
        json={
            "stars": ["a", "b"],
            "drafts": {"i1:title": draft},
            "create_draft": created,
        },
    )

    # A write replaces its key whole: the other draft goes with the map it was in.
    replaced = await client.patch(
        "/api/me/preferences",
        json={"stars": ["c"], "drafts": {"i2:title": draft}},
    )

    assert replaced.json() == {
        **_DEFAULTS,
        "stars": ["c"],
        "drafts": {"i2:title": draft},
        "create_draft": {
            **created,
            "relations": [
                {
                    "option": "narrows:out",
                    "target": {"id": "i1", "kind": "Issue", "seq": 1, "title": None},
                },
            ],
        },
    }
    assert (await _profile(client))["preferences"] == replaced.json()
    cleared = await client.patch(
        "/api/me/preferences",
        json={"drafts": None, "create_draft": None},
    )
    assert cleared.json() == {**_DEFAULTS, "stars": ["c"]}
    assert await _stored_preferences(store, TEST_USER_ID) == {"stars": ["c"]}


async def test_data_past_its_bound_writes_nothing(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    await client.patch("/api/me/preferences", json={"stars": ["kept"]})

    refused = await client.patch(
        "/api/me/preferences",
        json={"lens": "graph", "stars": ["s"] * 2_001},
    )

    assert refused.status_code == 422
    assert "stars" in refused.text
    assert await _stored_preferences(store, TEST_USER_ID) == {"stars": ["kept"]}


async def test_one_users_preferences_leave_anothers_alone(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    await client.patch("/api/me/preferences", json={"theme": "light"})

    install_identity(
        make_test_identity(
            user_id=OTHER_USER_ID,
            api_key_id=OTHER_KEY_ID,
            email=OTHER_EMAIL,
        ),
    )

    assert (await _profile(client))["preferences"] == _DEFAULTS
    assert await _stored_preferences(store, OTHER_USER_ID) is None


async def test_an_admin_resets_a_users_preferences_and_never_sees_them(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    await client.patch(
        "/api/me/preferences",
        json={"theme": "light", "chat_side": "right"},
    )
    install_identity(
        make_test_identity(
            user_id=OTHER_USER_ID,
            api_key_id=None,
            email=OTHER_EMAIL,
            role="admin",
        ),
    )

    listed = await client.get("/api/admin/users")
    reset = await client.delete(f"/api/admin/users/{TEST_USER_ID}/preferences")

    assert listed.status_code == 200, listed.text
    # No admin route answers with them: the user list leaves the column out.
    assert "preferences" not in listed.text
    assert "terminal" not in listed.text
    assert (reset.status_code, reset.content) == (204, b"")
    assert await _stored_preferences(store, TEST_USER_ID) is None


async def _profile(client: httpx2.AsyncClient) -> dict[str, object]:
    response = await client.get("/api/me/profile")
    assert response.status_code == 200, response.text
    return from_plain(response.json(), dict[str, object])


async def _stored_preferences(store: Store, user_id: uuid.UUID) -> object:
    async with store.engine.acquire() as conn:
        return await conn.fetchval(
            "SELECT preferences FROM users WHERE id = $1",
            user_id,
        )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
