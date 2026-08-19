"""Field-edit replay semantics on the in-process PGlite substrate."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

import asyncio
import uuid

import pytest
import pytest_asyncio

from trackinizer.lib.postgres import Conn, PGliteEngine
from trackinizer.server.store.change_id_slot import set_client_change_id
from trackinizer.server.store.core import Store, StubEmbedder
from trackinizer.types.change_log import Change
from trackinizer.types.cost import Cost
from trackinizer.types.errors import ConflictError
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.bodies import (
    SubmitBelief,
    SubmitCodeChange,
    SubmitExperiment,
    SubmitIssue,
)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    """A bootstrapped Store over an ephemeral in-process PGlite engine."""
    async with PGliteEngine(
        workdir=tmp_path / "pglite", persist=False, extensions=("pgvector",)
    ) as engine:
        store = Store(engine, embed=StubEmbedder())
        await store.bootstrap()
        yield store


class _ReplayFieldChange(Protocol):
    """Typed shape of Store's replay probe for the test seam."""

    async def __call__(
        self,
        conn: Conn,
        target_id: uuid.UUID,
        change_kind: Change.Kind,
        *,
        actor: Inquiry.Actor,
    ) -> uuid.UUID | None: ...


class _MissFirstReplayProbe:
    """Model a row committed after the transaction's first visibility probe."""

    def __init__(self, replay: _ReplayFieldChange) -> None:
        self._replay = replay
        self.calls = 0

    async def __call__(
        self,
        conn: Conn,
        target_id: uuid.UUID,
        change_kind: Change.Kind,
        *,
        actor: Inquiry.Actor,
    ) -> uuid.UUID | None:
        self.calls += 1
        if self.calls == 1:
            return None
        return await self._replay(conn, target_id, change_kind, actor=actor)


