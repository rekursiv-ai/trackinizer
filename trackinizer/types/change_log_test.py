"""Tests for ``Change`` / ``Snapshot`` row converters."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from typing import Any, cast, get_args

import pytest

from trackinizer.conftest import new_uuid
from trackinizer.types.change_log import Change, Snapshot
from trackinizer.types.columns import column_specs, storage_name
from trackinizer.types.inquiries import (
    AgentSession,
    Artifact,
    Belief,
    CodeChange,
    Experiment,
    Inquiry,
    Issue,
    Paper,
    WebResult,
    WebSearch,
)


class TestChangeKindAlignment:
    """A field edit's change kind is the field's flat storage name.

    One identifier spans the flat surfaces: the ``inquiries`` column, the
    ``change_log.kind`` value, the ``Snapshot`` field, and the
    ``old_*`` / ``new_*`` mirror columns.
    """

    INQUIRY_CLASSES = (
        Inquiry,
        Issue,
        Artifact,
        Experiment,
        Paper,
        Belief,
        CodeChange,
        WebResult,
        WebSearch,
        AgentSession,
    )

    def test_every_storage_name_is_a_change_kind(self) -> None:
        valid = set(get_args(Change.Kind.__value__))
        for cls in self.INQUIRY_CLASSES:
            for name, spec in column_specs(cls).items():
                # Immutable columns are set once at submit and never edited, so
                # they have no edit ``Change.Kind`` (nothing to audit).
                if spec.immutable:
                    continue
                assert storage_name(name, spec) in valid

    def test_every_storage_name_is_a_snapshot_field(self) -> None:
        snapshot_fields = {f.name for f in fields(Snapshot)}
        for cls in self.INQUIRY_CLASSES:
            for name, spec in column_specs(cls).items():
                # Immutable columns never change, so they carry no ``old_*`` /
                # ``new_*`` mirror and thus no ``Snapshot`` field.
                if spec.immutable:
                    continue
                flat = storage_name(name, spec)
                # marginal_cost is flattened further into per-axis mirror
                # columns; its Snapshot side is the composite Cost field.
                assert flat in snapshot_fields, flat


class TestRowConverters:
    def test_row_to_change_with_judgement_delta(self) -> None:
        ev_id, sid, prior = new_uuid(), new_uuid(), new_uuid()
        ts = datetime.now(UTC)
        row = cast(
            Any,
            {
                "id": ev_id,
                "created": ts,
                "actor": "librarian",
                "api_key_id": None,
                "subject_id": sid,
                "subject_kind": "Belief",
                "kind": "belief_judgement",
                "caused_by": prior,
                "reason": "",
                "old_belief_judgement": "unproven",
                "new_belief_judgement": "proven",
                "old_marginal_cost_agent_usd": 1.00,
                "old_marginal_cost_resource_usd": 2.50,
                "new_marginal_cost_agent_usd": 1.02,
                "new_marginal_cost_resource_usd": 4.00,
            },
        )
        e = Change.from_row(row)
        assert e.caused_by == prior
        assert e.old.belief_judgement == "unproven"
        assert e.new.belief_judgement == "proven"
        # Per-event spend = new - old of running totals.
        assert e.marginal_cost.agent_usd == pytest.approx(0.02)
        assert e.marginal_cost.resource_usd == pytest.approx(1.50)

    def test_row_to_change_accepts_negative_cost_delta(self) -> None:
        row = cast(
            Any,
            {
                "id": new_uuid(),
                "created": datetime.now(UTC),
                "actor": "system",
                "api_key_id": None,
                "subject_id": new_uuid(),
                "subject_kind": "Issue",
                "kind": "marginal_cost",
                "caused_by": None,
                "reason": "correction",
                "old_marginal_cost_agent_usd": 1.0,
                "old_marginal_cost_resource_usd": 0.0,
                "new_marginal_cost_agent_usd": 0.5,
                "new_marginal_cost_resource_usd": 0.0,
            },
        )
        assert Change.from_row(row).marginal_cost.agent_usd == pytest.approx(-0.5)

    def test_row_to_change_no_cause(self) -> None:
        row = cast(
            Any,
            {
                "id": new_uuid(),
                "created": datetime.now(UTC),
                "actor": "system",
                "api_key_id": None,
                "subject_id": new_uuid(),
                "subject_kind": "Issue",
                "kind": "created",
                "caused_by": None,
                "reason": "",
                "old_marginal_cost_agent_usd": 0.0,
                "old_marginal_cost_resource_usd": 0.0,
                "new_marginal_cost_agent_usd": 0.0,
                "new_marginal_cost_resource_usd": 0.0,
            },
        )
        assert Change.from_row(row).caused_by is None


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
