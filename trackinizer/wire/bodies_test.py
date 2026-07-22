"""Tests for Pydantic submit / field-mutation bodies.

Also houses edge-create priority validation: :class:`CreateEdgeItem`
shares the nonnegative-priority constraint with the inquiry bodies, so
the single negative-priority case is colocated here rather than split
across modules.
"""

from __future__ import annotations

import math

from pydantic import ValidationError

import pytest

from trackinizer.conftest import new_uuid
from trackinizer.wire.bodies import (
    BATCH_MAX_ITEMS,
    BatchEdge,
    FieldMutation,
    FieldOp,
    FieldSet,
    SubmitAgentSession,
    SubmitArtifact,
    SubmitBatch,
    SubmitBelief,
    SubmitCodeChange,
    SubmitExperiment,
    SubmitIssue,
    SubmitPaper,
    SubmitWebResult,
    SubmitWebSearch,
)
from trackinizer.wire.edge_bodies import CreateEdge, CreateEdgeItem


class TestSubmitModels:
    def test_pydantic_submit_models_have_defaults(self) -> None:
        assert SubmitArtifact(title="x").labels is None
        assert SubmitIssue(title="x").priority is None
        assert SubmitIssue(title="x").issue_kind is None
        assert SubmitIssue(title="x").requires == []
        assert SubmitBelief(title="s").proved_by == []
        assert SubmitBelief(title="s").judgement is None
        assert SubmitBelief(title="s").confidence is None
        assert SubmitExperiment(title="x", codechanges=[]).outcome is None
        assert SubmitPaper(title="x").source is None
        assert SubmitPaper(title="x").publication_type is None
        assert SubmitPaper(title="x").venue is None
        assert SubmitPaper(title="x").authors is None
        assert SubmitCodeChange(title="c", sha="abc").labels is None
        assert SubmitWebResult(title="r", url="https://x").labels is None
        assert SubmitWebSearch(title="s", query="x").provider is None

    def test_blank_actor_rejected_on_submit(self) -> None:
        """A blank (whitespace-only) ``actor`` is malformed input (F18).

        ``submit_batch`` does ``item.actor or actor``, so ``actor='  '``
        silently falls through to the batch actor -- inconsistent with
        ``account``'s blank-rejection. Reject it at the wire (422). ``None``
        (server defaults from the principal) stays valid.
        """
        for bad in ("   ", "\t", "\n", ""):
            with pytest.raises(ValueError, match="non-empty"):
                SubmitIssue(title="x", actor=bad)
        assert SubmitIssue(title="x", actor=None).actor is None

    def test_blank_subscribers_rejected_on_submit(self) -> None:
        """``SubmitBase`` rejects empty / whitespace-only subscriber ids."""
        with pytest.raises(ValueError, match="non-empty"):
            SubmitIssue(title="x", subscribers=["   "])
        with pytest.raises(ValueError, match="non-empty"):
            SubmitIssue(title="x", subscribers=[""])

    def test_blank_labels_canonicalized_not_rejected(self) -> None:
        """Labels keep their canonicalize-at-write contract (drop blanks).

        Unlike ``subscribers`` (which reject blanks at the wire), the
        Store strips/drops/dedups labels at write time, so the wire body
        accepts blank entries rather than 422-ing. Reviewer finding F8
        ("labels should also reject") was rejected: the two fields run
        deliberately different policies and an integration test pins the
        label canonicalization.
        """
        assert SubmitIssue(title="x", labels=["", "  "]).labels == ["", "  "]

    def test_whitespace_title_rejected_on_submit(self) -> None:
        """``title`` must carry non-whitespace content (F25)."""
        with pytest.raises(ValueError, match="non-empty"):
            SubmitIssue(title="   ")

    def test_whitespace_cli_rejected_on_submit(self) -> None:
        """``SubmitAgentSession.cli`` rejects whitespace-only (mirrors rooms)."""
        for bad in ("   ", "\t", "\n"):
            with pytest.raises(ValueError, match="non-empty"):
                SubmitAgentSession(title="s", cli=bad)

    def test_whitespace_cli_session_id_rejected_on_submit(self) -> None:
        """``SubmitAgentSession.cli_session_id`` rejects whitespace-only."""
        for bad in ("   ", "\t", "\n"):
            with pytest.raises(ValueError, match="non-empty"):
                SubmitAgentSession(title="s", cli_session_id=bad)
        assert SubmitAgentSession(title="s", cli_session_id=None).cli_session_id is None

    def test_comma_in_room_rejected_on_submit(self) -> None:
        """``SubmitAgentSession.rooms`` rejects a comma in a room name.

        A room name carrying ',' breaks the ``TRAX_ROOMS`` comma-joined
        serialization (session.py): ``['a,b']`` and ``['a', 'b']`` collapse to
        the same env string. Reject the comma at the wire boundary, mirroring
        the ``SessionStart.rooms`` rule so both create paths agree.
        """
        for bad in (["a,b"], ["ok", "x,y"], [","]):
            with pytest.raises(ValueError, match="comma"):
                SubmitAgentSession(title="s", rooms=bad)
        assert SubmitAgentSession(title="s", rooms=["lab", "sear"]).rooms == [
            "lab",
            "sear",
        ]
        assert SubmitAgentSession(title="s", rooms=None).rooms is None

    def test_priority_pydantic_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="greater than or equal"):
            SubmitIssue(title="x", priority=-1)
        with pytest.raises(ValueError, match="greater than or equal"):
            CreateEdgeItem(
                from_id=new_uuid(),
                to_id=new_uuid(),
                edge_kind="narrows",
                priority=-3,
            )

    def test_narrows_rejects_negative_priority(self) -> None:
        """A negative contextual priority on a ``narrows`` parent is malformed.

        ``narrows=[(uuid, -5)]`` is accepted at the wire but escapes to the
        DB ``CHECK priority >= 0`` mid-transaction -> a 500 instead of a clean
        422 (F50). The inner priority carries the same nonnegative bound as
        ``SubmitIssue.priority`` and ``BatchEdge.priority``.
        """
        with pytest.raises(ValueError, match="greater than or equal"):
            SubmitIssue(title="x", narrows=[(new_uuid(), -5)])

    def test_narrows_accepts_nonnegative_and_none_priority(self) -> None:
        parent = new_uuid()
        other = new_uuid()
        issue = SubmitIssue(title="x", narrows=[(parent, 0), (other, None)])
        assert issue.narrows == [(parent, 0), (other, None)]

    def test_requires_dedupes_duplicate_ids(self) -> None:
        """Duplicate ``requires`` ids collapse so the insert path stays clean.

        A repeated prerequisite id would drive an insert-then-upsert phantom
        audit; dedup at the wire keeps one edge per parent (F53). First
        occurrence order is preserved.
        """
        a = new_uuid()
        b = new_uuid()
        assert SubmitIssue(title="x", requires=[a, b, a]).requires == [a, b]

    def test_proved_by_dedupes_duplicate_citations(self) -> None:
        """Duplicate ``proved_by`` citations collapse (same artifact id) (F53)."""
        art = new_uuid()
        cite = {"artifact_id": art, "artifact_kind": "CodeChange"}
        belief = SubmitBelief.model_validate({"title": "b", "proved_by": [cite, cite]})
        assert len(belief.proved_by) == 1

    def test_favored_by_dedupes_duplicate_citations(self) -> None:
        art = new_uuid()
        cite = {"artifact_id": art, "artifact_kind": "CodeChange"}
        belief = SubmitBelief.model_validate({"title": "b", "favored_by": [cite, cite]})
        assert len(belief.favored_by) == 1

    def test_nonfinite_marginal_cost_rejected(self) -> None:
        """A NaN / inf ``marginal_cost`` axis is a 422 at the wire (K2).

        The non-finite value otherwise reaches storage and defeats the
        floor-guard, poisoning the running total; the ``Cost`` type guard
        rejects it before it can be submitted.
        """
        for bad in (math.nan, math.inf, -math.inf):
            with pytest.raises(ValidationError, match="finite"):
                SubmitIssue.model_validate(
                    {"title": "x", "marginal_cost": {"agent_usd": bad}}
                )
            with pytest.raises(ValidationError, match="finite"):
                SubmitIssue.model_validate(
                    {"title": "x", "marginal_cost": {"resource_usd": bad}}
                )

    def test_blank_optional_scalars_clear_to_none(self) -> None:
        """Whitespace-only optional scalars clear to ``None`` at submit (F51).

        ``submit_X`` passed these raw to storage while the edit path
        (``_set_field`` -> ``empty_optional_to_none``) and the pinned
        ``paper_venue`` insert collapse a blank to NULL. Clearing here makes
        one "blank is unset (NULL)" rule hold across submit and edit, mirroring
        ``_validate_source``. (``cli`` / ``cli_session_id`` keep their distinct
        reject policy as correlation identifiers.)
        """
        for bad in ("   ", "\t", "\n", ""):
            assert SubmitCodeChange(title="c", sha=bad).sha is None
            assert SubmitWebResult(title="r", url=bad).url is None
            assert SubmitWebSearch(title="s", query=bad).query is None
            assert SubmitWebSearch(title="s", provider=bad).provider is None
            assert SubmitPaper(title="p", venue=bad).venue is None
            assert SubmitPaper(title="p", subvenue=bad).subvenue is None
            assert SubmitPaper(title="p", abstract=bad).abstract is None

    def test_nonblank_optional_scalars_preserved(self) -> None:
        assert SubmitCodeChange(title="c", sha="abc").sha == "abc"
        assert SubmitWebSearch(title="s", provider="google").provider == "google"
        assert SubmitPaper(title="p", venue="NeurIPS").venue == "NeurIPS"

    def test_paper_source_requires_scheme_prefix(self) -> None:
        """``Paper.source`` is a scheme-tagged identifier: ``<scheme>:<rest>``.

        The field documents a self-describing id whose scheme prefix names its
        kind (``arXiv:``, ``doi:``, ``http(s)://``, ``isbn:``, ...). The wire
        enforces the SHAPE -- a non-empty scheme and a non-empty remainder --
        not a closed scheme list, so any well-formed identifier passes but a
        bare value (``2405.16391``) that drops the scheme is rejected.
        """
        # Well-formed, scheme-tagged identifiers pass (open set of schemes).
        for ok in (
            "arXiv:2405.16391",
            "doi:10.1145/3292500",
            "https://example.com/p",
            "http://example.com/p",
            "isbn:978-3-16-148410-0",
            "ArXiv:2405.16391",  # scheme match is case-insensitive
        ):
            assert SubmitPaper(title="x", source=ok).source == ok

        # Bare (un-prefixed) or malformed values are rejected.
        for bad in ("2405.16391", ":nohead", "noscheme:", "doi:"):
            with pytest.raises(ValueError, match="scheme"):
                SubmitPaper(title="x", source=bad)

        # A whitespace-only / empty source is CLEAR (-> None), not a malformed
        # value -- the same contract as ``Store.set_source`` on the edit path, so
        # the two boundaries agree. An empty source is "no source", not invalid.
        assert SubmitPaper(title="x", source="  ").source is None
        assert SubmitPaper(title="x", source="").source is None

        # An unset source stays valid (the field is optional).
        assert SubmitPaper(title="x").source is None


