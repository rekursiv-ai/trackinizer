"""Tests for inquiry dataclasses (round-trip ``from_row``)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast, get_args

import pytest

from trackinizer.conftest import new_uuid
from trackinizer.types.change_log import Change, Snapshot
from trackinizer.types.cost import Cost
from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import (
    INQUIRY_CLASSES,
    KIND_TO_CLASS,
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
    is_valid_source,
)


class TestRowConverters:
    def test_row_to_bugreport(self) -> None:
        bid = new_uuid()
        now = datetime.now(UTC)
        row = cast(
            Any,
            {
                "id": bid,
                "seq": 7,
                "owner": "alice",
                "account": "alice",
                "status": "active",
                "title": "login crash",
                "description": "...",
                "labels": ["auth"],
                "subscribers": [],
                "codechanges": [],
                "created": now,
                "modified": now,
            },
        )
        b = Issue.from_row(row)
        assert b.id == bid
        assert b.seq == 7
        assert b.title == "login crash"
        assert b.labels == ("auth",)
        assert b.narrows == ()
        assert b.narrowed_by == ()

    def test_row_to_belief_no_citations(self) -> None:
        cid = new_uuid()
        now = datetime.now(UTC)
        row = cast(
            Any,
            {
                "id": cid,
                "seq": 3,
                "owner": "alice",
                "account": "alice",
                "status": "active",
                "title": "X",
                "description": "",
                "labels": ["thesis"],
                "subscribers": [],
                "belief_judgement": "proven",
                "created": now,
                "modified": now,
            },
        )
        c = Belief.from_row(row)
        assert c.id == cid
        assert c.judgement == "proven"
        assert c.proved_by == ()
        assert c.favored_by == ()

    def test_row_to_session(self) -> None:
        sid = new_uuid()
        now = datetime.now(UTC)
        row = cast(
            Any,
            {
                "id": sid,
                "seq": 9,
                "owner": "alice",
                "account": "alice",
                "status": "active",
                "title": "refactor auth",
                "description": "",
                "labels": [],
                "subscribers": [],
                "agentsession_cli": "codex",
                "agentsession_cli_session_id": "019e8014-321d-7bc2",
                "agentsession_started": now,
                "agentsession_ended": now,
                "created": now,
                "modified": now,
            },
        )
        s = AgentSession.from_row(row)
        assert s.id == sid
        assert s.cli == "codex"
        assert s.cli_session_id == "019e8014-321d-7bc2"
        assert s.started == now
        assert s.ended == now

    def test_row_to_experiment_with_codechanges(self) -> None:
        eid, cc_id = new_uuid(), new_uuid()
        now = datetime.now(UTC)
        row = cast(
            Any,
            {
                "id": eid,
                "seq": 4,
                "owner": "alice",
                "account": "alice",
                "status": "active",
                "title": "x10 baseline",
                "description": "...",
                "labels": ["x10"],
                "subscribers": [],
                "experiment_codechanges": [cc_id],
                "experiment_outcome": "peaks at 76.46%",
                "created": now,
                "modified": now,
            },
        )
        e = Experiment.from_row(row)
        assert e.outcome == "peaks at 76.46%"
        assert e.codechanges == (cc_id,)


class TestModels:
    def test_custom_type_dataclasses_are_default_constructable(self) -> None:
        assert Cost().total_usd == 0.0
        assert Inquiry().title == ""
        assert Artifact().title == ""
        assert Experiment().codechanges is None
        assert Paper().source is None
        assert Paper().abstract is None
        assert Paper().authors is None
        assert Paper().publication_type is None
        assert Paper().venue is None
        assert Paper().subvenue is None
        assert Paper().publish_date is None
        assert not hasattr(Paper(), "source_kind")
        assert Belief().judgement is None
        assert Belief().confidence is None
        assert Issue().issue_kind is None
        assert Issue().required_by == ()
        assert CodeChange().sha is None
        assert WebResult().url is None
        assert WebSearch().query is None
        assert AgentSession().cli is None
        assert AgentSession().cli_session_id is None
        assert AgentSession().started is None
        assert Issue().priority is None
        assert Snapshot().title is None
        assert Change().kind is None
        assert Change().marginal_cost.agent_usd == 0.0
        # Edge identity fields are required: no default-construction.
        sample = new_uuid()
        edge = Edge(
            from_id=sample,
            from_kind="Issue",
            to_id=sample,
            to_kind="Issue",
            edge_kind="narrows",
        )
        assert edge.edge_kind == "narrows"
        assert not Snapshot.from_row(cast(Any, {}), prefix="old_")

    def test_from_row_requires_persisted_identity_fields(self) -> None:
        # ``Inquiry.from_row`` raises a clear ValueError naming the missing
        # base column, matching its kind-loop contract -- not a bare KeyError.
        with pytest.raises(ValueError, match="id"):
            Inquiry.from_row(cast(Any, {}))
        with pytest.raises(KeyError, match="'subject_id'"):
            Change.from_row(
                cast(
                    Any, {"id": new_uuid(), "created": datetime.now(UTC), "actor": "x"}
                )
            )
        with pytest.raises(KeyError, match="'from_id'"):
            Edge.from_row(cast(Any, {}))

    def test_from_row_partial_projection_raises_clear_value_error(self) -> None:
        # A partial-projection row (base half incomplete) must surface a
        # clear ValueError naming the missing column, not a bare KeyError
        # mid-construction. Mirrors the kind-loop's ``col not in row`` gate.
        with pytest.raises(ValueError, match="owner"):
            Inquiry.from_row(cast(Any, {"kind": "Issue", "id": new_uuid(), "seq": 0}))


class TestIsValidSource:
    """``is_valid_source`` requires a non-whitespace remainder after the scheme."""

    def test_whitespace_only_remainder_rejected(self) -> None:
        # ``.+`` matched a lone space; the docstring promises a non-empty
        # remainder, so "doi: " (only whitespace after the colon) is invalid.
        assert not is_valid_source("doi: ")

    def test_real_remainder_accepted(self) -> None:
        assert is_valid_source("doi:10.1/x")
        assert is_valid_source("arXiv:2405.16391")


class TestKindToClass:
    """``KIND_TO_CLASS`` is the canonical row-discriminator -> class registry."""

    def test_covers_every_declared_kind(self) -> None:
        declared = set(get_args(Inquiry.InquiryKind.__value__))
        assert set(KIND_TO_CLASS) == declared

    def test_enumerates_every_concrete_subclass(self) -> None:
        # Drift guard: the registry must equal the live ``Inquiry`` subclass
        # set (``Inquiry`` itself excluded -- it is the abstract base, never a
        # stored kind). A new subclass without a registry edit is caught here.
        subclasses: set[type[Inquiry]] = set()
        stack: list[type[Inquiry]] = list(Inquiry.__subclasses__())
        while stack:
            cls = stack.pop()
            subclasses.add(cls)
            stack.extend(cls.__subclasses__())
        # Resolve through the module namespace so a slots-rebuild ghost (a
        # stale duplicate in ``__subclasses__``) does not spuriously fail the
        # set comparison -- the registry pins to the canonical export.
        assert set(KIND_TO_CLASS.values()) == {
            KIND_TO_CLASS[cast(Inquiry.InquiryKind, c.__name__)] for c in subclasses
        }

    def test_each_kind_maps_to_its_named_class(self) -> None:
        assert KIND_TO_CLASS["Issue"] is Issue
        assert KIND_TO_CLASS["Artifact"] is Artifact
        assert KIND_TO_CLASS["Experiment"] is Experiment
        assert KIND_TO_CLASS["Paper"] is Paper
        assert KIND_TO_CLASS["Belief"] is Belief
        assert KIND_TO_CLASS["CodeChange"] is CodeChange
        assert KIND_TO_CLASS["WebResult"] is WebResult
        assert KIND_TO_CLASS["WebSearch"] is WebSearch
        assert KIND_TO_CLASS["AgentSession"] is AgentSession

    def test_wire_consumers_derive_from_registry(self) -> None:
        # The hierarchy walk must span ``Inquiry`` plus exactly the canonical
        # registry's concrete classes -- the E2 dedup contract (no parallel
        # hand-maintained kind list). One definition now, so this asserts the
        # derivation rather than the agreement of copies.
        assert (Inquiry, *KIND_TO_CLASS.values()) == INQUIRY_CLASSES


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