async def _submit_task(store: Store) -> uuid.UUID:
    """Create one open task Issue for field-transition tests."""
    return await store.submit_issue(
        SubmitIssue(account="tester@example.com", title="Task: spec arm 42")
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_title_drifted_retry_preserves_title_and_embedding(store: Store) -> None:
    """A replay returns before a drifted title or embedding is written."""
    task = await _submit_task(store)
    key = uuid.uuid4()
    set_client_change_id(key)
    try:
        assert (
            await store.set_title(task, "Task: first title", actor="scientist1") == key
        )
    finally:
        set_client_change_id(None)
    async with store.engine.acquire() as conn:
        first_vec = await conn.fetchval(
            "SELECT embedding::text FROM inquiry_embeddings "
            "WHERE inquiry_id = $1 AND model = 'stub'",
            task,
        )

    set_client_change_id(key)
    try:
        assert await store.set_title(task, "Task: drifted", actor="scientist1") == key
    finally:
        set_client_change_id(None)

    row = await store.get_inquiry(task)
    assert row is not None
    assert row.title == "Task: first title"
    async with store.engine.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT embedding::text FROM inquiry_embeddings "
                "WHERE inquiry_id = $1 AND model = 'stub'",
                task,
            )
            == first_vec
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_transition_status_retry_replays_before_cas(store: Store) -> None:
    """An identical status-transition retry returns its original change."""
    task = await _submit_task(store)
    key = uuid.uuid4()
    set_client_change_id(key)
    try:
        first = await store.transition_status(
            task,
            expected_from="active",
            to="complete",
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert first == key

    set_client_change_id(key)
    try:
        replay = await store.transition_status(
            task,
            expected_from="active",
            to="complete",
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert replay == key


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_transition_owner_allows_one_concurrent_acquirer(store: Store) -> None:
    """Exactly one worker can transition an unowned task to itself."""
    task = await _submit_task(store)

    async def acquire(owner: str) -> str | None:
        try:
            await store.transition_owner(
                task,
                expected_from=None,
                to=owner,
                actor=owner,
            )
        except ConflictError:
            return None
        return owner

    winners = await asyncio.gather(acquire("worker-1"), acquire("worker-2"))

    assert len([winner for winner in winners if winner is not None]) == 1
    task_row = await store.get_inquiry(task)
    assert task_row is not None
    assert task_row.owner in winners


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_transition_owner_retry_replays_before_cas(store: Store) -> None:
    """An identical owner-transition retry returns its original change."""
    task = await _submit_task(store)
    key = uuid.uuid4()
    set_client_change_id(key)
    try:
        first = await store.transition_owner(
            task,
            expected_from=None,
            to="worker-1",
            actor="worker-1",
        )
    finally:
        set_client_change_id(None)
    assert first == key

    set_client_change_id(key)
    try:
        replay = await store.transition_owner(
            task,
            expected_from=None,
            to="worker-1",
            actor="worker-1",
        )
    finally:
        set_client_change_id(None)
    assert replay == key


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_transition_judgement_retry_replays_before_cas(store: Store) -> None:
    """An identical judgement-transition retry returns its original change."""
    belief = await store.submit_belief(
        SubmitBelief(account="tester@example.com", title="Claim")
    )
    key = uuid.uuid4()
    set_client_change_id(key)
    try:
        first = await store.transition_judgement(
            belief,
            expected_from=None,
            to="proven",
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert first == key

    set_client_change_id(key)
    try:
        replay = await store.transition_judgement(
            belief,
            expected_from=None,
            to="proven",
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert replay == key


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_status_retry_reprobes_after_initial_visibility_miss(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry replays when its first probe predates the winner's commit."""
    task = await _submit_task(store)
    key = uuid.uuid4()
    set_client_change_id(key)
    try:
        first = await store.transition_status(
            task,
            expected_from="active",
            to="complete",
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert first == key

    # Inject a visibility miss at the private replay seam.
    probe = _MissFirstReplayProbe(store._replay_field_change)
    monkeypatch.setattr(store, "_replay_field_change", probe)
    set_client_change_id(key)
    try:
        replay = await store.transition_status(
            task,
            expected_from="active",
            to="complete",
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert replay == key
    assert probe.calls == 2


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_drifted_retry_reprobes_before_reference_validation(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry after an initial miss discards invalid reference drift."""
    codechange = await store.submit_codechange(
        SubmitCodeChange(account="tester@example.com", title="Change")
    )
    experiment = await store.submit_experiment(
        SubmitExperiment(account="tester@example.com", title="Experiment")
    )
    key = uuid.uuid4()
    set_client_change_id(key)
    try:
        first = await store.set_codechanges(
            experiment,
            [codechange],
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert first == key

    # Inject a visibility miss at the private replay seam.
    probe = _MissFirstReplayProbe(store._replay_field_change)
    monkeypatch.setattr(store, "_replay_field_change", probe)
    set_client_change_id(key)
    try:
        replay = await store.set_codechanges(
            experiment,
            [uuid.uuid4()],
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert replay == key
    assert probe.calls == 2


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_add_cost_retry_replays_after_subject_is_purged(store: Store) -> None:
    """A committed cost retry replays before checking target existence."""
    task = await _submit_task(store)
    key = uuid.uuid4()
    set_client_change_id(key)
    try:
        first = await store.add_cost(
            task,
            Cost(agent_usd=1.0),
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert first == key
    await store.purge(task, actor="scientist1")

    set_client_change_id(key)
    try:
        replay = await store.add_cost(
            task,
            Cost(agent_usd=1.0),
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert replay == key


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_add_cost_zero_retry_reprobes_after_initial_visibility_miss(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drifted zero retry cannot bypass a winner missed by its first probe."""
    task = await _submit_task(store)
    key = uuid.uuid4()
    set_client_change_id(key)
    try:
        first = await store.add_cost(
            task,
            Cost(agent_usd=1.0),
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert first == key

    probe = _MissFirstReplayProbe(store._replay_field_change)
    monkeypatch.setattr(store, "_replay_field_change", probe)
    set_client_change_id(key)
    try:
        replay = await store.add_cost(
            task,
            Cost(),
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert replay == key
    assert probe.calls == 2


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_set_cost_axis_identical_retry_returns_original_change(
    store: Store,
) -> None:
    """An identical cost-axis retry returns its original change id."""
    task = await _submit_task(store)
    key = uuid.uuid4()
    set_client_change_id(key)
    try:
        first = await store.set_cost_axis(
            task,
            "agent_usd",
            2.0,
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert first == key

    set_client_change_id(key)
    try:
        replay = await store.set_cost_axis(
            task,
            "agent_usd",
            2.0,
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert replay == key


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_set_cost_axis_negative_retry_reprobes_after_visibility_miss(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative drifted body cannot beat a winner missed by probe one."""
    task = await _submit_task(store)
    key = uuid.uuid4()
    set_client_change_id(key)
    try:
        first = await store.set_cost_axis(
            task,
            "agent_usd",
            2.0,
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert first == key

    probe = _MissFirstReplayProbe(store._replay_field_change)
    monkeypatch.setattr(store, "_replay_field_change", probe)
    set_client_change_id(key)
    try:
        replay = await store.set_cost_axis(
            task,
            "agent_usd",
            -1.0,
            actor="scientist1",
        )
    finally:
        set_client_change_id(None)
    assert replay == key
    assert probe.calls == 2


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_list_field_drifted_retry_replays_before_reference_validation(
    store: Store,
) -> None:
    """A replayed list-field edit wins before validating drifted references."""
    codechange = await store.submit_codechange(
        SubmitCodeChange(account="tester@example.com", title="Change")
    )
    experiment = await store.submit_experiment(
        SubmitExperiment(account="tester@example.com", title="Experiment")
    )
    key = uuid.uuid4()
    set_client_change_id(key)
    try:
        first = await store.set_codechanges(
            experiment, [codechange], actor="scientist1"
        )
    finally:
        set_client_change_id(None)
    assert first == key

    set_client_change_id(key)
    try:
        replay = await store.set_codechanges(
            experiment, [uuid.uuid4()], actor="scientist1"
        )
    finally:
        set_client_change_id(None)
    assert replay == key


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_list_mutation_drifted_retry_replays_before_reference_validation(
    store: Store,
) -> None:
    """A replayed list mutation wins before validating its drifted item."""
    codechange = await store.submit_codechange(
        SubmitCodeChange(account="tester@example.com", title="Change")
    )
    experiment = await store.submit_experiment(
        SubmitExperiment(account="tester@example.com", title="Experiment")
    )
    key = uuid.uuid4()
    set_client_change_id(key)
    try:
        first = await store.add_codechange(experiment, codechange, actor="scientist1")
    finally:
        set_client_change_id(None)
    assert first == key

    set_client_change_id(key)
    try:
        replay = await store.add_codechange(
            experiment, uuid.uuid4(), actor="scientist1"
        )
    finally:
        set_client_change_id(None)
    assert replay == key


if __name__ == "__main__":
    from trackinizer.lib.testing import test_main

    test_main(__file__)