class TestEdgeBodies:
    def test_create_edge_labels_default_none(self) -> None:
        """Bare edge create leaves labels unset (NULL), not ``[]`` (F11).

        ``Edge.labels`` defaults to ``None``; the wire body must agree so
        a created edge's labels column matches a Store-created edge's.
        """
        assert CreateEdge().labels is None


class TestSubmitBatch:
    def test_requires_idempotency_key_per_item(self) -> None:
        """Batch items must carry ``idempotency_key`` for retry-safe replay."""
        with pytest.raises(ValueError, match="require idempotency_key"):
            SubmitBatch(items=[SubmitIssue(title="x")])

    def test_accepts_items_with_idempotency_keys(self) -> None:
        batch = SubmitBatch(items=[SubmitIssue(title="x", idempotency_key=new_uuid())])
        assert len(batch.items) == 1

    def test_rejects_empty_items(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            SubmitBatch(items=[])

    def test_accepts_edges_by_index(self) -> None:
        batch = SubmitBatch(
            items=[
                SubmitIssue(title="root", idempotency_key=new_uuid()),
                SubmitIssue(title="blocker", idempotency_key=new_uuid()),
            ],
            edges=[BatchEdge(from_index=0, to_index=1, edge_kind="requires")],
        )
        assert batch.edges[0].to_index == 1

    def test_rejects_edge_index_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            SubmitBatch(
                items=[SubmitIssue(title="root", idempotency_key=new_uuid())],
                edges=[BatchEdge(from_index=0, to_index=5, edge_kind="requires")],
            )

    def test_rejects_edge_index_kind_mismatch(self) -> None:
        """A new-row edge whose endpoint kinds violate the policy is rejected.

        ``proves`` requires ``from`` in {Artifact kinds} and ``to`` in
        {Belief, Experiment}. An edge linking two new Issue rows by index
        carries kinds the DB ``CHECK`` would reject mid-transaction -> a 500;
        catch it at the wire as a clean 422 (F64).
        """
        with pytest.raises(ValueError, match="edge_kind"):
            SubmitBatch(
                items=[
                    SubmitIssue(title="a", idempotency_key=new_uuid()),
                    SubmitIssue(title="b", idempotency_key=new_uuid()),
                ],
                edges=[BatchEdge(from_index=0, to_index=1, edge_kind="proves")],
            )

    def test_accepts_edge_index_kind_match(self) -> None:
        """A ``proves`` from a new Artifact to a new Belief passes the check."""
        batch = SubmitBatch(
            items=[
                SubmitArtifact(title="evidence", idempotency_key=new_uuid()),
                SubmitBelief(title="claim", idempotency_key=new_uuid()),
            ],
            edges=[BatchEdge(from_index=0, to_index=1, edge_kind="proves")],
        )
        assert batch.edges[0].edge_kind == "proves"

    def test_id_endpoints_skip_kind_check(self) -> None:
        """An existing-row endpoint (id, not index) has unknown kind at wire.

        The new-row kind check applies only to index endpoints; an ``id``
        endpoint references an already-stored row whose kind the wire cannot
        see, so the check leaves it for the Store's reference validation. Here
        the from-side is an existing-row id (kind unknown, skipped) and the
        to-side index is a valid Belief, so a ``proves`` edge with an id
        from-side that would be an incompatible kind is NOT rejected at the
        wire.
        """
        batch = SubmitBatch(
            items=[SubmitBelief(title="claim", idempotency_key=new_uuid())],
            edges=[BatchEdge(from_id=new_uuid(), to_index=0, edge_kind="proves")],
        )
        assert batch.edges[0].from_id is not None

    def test_rejects_too_many_edges(self) -> None:
        """``edges`` is bounded like ``items`` so a batch cannot be unbounded (F65)."""
        item = SubmitIssue(title="root", idempotency_key=new_uuid())
        edges = [
            BatchEdge(from_index=0, to_index=0, edge_kind="requires")
            for _ in range(BATCH_MAX_ITEMS + 1)
        ]
        with pytest.raises(ValueError, match="at most"):
            SubmitBatch(items=[item], edges=edges)

    def test_rejects_duplicate_idempotency_key(self) -> None:
        """Two items sharing one key would collapse to a single row (F23).

        ``submit_batch`` processes items on one tx; the second item with the
        same key pre-probes, finds the first's just-written change_log row,
        and returns the first's id -- so two items become one row and edges
        by index mis-target. Reject the duplicate at the wire (clean 422).
        """
        key = new_uuid()
        with pytest.raises(ValueError, match="duplicate idempotency_key"):
            SubmitBatch(
                items=[
                    SubmitIssue(title="a", idempotency_key=key),
                    SubmitIssue(title="b", idempotency_key=key),
                ]
            )

    def test_reports_duplicate_idempotency_key_indexes(self) -> None:
        """The error names the colliding index so the client can fix it.

        Each later item that reuses a key already seen is reported (the
        first occurrence is the keeper); index 3 collides with index 1's key.
        """
        key = new_uuid()
        other = new_uuid()
        with pytest.raises(ValueError, match=r"indexes \[3\]"):
            SubmitBatch(
                items=[
                    SubmitIssue(title="a", idempotency_key=other),
                    SubmitIssue(title="b", idempotency_key=key),
                    SubmitIssue(title="c", idempotency_key=new_uuid()),
                    SubmitIssue(title="d", idempotency_key=key),
                ]
            )

    def test_accepts_distinct_idempotency_keys(self) -> None:
        batch = SubmitBatch(
            items=[
                SubmitIssue(title="a", idempotency_key=new_uuid()),
                SubmitIssue(title="b", idempotency_key=new_uuid()),
            ]
        )
        assert len(batch.items) == 2


class TestBatchEdge:
    def test_requires_exactly_one_per_endpoint(self) -> None:
        with pytest.raises(ValueError, match="exactly one of from"):
            BatchEdge(
                from_index=0,
                from_id=new_uuid(),
                to_index=1,
                edge_kind="requires",
            )
        with pytest.raises(ValueError, match="exactly one of to"):
            BatchEdge(from_index=0, edge_kind="requires")

    def test_accepts_id_endpoint(self) -> None:
        edge = BatchEdge(from_index=0, to_id=new_uuid(), edge_kind="requires")
        assert edge.from_index == 0
        assert edge.to_id is not None


class TestFieldBodies:
    def test_field_set_defaults_to_set_mode(self) -> None:
        """A blind overwrite is ``mode='set'`` and carries no ``expected``."""
        body = FieldSet(value="complete", actor="alice")
        assert body.value == "complete"
        assert body.mode == "set"
        assert "expected" not in body.model_fields_set

    def test_field_set_cas_mode_records_expected(self) -> None:
        body = FieldSet(value="complete", mode="cas", expected="active", actor="alice")
        assert body.mode == "cas"
        assert body.expected == "active"

    def test_field_set_cas_mode_accepts_explicit_none_expected(self) -> None:
        """``mode='cas'`` with ``expected=None`` is a real guard (clear-to-None)."""
        body = FieldSet(value="x", mode="cas", expected=None, actor="alice")
        assert body.mode == "cas"
        assert "expected" in body.model_fields_set

    def test_field_set_cas_mode_requires_expected(self) -> None:
        with pytest.raises(ValidationError, match="requires 'expected'"):
            FieldSet(value="x", mode="cas", actor="alice")

    def test_field_set_set_mode_forbids_expected(self) -> None:
        with pytest.raises(ValidationError, match="mode='cas'"):
            FieldSet(value="x", expected="active", actor="alice")

    def test_field_set_forbids_unknown_field(self) -> None:
        # A typo'd guard (``expected``) is a hard 422, not a silent blind
        # write (REV-OPUS-04).
        body = {
            "value": "x",
            "expcted": "active",  # codespell:ignore expcted -- deliberate typo of 'expected'
            "actor": "alice",
        }
        with pytest.raises(ValidationError):
            FieldSet.model_validate(body)

    def test_field_op_validates_op_literal(self) -> None:
        assert FieldOp(op="add", value="urgent").op == "add"
        assert FieldOp(op="sub", value="urgent").op == "sub"
        # ``model_validate`` on an untyped dict exercises pydantic's
        # runtime literal rejection without a statically invalid call.
        with pytest.raises(ValueError, match="should be 'add' or 'sub'"):
            FieldOp.model_validate({"op": "replace", "value": "urgent"})

    def test_field_op_requires_value(self) -> None:
        with pytest.raises(ValueError, match="value"):
            FieldOp.model_validate({"op": "add"})

    def test_field_mutation_defaults(self) -> None:
        """The DELETE/unset body carries only ``actor`` + ``reason``."""
        body = FieldMutation()
        assert body.actor is None
        assert body.reason == ""
        body = FieldMutation(actor="alice", reason="cleanup")
        assert body.actor == "alice"
        assert body.reason == "cleanup"


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
