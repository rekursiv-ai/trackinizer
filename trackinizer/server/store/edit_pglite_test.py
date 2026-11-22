"""Field edits on the in-process PGlite substrate."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.types.errors import ConflictError
from trackinizer.wire.bodies import SubmitExperiment, SubmitIssue


if TYPE_CHECKING:
    from uuid import UUID

    from trackinizer.server.store.core import Store


async def _row_app(store: Store, row_id: UUID) -> dict[str, object] | None:
    """Read one row's ``app`` back through the Store."""
    row = await store.get_inquiry(row_id)
    assert row is not None
    return row.app


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_app_is_stored_verbatim_replaced_and_cleared_with_an_audit(
    store: Store,
) -> None:
    created: dict[str, object] = {"tracker": {"budget_usd": 20, "phase": "smoke"}}
    replaced: dict[str, object] = {"tracker": {"budget_usd": 20, "phase": "climb"}}
    # Any kind holds one: here the Issue an app's visual is anchored on.
    issue_id = await store.submit_issue(
        SubmitIssue(account="tester@example.com", title="brief", app=created),
    )
    assert await _row_app(store, issue_id) == created

    set_id = await store.set_app(issue_id, replaced, actor="scientist")
    assert await _row_app(store, issue_id) == replaced
    cleared_id = await store.set_app(issue_id, None, actor="scientist")
    assert await _row_app(store, issue_id) is None

    audits: list[tuple[object, ...]] = []
    for change_id in (set_id, cleared_id):
        assert change_id is not None
        change = await store.get_change(change_id)
        assert change is not None
        audits.append(
            (change.kind, change.old.app, change.new.app),
        )
    assert audits == [
        ("app", created, replaced),
        ("app", replaced, None),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_app_over_64_kib_is_refused_and_leaves_the_row_alone(
    store: Store,
) -> None:
    kept: dict[str, object] = {"tracker": {"verdict": "works"}}
    experiment_id = await store.submit_experiment(
        SubmitExperiment(account="tester@example.com", title="run", app=kept),
    )

    with pytest.raises(ConflictError, match="check constraint"):
        await store.set_app(experiment_id, {"b": "x" * 65_528}, actor="scientist")

    assert await _row_app(store, experiment_id) == kept


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_app_transition_writes_only_over_the_app_it_expected(
    store: Store,
) -> None:
    filed: dict[str, object] = {"tracker": {"verdict": "works"}}
    corrected: dict[str, object] = {
        "tracker": {"verdict": "inconclusive", "reason": "Within noise."},
    }
    experiment_id = await store.submit_experiment(
        SubmitExperiment(account="tester@example.com", title="run", app=filed),
    )
    await store.set_app(experiment_id, corrected, actor="lead")

    # A write planned from the app before the correction would erase it.
    with pytest.raises(ConflictError, match="app transition rejected"):
        await store.transition_app(
            experiment_id,
            expected_from=filed,
            to={"tracker": {"verdict": "works", "patch": "p"}},
            actor="meter",
        )
    assert await _row_app(store, experiment_id) == corrected

    merged: dict[str, object] = {
        "tracker": {"verdict": "inconclusive", "reason": "Within noise.", "patch": "p"},
    }
    change_id = await store.transition_app(
        experiment_id,
        expected_from=corrected,
        to=merged,
        actor="meter",
        reason="code change",
    )
    assert await _row_app(store, experiment_id) == merged
    assert change_id is not None
    change = await store.get_change(change_id)
    assert change is not None
    assert (change.kind, change.old.app, change.new.app) == (
        "app",
        corrected,
        merged,
    )
    assert change.reason == "code change"
    assert (
        await store.transition_app(
            experiment_id,
            expected_from=merged,
            to=merged,
            actor="meter",
        )
        is None
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_app_transition_compares_json_values_exactly(store: Store) -> None:
    experiment_id = await store.submit_experiment(
        SubmitExperiment(account="tester@example.com", title="run"),
    )
    # Unset is expected as None.
    assert await store.transition_app(
        experiment_id,
        expected_from=None,
        to={"tracker": {"done": True}},
        actor="meter",
    )
    # True and 1 are equal in Python and different JSON.
    with pytest.raises(ConflictError, match="app transition rejected"):
        await store.transition_app(
            experiment_id,
            expected_from={"tracker": {"done": 1}},
            to=None,
            actor="meter",
        )
    assert await _row_app(store, experiment_id) == {"tracker": {"done": True}}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_app_transition_keeps_the_size_check_on_any_kind(
    store: Store,
) -> None:
    experiment_id = await store.submit_experiment(
        SubmitExperiment(account="tester@example.com", title="run"),
    )
    with pytest.raises(ConflictError, match="check constraint"):
        await store.transition_app(
            experiment_id,
            expected_from=None,
            to={"b": "x" * 65_528},
            actor="meter",
        )
    assert await _row_app(store, experiment_id) is None
    issue_id = await store.submit_issue(
        SubmitIssue(account="tester@example.com", title="brief"),
    )
    assert await store.transition_app(
        issue_id,
        expected_from=None,
        to={"b": 1},
        actor="meter",
    )
    assert await _row_app(store, issue_id) == {"b": 1}


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
