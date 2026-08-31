from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast, get_args, override

import io
import uuid

import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.lib.custom_json import IntCodec
from trackinizer.trax import verbs
from trackinizer.trax.conftest import FakeClient, run
from trackinizer.trax.grammar import (
    VALID_KINDS,
    WRITE_FIELDS_CLI,
    ListQuery,
)
from trackinizer.trax.verbs import (
    LABELS_BY_EDGE_KIND,
    Blocked,
    Board,
    Graph,
    Kind,
    _query_rows,
    resolve_actor,
)
from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.filters import Filter
from trackinizer.wire.refs import Ref, SeqRef, UuidRef
from trackinizer.wire.routes import MAX_LIST_LIMIT
from trackinizer.wire.wire_metrics import MetricPoint
from trackinizer.wire.wire_metrics_query import (
    MetricMaskClause,
    MetricRankRow,
)


def _batch_items(client: FakeClient) -> list[tuple[str, dict[str, object]]]:
    """The (kind, body) items from the single ``submit_batch`` call."""
    calls = [c for c in client.calls if c[0] == "submit_batch"]
    assert len(calls) == 1, f"expected one submit_batch, got {len(calls)}"
    items = cast(list[tuple[str, dict[str, object]]], calls[0][1][0])
    return [(str(kind), dict(body)) for kind, body in items]


def _batch_edges(client: FakeClient) -> list[dict[str, object]]:
    """The edge payloads from the single ``submit_batch`` call."""
    calls = [c for c in client.calls if c[0] == "submit_batch"]
    assert len(calls) == 1, f"expected one submit_batch, got {len(calls)}"
    edges = cast(list[dict[str, object]], calls[0][2]["edges"])
    return [dict(e) for e in edges]


