"""The welcome flow's agreement, recorded per user against the rules version.

The rules version is Issue#1's last change, so editing the rules makes a recorded
agreement stale. The whole app answers each request over PGlite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from treekle import from_plain

import pytest

from trackinizer.server.api.canvas_test_support import (
    OTHER_EMAIL,
    OTHER_KEY_ID,
    OTHER_USER_ID,
    seed_accounts,
)
from trackinizer.server.api.conftest import (
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)


if TYPE_CHECKING:
    import uuid

    import httpx2

    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
]


async def test_a_new_user_has_agreed_to_nothing(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)

    profile = await _profile(client)

    assert profile["acknowledged_at"] is None
    assert profile["acknowledged_rules_version"] is None
    assert profile["rules_issue_id"] is None
    assert profile["rules_version"] is None


async def test_an_unlocked_rules_issue_is_not_in_force(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    await _rules_issue(client)

    profile = await _profile(client)
    agreed = await client.put("/api/me/acknowledge", json={"rules_version": "x"})

    assert profile["rules_issue_id"] is None
    assert profile["rules_version"] is None
    assert agreed.status_code == 409


async def test_locking_the_rules_puts_them_in_force(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    rules = await _rules_issue(client)
    before = await _profile(client)

    await _lock(client, rules, locked=True)
    after = await _profile(client)
    agreed = await _agree(client)

    assert before["rules_version"] is None
    assert after["rules_issue_id"] == rules
    assert isinstance(after["rules_version"], str)
    assert agreed.status_code == 200, agreed.text


async def test_unlocking_the_rules_takes_them_out_of_force(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    rules = await _rules_issue(client)
    await _lock(client, rules, locked=True)

    await _lock(client, rules, locked=False)

    assert (await _profile(client))["rules_version"] is None


async def test_agreeing_records_the_current_rules_version(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    rules = await _rules_issue(client)
    await _lock(client, rules, locked=True)

    agreed = await _agree(client)
    profile = await _profile(client)

    assert agreed.status_code == 200, agreed.text
    assert profile["rules_issue_id"] == rules
    assert profile["acknowledged_rules_version"] == profile["rules_version"]
    assert profile["acknowledged_rules_version"] != "none"
    assert isinstance(profile["acknowledged_at"], str)
    assert from_plain(agreed.json(), dict[str, object]) == {
        "acknowledged_at": profile["acknowledged_at"],
        "acknowledged_rules_version": profile["rules_version"],
    }


async def test_editing_the_rules_makes_the_agreement_stale(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    rules = await _rules_issue(client)
    await _lock(client, rules, locked=True)
    await _agree(client)
    agreed = await _profile(client)

    edit = await client.put(
        f"/api/inquiries/{rules}/description",
        json={"value": "new rules"},
    )
    stale = await _profile(client)

    assert edit.status_code == 200, edit.text
    assert stale["rules_version"] != agreed["rules_version"]
    assert stale["acknowledged_rules_version"] == agreed["rules_version"]


async def test_locking_the_rules_again_leaves_the_version_alone(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    rules = await _rules_issue(client)
    await _lock(client, rules, locked=True)
    before = await _profile(client)

    await _lock(client, rules, locked=False)
    await _lock(client, rules, locked=True)
    after = await _profile(client)

    assert after["rules_version"] == before["rules_version"]


async def test_editing_a_child_of_the_rules_leaves_the_version_alone(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    rules = await _rules_issue(client)
    await _lock(client, rules, locked=True)
    child = await client.post(
        "/api/inquiries/issue",
        json={"title": "child", "narrows": [[rules, None]]},
    )
    assert child.status_code == 201, child.text
    child_id = from_plain(from_plain(child.json(), dict[str, object])["id"], str)
    before = await _profile(client)

    edit = await client.put(
        f"/api/inquiries/{child_id}/title",
        json={"value": "renamed"},
    )
    after = await _profile(client)

    assert edit.status_code == 200, edit.text
    assert after["rules_version"] == before["rules_version"]


async def test_agreeing_to_rules_that_changed_since_they_were_read_is_refused(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    rules = await _rules_issue(client)
    await _lock(client, rules, locked=True)
    read = await _profile(client)
    await client.put(f"/api/inquiries/{rules}/description", json={"value": "new"})

    refused = await client.put(
        "/api/me/acknowledge",
        json={"rules_version": read["rules_version"]},
    )
    profile = await _profile(client)

    assert refused.status_code == 409, refused.text
    assert profile["acknowledged_rules_version"] is None


async def test_a_key_cannot_agree_for_its_user(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    install_identity(make_test_identity(role="writer"))

    refused = await client.put(
        "/api/me/acknowledge",
        json={"rules_version": "none"},
    )

    assert refused.status_code == 403


def _as(role: Role) -> None:
    install_identity(make_test_identity(role=role, api_key_id=None))


async def _rules_issue(client: httpx2.AsyncClient) -> str:
    """Create the first Issue, which is the rules."""
    made = await client.post("/api/inquiries/issue", json={"title": "Rules"})
    assert made.status_code == 201, made.text
    return from_plain(from_plain(made.json(), dict[str, object])["id"], str)


async def _lock(client: httpx2.AsyncClient, target: str, *, locked: bool) -> None:
    """Lock or unlock a row as an admin, who then makes the requests that follow."""
    _as("admin")
    put = await client.put(
        f"/api/admin/inquiries/{target}/lock",
        json={"locked": locked},
    )
    assert put.status_code == 200, put.text


async def _agree(client: httpx2.AsyncClient) -> httpx2.Response:
    """Agree to the rules version the caller's profile shows."""
    shown = await _profile(client)
    return await client.put(
        "/api/me/acknowledge",
        json={"rules_version": shown["rules_version"]},
    )


async def _profile(client: httpx2.AsyncClient) -> dict[str, object]:
    response = await client.get("/api/me/profile")
    assert response.status_code == 200, response.text
    return from_plain(response.json(), dict[str, object])


_DEFAULTS: dict[str, object] = {
    "theme": "dark",
    "skin": "default",
    "chat_side": "left",
    "highlight_mentions": True,
    "terminal_font_size": "13px",
    "terminal_tab_chords": False,
    "home": "graph",
    "chat_provider": "assistant",
    "chat_tool_calls": False,
    "stars": [],
    "views": [],
    "console_views": [],
    "aliases": [],
    "people": {},
    "read": {"boundary": None, "marks": []},
    "collapsed": [],
    "lens": None,
    "drafts": {},
    "create_draft": None,
    "workspaces": {"order": [], "pinned": [], "hidden": []},
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


async def _stored_preferences(store: Store, user_id: uuid.UUID) -> object:
    async with store.engine.acquire() as conn:
        return await conn.fetchval(
            "SELECT preferences FROM users WHERE id = $1",
            user_id,
        )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