def test_resolve_actor_uses_windows_username_env(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.author = ""
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setenv("USERNAME", "alice")
    assert resolve_actor("", cast(Client, client)) == "alice"


def test_kind_verb_no_ref_lists(client: FakeClient) -> None:
    run(["issue"], client)
    assert any(call[0] == "list_kind" for call in client.calls)


def test_id_verb_shows_row_kind_agnostically(client: FakeClient) -> None:
    """``trax id <uuid>`` shows the row by global id, with no leading kind.

    ``id`` is already a filterable column; since a UUID is globally unique the
    leading kind is redundant, so a bare ``id <uuid>`` resolves the row whatever
    its kind. It dispatches to a show, fetching by a kind-agnostic UuidRef
    (``expected_kind=None``) -- no typo-guard, because the caller asserted no
    kind to guard against.
    """
    target = str(uuid.uuid4())
    run(["id", target], client)
    show_calls = [c for c in client.calls if c[0] == "get_inquiry"]
    assert show_calls, "trax id <uuid> must fetch the row"
    ref = cast(UuidRef, show_calls[0][1][0])
    assert str(ref.uuid) == target
    assert ref.expected_kind is None  # kind-agnostic, no guard


def test_id_verb_rejects_non_uuid(client: FakeClient) -> None:
    """``trax id <not-a-uuid>`` is a clear error, not a silent miss."""
    with pytest.raises(ClientError, match="not a valid uuid"):
        run(["id", "not-a-uuid"], client)


def test_kind_union_lists_multiple_subjects(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["issue", "experiment"], client)
    out = capsys.readouterr().out
    assert "Issue#1" in out
    assert "Experiment#2" in out
    assert "Belief#3" not in out


def test_kind_range_filters_seq(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["issue", "4..5"], client)
    out = capsys.readouterr().out
    assert "Issue#4" in out
    assert "Issue#5" in out
    assert "Issue#1" not in out
    assert "Issue#6" not in out


def test_kind_csv_range_unions_disjoint_intervals(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """A comma-separated selector fans out and unions disjoint intervals."""
    run(["issue", "1,5..6"], client)
    out = capsys.readouterr().out
    assert "Issue#1" in out
    assert "Issue#5" in out
    assert "Issue#6" in out
    assert "Issue#4" not in out


def test_kind_csv_range_dedups_overlapping_intervals(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Overlapping intervals never list a row twice."""
    run(["issue", "4..5,5..6"], client)
    out = capsys.readouterr().out
    assert out.count("Issue#5") == 1
    assert "Issue#4" in out
    assert "Issue#6" in out


def test_kind_filters_are_flat_conjunctions(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["belief", "title", "re", "foo.*bar", "confidence", "ge", "0.9"], client)
    out = capsys.readouterr().out
    assert "Belief#3" in out


def test_kind_filter_unknown_field_errors_for_single_kind(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="unknown filter field"):
        run(["issue", "judgement", "is", "proven"], client)


def test_priority_filter_excludes_null_priority_rows(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``priority gt 5`` must not match rows with no priority set.

    Issue#1 in the fake has no ``priority`` key (asyncpg NULL once
    on the wire). Under SQL three-valued logic, an order comparison
    against NULL is unknown, i.e. not a match. The previous
    implementation stringified ``None`` and compared
    ``"None" > "5"``, which is true in ASCII -- so every NULL-
    priority row silently matched. Issue#4 (priority=30) and
    Issue#5 (priority=10) must still appear; Issue#1 must not.
    """
    run(["issue", "priority", "gt", "5"], client)
    out = capsys.readouterr().out
    assert "Issue#4" in out
    assert "Issue#5" in out
    assert "Issue#1" not in out, out


def test_kind_filter_rejected_when_field_missing_on_any_listed_kind(
    client: FakeClient,
) -> None:
    """A filter must apply to every listed kind, not just one.

    ``priority`` exists for Issue but not for Belief, so combining the two
    kinds with a ``priority`` filter previously slipped past validation
    and crashed at row-matching time when Belief rows produced ``None``.
    """
    with pytest.raises(ClientError, match="unknown filter field"):
        run(["issue", "belief", "priority", "gt", "5"], client)


def test_kind_filters_are_forwarded_to_list_kind(client: FakeClient) -> None:
    """Filters belong on the wire, not in a client-side post-pass.

    The legacy implementation downloaded ``--limit`` rows and filtered
    locally, which silently dropped any matching row outside the
    server's recency window. The contract is now: every filter clause
    parsed from the CLI reaches ``Client.list_kind`` so the server can
    apply it before ``LIMIT``.
    """
    run(["issue", "status", "is", "active", "title", "re", "row"], client)
    list_calls = [c for c in client.calls if c[0] == "list_kind"]
    assert list_calls, "issue listing must hit list_kind"
    forwarded = cast(tuple[Filter, ...], list_calls[0][2].get("filters") or ())
    assert forwarded == (
        Filter(field="status", op="is", value="active"),
        Filter(field="title", op="re", value="row"),
    )


def test_negated_regex_filter_is_forwarded_to_list_kind(client: FakeClient) -> None:
    """``nre`` reaches ``list_kind`` intact, like every other filter op."""
    run(["issue", "title", "nre", "row"], client)
    list_calls = [c for c in client.calls if c[0] == "list_kind"]
    assert list_calls, "issue listing must hit list_kind"
    forwarded = cast(tuple[Filter, ...], list_calls[0][2].get("filters") or ())
    assert forwarded == (Filter(field="title", op="nre", value="row"),)


def test_kind_filter_kind_alias_resolves_to_issue_kind(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``kind`` is a CLI alias for the ``issue_kind`` payload key.

    The trax grammar declares three CLI names (``kind``, ``issuekind``,
    ``issue_kind``) all mapping to the ``issue_kind`` payload column.
    Reading the raw row's ``kind`` discriminator (``"Issue"`` /
    ``"Belief"`` / ...) would silently match nothing for any
    Issue.Kind literal, so the filter pipeline must consult the
    grammar's payload-field table.
    """
    run(["issue", "kind", "is", "bug"], client)
    out = capsys.readouterr().out
    # ``Issue#4`` / ``Issue#5`` have ``issue_kind=["bug"]`` (conftest).
    assert "Issue#4" in out
    assert "Issue#5" in out
    # ``Issue#1`` has no ``issue_kind`` set; it must not match.
    assert "Issue#1" not in out


def test_kind_filters_match_rows_outside_default_limit_window(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A matching row past the natural LIMIT window must still appear.

    Reproduces Issue#256: with filtering server-side, even a row the
    server would have dropped from the unfiltered top-N must come
    back. The needle gets the oldest ``created`` timestamp so the
    FakeClient's ``ORDER BY created DESC`` puts it *behind* every
    noise row -- exactly the recency window the original bug used to
    silently truncate.
    """
    buried_id = str(uuid.uuid4())
    rows: list[dict[str, object]] = [
        {
            "id": str(uuid.uuid4()),
            "kind": "Issue",
            "seq": 100 + i,
            "status": "complete",
            "title": f"noise {i}",
            "created": f"2026-06-{i + 1:02d}T00:00:00",
        }
        for i in range(50)
    ]
    rows.append(
        {
            "id": buried_id,
            "kind": "Issue",
            "seq": 1,
            "status": "active",
            "title": "buried needle",
            # Oldest timestamp -> sorts last under ``ORDER BY created
            # DESC`` -> only the filter pipeline can rescue it.
            "created": "2020-01-01T00:00:00",
        }
    )
    client.rows = rows
    # FakeClient honours filters before applying ``limit``; the test
    # asks for ``--limit 5`` to make the truncation aggressive.
    run(["issue", "--limit", "5", "status", "is", "active"], client)
    out = capsys.readouterr().out
    assert "Issue#1" in out, out
    assert "buried needle" in out, out


def test_kind_verb_with_seq_shows_without_changes(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["issue", "1"], client)
    out = capsys.readouterr().out
    assert "Issue#1" in out
    assert "Recent changes:" not in out
    assert any(call[0] == "get_inquiry" for call in client.calls)


def test_kind_verb_uuid_subject_carries_expected_kind(client: FakeClient) -> None:
    target_id = uuid.uuid4()
    run(["belief", str(target_id)], client)
    ref = client.calls[0][1][0]
    assert ref == UuidRef(uuid=target_id, expected_kind="Belief")


def test_kind_verb_with_seq_changes_shows_recent_changes(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client.detail["changes"] = client.changes
    run(["issue", "1", "--changes"], client)
    out = capsys.readouterr().out
    assert "Issue#1" in out
    assert "Recent changes:" in out


def test_kind_verb_field_displays_field(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["issue", "1", "title"], client)
    assert capsys.readouterr().out == "row\n"


def test_kind_verb_projects_multiple_fields(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["issue", "1", "title", "owner"], client)
    assert capsys.readouterr().out == "row\nalice\n"
    assert [call[0] for call in client.calls].count("get_inquiry") == 2


def test_kind_verb_with_seq_and_positional_field_edits(client: FakeClient) -> None:
    run(["belief", "3", "judgement", "to", "proven", "--as=alice"], client)
    edit_calls = [c for c in client.calls if c[0] == "edit"]
    assert len(edit_calls) == 1
    assert edit_calls[0][1][1] == "judgement"
    assert edit_calls[0][1][2] == "proven"
    assert edit_calls[0][2]["actor"] == "alice"


def test_kind_verb_multi_field_edit(client: FakeClient) -> None:
    run(
        [
            "belief",
            "3",
            "judgement",
            "to",
            "proven",
            "confidence",
            "to",
            "0.9",
            "--as=alice",
        ],
        client,
    )
    edit_calls = [c for c in client.calls if c[0] == "edit"]
    fields = {call[1][1] for call in edit_calls}
    assert fields == {"judgement", "confidence"}


def test_kind_verb_multi_field_rejects_all_on_kind_mismatch(
    client: FakeClient,
) -> None:
    """A kind-invalid field aborts the whole write before any edit fires.

    ``status`` applies to every kind; ``judgement`` is Belief-only. On an
    Issue the command must reject up front and leave the row untouched,
    rather than committing ``status`` and then 409-ing on ``judgement``.
    """
    with pytest.raises(ClientError, match=r"judgement.*not valid.*Issue"):
        run(
            ["issue", "1", "status", "to", "complete", "judgement", "to", "proven"],
            client,
        )
    assert [c for c in client.calls if c[0] in ("edit", "transition_status")] == []


def test_kind_verb_field_edit_reads_stdin_value(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("## Problem\nDetails.\n"))
    run(["issue", "1", "description", "to", "-"], client)

    edit_calls = [c for c in client.calls if c[0] == "edit"]
    assert edit_calls[0][1][1:] == ("description", "## Problem\nDetails.\n")


def test_kind_verb_mixes_read_write_left_to_right(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["issue", "1", "title", "title", "to", "New", "title"], client)
    assert capsys.readouterr().out == "row\nset: Issue#1 title = New\nrow\n"
    assert [call[0] for call in client.calls] == [
        "get_inquiry",
        "resolve_id",
        "edit",
        "get_inquiry",
    ]


def test_kind_verb_priority_accepts_named_band(client: FakeClient) -> None:
    run(["issue", "7", "priority", "to", "high", "--as=alice"], client)
    edit_calls = [c for c in client.calls if c[0] == "edit"]
    assert edit_calls[0][1][2] == 10


def test_experiment_config_file_value_writes_parsed_json(
    client: FakeClient,
    tmp_path: Path,
) -> None:
    """``config to @file.json`` sends the parsed dict through ``edit``."""
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"lr": 0.1, "epochs": 2}\n', encoding="utf-8")
    run(["experiment", "2", "config", "to", f"@{cfg}"], client)
    edit_calls = [c for c in client.calls if c[0] == "edit"]
    assert edit_calls[0][1][1:] == ("config", {"lr": 0.1, "epochs": 2})


def test_experiment_config_stdin_value_writes_parsed_json(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO('{"seed": 7}\n'))
    run(["experiment", "2", "config", "to", "-"], client)
    edit_calls = [c for c in client.calls if c[0] == "edit"]
    assert edit_calls[0][1][1:] == ("config", {"seed": 7})


def test_experiment_config_missing_file_raises_client_error(
    client: FakeClient,
) -> None:
    with pytest.raises(ClientError, match=r"cannot read @missing\.json"):
        run(["experiment", "2", "config", "to", "@missing.json"], client)


def test_experiment_config_invalid_json_file_raises_client_error(
    client: FakeClient,
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "cfg.json"
    cfg.write_text("not json\n", encoding="utf-8")
    with pytest.raises(ClientError, match="config must be valid JSON"):
        run(["experiment", "2", "config", "to", f"@{cfg}"], client)


def test_experiment_config_read_prints_json(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare ``config`` read prints the stored object as indented JSON."""
    for row in client.rows:
        if row.get("kind") == "Experiment":
            row["config"] = {"lr": 0.1}
    run(["experiment", "2", "config"], client)
    assert capsys.readouterr().out == '{\n  "lr": 0.1\n}\n'


def test_config_rejected_on_non_experiment_kind(client: FakeClient) -> None:
    """``config`` is Experiment-only; other kinds reject before any request."""
    with pytest.raises(ClientError, match=r"'config'.*not valid on Issue"):
        run(["issue", "1", "config", "to", "{}"], client)


def test_create_experiment_with_config_file(
    client: FakeClient,
    tmp_path: Path,
) -> None:
    """The create path carries the parsed config dict in the submit body."""
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"lr": 0.1}\n', encoding="utf-8")
    run(["experiment", "title", "to", "Run", "config", "to", f"@{cfg}"], client)
    items = _batch_items(client)
    assert items[0][1]["config"] == {"lr": 0.1}


def test_bare_subkind_rejects(client: FakeClient) -> None:
    """Subkind names are nouns, not verbs; the parser must reject them."""
    with pytest.raises(ClientError, match="unexpected token"):
        run(["issue", "1", "bug"], client)


def test_relation_lists_edges_by_valence(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["belief", "3", "proves", "--sort=valence"], client)
    out = capsys.readouterr().out
    assert out.index("strong evidence") < out.index("weak evidence")


def test_relation_selects_edge_by_valence(client: FakeClient) -> None:
    run(["belief", "3", "proves", "1", "--sort=valence"], client)
    get_calls = [c for c in client.calls if c[0] == "get_inquiry"]
    assert get_calls[1][1][0] == UuidRef(uuid=client.evidence_high_id)


def test_relation_index_is_local_to_selected_relation(client: FakeClient) -> None:
    run(["issue", "1", "blocks", "1"], client)
    get_calls = [c for c in client.calls if c[0] == "get_inquiry"]
    assert get_calls[1][1][0] == UuidRef(uuid=client.other_child_id)


def test_edge_alias_without_target_lists_relation(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["issue", "1", "blocks"], client)
    out = capsys.readouterr().out
    assert "task child" in out
    assert "EDGE-PRI" in out
    assert "VALENCE" in out
    assert "must land first" in out


def test_relation_numeric_token_selects_peer_seq(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["issue", "1", "blocks", "6"], client)
    out = capsys.readouterr().out
    assert "Issue#6" in out
    assert "task child" in out


def test_kind_verb_svo_edge_add_forward(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``trax belief 3 proves paper 5`` wires Belief -> Paper."""
    paper_id = uuid.uuid4()
    belief_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        assert isinstance(ref, SeqRef)
        if ref.kind == "Paper":
            return "Paper", paper_id
        return "Belief", belief_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(["belief", "3", "proves", "paper", "5", "--as=alice"], client)
    edge_calls = [c for c in client.calls if c[0] == "add_edge"]
    assert len(edge_calls) == 1
    assert edge_calls[0][1] == (belief_id, paper_id, "proves")
    assert edge_calls[0][2]["actor"] == "alice"


def test_kind_verb_svo_edge_reverse_alias_swaps_endpoints(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``trax paper 5 proved_by belief 3`` still stores Belief -> Paper.

    The reverse alias is the artifact-anchored CLI shape; the underlying
    edge is the same ``proves`` row with Belief as ``from_id``. This
    test guards the Finding 1 direction fix: the previous alias mapping
    had ``reverse=False`` and incorrectly stored Paper -> Belief.
    """
    paper_id = uuid.uuid4()
    belief_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        assert isinstance(ref, SeqRef)
        if ref.kind == "Paper":
            return "Paper", paper_id
        return "Belief", belief_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(["paper", "5", "proved_by", "belief", "3", "--as=alice"], client)
    edge_calls = [c for c in client.calls if c[0] == "add_edge"]
    assert len(edge_calls) == 1
    # Belief is the from-side, Paper is the to-side.
    assert edge_calls[0][1] == (belief_id, paper_id, "proves")


@pytest.mark.parametrize(
    "argv",
    [
        ["paper", "5", "favors", "belief", "3", "--as=alice"],
        ["belief", "3", "favored_by", "paper", "5", "--as=alice"],
    ],
)
def test_favors_both_anchorings_store_artifact_to_belief(
    argv: list[str], client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both ``paper P favors belief B`` and ``belief B favored_by paper P``
    store the SAME edge: from=Paper, to=Belief.

    A belief is favored BY evidence, so the active reading is ``paper favors
    belief`` -- the Paper (evidence) is the subject/from-side. The CLI keeps
    both anchorings stable: the leading-subject form and the ``*_by`` reverse
    form must collapse to one stored edge with the Artifact on the from-side.
    """
    paper_id = uuid.uuid4()
    belief_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        assert isinstance(ref, SeqRef)
        if ref.kind == "Paper":
            return "Paper", paper_id
        return "Belief", belief_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(argv, client)
    edge_calls = [c for c in client.calls if c[0] == "add_edge"]
    assert len(edge_calls) == 1
    # Paper (evidence) is the from-side; Belief is the to-side.
    assert edge_calls[0][1] == (paper_id, belief_id, "favors")


def test_disfavors_both_anchorings_store_artifact_to_belief(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``paper P disfavors belief B`` and ``belief B disfavored_by paper P``
    both store from=Paper, to=Belief under the ``disfavors`` kind.
    """
    paper_id = uuid.uuid4()
    belief_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        assert isinstance(ref, SeqRef)
        if ref.kind == "Paper":
            return "Paper", paper_id
        return "Belief", belief_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    for argv in (
        ["paper", "5", "disfavors", "belief", "3", "--as=alice"],
        ["belief", "3", "disfavored_by", "paper", "5", "--as=alice"],
    ):
        client.calls.clear()
        run(argv, client)
        edge_calls = [c for c in client.calls if c[0] == "add_edge"]
        assert len(edge_calls) == 1
        # ``disfavors`` stores the ``favors`` kind with a negated valence.
        assert edge_calls[0][1] == (paper_id, belief_id, "favors")
        assert edge_calls[0][2]["valence"] == -0.5


def test_edge_payload_favors_labels_match_stored_direction() -> None:
    """``_edge_payload`` labels the from-side artifact, the to-side claim.

    ``favors`` stores Artifact -> Belief, so the source (from) is the citing
    artifact and the target (to) the favored claim. Anchored at the belief,
    projecting inbound favored_by (inbound=True), the source is the peer (Paper)
    and the target the subject (Belief).
    """
    belief_payload: dict[str, object] = {
        "self": {"id": "b", "kind": "Belief", "seq": 3},
        "changes": [],
    }
    paper_payload: dict[str, object] = {
        "self": {"id": "p", "kind": "Paper", "seq": 5},
        "changes": [],
    }
    payload = Kind._edge_payload(belief_payload, paper_payload, {}, ("favors", True))
    source, target = cast(
        tuple[dict[str, object], dict[str, object]], payload["endpoints"]
    )
    assert source["kind"] == "Paper"
    assert source["label"] == "citing artifact"
    assert target["kind"] == "Belief"
    assert target["label"] == "favored claim"


def test_edge_payload_negative_valence_renders_disproves_polarity() -> None:
    """A negative-valence ``proves`` edge reads as ``disproves`` / ``disproven``.

    For-vs-against is the SIGN of valence, not a separate stored kind. The CLI
    view must reflect that sign: a ``proves`` citation with valence < 0 is an
    against-citation and must render with the ``dis*`` spelling, not "proves".
    """
    belief_payload: dict[str, object] = {
        "self": {"id": "b", "kind": "Belief", "seq": 3},
        "changes": [],
    }
    paper_payload: dict[str, object] = {
        "self": {"id": "p", "kind": "Paper", "seq": 5},
        "changes": [],
    }
    payload = Kind._edge_payload(
        belief_payload, paper_payload, {"valence": -0.5}, ("proves", True)
    )
    assert payload["title"] == "disproves"
    _source, target = cast(
        tuple[dict[str, object], dict[str, object]], payload["endpoints"]
    )
    assert target["label"] == "disproven claim"


def test_disproves_relation_filters_to_negative_valence() -> None:
    """``... disproves`` lists only the against (valence < 0) citations.

    The stored kind is the same ``proves``; the dis* spelling selects the
    negative-valence subset, so it must differ from the unfiltered ``proves``
    projection.
    """
    payload: dict[str, object] = {
        "self": {"id": "b", "kind": "Belief", "seq": 3},
        "backlinks": {
            "proves": [
                {"id": "p1", "kind": "Paper", "seq": 5, "valence": 0.7},
                {"id": "p2", "kind": "Paper", "seq": 6, "valence": -0.4},
            ]
        },
    }
    all_proves = Kind._relation_rows(payload, ("proves", True))
    against = Kind._relation_rows(payload, ("proves", True), against=True)
    assert len(all_proves) == 2
    assert [r["id"] for r in against] == ["p2"]


def test_edge_payload_positive_valence_keeps_proves_polarity() -> None:
    """A non-negative valence keeps the plain ``proves`` / ``proven`` spelling."""
    belief_payload: dict[str, object] = {
        "self": {"id": "b", "kind": "Belief", "seq": 3},
        "changes": [],
    }
    paper_payload: dict[str, object] = {
        "self": {"id": "p", "kind": "Paper", "seq": 5},
        "changes": [],
    }
    payload = Kind._edge_payload(
        belief_payload, paper_payload, {"valence": 0.5}, ("proves", True)
    )
    assert payload["title"] == "proves"
    _source, target = cast(
        tuple[dict[str, object], dict[str, object]], payload["endpoints"]
    )
    assert target["label"] == "proven claim"


def test_multi_subject_row_command_shows_each(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``trax issue 5 issue 6`` shows each subject in sequence."""
    run(["issue", "1", "belief", "3"], client)
    out = capsys.readouterr().out
    assert "Issue#1" in out
    assert "Belief#3" in out


def test_edge_mutation_always_adds_and_shows_detail(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``trax issue X blocks issue Y`` always issues the add and shows the result.

    The mutation runs unconditionally (no hidden state-based dispatch
    between projection and mutation) and the resulting edge is shown
    so the user sees what they affected. The changelog stays gated on
    ``--changes`` regardless of whether the edge was new or existed.
    """
    run(["issue", "1", "blocks", "issue", "6"], client)
    out = capsys.readouterr().out
    edge_calls = [c for c in client.calls if c[0] == "add_edge"]
    assert len(edge_calls) == 1
    assert "added:" in out
    assert "edge:" in out
    assert "Recent changes:" not in out


def test_kind_verb_svo_edge_metadata_creates_then_annotates(
    client: FakeClient,
) -> None:
    """A metadata-bearing edge command must CREATE the edge, not only annotate it.

    A metadata-bearing edge command (e.g. ``belief 3 proves paper 5 valence to
    0.9 edge label add key``) must CREATE the edge AND apply the metadata in one
    shot. The previous impl flipped to an annotate-only PUT whenever any metadata
    rode along, so the very first ``paper 5 proves belief 3 valence to 0.9``
    409'd ("edge not found") because the edge did not exist yet. The fix issues
    an idempotent ``add_edge`` carrying the metadata so a NEW edge is created
    with it. (Edge ``label`` uses the ``edge`` marker; bare ``valence`` does
    not -- it is edge-only.)
    """
    run(
        [
            "belief",
            "3",
            "proves",
            "paper",
            "5",
            "valence",
            "to",
            "0.9",
            "edge",
            "label",
            "add",
            "key",
            "--as=alice",
        ],
        client,
    )
    add_calls = [c for c in client.calls if c[0] == "add_edge"]
    assert add_calls, "metadata-bearing edge must create the edge (add_edge)"
    assert add_calls[0][2]["valence"] == 0.9
    assert add_calls[0][2]["labels"] == ("key",)


def test_kind_verb_edge_annotation_omits_unspecified_metadata(
    client: FakeClient,
) -> None:
    """A NEW metadata edge carries only the named metadata on its create.

    The create-path applies the metadata directly; unspecified axes default
    (priority/valence None, note empty, labels empty) rather than patching.
    """
    run(
        [
            "belief",
            "3",
            "proves",
            "paper",
            "5",
            "valence",
            "to",
            "0.9",
            "--as=alice",
        ],
        client,
    )
    add_calls = [c for c in client.calls if c[0] == "add_edge"]
    assert add_calls[0][2] == {
        "actor": "alice",
        "priority": None,
        "note": "",
        "valence": 0.9,
        "labels": (),
        "reason": "",
    }


def test_kind_verb_edge_metadata_upserts_preexisting_edge(client: FakeClient) -> None:
    """Re-annotating an EXISTING edge upserts via a single ``add_edge`` call.

    Edge creation is an upsert: the first metadata command creates the edge,
    and a second metadata command on the same edge applies its new annotation
    through the same ``add_edge`` path (no separate ``annotate_edge`` call, no
    409). The CLI never special-cases "already exists".
    """
    run(["belief", "3", "proves", "paper", "5", "valence", "to", "0.9"], client)
    run(["belief", "3", "proves", "paper", "5", "edge", "label", "add", "key"], client)
    # Both commands go through add_edge (the upsert primitive); no command ever
    # routes through annotate_edge or raises an already-exists error.
    add_calls = [c for c in client.calls if c[0] == "add_edge"]
    assert len(add_calls) == 2
    assert [c for c in client.calls if c[0] == "annotate_edge"] == []
    # The second add_edge carries the new label; the first carried valence.
    assert add_calls[0][2]["valence"] == 0.9
    assert add_calls[1][2]["labels"] == ("key",)


def test_edge_label_del_clears_to_empty(client: FakeClient) -> None:
    """Removing an edge's last label CLEARS it, not a silent no-op (TRAX-CLI-004).

    ``label add x label del x`` resolves to an EMPTY label list. The upsert
    ``add_edge`` treats empty labels as "unset -- leave unchanged" (store
    design), so the clear cannot ride it. The CLI must thread the explicit
    clear-to-empty intent through the annotate path, which sets labels to the
    empty list (NULL) on the existing edge. A bare ``label add x`` (non-empty)
    still rides ``add_edge`` alone.
    """
    run(
        [
            "issue",
            "1",
            "requires",
            "issue",
            "4",
            "edge",
            "label",
            "add",
            "x",
            "edge",
            "label",
            "del",
            "x",
        ],
        client,
    )
    annotate_calls = [c for c in client.calls if c[0] == "annotate_edge"]
    assert len(annotate_calls) == 1, "the clear-to-empty must reach annotate_edge"
    assert annotate_calls[0][2]["labels"] == ()


def test_edge_label_add_does_not_annotate(client: FakeClient) -> None:
    """A non-empty edge label still rides ``add_edge`` alone (no annotate)."""
    run(["issue", "1", "requires", "issue", "4", "edge", "label", "add", "x"], client)
    assert [c for c in client.calls if c[0] == "annotate_edge"] == []
    add_calls = [c for c in client.calls if c[0] == "add_edge"]
    assert add_calls[0][2]["labels"] == ("x",)


def test_kind_verb_edge_metadata_echo_discriminates_create(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """The echo says `added:` when the metadata edge was created, `annotated:` when patched."""
    run(["belief", "3", "proves", "paper", "5", "valence", "to", "0.9"], client)
    assert "added: Belief#3 proves Paper#5" in capsys.readouterr().out
    run(["belief", "3", "proves", "paper", "5", "edge", "label", "add", "key"], client)
    assert "annotated: Belief#3 proves Paper#5" in capsys.readouterr().out


def test_edge_marker_sets_priority_on_the_narrows_edge(client: FakeClient) -> None:
    """``edge priority`` after the target sets the EDGE priority; bare sets the row.

    ``issue 7 priority to high`` sets row 7 (P-high=10). After the narrows target,
    ``edge priority to critical`` annotates the EDGE (P-critical=0). Without the
    ``edge`` marker the second priority would roll up to the subject -- the
    explicit marker is what reaches the edge (closing the old silent footgun).
    """
    run(
        [
            "issue",
            "7",
            "priority",
            "to",
            "high",
            "narrows",
            "issue",
            "8",
            "edge",
            "priority",
            "to",
            "critical",
            "--as=alice",
        ],
        client,
    )
    edit_calls = [c for c in client.calls if c[0] == "edit"]
    edge_calls = [c for c in client.calls if c[0] == "add_edge"]
    assert edit_calls[0][1][2] == 10
    assert edge_calls[0][2]["priority"] == 0


def test_kind_verb_svo_requires_complete_ref(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="incomplete ref"):
        run(["belief", "3", "proves", "paper"], client)


# ----------------------------------------------------------------------
# add (create row or list field)
# ----------------------------------------------------------------------


def test_kind_creates_row(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["issue", "title", "to", "Hi", "priority", "to", "high", "--as=alice"], client)
    items = _batch_items(client)
    assert len(items) == 1
    assert items[0][0] == "Issue"
    body = items[0][1]
    assert body["title"] == "Hi"
    assert body["priority"] == 10
    assert "created: Issue#1" in capsys.readouterr().out


def test_create_with_ref_list_resolves_typed_ref(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating a row with a ref-list field (`codechange to N`) resolves it.

    Regression for trax #419 third site: the create-body builder set
    body[field] to the raw "codechange 1" string, same gap as the edit/replace
    path. Create must resolve ref-list values to the wire id too.
    """
    cc_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        if isinstance(ref, SeqRef) and ref.kind == "CodeChange":
            return "CodeChange", cc_id
        return "Issue", client.target_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(
        ["experiment", "title", "to", "Q", "codechange", "to", "codechange", "1"],
        client,
    )
    body = _batch_items(client)[0][1]
    assert body["codechanges"] == [str(cc_id)]


def test_session_creates_row(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(
        [
            "agentsession",
            "title",
            "to",
            "refactor auth",
            "cli",
            "to",
            "codex",
            "cli_session_id",
            "to",
            "019e8014",
            "--as=alice",
        ],
        client,
    )
    items = _batch_items(client)
    assert len(items) == 1
    assert items[0][0] == "AgentSession"
    body = items[0][1]
    assert body["title"] == "refactor auth"
    assert body["cli"] == "codex"
    assert body["cli_session_id"] == "019e8014"
    # FakeClient mints ids; the verifiable part is the kind + body, asserted
    # above. Confirm a row was reported.
    assert "created:" in capsys.readouterr().out


def test_kind_create_owner_defaults_to_caller(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USER", "doe")
    run(["issue", "title", "to", "Hi"], client)
    body = _batch_items(client)[0][1]
    assert body["owner"] == "doe"


def test_kind_create_reads_stdin_value(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("body\n"))
    run(["issue", "title", "to", "Hi", "description", "to", "-"], client)

    body = _batch_items(client)[0][1]
    assert body["description"] == "body\n"


def test_one_command_reads_stdin_once(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("body\n"))
    with pytest.raises(ClientError, match="stdin value can only be used once"):
        run(
            [
                "issue",
                "title",
                "to",
                "-",
                "description",
                "to",
                "-",
            ],
            client,
        )


def test_inline_create_reads_stdin_value(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("blocker body\n"))
    run(
        [
            "issue",
            "title",
            "to",
            "root",
            "blocked_by",
            "issue",
            "title",
            "to",
            "blocker",
            "description",
            "to",
            "-",
        ],
        client,
    )

    items = _batch_items(client)
    # Item 0 is the root; the inline blocker is item 1.
    blocker = items[1][1]
    assert blocker["description"] == "blocker body\n"


def test_create_reads_file_values_for_root_and_inline_targets(
    client: FakeClient,
    tmp_path: Path,
) -> None:
    root_body = tmp_path / "root.md"
    blocker_body = tmp_path / "blocker.md"
    leaf_body = tmp_path / "leaf.md"
    root_body.write_text("root body\n", encoding="utf-8")
    blocker_body.write_text("blocker body\n", encoding="utf-8")
    leaf_body.write_text("leaf body\n", encoding="utf-8")

    run(
        [
            "issue",
            "title",
            "to",
            "root",
            "description",
            "to",
            f"@{root_body}",
            "blocked_by",
            "issue",
            "title",
            "to",
            "blocker",
            "description",
            "to",
            f"@{blocker_body}",
            "blocks",
            "issue",
            "title",
            "to",
            "leaf",
            "description",
            "to",
            f"@{leaf_body}",
        ],
        client,
    )

    items = _batch_items(client)
    # Item 0 is the root; inline targets follow in edge order (blocker, leaf).
    root = items[0][1]
    blocker = items[1][1]
    leaf = items[2][1]
    assert root["description"] == "root body\n"
    assert blocker["description"] == "blocker body\n"
    assert leaf["description"] == "leaf body\n"


def test_kind_create_applies_trailing_edge_action(client: FakeClient) -> None:
    run(
        [
            "issue",
            "title",
            "to",
            "trax cli ergonomics",
            "priority",
            "to",
            "high",
            "blocks",
            "issue",
            "5",
            "--as=alice",
        ],
        client,
    )
    body = _batch_items(client)[0][1]
    assert body["title"] == "trax cli ergonomics"
    assert body["priority"] == 10
    # `blocks` is the REVERSE alias of `requires` (A blocks B == B requires A):
    # the existing issue5 (requirer) -> the new root (prerequisite), so the
    # reverse alias puts issue5 on the from-side and the root on the to-side.
    edge = _batch_edges(client)[0]
    assert edge["edge_kind"] == "requires"
    assert edge["to_index"] == 0
    assert edge["from_id"] == str(client.child_high_id)


def test_inline_create_accepts_list_set_fields(client: FakeClient) -> None:
    run(
        [
            "issue",
            "title",
            "to",
            "root",
            "blocked_by",
            "issue",
            "title",
            "to",
            "blocker",
            "kind",
            "to",
            "bug",
        ],
        client,
    )

    items = _batch_items(client)
    root = items[0][1]
    blocker = items[1][1]
    assert blocker["title"] == "blocker"
    assert blocker["issue_kind"] == ("bug",)
    assert root["title"] == "root"
    assert "issue_kind" not in root


def test_kind_create_resolves_edge_target_before_submit(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        if isinstance(ref, SeqRef):
            if ref.kind == "Belief" and ref.seq == 404:
                raise StopIteration
            return ref.kind, client.target_id
        return "Issue", ref.uuid

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    with pytest.raises(StopIteration):
        run(["issue", "title", "to", "Hi", "blocks", "belief", "404"], client)
    assert not [c for c in client.calls if c[0] == "submit_batch"]


def test_create_inline_cost_lands_on_the_inline_node_not_root(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inline-created node's ``agent-cost`` is applied to THAT node (B2/B3).

    Issue#425 item 6: ``belief produced websearch <fields> agent-cost add N``
    creates the websearch inline and the cost rides the WEBSEARCH, not the root
    belief. Before the fix all create costs were applied to ``ids[0]`` (the
    root), mis-attributing a search's cost to its producing belief. Pins that
    the deferred ``add_cost`` targets the websearch's minted id.
    """
    root_id = uuid.uuid4()
    websearch_id = uuid.uuid4()

    def _batch(
        _self: FakeClient,
        items: object,
        *,
        edges: object = (),
    ) -> list[uuid.UUID]:
        del edges
        # Deterministic ids: item 0 = root belief, item 1 = inline websearch.
        return [root_id, websearch_id][: len(cast(list[object], items))]

    monkeypatch.setattr(FakeClient, "submit_batch", _batch)
    run(
        [
            "belief",
            "title",
            "to",
            "bet",
            "produced",
            "websearch",
            "query",
            "to",
            "q",
            "agent-cost",
            "add",
            "0.88",
        ],
        client,
    )
    cost_calls = [c for c in client.calls if c[0] == "add_cost"]
    assert len(cost_calls) == 1, "exactly one cost delta applied"
    target_id = cost_calls[0][1][0]
    assert target_id == websearch_id, "cost lands on the websearch, not the root belief"
    assert target_id != root_id


def test_kind_create_accepts_list_add_before_edges(client: FakeClient) -> None:
    run(
        [
            "issue",
            "title",
            "to",
            "Hi",
            "kind",
            "to",
            "bug",
            "kind",
            "add",
            "task",
            "label",
            "to",
            "sagent",
            "label",
            "add",
            "bash",
            "blocks",
            "issue",
            "5",
        ],
        client,
    )
    body = _batch_items(client)[0][1]
    assert body["issue_kind"] == ("bug", "task")
    assert body["labels"] == ("sagent", "bash")
    assert len(_batch_edges(client)) == 1


def test_kind_create_accepts_fields_after_explicit_edge_target(
    client: FakeClient,
) -> None:
    run(
        [
            "issue",
            "title",
            "to",
            "Hi",
            "blocks",
            "issue",
            "5",
            "description",
            "to",
            "root body",
        ],
        client,
    )
    body = _batch_items(client)[0][1]
    assert body["description"] == "root body"
    assert len(_batch_edges(client)) == 1


def test_kind_create_applies_cost_action_as_tail(client: FakeClient) -> None:
    """``agent-cost add`` rides a create: the row is made, then the cost lands."""
    run(
        [
            "issue",
            "title",
            "to",
            "Hi",
            "agent-cost",
            "add",
            "0.5",
        ],
        client,
    )
    items = _batch_items(client)
    assert items[0][1]["title"] == "Hi"
    cost_calls = [c for c in client.calls if c[0] == "add_cost"]
    assert len(cost_calls) == 1
    target_id, field, value = cost_calls[0][1]
    assert target_id == client.target_id
    assert field == "marginal_cost_agent_usd"
    assert value == 0.5


def test_kind_create_cost_action_applied_after_row_exists(
    client: FakeClient,
) -> None:
    """The cost delta is applied only after the root row is submitted."""
    run(["issue", "title", "to", "Hi", "agent-cost", "add", "0.5"], client)
    names = [c[0] for c in client.calls]
    assert names.index("submit_batch") < names.index("add_cost")


def test_kind_create_format_ids_prints_bare_id(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--format ids`` prints only the bare new row UUID, no ``created:`` line."""
    run(["issue", "title", "to", "Hi", "--format", "ids"], client)
    out = capsys.readouterr().out
    assert out == f"{client.target_id}\n"
    assert "created:" not in out


def test_kind_create_format_ids_lists_inline_targets(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--format ids`` prints the root then each inline-created target id."""
    run(
        [
            "issue",
            "title",
            "to",
            "root",
            "blocked_by",
            "issue",
            "title",
            "to",
            "blocker",
            "--format",
            "ids",
        ],
        client,
    )
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == str(client.target_id)
    assert len(lines) == 2  # root + one inline target
    assert "created:" not in out
    assert "added:" not in out


def test_kind_create_default_format_keeps_created_line(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default (no ``--format``) still prints the human ``created: Kind#seq`` line."""
    run(["issue", "title", "to", "Hi"], client)
    out = capsys.readouterr().out
    assert "created: Issue#1" in out


def test_kind_create_added_echo_prints_directional_triple(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """The create-path ``added:`` line names both endpoints in stored order.

    Previously it printed only ``added: <edge_kind> <target>``, dropping the
    source and so direction-ambiguous for a reverse alias. It now mirrors the
    row-local edit echo: ``added: <source> <edge_kind> <target>``, with a
    reverse alias swapping the shown endpoints to the direction the user wrote.
    """
    # ``blocks`` is the reverse alias of ``requires``: existing Issue#5
    # (requirer) -> new root (prerequisite), shown in the direction written.
    run(["issue", "title", "to", "X", "blocks", "issue", "5"], client)
    assert "added: Issue#5 requires Issue#1" in capsys.readouterr().out
    # ``blocked_by`` is the forward alias of ``requires``: the new root
    # (requirer) -> existing Issue#5 (prerequisite).
    run(["issue", "title", "to", "Y", "blocked_by", "issue", "5"], client)
    assert "added: Issue#1 requires Issue#5" in capsys.readouterr().out


def test_kind_create_rejects_remove_edge_action(client: FakeClient) -> None:
    """A remove-edge action is still rejected by create."""
    with pytest.raises(ClientError, match="create supports"):
        run(
            ["issue", "title", "to", "Hi", "blocks", "issue", "5", "del"],
            client,
        )


def test_kind_create_propagates_batch_failure_without_partial_output(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An atomic batch failure surfaces; nothing is reported as created.

    The server rolls the whole create back (e.g. a duplicate edge 409s
    mid-transaction), so the CLI must propagate the error and print no
    ``created:`` line -- there is no partial state to report.
    """

    def _boom(*_args: object, **_kwargs: object) -> list[uuid.UUID]:
        raise ClientError("edge already exists")

    monkeypatch.setattr(client, "submit_batch", _boom)
    with pytest.raises(ClientError, match="edge already exists"):
        run(["issue", "title", "to", "Hi", "blocks", "issue", "5"], client)
    assert "created:" not in capsys.readouterr().out


def test_kind_create_rejects_kind_invalid_field_before_submit(
    client: FakeClient,
) -> None:
    """A kind-invalid field aborts a create before any row is submitted."""
    with pytest.raises(ClientError, match=r"judgement.*not valid.*Issue"):
        run(["issue", "title", "to", "Hi", "judgement", "to", "proven"], client)
    assert [c for c in client.calls if c[0] == "submit"] == []


def test_inline_create_rejects_kind_invalid_field_before_any_submit(
    client: FakeClient,
) -> None:
    """A kind-invalid field on an inline-create target submits nothing.

    The inline blocker (an Issue) is given ``judgement``, a Belief-only
    field. Validation must fire before the inline target is created, so no
    row -- inline or root -- is left orphaned.
    """
    with pytest.raises(ClientError, match=r"judgement.*not valid.*Issue"):
        run(
            [
                "issue",
                "title",
                "to",
                "root",
                "blocked_by",
                "issue",
                "title",
                "to",
                "blocker",
                "judgement",
                "to",
                "proven",
            ],
            client,
        )
    assert [c for c in client.calls if c[0] == "submit"] == []


def test_row_local_inline_create_builds_nested_subtree(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row-local edit with a DEEP inline tree creates the whole subtree.

    Regression (TRAX-425-007): ``trax belief 1 produced websearch <f> produced
    paper <f>`` -- where ``belief 1`` already exists -- must create BOTH the
    websearch and the paper and wire belief->ws->paper. The edit path used to
    submit only the websearch and silently drop the nested ``produced paper``,
    unlike the create path, which flattens the whole tree atomically.
    """
    belief_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        if isinstance(ref, SeqRef) and ref.kind == "Belief":
            return "Belief", belief_id
        return "Belief", belief_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(
        [
            "belief", "1",
            "produced", "websearch", "query", "to", "q",
            "produced", "paper", "title", "to", "p",
        ],
        client,
    )  # fmt: skip
    # One atomic batch creates both new nodes (websearch + paper).
    items = _batch_items(client)
    kinds = [kind for kind, _body in items]
    assert kinds == ["WebSearch", "Paper"], "both inline nodes created in one batch"
    # Two edges survive. ``produced`` is the reverse alias of ``produced_by``
    # (stored from=produced -> to=producer), so the stored direction flips:
    # the websearch (item 0, produced) -> the existing belief (producer), and
    # the paper (item 1, produced) -> the websearch (item 0, producer).
    edges = _batch_edges(client)
    assert len(edges) == 2, "the nested produced-paper edge is not dropped"
    assert edges[0].get("from_index") == 0
    assert edges[0].get("to_id") == str(belief_id)
    assert edges[1].get("from_index") == 1
    assert edges[1].get("to_index") == 0


def test_agentsession_edit_does_not_crash_on_write_validation(
    client: FakeClient,
) -> None:
    """Editing an AgentSession field must validate, not KeyError.

    AgentSession is absent from the filter-kind map; the write whitelist
    must still cover it so ``agentsession <ref> cli to ...`` validates and
    sends the edit instead of crashing during validation.
    """
    run(["agentsession", str(uuid.uuid4()), "cli", "to", "codex"], client)
    edit_calls = [c for c in client.calls if c[0] == "edit"]
    assert edit_calls[0][1][1] == "cli"


def test_write_fields_cli_covers_every_kind() -> None:
    """The write-field whitelist must cover every valid kind, or edits crash.

    AgentSession is a valid editable kind absent from the filter-kind map;
    keying the write whitelist off that map once raised ``KeyError`` on any
    AgentSession write. Guard against that gap returning.
    """
    assert set(WRITE_FIELDS_CLI) == set(VALID_KINDS)


def test_blocks_alias_is_reverse_of_requires(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``blocks`` is the reverse alias of ``requires`` (stored from=requirer).

    ``issue 9 blocks issue 7`` means Issue#9 is a prerequisite Issue#7 requires,
    so the stored edge is from=#7 (requirer) to=#9 (prerequisite) under the
    ``requires`` kind -- the reverse alias swaps the subject onto the to-side.
    """
    blocker_id = uuid.uuid4()
    blocked_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        assert isinstance(ref, SeqRef)
        if ref.seq == 9:
            return ref.kind, blocker_id
        return ref.kind, blocked_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(["issue", "9", "blocks", "issue", "7", "--as=alice"], client)
    edge_calls = [c for c in client.calls if c[0] == "add_edge"]
    assert edge_calls[0][1] == (blocked_id, blocker_id, "requires")


def test_label_is_replaces_list(client: FakeClient) -> None:
    run(["issue", "7", "label", "to", "urgent", "--as=alice"], client)
    edit_calls = [c for c in client.calls if c[0] == "edit"]
    assert edit_calls[0][1][1] == "labels"
    assert edit_calls[0][1][2] == ("urgent",)


def test_label_add_appends(client: FakeClient) -> None:
    run(["issue", "7", "label", "add", "urgent", "--as=alice"], client)
    add_calls = [c for c in client.calls if c[0] == "add_label"]
    assert len(add_calls) == 1
    assert add_calls[0][1][1] == "urgent"


def test_subscriber_reads_list(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["issue", "7", "label"], client)
    assert capsys.readouterr().out == "urgent\n"


def test_subscribers_appends(client: FakeClient) -> None:
    run(["issue", "7", "subscriber", "add", "bob", "--as=alice"], client)
    assert any(c[0] == "add_subscriber" for c in client.calls)


def test_kind_is_replaces_issue_kind_list(client: FakeClient) -> None:
    run(["issue", "7", "kind", "to", "bug", "--as=alice"], client)
    edit_calls = [c for c in client.calls if c[0] == "edit"]
    assert edit_calls[0][1][1] == "issue_kind"
    assert edit_calls[0][1][2] == ("bug",)


def test_add_codechanges_resolves_ref(client: FakeClient) -> None:
    run(["experiment", "1", "codechange", "add", "5", "--as=alice"], client)
    assert any(c[0] == "add_codechange" for c in client.calls)


def test_set_codechanges_replace_sends_bare_uuid(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`codechange to N` (replace) sends a bare-UUID list.

    Regression for trax #419: ``codechanges`` is monomorphic (CodeChange) and
    the server stores ``list[uuid]`` (bodies.py), so the ref-list resolver
    emits a bare id, not an ``[id, kind]`` pair.
    """
    codechange_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        if isinstance(ref, SeqRef) and ref.kind == "CodeChange":
            return "CodeChange", codechange_id
        return "Experiment", client.target_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(["experiment", "1", "codechange", "to", "5", "--as=alice"], client)
    edits = [c for c in client.calls if c[0] == "edit"]
    assert edits, "expected an edit call for the codechanges replace"
    _, field, value = edits[-1][1]
    assert field == "codechanges"
    assert value == [str(codechange_id)]


def test_create_with_codechange_ref_list_sends_bare_uuid(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating an Experiment with `codechange to N` resolves to a bare UUID."""
    codechange_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        if isinstance(ref, SeqRef) and ref.kind == "CodeChange":
            return "CodeChange", codechange_id
        return "Experiment", client.target_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(["experiment", "title", "to", "q", "codechange", "to", "5"], client)
    body = _batch_items(client)[0][1]
    assert body["codechanges"] == [str(codechange_id)]


def test_inline_create_resolves_ref_list_to_wire_shape(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inline-create edge target resolves its ref-list `to` value.

    Regression for trax #419: the inline-create parse/build path bypassed
    ``consume_ref`` and ``_resolve_set_value``, so `... produced experiment
    codechange to codechange 5` left the seq token dangling and shipped the
    ref-list value unresolved.
    """
    codechange_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        if isinstance(ref, SeqRef) and ref.kind == "CodeChange":
            return "CodeChange", codechange_id
        return "Issue", client.target_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(
        [
            "issue",
            "title",
            "to",
            "root",
            "produced",
            "experiment",
            "title",
            "to",
            "x",
            "codechange",
            "to",
            "codechange",
            "5",
        ],
        client,
    )
    inline_body = next(
        body for kind, body in _batch_items(client) if kind == "Experiment"
    )
    assert inline_body["codechanges"] == [str(codechange_id)]


def test_del_codechange_resolves_typed_ref(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`codechange del N` resolves the typed ref to a bare id (trax #419)."""
    cc_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        if isinstance(ref, SeqRef) and ref.kind == "CodeChange":
            return "CodeChange", cc_id
        return "Experiment", client.target_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(
        ["experiment", "1", "codechange", "del", "codechange", "1", "--as=alice"],
        client,
    )
    removes = [c for c in client.calls if c[0] == "remove_codechange"]
    assert removes, "expected a remove_codechange call"
    assert removes[-1][1] == (client.target_id, cc_id)


def test_del_row_purges_without_prompt(client: FakeClient) -> None:
    run(["issue", "7", "del"], client)
    assert any(c[0] == "purge" for c in client.calls)


def test_del_edge_removes_edge(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``trax paper 5 proved_by belief 3 del`` removes the Belief -> Paper edge."""
    paper_id = uuid.uuid4()
    belief_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        assert isinstance(ref, SeqRef)
        if ref.kind == "Paper":
            return "Paper", paper_id
        return "Belief", belief_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(["paper", "5", "proved_by", "belief", "3", "del", "--as=alice"], client)
    remove_calls = [c for c in client.calls if c[0] == "remove_edge"]
    assert len(remove_calls) == 1
    # Same direction as add: Belief is the from-side, Paper is the to-side.
    assert remove_calls[0][1] == (belief_id, paper_id, "proves")


def test_del_label_removes(client: FakeClient) -> None:
    run(["issue", "7", "label", "del", "urgent", "--as=alice"], client)
    assert any(c[0] == "remove_label" for c in client.calls)


def test_agent_cost_adds_signed_delta(client: FakeClient) -> None:
    run(["issue", "7", "agent-cost", "add", "-0.50", "--as=alice"], client)
    cost_calls = [c for c in client.calls if c[0] == "add_cost"]
    assert cost_calls[0][1][1] == "marginal_cost_agent_usd"
    assert cost_calls[0][1][2] == -0.5


def test_cost_is_rejected(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="cost fields use add"):
        run(["issue", "7", "agent-cost", "to", "0.50"], client)


def test_cost_del_rejected(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="cost fields use add"):
        run(["issue", "7", "agent-cost", "del", "0.50"], client)


def test_resource_cost_reads_current_axis(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["issue", "7", "resource-cost"], client)
    assert capsys.readouterr().out == "2.000000\n"


def test_status_uses_positional_field_edit(client: FakeClient) -> None:
    run(["issue", "7", "status", "to", "complete", "--as=alice"], client)
    edit_calls = [c for c in client.calls if c[0] == "edit"]
    assert len(edit_calls) == 1
    assert edit_calls[0][1][1] == "status"
    assert edit_calls[0][1][2] == "complete"


def test_owner_uses_positional_field_edit(client: FakeClient) -> None:
    run(["issue", "7", "owner", "to", "alice", "--as=alice"], client)
    edit_calls = [c for c in client.calls if c[0] == "edit"]
    assert edit_calls[0][1][1] == "owner"
    assert edit_calls[0][1][2] == "alice"


def test_del_issue_kind_removes_list_value(client: FakeClient) -> None:
    run(["issue", "7", "issuekind", "del", "bug", "--as=alice"], client)
    assert any(c[0] == "remove_issue_kind" for c in client.calls)


def test_kind_verb_unknown_flag_rejected(client: FakeClient) -> None:
    with pytest.raises(SystemExit):
        run(["issue", "1", "--bogus=value", "--as=alice"], client)
    assert not [c for c in client.calls if c[0] == "edit"]


def test_kind_verb_unknown_positional_field_rejected(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="unexpected token"):
        run(["issue", "1", "bogus", "value", "--as=alice"], client)
    assert not [c for c in client.calls if c[0] == "edit"]


def test_list_del_requires_value(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="del requires a value"):
        run(["issue", "1", "subscriber", "del"], client)
    assert not [c for c in client.calls if c[0] == "remove_subscriber"]


def test_scalar_set_then_del_is_row_delete(client: FakeClient) -> None:
    """``title to x del`` sets the field then deletes the row (BUG-001).

    ``del`` is uniformly the terminal row-delete; the preceding set runs as a
    no-op before the delete rather than erroring as a scalar-field delete (the
    old ``index+3`` lookahead misread it). The edit lands, then the row purges.
    """
    run(["issue", "1", "title", "to", "x", "del"], client)
    assert [c[0] for c in client.calls if c[0] in ("edit", "purge")] == [
        "edit",
        "purge",
    ]


def test_bare_equals_rejected(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="unexpected token"):
        run(["issue", "=", "3"], client)
    assert not [c for c in client.calls if c[0] == "submit"]


def test_create_requires_to(client: FakeClient) -> None:
    with pytest.raises(ClientError, match=r"(unexpected token|FIELD to VALUE)"):
        run(["issue", "title", "RetryBug"], client)
    assert not [c for c in client.calls if c[0] == "submit"]


def test_next_calls_next_issue(client: FakeClient) -> None:
    run(["next"], client)
    assert any(c[0] == "next_issue" for c in client.calls)


def test_version_prints_server_sha(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["version"], client)
    assert any(c[0] == "version" for c in client.calls)
    assert "testsha" in capsys.readouterr().out


def test_search_calls_search(client: FakeClient) -> None:
    run(["search", "needle"], client)
    search_calls = [c for c in client.calls if c[0] == "search"]
    assert search_calls[0][1][0] == "needle"


def test_kind_list_width_limits_table_output(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client.rows[0]["title"] = "summary " * 20
    client.rows[0]["description"] = "description " * 20
    client.rows[0]["validation"] = "validation " * 20

    run(["issue", "--width", "80"], client)

    assert all(len(line) <= 80 for line in capsys.readouterr().out.splitlines())


def test_standalone_command_help_shows_local_forms(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["recent", "help"], client)
    out = capsys.readouterr().out
    assert "Usage: trax recent [OPTIONS]" in out
    assert "Examples:" in out
    assert not client.calls


def test_recent_calls_recent_changes(client: FakeClient) -> None:
    run(["recent"], client)
    assert any(c[0] == "recent_changes" for c in client.calls)


def test_cost_calls_cost_for(client: FakeClient) -> None:
    run(["cost", "issue", "1"], client)
    assert any(c[0] == "cost_for" for c in client.calls)


def test_board_lists_issues(client: FakeClient) -> None:
    run(["board"], client)
    # Board pages the whole collection via list_kind_all (no silent truncation).
    all_calls = [c for c in client.calls if c[0] == "list_kind_all"]
    assert all_calls
    assert all_calls[0][1][0] == "Issue"


def test_board_width_limits_output(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client.rows[0]["owner"] = "issue90-very-long-agent-name"
    client.rows[0]["title"] = "summary " * 20

    run(["board", "--width", "60"], client)

    lines = capsys.readouterr().out.splitlines()
    issue_lines = [line for line in lines if line.startswith("  Issue#")]
    assert issue_lines
    assert all(len(line) <= 60 for line in issue_lines)
    assert any("…" in line for line in issue_lines)


def test_blocked_lists_issues(client: FakeClient) -> None:
    run(["blocked"], client)
    assert any(c[0] == "list_kind_all" for c in client.calls)


def test_graph_lists_issues(client: FakeClient) -> None:
    run(["graph"], client)
    assert any(c[0] == "list_kind_all" for c in client.calls)


def test_whole_collection_views_never_exceed_server_cap(client: FakeClient) -> None:
    """graph/board/blocked must request pages the server accepts (<= MAX_LIST_LIMIT).

    These views need EVERY issue, but a single ``limit=2000`` is rejected with a
    400 (the server caps at MAX_LIST_LIMIT). They now page via ``list_kind_all``,
    whose every underlying ``list_kind`` request must stay within the cap.
    """
    for verb in ("graph", "board", "blocked"):
        client.calls.clear()
        run([verb], client)
        assert any(c[0] == "list_kind_all" for c in client.calls), verb
        # Every paged request the helper issued must be within the server cap.
        for call in (c for c in client.calls if c[0] == "list_kind"):
            kwargs = call[-1]
            assert isinstance(kwargs, dict)
            assert IntCodec.coerce(kwargs["limit"], 0) <= MAX_LIST_LIMIT, verb


# Folded in from former crasher_test.py.


def test_multi_subject_row_command_accepts_explicit_kind_after_bare_seq(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["issue", "1", "belief", "3"], client)
    out = capsys.readouterr().out
    assert "Issue#1" in out
    assert "Belief#3" in out


def test_create_file_value_missing_path_raises_client_error(client: FakeClient) -> None:
    with pytest.raises(ClientError, match=r"cannot read @missing\.md"):
        run(["issue", "title", "to", "Hi", "description", "to", "@missing.md"], client)


def test_render_tree_does_not_share_visited_across_roots(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If two roots both reach the same descendant, both must render it
    (with whatever depth is appropriate), not flag one as 'cycle'.
    """
    root_a: dict[str, object] = {
        "id": "id-a",
        "seq": 1,
        "status": "active",
        "title": "root a",
        "requires": [{"id": "id-shared"}],
    }
    root_b: dict[str, object] = {
        "id": "id-b",
        "seq": 2,
        "status": "active",
        "title": "root b",
        "requires": [{"id": "id-shared"}],
    }
    shared: dict[str, object] = {
        "id": "id-shared",
        "seq": 3,
        "status": "active",
        "title": "shared",
    }
    Graph.render([root_a, root_b, shared])
    output = capsys.readouterr().out
    assert "cycle" not in output, (
        "shared descendant should render under both roots, not appear as cycle"
    )
    assert output.count("issue 3") == 2, (
        f"expected shared 'issue 3' to render under both roots; got:\n{output}"
    )


def _graph_row(rid: str, seq: int, requires: Sequence[str] = ()) -> dict[str, object]:
    """One issue row for the dependency-graph tests."""
    return {
        "id": rid,
        "seq": seq,
        "status": "active",
        "title": f"n{seq}",
        "requires": [{"id": r} for r in requires],
    }


@pytest.mark.parametrize(
    ("label", "rows"),
    [
        ("one cycle", [_graph_row("a", 1, ["b"]), _graph_row("b", 2, ["a"])]),
        (
            "two disjoint cycles",
            [
                _graph_row("a", 1, ["b"]),
                _graph_row("b", 2, ["a"]),
                _graph_row("c", 3, ["d"]),
                _graph_row("d", 4, ["c"]),
            ],
        ),
        (
            "a root beside a disjoint cycle",
            [
                _graph_row("r", 1),
                _graph_row("c", 3, ["d"]),
                _graph_row("d", 4, ["c"]),
            ],
        ),
        (
            "a self-requiring row",
            [_graph_row("r", 1), _graph_row("s", 2, ["s"])],
        ),
        (
            "a cycle hanging off a root",
            [
                _graph_row("r", 1, ["c"]),
                _graph_row("c", 2, ["d"]),
                _graph_row("d", 3, ["c"]),
            ],
        ),
    ],
)
def test_render_shows_every_issue(
    label: str,
    rows: Sequence[Mapping[str, object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every fetched issue must appear somewhere in the output.

    The forest is built from rows nothing else requires, so any component
    with no such row -- a cycle -- contributes no entry point and its members
    are silently dropped. Dropping a row from a dependency view is the one
    failure that cannot be noticed: the reader has no way to tell a graph
    that omitted work from one that had none.

    Asserted as total coverage rather than per-shape, because the shapes that
    lose rows are exactly the ones nobody thinks to enumerate.
    """
    Graph.render(rows)
    output = capsys.readouterr().out

    missing = [row["seq"] for row in rows if f"issue {row['seq']} " not in output]
    assert not missing, f"{label}: issues {missing} never appeared:\n{output}"


def test_render_tree_does_not_blow_up_on_a_deep_diamond(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A per-path visited set makes a layered graph exponential.

    Copying ``visited`` for each child means a node reachable by many paths
    is re-rendered once per path. With each row requiring the next two, a
    30-deep graph is ~2**30 renders -- ``trax graph`` never returns, and
    ``list_kind_all`` feeds it the whole table.
    """
    depth = 30
    rows: list[Mapping[str, object]] = [
        {
            "id": f"id-{index}",
            "seq": index,
            "status": "active",
            "title": f"n{index}",
            "requires": [
                {"id": f"id-{child}"}
                for child in (index + 1, index + 2)
                if child <= depth
            ],
        }
        for index in range(depth + 1)
    ]

    Graph.render(rows)
    output = capsys.readouterr().out

    assert output.count("issue ") < 10 * depth, (
        "the traversal re-rendered shared subtrees per path; a real graph of "
        "this shape would not terminate"
    )


def test_run_action_rejects_unknown_action_variant() -> None:
    """``run_action`` must reject any unknown ``Action`` subtype rather than
    silently dispatching to ``Kind.run_purge``.
    """

    class _UnknownAction:
        pass

    client = FakeClient()
    ref = SeqRef(kind="Issue", seq=1)
    args = type(
        "_Args",
        (),
        {
            "actor": "",
            "reason": "",
            "format_": "table",
            "limit": 50,
            "sort": "seq",
            "width": None,
        },
    )()
    with pytest.raises((TypeError, ValueError, AssertionError)):
        verbs.run_action(
            ref,
            cast(Any, _UnknownAction()),
            cast(Any, args),
            lambda: cast(Any, client),
        )
    assert not any(call[0] == "purge" for call in client.calls), (
        "run_action silently purged for an unknown Action variant"
    )


def test_bulk_apply_single_match_applies_without_flag(client: FakeClient) -> None:
    """A query matching one row applies directly, no ``--makeitso`` needed."""
    run(["experiment", "status", "is", "complete", "owner", "to", "alice"], client)
    edits = [c for c in client.calls if c[0] == "edit"]
    assert len(edits) == 1
    assert edits[0][1][1:] == ("owner", "alice")


def test_bulk_apply_multi_match_without_flag_previews_only(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A multi-row match without ``--makeitso`` previews and writes nothing."""
    run(["issue", "status", "is", "active", "owner", "to", "Josh"], client)
    assert "would apply to" in capsys.readouterr().out
    assert not [c for c in client.calls if c[0] == "edit"]


def test_bulk_apply_multi_match_with_flag_edits_each(client: FakeClient) -> None:
    """``--makeitso`` applies the mutation to every matched row."""
    active = sum(
        1
        for row in client.rows
        if row.get("kind") == "Issue" and row.get("status") == "active"
    )
    run(
        ["issue", "status", "is", "active", "owner", "to", "Josh", "--makeitso"],
        client,
    )
    edits = [c for c in client.calls if c[0] == "edit"]
    assert len(edits) == active
    assert all(c[1][1:] == ("owner", "Josh") for c in edits)


def test_bulk_apply_range_selects_each_matched_row(client: FakeClient) -> None:
    """A seq range applies to every row in the window."""
    run(["issue", "4..5", "priority", "to", "low", "--makeitso"], client)
    edits = [c for c in client.calls if c[0] == "edit"]
    assert len(edits) == 2


def test_bulk_apply_list_add_per_row(client: FakeClient) -> None:
    """A list ``add`` mutation applies to every matched row."""
    active = sum(
        1
        for row in client.rows
        if row.get("kind") == "Issue" and row.get("status") == "active"
    )
    run(
        ["issue", "status", "is", "active", "label", "add", "triage", "--makeitso"],
        client,
    )
    assert len([c for c in client.calls if c[0] == "add_label"]) == active


def test_bulk_apply_ref_list_add_resolves_typed_ref_per_row(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bulk `codechange add N` resolves the typed ref for every matched row.

    The mutation scanner truncated a typed ref before it reached the resolver
    (trax #419); the variable-width mutation span keeps it intact. ``codechange``
    is also a kind keyword, so this also pins that a following operator routes it
    to a mutation, not a bare kind-widening clause.
    """
    client.rows = [
        {"id": str(uuid.uuid4()), "kind": "Experiment", "seq": i, "status": "active"}
        for i in (1, 2)
    ]
    cc_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        if isinstance(ref, SeqRef) and ref.kind == "CodeChange":
            return "CodeChange", cc_id
        return "Experiment", client.target_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(
        [
            "experiment",
            "status",
            "is",
            "active",
            "codechange",
            "add",
            "5",
            "--makeitso",
        ],
        client,
    )
    adds = [c for c in client.calls if c[0] == "add_codechange"]
    assert len(adds) == 2
    assert all(c[1] == (client.target_id, cc_id) for c in adds)


def test_set_ref_list_echoes_user_spelling(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`codechange to N` echoes the ref CLI form, not the resolved wire value."""

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        if isinstance(ref, SeqRef) and ref.kind == "CodeChange":
            return "CodeChange", uuid.uuid4()
        return "Experiment", client.target_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(["experiment", "1", "codechange", "to", "5", "--as=alice"], client)
    assert "codechanges = CodeChange#5" in capsys.readouterr().out


def test_bulk_apply_updates_matches_beyond_default_limit(client: FakeClient) -> None:
    """Bulk selection is not capped at the display ``--limit`` default of 50."""
    client.rows = [
        {
            "id": str(uuid.uuid4()),
            "kind": "Issue",
            "seq": i,
            "status": "active",
            "title": f"row {i}",
        }
        for i in range(1, 52)
    ]
    run(["issue", "status", "is", "active", "owner", "to", "bob", "--makeitso"], client)
    assert len([c for c in client.calls if c[0] == "edit"]) == 51


def test_bulk_apply_resolves_file_value(
    client: FakeClient,
    tmp_path: Path,
) -> None:
    """A ``@path`` mutation value is read from disk before each per-row write."""
    body = tmp_path / "body.md"
    body.write_text("body\n", encoding="utf-8")
    client.rows = [
        {
            "id": str(uuid.uuid4()),
            "kind": "Issue",
            "seq": 1,
            "status": "active",
            "title": "row",
        }
    ]
    run(["issue", "status", "is", "active", "description", "to", f"@{body}"], client)
    edits = [c for c in client.calls if c[0] == "edit"]
    assert edits[0][1][1:] == ("description", "body\n")


def test_query_rows_caps_total_across_kinds(client: FakeClient) -> None:
    """The limit bounds the total matched set, not each kind independently."""
    client.rows = [
        {"id": str(uuid.uuid4()), "kind": kind, "seq": i, "status": "active"}
        for kind in ("Issue", "Belief")
        for i in range(1, 6)
    ]
    query = ListQuery(kinds=("Issue", "Belief"), ranges={}, filters=())
    rows = _query_rows(query, cast(Client, client), limit=7)
    assert len(rows) == 7


def test_send_parses_actor_only(client: FakeClient) -> None:
    run(["send", "@scientist", "hello", "there"], client)
    sends = [c for c in client.calls if c[0] == "send_message"]
    assert sends == [("send_message", ("scientist", "hello there"), {"room": None})]


def test_send_parses_actor_and_room(client: FakeClient) -> None:
    run(["send", "@scientist:sear", "status?"], client)
    sends = [c for c in client.calls if c[0] == "send_message"]
    assert sends[0][1] == ("scientist", "status?")
    assert sends[0][2] == {"room": "sear"}


def test_send_at_prefix_is_optional(client: FakeClient) -> None:
    run(["send", "scientist", "hi"], client)
    sends = [c for c in client.calls if c[0] == "send_message"]
    assert sends[0][1][0] == "scientist"


def test_send_undelivered_when_no_match(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _send(actor: str, text: str, *, room: str | None = None) -> list[uuid.UUID]:
        del actor, text, room
        return []  # no live session matched

    monkeypatch.setattr(client, "send_message", _send)
    run(["send", "@ghost", "hi"], client)
    assert "undelivered" in capsys.readouterr().out


def test_flat_inline_create_edge_is_atomic_no_orphan_on_edge_failure(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flat inline-create edge must not orphan the new row on edge failure.

    ``issue 1 produced websearch query to q`` inline-creates a flat WebSearch
    (fields only) and links it. Routed through a single ``submit_batch`` the
    create and edge land or roll back together; the old two-call path
    (``submit`` then ``add_edge``) left the WebSearch orphaned when the edge
    POST failed. With the batch path there is no bare ``submit`` to commit an
    orphan, so a batch failure leaves nothing behind.
    """

    def _boom(*_args: object, **_kwargs: object) -> list[uuid.UUID]:
        raise ClientError("edge add failed")

    monkeypatch.setattr(client, "submit_batch", _boom)
    with pytest.raises(ClientError, match="edge add failed"):
        run(["issue", "1", "produced", "websearch", "query", "to", "q"], client)
    assert [c for c in client.calls if c[0] == "submit"] == []


def test_flat_inline_create_edge_uses_single_submit_batch(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flat inline-create edge path flattens into one atomic batch.

    One item (the WebSearch) and one anchor edge sourcing the existing
    subject by id -- the same atomic shape as the deep-subtree path, not a
    bare ``submit`` followed by a separate ``add_edge``.
    """
    issue_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        del ref
        return "Issue", issue_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(["issue", "1", "produced", "websearch", "query", "to", "q"], client)
    items = _batch_items(client)
    assert [kind for kind, _body in items] == ["WebSearch"]
    edges = _batch_edges(client)
    assert len(edges) == 1
    # ``produced`` is the reverse alias of ``produced_by`` (stored from=produced
    # -> to=producer): the new websearch (item 0) is the from-side, the existing
    # issue the to-side.
    assert edges[0].get("from_index") == 0
    assert edges[0].get("to_id") == str(issue_id)
    assert [c for c in client.calls if c[0] == "submit"] == []
    assert [c for c in client.calls if c[0] == "add_edge"] == []


def test_paper_author_add_routes_to_client_add_author(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``paper N author add X`` dispatches to ``Client.add_author``.

    The grammar wires the author list field to ``add_author``/``remove_author``;
    those methods must exist on the client (and FakeClient) or the verb layer
    raises a raw ``AttributeError`` instead of performing the byline mutation.
    """
    paper_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        del ref
        return "Paper", paper_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(["paper", "5", "author", "add", "Doe"], client)
    add_calls = [c for c in client.calls if c[0] == "add_author"]
    assert add_calls
    assert add_calls[0][1] == (paper_id, "Doe")


def test_paper_author_del_routes_to_client_remove_author(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``paper N author del X`` dispatches to ``Client.remove_author``."""
    paper_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        del ref
        return "Paper", paper_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(["paper", "5", "author", "del", "Doe"], client)
    rm_calls = [c for c in client.calls if c[0] == "remove_author"]
    assert rm_calls
    assert rm_calls[0][1] == (paper_id, "Doe")


def test_edge_labels_dict_is_pinned_to_edge_kind() -> None:
    """The edge-label table must cover exactly ``Edge.Kind``, no drift.

    A missing key raises a raw ``KeyError`` deep in ``_edge_payload``; a stale
    extra key is dead code. Pin the constant's keys to the closed ``Edge.Kind``
    literal so adding a kind forces a label entry.
    """
    assert set(LABELS_BY_EDGE_KIND) == set(get_args(Edge.Kind.__value__))


def test_blocked_render_builds_seq_index_once_no_nested_scan() -> None:
    """``Blocked.render`` must not linear-scan ``rows`` per blocker.

    With ``status_by_id`` already built once, the blocker-ref formatting
    should look up each blocker's ``seq`` from a prebuilt index, not a fresh
    ``next(... for x in rows ...)`` per blocker (O(rows x blockers)). Guard by
    counting row iterations: a single pre-pass, not one scan per blocker.
    """
    n = 400
    ids = [str(uuid.uuid4()) for _ in range(n)]
    rows: list[dict[str, object]] = [
        {
            "id": ids[i],
            "seq": i + 1,
            "status": "active",
            "title": f"row {i}",
            "priority": 10,
            "requires": [{"id": pid} for pid in ids[:5]],
        }
        for i in range(n)
    ]
    scans = 0

    class _CountingList(list[dict[str, object]]):
        @override
        def __iter__(self) -> Iterator[dict[str, object]]:
            nonlocal scans
            scans += 1
            return super().__iter__()

    Blocked.render(_CountingList(rows))
    # status_by_id pass + the active-row pass = a small constant number of full
    # iterations. A per-(row, blocker) nested scan would be hundreds.
    assert scans < 10, f"rows iterated {scans} times; expected a constant pre-pass"


def test_sort_relation_rows_orders_priority_zero_first() -> None:
    """A P0 row (priority=0) must sort ABOVE P1 (priority=10), not as medium.

    F33: ``row.get("priority") or 20`` mapped the falsy ``0`` to ``20``, so a
    critical P0 row sorted as medium -- below P1. The default-priority sort is
    the default for every relation listing, so this silently mis-ordered the
    most important rows. The default for a truly absent priority is still 20.
    """
    p0: dict[str, object] = {"id": "a", "seq": 1, "status": "active", "priority": 0}
    p1: dict[str, object] = {"id": "b", "seq": 2, "status": "active", "priority": 10}
    ordered = Kind._sort_relation_rows([p1, p0], "priority")
    assert [row["priority"] for row in ordered] == [0, 10]


def test_sort_relation_rows_absent_priority_defaults_to_medium() -> None:
    """A row with no priority key still defaults to 20 (sorts below P10)."""
    absent: dict[str, object] = {"id": "a", "seq": 1, "status": "active"}
    p1: dict[str, object] = {"id": "b", "seq": 2, "status": "active", "priority": 10}
    ordered = Kind._sort_relation_rows([absent, p1], "priority")
    assert [row["seq"] for row in ordered] == [2, 1]


def test_blocked_render_shows_priority_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A P0 blocked row must display its priority ``0``, not a blank cell.

    F33 display: ``row.get('priority', '') or ''`` blanked a falsy ``0``, so a
    P0 row showed an empty priority column -- indistinguishable from unset.
    """
    rows: list[dict[str, object]] = [
        {
            "id": "a",
            "seq": 1,
            "status": "active",
            "title": "critical",
            "priority": 0,
            "requires": [{"id": "b"}],
        },
        {"id": "b", "seq": 2, "status": "active", "title": "blocker"},
    ]
    Blocked.render(rows)
    out = capsys.readouterr().out
    assert "issue    1" in out
    # The priority cell holds 0, not blank.
    assert "[       0]" in out


def test_board_format_row_shows_priority_zero() -> None:
    """A P0 board row must render ``P0``, not ``P?``.

    F33 display: ``row.get('priority', '') or '?'`` mapped falsy ``0`` to
    ``?``, so a P0 issue looked priority-less on the board.
    """
    row: dict[str, object] = {
        "id": "a",
        "seq": 1,
        "status": "active",
        "title": "critical",
        "priority": 0,
        "owner": "alice",
    }
    line = Board._format_row(row, width=200)
    assert "P0 " in line
    assert "P? " not in line


def test_blocked_render_keeps_off_window_blockers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A row whose only blocker is off-window must still show as blocked.

    F30: ``Blocked.render`` filtered blockers by ``status_by_id == "active"``,
    silently dropping a blocker absent from the fetched window. A row with ONLY
    off-window blockers then rendered as UNBLOCKED -- available work it is not.
    The off-window blocker must surface as an unknown/off-window stub.
    """
    rows: list[dict[str, object]] = [
        {
            "id": "a",
            "seq": 1,
            "status": "active",
            "title": "waits on off-window",
            "priority": 10,
            "requires": [{"id": "off-window-id"}],
        },
    ]
    Blocked.render(rows)
    out = capsys.readouterr().out
    assert "(no blocked issues)" not in out
    assert "issue    1" in out
    # The unknown blocker surfaces rather than vanishing.
    assert "off-window-id" in out or "??" in out


def test_render_tree_keeps_off_window_children(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An off-window blocker child must still appear in the dependency tree.

    F31: ``Graph._render_tree`` only recursed into children present in
    ``rows_by_id``; an off-window child vanished from the tree, hiding a real
    dependency. It must render as an off-window stub.
    """
    root: dict[str, object] = {
        "id": "a",
        "seq": 1,
        "status": "active",
        "title": "root",
        "requires": [{"id": "off-window-id"}],
    }
    Graph.render([root])
    out = capsys.readouterr().out
    assert "off-window-id" in out or "??" in out


def test_anchored_inline_subtree_emits_added_echo(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The row-local inline-create path must echo ``added:`` for each edge.

    F9: ``run_create`` emits an ``added: source kind target`` line per edge,
    but ``_run_anchored_inline_subtree`` (the edit path) emitted only
    ``created:`` lines, so a ``belief 1 produced websearch ...`` reported the
    new row but never the relationship. Both paths must echo consistently.
    """
    belief_id = uuid.uuid4()

    def _resolve(_self: FakeClient, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        del ref
        return "Belief", belief_id

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve)
    run(["belief", "1", "produced", "websearch", "query", "to", "q"], client)
    out = capsys.readouterr().out
    assert "created:" in out
    # ``produced`` is the reverse alias of ``produced_by``: the websearch
    # (produced) is the from-side, Belief#1 (producer) the to-side.
    assert "produced_by Belief#1" in out


def test_create_flatten_batches_existing_ref_resolution(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_create`` resolves all existing-ref edge targets in one batch.

    F14: each existing-ref edge target was resolved with a sequential
    ``resolve_id`` round-trip. With several edges to existing rows this is N
    round-trips; the shared flatten must pre-collect the refs and call
    ``resolve_ids`` once.
    """

    def _resolve_one(
        _self: FakeClient, ref: Ref
    ) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        del ref
        return "Issue", uuid.uuid4()

    monkeypatch.setattr(FakeClient, "resolve_id", _resolve_one)
    run(
        [
            "issue", "title", "to", "root",
            "blocks", "issue", "5",
            "blocks", "issue", "6",
            "blocks", "issue", "7",
        ],
        client,
    )  # fmt: skip
    # All three existing-ref targets resolve through a single resolve_ids batch.
    # (FakeClient.resolve_ids fans out to resolve_id internally; the real client
    # batches the UUID lookups into one round-trip -- the point of F14.)
    batched = [c for c in client.calls if c[0] == "resolve_ids"]
    assert len(batched) == 1, f"expected one resolve_ids batch, got {len(batched)}"
    refs_arg = cast(tuple[Ref, ...], batched[0][1][0])
    assert len(refs_arg) == 3, "all three edge targets in one batch"


def test_send_empty_target_errors(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="must name an actor"):
        run(["send", "@", "hi"], client)


def test_send_trailing_colon_errors_cleanly(client: FakeClient) -> None:
    # A bare trailing ':' used to ship room="" to the wire validator and
    # surface as an opaque Pydantic dump; now it is a local ClientError.
    with pytest.raises(ClientError, match="empty room"):
        run(["send", "@scientist:", "hi"], client)


def test_limit_zero_is_rejected(client: FakeClient) -> None:
    # ``--limit 0`` (or negative) silently returned no rows; argparse must
    # reject it so the user gets a clear error, one rule across CLI sites.
    with pytest.raises(SystemExit):
        run(["issue", "--limit", "0"], client)


def test_limit_negative_is_rejected(client: FakeClient) -> None:
    with pytest.raises(SystemExit):
        run(["search", "needle", "--limit", "-3"], client)


def test_help_flag_after_positional_shows_help_page(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    # ``trax issue 7 --help`` must route to the kind HelpPage, not argparse
    # usage -- a trailing --help/-h is the same tail action as ``help``.
    run(["issue", "7", "--help"], client)
    out = capsys.readouterr().out
    assert "Legend:" in out, out
    assert "trax issue" in out, out


def _metric_call(client: FakeClient, name: str) -> dict[str, object]:
    """The kwargs of the single recorded ``name`` call (query/write/rank)."""
    calls = [c for c in client.calls if c[0] == name]
    assert len(calls) == 1, f"expected one {name} call, got {calls}"
    return calls[0][2]


def _exp_id(client: FakeClient) -> uuid.UUID:
    """The id of the Experiment at seq 2 (``experiment 2``)."""
    row = next(
        r for r in client.rows if r.get("kind") == "Experiment" and r.get("seq") == 2
    )
    return uuid.UUID(str(row["id"]))


# -- single-experiment write forms -------------------------------------------


def test_metric_single_cell_write(client: FakeClient) -> None:
    run(
        [
            "experiment",
            "2",
            "metric",
            "at",
            "key",
            "is",
            "loss",
            "at",
            "step",
            "is",
            "3",
            "to",
            "0.5",
        ],
        client,
    )
    kw = _metric_call(client, "write_metrics_masked")
    masks = cast(tuple[MetricMaskClause, ...], kw["masks"])
    assert kw["value"] == 0.5
    assert {(m.axis, m.op, m.value) for m in masks} == {
        ("key", "is", "loss"),
        ("step", "is", "3"),
    }


def test_metric_write_targets_resolved_experiment(client: FakeClient) -> None:
    run(["experiment", "2", "metric", "at", "step", "is", "3", "to", "0.5"], client)
    calls = [c for c in client.calls if c[0] == "write_metrics_masked"]
    assert calls[0][1][0] == _exp_id(client)


def test_metric_multi_key_one_step_write(client: FakeClient) -> None:
    # ``at step is 3`` constrains the step; each ``at key is X to V`` would be a
    # separate command in practice, but a single command sets one masked value.
    run(
        [
            "experiment",
            "2",
            "metric",
            "at",
            "step",
            "is",
            "3",
            "at",
            "key",
            "is",
            "acc",
            "to",
            "0.9",
        ],
        client,
    )
    kw = _metric_call(client, "write_metrics_masked")
    assert kw["value"] == 0.9


def test_metric_bareword_key_write(client: FakeClient) -> None:
    run(
        [
            "experiment",
            "2",
            "metric",
            "at",
            "loss",
            "at",
            "step",
            "is",
            "3",
            "to",
            "0.5",
        ],
        client,
    )
    kw = _metric_call(client, "write_metrics_masked")
    masks = cast(tuple[MetricMaskClause, ...], kw["masks"])
    # ``at loss`` is the ``at key is loss`` shorthand.
    assert ("key", "is", "loss") in {(m.axis, m.op, m.value) for m in masks}


def test_metric_write_requires_step_mask(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="must mask 'step'"):
        run(
            ["experiment", "2", "metric", "at", "key", "is", "loss", "to", "0.5"],
            client,
        )


def test_metric_write_rejects_non_finite(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="must be finite"):
        run(["experiment", "2", "metric", "at", "step", "is", "3", "to", "inf"], client)


def test_metric_write_rejects_non_numeric(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="must be a number"):
        run(
            ["experiment", "2", "metric", "at", "step", "is", "3", "to", "high"], client
        )


# -- bulk write guard --------------------------------------------------------


def test_metric_bulk_write_needs_makeitso(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A mask that resolves to >1 cell is a bulk write; without --makeitso the
    # CLI refuses after a dry read discovers the blast radius.
    def _two_hits(
        experiment_id: uuid.UUID,
        *,
        masks: Sequence[MetricMaskClause],
        sort: str | None = None,
        limit: int | None = None,
    ) -> list[MetricPoint]:
        del experiment_id, masks, sort, limit
        return [
            MetricPoint(key="loss", step=4, value=0.1),
            MetricPoint(key="loss", step=5, value=0.2),
        ]

    monkeypatch.setattr(client, "query_metrics", _two_hits)
    with pytest.raises(ClientError, match="would write 2 cells"):
        run(
            [
                "experiment",
                "2",
                "metric",
                "at",
                "key",
                "is",
                "loss",
                "at",
                "step",
                "gt",
                "3",
                "to",
                "0.5",
            ],
            client,
        )
    assert not [c for c in client.calls if c[0] == "write_metrics_masked"]


def test_metric_bulk_write_with_makeitso(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _two_hits(
        experiment_id: uuid.UUID,
        *,
        masks: Sequence[MetricMaskClause],
        sort: str | None = None,
        limit: int | None = None,
    ) -> list[MetricPoint]:
        del experiment_id, masks, sort, limit
        return [
            MetricPoint(key="loss", step=4, value=0.1),
            MetricPoint(key="loss", step=5, value=0.2),
        ]

    monkeypatch.setattr(client, "query_metrics", _two_hits)
    run(
        [
            "experiment",
            "2",
            "metric",
            "at",
            "key",
            "is",
            "loss",
            "at",
            "step",
            "gt",
            "3",
            "to",
            "0.5",
            "--makeitso",
        ],
        client,
    )
    # --makeitso skips the dry read and writes directly.
    assert [c for c in client.calls if c[0] == "write_metrics_masked"]


# -- single-experiment read forms --------------------------------------------


def test_metric_read_masked(client: FakeClient) -> None:
    run(
        [
            "experiment",
            "2",
            "metric",
            "at",
            "key",
            "is",
            "loss",
            "at",
            "step",
            "gt",
            "3",
        ],
        client,
    )
    kw = _metric_call(client, "query_metrics")
    masks = cast(tuple[MetricMaskClause, ...], kw["masks"])
    assert {(m.axis, m.op, m.value) for m in masks} == {
        ("key", "is", "loss"),
        ("step", "gt", "3"),
    }
    assert not [c for c in client.calls if c[0] == "write_metrics_masked"]


def test_metric_read_bareword(client: FakeClient) -> None:
    run(["experiment", "2", "metric", "at", "loss"], client)
    kw = _metric_call(client, "query_metrics")
    masks = cast(tuple[MetricMaskClause, ...], kw["masks"])
    assert [(m.axis, m.op, m.value) for m in masks] == [("key", "is", "loss")]


def test_metric_read_step_is(client: FakeClient) -> None:
    run(["experiment", "2", "metric", "at", "step", "is", "3"], client)
    kw = _metric_call(client, "query_metrics")
    masks = cast(tuple[MetricMaskClause, ...], kw["masks"])
    assert [(m.axis, m.op, m.value) for m in masks] == [("step", "is", "3")]


def test_metric_read_value_gt(client: FakeClient) -> None:
    run(["experiment", "2", "metric", "at", "value", "gt", "0.9"], client)
    kw = _metric_call(client, "query_metrics")
    masks = cast(tuple[MetricMaskClause, ...], kw["masks"])
    assert [(m.axis, m.op, m.value) for m in masks] == [("value", "gt", "0.9")]


def test_metric_read_whole_grid(client: FakeClient) -> None:
    run(["experiment", "2", "metric"], client)
    kw = _metric_call(client, "query_metrics")
    assert kw["masks"] == ()


def test_metric_read_step_max_reduction(client: FakeClient) -> None:
    run(["experiment", "2", "metric", "at", "loss", "at", "step", "max"], client)
    kw = _metric_call(client, "query_metrics")
    masks = cast(tuple[MetricMaskClause, ...], kw["masks"])
    assert ("step", "max", "") in {(m.axis, m.op, m.value) for m in masks}


def test_metric_read_sort_limit(client: FakeClient) -> None:
    run(
        [
            "experiment",
            "2",
            "metric",
            "at",
            "key",
            "is",
            "loss",
            "sort",
            "desc",
            "limit",
            "5",
        ],
        client,
    )
    kw = _metric_call(client, "query_metrics")
    assert kw["sort"] == "desc"
    assert kw["limit"] == 5


def test_metric_read_renders_points(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _points(
        experiment_id: uuid.UUID,
        *,
        masks: Sequence[MetricMaskClause],
        sort: str | None = None,
        limit: int | None = None,
    ) -> list[MetricPoint]:
        del experiment_id, masks, sort, limit
        return [
            MetricPoint(key="loss", step=0, value=0.9),
            MetricPoint(key="loss", step=1, value=0.5),
        ]

    monkeypatch.setattr(client, "query_metrics", _points)
    run(["experiment", "2", "metric", "at", "loss"], client)
    out = capsys.readouterr().out
    assert "loss" in out
    assert "0.9" in out
    assert "0.5" in out


def test_metric_read_empty_placeholder(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["experiment", "2", "metric"], client)  # FakeClient returns []
    assert "(no metrics)" in capsys.readouterr().out


def test_metric_read_json(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _points(
        experiment_id: uuid.UUID,
        *,
        masks: Sequence[MetricMaskClause],
        sort: str | None = None,
        limit: int | None = None,
    ) -> list[MetricPoint]:
        del experiment_id, masks, sort, limit
        return [MetricPoint(key="loss", step=0, value=0.9)]

    monkeypatch.setattr(client, "query_metrics", _points)
    run(["experiment", "2", "metric", "at", "loss", "--format", "json"], client)
    out = capsys.readouterr().out
    assert '"loss"' in out


# -- cross-experiment read / rank --------------------------------------------


def test_metric_cross_experiment_bare(client: FakeClient) -> None:
    run(["experiment", "metric", "at", "loss", "at", "step", "is", "100"], client)
    kw = _metric_call(client, "rank_metrics")
    masks = cast(tuple[MetricMaskClause, ...], kw["masks"])
    assert {(m.axis, m.op, m.value) for m in masks} == {
        ("key", "is", "loss"),
        ("step", "is", "100"),
    }
    # It ranks across the experiments the list selects, not a single ref.
    assert not [c for c in client.calls if c[0] == "query_metrics"]


def test_metric_cross_experiment_ids_are_listed_experiments(
    client: FakeClient,
) -> None:
    run(["experiment", "metric", "at", "loss", "at", "step", "is", "100"], client)
    exp_ids = cast(
        tuple[uuid.UUID, ...],
        next(c for c in client.calls if c[0] == "rank_metrics")[1][0],
    )
    listed = {
        uuid.UUID(str(r["id"])) for r in client.rows if r.get("kind") == "Experiment"
    }
    assert set(exp_ids) == listed


def test_metric_cross_experiment_sort_limit(client: FakeClient) -> None:
    run(
        [
            "experiment",
            "metric",
            "at",
            "loss",
            "at",
            "step",
            "is",
            "100",
            "sort",
            "desc",
            "limit",
            "5",
        ],
        client,
    )
    kw = _metric_call(client, "rank_metrics")
    assert kw["sort"] == "desc"
    assert kw["limit"] == 5


def test_metric_cross_experiment_filtered(client: FakeClient) -> None:
    # A list-query prefix (``label is ml``) narrows the experiment set before rank.
    run(
        [
            "experiment",
            "label",
            "is",
            "ml",
            "metric",
            "at",
            "loss",
            "at",
            "step",
            "is",
            "100",
        ],
        client,
    )
    # The prefix routes to the cross-experiment list query (not create): the
    # ``label is ml`` filter reaches ``list_kind``. No fixture experiment carries
    # that label, so the selected set is empty and no rank is issued -- that the
    # filter propagated proves the routing.
    list_calls = [c for c in client.calls if c[0] == "list_kind"]
    assert list_calls
    assert any(
        any(f.value == "ml" for f in cast(tuple[Filter, ...], c[2]["filters"]))
        for c in list_calls
    )
    assert not [c for c in client.calls if c[0] == "submit_batch"]


def test_metric_cross_experiment_step_max(client: FakeClient) -> None:
    run(["experiment", "metric", "at", "loss", "at", "step", "max"], client)
    kw = _metric_call(client, "rank_metrics")
    masks = cast(tuple[MetricMaskClause, ...], kw["masks"])
    assert ("step", "max", "") in {(m.axis, m.op, m.value) for m in masks}


def test_metric_cross_experiment_write_rejected(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="cannot write"):
        run(["experiment", "metric", "at", "step", "is", "3", "to", "0.5"], client)


def test_metric_cross_experiment_renders_rank(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exp_id = _exp_id(client)

    def _rank(
        experiment_ids: Sequence[uuid.UUID],
        *,
        masks: Sequence[MetricMaskClause],
        sort: str | None = None,
        limit: int | None = None,
    ) -> list[MetricRankRow]:
        del experiment_ids, masks, sort, limit
        return [
            MetricRankRow(
                experiment_id=exp_id,
                point=MetricPoint(key="loss", step=100, value=0.42),
            )
        ]

    monkeypatch.setattr(client, "rank_metrics", _rank)
    run(["experiment", "metric", "at", "loss", "at", "step", "is", "100"], client)
    out = capsys.readouterr().out
    assert "experiment" in out
    assert "loss" in out
    assert "0.42" in out


def test_metric_cross_experiment_empty(
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _empty(*_a: object, **_k: object) -> list[MetricRankRow]:
        return []

    monkeypatch.setattr(client, "rank_metrics", _empty)
    run(["experiment", "metric", "at", "loss"], client)
    assert "(no metrics)" in capsys.readouterr().out


# -- create + log fusion -----------------------------------------------------


def test_metric_create_and_log_fusion(client: FakeClient) -> None:
    run(
        [
            "experiment",
            "title",
            "to",
            "trm exp031",
            "metric",
            "at",
            "step",
            "is",
            "3",
            "at",
            "loss",
            "to",
            "0.5",
        ],
        client,
    )
    # The Experiment is created, THEN the metric write lands into it.
    assert [c for c in client.calls if c[0] == "submit_batch"]
    write = _metric_call(client, "write_metrics_masked")
    assert write["value"] == 0.5


def test_metric_create_and_log_needs_write(client: FakeClient) -> None:
    # A create tail with a bare (read) metric makes no sense; require a `to`.
    with pytest.raises(ClientError, match=r"create\+log requires a metric write"):
        run(
            ["experiment", "title", "to", "x", "metric", "at", "step", "is", "3"],
            client,
        )


def test_metric_create_and_log_targets_new_experiment(client: FakeClient) -> None:
    run(
        [
            "experiment",
            "title",
            "to",
            "x",
            "metric",
            "at",
            "step",
            "is",
            "3",
            "to",
            "0.5",
        ],
        client,
    )
    write_id = next(c for c in client.calls if c[0] == "write_metrics_masked")[1][0]
    # The write targets the EXACT id ``run_create``/``submit_batch`` minted for
    # the new row (FakeClient returns ``target_id`` as the batch root), not a
    # re-read of "the newest experiment" -- so a concurrent create between the
    # create and the write cannot steal the log (the misattribution fix).
    assert write_id == client.target_id
    # No "latest experiment" list lookup happens: the id is threaded through.
    assert not any(
        c[0] == "list_kind" and c[1][0] == "Experiment" for c in client.calls
    )


# -- help + gating -----------------------------------------------------------


def test_experiment_help_shows_metric_section(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["experiment", "help"], client)
    out = capsys.readouterr().out
    assert "METRIC (experiment only)" in out
    assert "at FIELD OP VALUE" in out
    assert not client.calls


def test_issue_help_omits_metric_section(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["issue", "help"], client)
    assert "METRIC" not in capsys.readouterr().out


def test_issue_metric_is_unknown_token(client: FakeClient) -> None:
    # The metric intercept is gated on kind == Experiment; other kinds reject
    # ``metric`` as an unknown token (it is not a field/edge/relation).
    with pytest.raises(ClientError):
        run(["issue", "2", "metric", "at", "loss"], client)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
