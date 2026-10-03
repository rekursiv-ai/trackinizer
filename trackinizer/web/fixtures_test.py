r"""Contract fixtures: what the server answers to each call the web app makes.

The schema types most responses as free JSON, so ``openapi_drift_test.py`` cannot
see a response change shape. This test starts the server as the end-to-end suite
does (``python -m trackinizer.server --ephemeral --no-auth``: PGlite in
memory, every request a local admin), builds a small graph through the HTTP API,
and makes every call the API modules in ``src/api/`` make. Each exchange is
normalised (ids, times, token secrets) and compared with its fixture,
``test/testdata/<module>/<function>[.<case>].json``. Screen tests can render from
the same files, so they see the server's real shapes.

After a deliberate server change, rewrite the fixtures and review their diff::

    TRACKINIZER_WEB_FIXTURES_UPDATE=1 uv --quiet run --frozen pytest -m db_pglite \
        trackinizer/web/fixtures_test.py
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import difflib
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid

import httpx2
import pytest

from trackinizer.client.client import Client
from trackinizer.lib.agent.types.sessions import AssistantMessage, ToolCall, UserMessage
from trackinizer.lib.custom_json import DictCodec, JSONValue, ListCodec, StrCodec, loads
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_metrics import MetricPoint
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody
from trackinizer.wire.wire_sessions import SessionStart


if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from trackinizer.lib.custom_json import MutableJSONValue


_CWD: Final = Path(__file__).resolve().parent
_FIXTURES: Final = _CWD / "test" / "testdata"
_UPDATE: Final = "TRACKINIZER_WEB_FIXTURES_UPDATE"
# A second admin, as playwright.config.ts seeds one: the --no-auth admin cannot
# demote, disable or delete itself, so the admin routes need someone else.
_OTHER_ADMIN: Final = "fixtures-admin@example.com"
_UUID: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
)
_TIME: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(Z|[+-]\d{2}:\d{2})",
)


@pytest.mark.cli_python_subprocess
@pytest.mark.db_pglite
def test_every_call_answers_as_its_fixture_says(tmp_path: Path) -> None:
    with _server(tmp_path) as url, httpx2.Client(base_url=url, timeout=30.0) as http:
        recorder = _Recorder(http=http)
        _exercise(recorder, url=url)
    fixtures = recorder.fixtures()
    if os.environ.get(_UPDATE) == "1":
        _rewrite(fixtures)
        pytest.skip(f"{_UPDATE}=1: rewrote {len(fixtures)} fixtures in {_FIXTURES}")
    committed = {_name(path): path.read_text() for path in _FIXTURES.rglob("*.json")}
    regenerate = (
        f"If the server change is deliberate, rerun with {_UPDATE}=1 and review "
        "the fixtures' diff."
    )
    assert sorted(committed) == sorted(fixtures), (
        f"Calls without a fixture: {sorted(set(fixtures) - set(committed))}; "
        f"fixtures without a call: {sorted(set(committed) - set(fixtures))}. "
        + regenerate
    )
    changed = [
        _diff(name, committed[name], text)
        for name, text in fixtures.items()
        if committed[name] != text
    ]
    assert not changed, f"{len(changed)} responses changed. {regenerate}\n" + "".join(
        changed,
    )


def test_normalising_ignores_random_ids_times_and_the_order_of_timed_rows() -> None:
    first_row, second_row = str(uuid.uuid4()), str(uuid.uuid4())
    rows = {first_row: 1, second_row: 2}
    # Two changes made in one millisecond, which the server orders by random id.
    one_run = [
        {
            "id": str(uuid.uuid4()),
            "subject": first_row,
            "created": "2026-09-27T13:01:53.293000Z",
        },
        {
            "id": str(uuid.uuid4()),
            "subject": second_row,
            "created": "2026-09-27T13:01:53.293000Z",
        },
    ]
    # Another run: new change ids, and the same two a millisecond apart, the
    # later first, one with no fraction, as Python writes a whole second.
    another_run = [
        {
            "id": str(uuid.uuid4()),
            "subject": second_row,
            "created": "2026-09-27T13:01:54.001000Z",
        },
        {
            "id": str(uuid.uuid4()),
            "subject": first_row,
            "created": "2026-09-27T13:01:54Z",
        },
    ]

    normalised = [
        _render(_Normaliser(rows=rows).value(run)) for run in (one_run, another_run)
    ]

    assert normalised[0] == normalised[1]
    assert loads(normalised[0]) == [
        {
            "id": "00000000-0000-4000-9000-000000000001",
            "subject": "00000000-0000-4000-8000-000000000001",
            "created": "2026-01-01T00:00:00.000000Z",
        },
        {
            "id": "00000000-0000-4000-9000-000000000002",
            "subject": "00000000-0000-4000-8000-000000000002",
            "created": "2026-01-01T00:00:00.000000Z",
        },
    ]


@dataclass(slots=True, kw_only=True)
class _Recorder:
    """Sends requests as the web app does and keeps each recorded exchange, raw."""

    http: httpx2.Client
    rows: dict[str, int] = field(default_factory=dict)
    """Each inquiry the graph made, numbered in the order it was made."""

    exchanges: dict[str, dict[str, JSONValue]] = field(default_factory=dict)

    def call(
        self,
        name: str,
        method: str,
        path: str,
        *,
        query: Sequence[tuple[str, str]] = (),
        body: JSONValue = None,
        keyed: bool = False,
    ) -> MutableJSONValue:
        """Send one request and record it as fixture ``name``; return its body.

        A keyed request carries an ``Idempotency-Key`` header, as every edit does.
        """
        headers = {"Idempotency-Key": _key(name)} if keyed else {}
        response = self.http.request(
            method,
            path,
            params=list(query),
            json=body,
            headers=headers,
        )
        parsed = _parsed(response)
        request: dict[str, JSONValue] = {"method": method, "path": path}
        if query:
            request["query"] = [list(pair) for pair in query]
        if headers:
            request["headers"] = headers
        if body is not None:
            request["body"] = body
        self.exchanges[name] = {
            "request": request,
            "response": _response(response, body=parsed),
        }
        return parsed

    def setup(
        self,
        method: str,
        path: str,
        *,
        body: JSONValue = None,
    ) -> MutableJSONValue:
        """Send one request that builds the graph; it must succeed."""
        response = self.http.request(method, path, json=body)
        assert response.is_success, (
            f"{method} {path}: {response.status_code} {response.text}"
        )
        return _parsed(response)

    def row(self, raw: str) -> str:
        """Give the inquiry ``raw``, just made, the next number; return ``raw``."""
        self.rows.setdefault(raw, len(self.rows) + 1)
        return raw

    def fixtures(self) -> dict[str, str]:
        """Every exchange, normalised and rendered, by fixture name."""
        return {
            name: _render(_Normaliser(rows=self.rows).value(exchange))
            for name, exchange in sorted(self.exchanges.items())
        }


@dataclass(slots=True, kw_only=True)
class _Normaliser:
    """Rewrites what differs from run to run into stable values of the same type.

    An inquiry the graph made keeps one id in every fixture, ``...-8000-<n>`` for
    the n-th made; any other UUID (a change, a user, a token) is numbered by its
    first appearance in one fixture, ``...-9000-<n>``. Every time becomes one
    instant, keeping the server's zone suffix. A token's secret and prefix become
    fixed strings. Seqs stay: a fresh in-memory database numbers rows in the
    order the graph makes them.
    """

    rows: Mapping[str, int]
    others: dict[str, str] = field(default_factory=dict)

    def value(self, raw: JSONValue) -> JSONValue:
        """Return ``raw`` normalised, walking objects in key order."""
        if isinstance(raw, str):
            return self._text(raw)
        if isinstance(raw, Mapping):
            return {
                self._text(key): f"<{key}>"
                if key in {"secret", "prefix"} and isinstance(member, str)
                else self.value(member)
                for key, member in sorted(raw.items())
            }
        if isinstance(raw, list | tuple):
            return [self.value(item) for item in _ordered(raw, rows=self.rows)]
        return raw

    def _text(self, text: str) -> str:
        return _UUID.sub(self._uuid, _TIME.sub(r"2026-01-01T00:00:00.000000\1", text))

    def _uuid(self, match: re.Match[str]) -> str:
        raw = match.group()
        if raw in self.rows:
            return f"00000000-0000-4000-8000-{self.rows[raw]:012d}"
        return self.others.setdefault(
            raw,
            f"00000000-0000-4000-9000-{len(self.others) + 1:012d}",
        )


# The server orders a row's relations by peer id, and rows and changes by the time they
# were made, then id. Ids are random, and PGlite keeps times to the millisecond, so
# changes made together came back in either order from run to run. Sorting only the tied
# ones would still differ between a run where two rows tied and one where they did not.
# The graph's edges (``from_id``) come in no order at all. Lists of anything else
# (metric points, transcript records, validation errors) keep the server's order.
def _ordered(items: Sequence[JSONValue], *, rows: Mapping[str, int]) -> list[JSONValue]:
    """Put a list of rows in an order by content, since the server's rests on ids."""
    if not any(
        isinstance(item, Mapping)
        and not {"id", "created", "created_at", "added_at", "from_id"}.isdisjoint(item)
        for item in items
    ):
        return list(items)
    return sorted(items, key=lambda item: _render(_Normaliser(rows=rows).value(item)))


def _exercise(rec: _Recorder, *, url: str) -> None:
    """Build the graph, then make each call the API modules make, in turn."""
    for name, path in (
        ("meta/getEnums", "/api/meta/enums"),
        ("meta/getFieldOwners", "/api/meta/fields"),
        ("meta/getEdgeTopology", "/api/meta/edges"),
        ("me/getProfile", "/api/me/profile"),
    ):
        _ = rec.call(name, "GET", path)
    graph = _build_graph(rec)
    graph |= _add_evidence(rec, graph, url=url)
    _write_fields(rec, graph)
    _write_edges(rec, graph)
    _read_lists(rec)
    _read_rows(rec, graph)
    _account(rec)
    _admin(rec)
    # Last: it renames the root, which a read after it would show.
    _stream(rec, target=graph["root"])


def _build_graph(rec: _Recorder) -> dict[str, str]:
    """Make Issues with parents and a prerequisite, and a Belief with evidence."""
    root = _create(rec, "issue", title="Web app fixtures", priority=10, owner="dan")
    prereq = _create(rec, "issue", title="Prerequisite", priority=20)
    child = _id(
        rec.call(
            "inquiries/createInquiry",
            "POST",
            "/api/inquiries/issue",
            body={
                "title": "Child",
                "description": "Narrows **Issue#1** and requires Issue#2.",
                "priority": 0,
                "issue_kind": ["bug"],
                "narrows": [[root, 0]],
                "requires": [prereq],
                "subscribers": ["Agent"],
                "labels": ["fixture"],
                "idempotency_key": _key("inquiries/createInquiry"),
            },
        ),
    )
    _ = rec.row(child)
    question = _create(
        rec,
        "issue",
        title="Which sort?",
        issue_kind=["question"],
        owner="dan",
        subscribers=["Agent"],
    )
    paper = _create(
        rec,
        "paper",
        title="Incremental layout",
        authors=["Ada Lovelace", "Alan Turing"],
        venue="UIST",
        publish_date="2024-05-01",
        source="https://example.com/paper",
    )
    notes = _create(rec, "artifact", title="Probe notes")
    belief = _create(
        rec,
        "belief",
        title="A membership check holds at most 13 ids",
        judgement="unproven",
        confidence=0.7,
        proved_by=[{"artifact_id": paper, "artifact_kind": "Paper", "valence": 0.8}],
        favored_by=[
            {"artifact_id": notes, "artifact_kind": "Artifact", "valence": -0.4},
        ],
    )
    purged = _create(rec, "issue", title="Purged")
    return {
        "root": root,
        "prereq": prereq,
        "child": child,
        "question": question,
        "belief": belief,
        "purged": purged,
    }


def _add_evidence(
    rec: _Recorder,
    graph: Mapping[str, str],
    *,
    url: str,
) -> dict[str, str]:
    """Add an Experiment with metrics, an answer, and a session with a transcript."""
    experiment = _create(
        rec,
        "experiment",
        title="Membership check latency",
        outcome="0.19 to 0.23 s",
        config={"ids": 13},
    )
    _ = rec.setup(
        "POST",
        f"/api/edges/{experiment}/produced_by/{graph['child']}",
        body={},
    )
    _ = rec.setup(
        "POST",
        f"/api/edges/{experiment}/proves/{graph['belief']}",
        body={"valence": 0.6, "note": "Measured on PGlite."},
    )
    answer = rec.call(
        "inquiries/createBatch",
        "POST",
        "/api/inquiries/batch",
        body={
            "items": [
                {
                    "kind": "Artifact",
                    "title": "Answer: newest first",
                    "subscribers": ["Agent"],
                    "idempotency_key": _key("inquiries/createBatch"),
                },
            ],
            "edges": [
                {
                    "from_index": 0,
                    "to_id": graph["question"],
                    "edge_kind": "produced_by",
                },
            ],
        },
    )
    for made in ListCodec.coerce(DictCodec.coerce(answer).get("ids"), str):
        _ = rec.row(made)
    with Client(url) as client:
        client.log_metrics(
            uuid.UUID(experiment),
            [
                MetricPoint(key="latency_s", step=step, value=value)
                for step, value in enumerate((0.23, 0.21, 0.19))
            ],
        )
        session = client.session_start(
            SessionStart(
                cli="claude",
                title="Fixture session",
                actor="Agent",
                idempotency_key=_uuid_key("session"),
            ),
        )
        turns = (
            UserMessage(content="What does **Issue#1** ask for?"),
            ToolCall(call_id="c1", name="Bash", arguments={"command": "trax issue 1"}),
            AssistantMessage(content="Fixtures for the web app."),
        )
        _ = client.append_records(
            session.id,
            name="main.jsonl",
            manifest=ManifestBody(
                name="main.jsonl",
                metadata={},
                ir_id=_uuid_key("transcript"),
                format="claude",
                records=len(turns),
            ),
            records=[
                RecordBody.of(
                    SessionRecordRow.of(
                        session_id=session.id,
                        part=0,
                        idx=idx,
                        record=turn,
                    ),
                )
                for idx, turn in enumerate(turns)
            ],
        )
    return {"experiment": experiment, "session": rec.row(str(session.id))}


def _write_fields(rec: _Recorder, graph: Mapping[str, str]) -> None:
    """Set, compare-and-set, clear and patch fields; purge a row."""
    child, prereq, purged = graph["child"], graph["prereq"], graph["purged"]
    for name, method, path, body in (
        (
            "inquiries/setField",
            "PUT",
            f"/api/inquiries/{child}/title",
            {"value": "Child, renamed"},
        ),
        (
            "inquiries/setField.cas",
            "PUT",
            f"/api/inquiries/{prereq}/status",
            {
                "value": "complete",
                "mode": "cas",
                "expected": "active",
                "reason": "Done.",
            },
        ),
        (
            "inquiries/setField.conflict",
            "PUT",
            f"/api/inquiries/{prereq}/status",
            {"value": "abandoned", "mode": "cas", "expected": "active"},
        ),
        (
            "inquiries/setField.invalid",
            "PUT",
            f"/api/issue/{child}/priority",
            {"value": "high"},
        ),
        ("inquiries/clearField", "DELETE", f"/api/inquiries/{child}/description", {}),
        (
            "inquiries/patchField",
            "PATCH",
            f"/api/inquiries/{child}/labels",
            {"op": "add", "value": "ui"},
        ),
        (
            "inquiries/purgeInquiry",
            "DELETE",
            f"/api/inquiries/{purged}",
            {"reason": "Made to be purged."},
        ),
    ):
        _ = rec.call(name, method, path, body=body, keyed=True)
    _ = rec.call("inquiries/getInquiry.purged", "GET", f"/api/inquiries/{purged}")


def _write_edges(rec: _Recorder, graph: Mapping[str, str]) -> None:
    """Add an edge, annotate it, and remove another."""
    edge = f"/api/edges/{graph['child']}/requires/{graph['question']}"
    for name, method, path, body in (
        ("edges/addEdge", "POST", edge, {}),
        (
            "edges/setEdgeAnnotation",
            "PUT",
            f"{edge}/note",
            {"value": "Blocks the sort."},
        ),
        ("edges/clearEdgeAnnotation", "DELETE", f"{edge}/note", {}),
        (
            "edges/patchEdgeLabels",
            "PATCH",
            f"{edge}/labels",
            {"op": "add", "value": "fixture"},
        ),
    ):
        _ = rec.call(name, method, path, body=body, keyed=True)
    removed = f"/api/edges/{graph['question']}/narrows/{graph['root']}"
    _ = rec.setup("POST", removed, body={})
    _ = rec.call("edges/removeEdge", "DELETE", removed, body={}, keyed=True)


def _read_lists(rec: _Recorder) -> None:
    """List rows, one kind and every kind, and search, the change log and the graph."""
    active = json.dumps(
        {"field": "status", "op": "is", "value": "active"},
        separators=(",", ":"),
    )
    kinds = ListCodec.coerce(
        DictCodec.coerce(rec.setup("GET", "/api/meta/enums")).get("inquiry_kind_all"),
        str,
    )
    # The quarter hour that held the time an hour ago: the histogram reads only the
    # last 7 days, and its window must end before the fixture session began.
    ago = datetime.now(UTC) - timedelta(hours=1)
    quarter = ago - timedelta(
        minutes=ago.minute % 15,
        seconds=ago.second,
        microseconds=ago.microsecond,
    )
    for name, path, query in (
        (
            "inquiries/listInquiries",
            "/api/inquiries",
            [("kind", "Issue"), ("filter", active), ("limit", "50"), ("offset", "0")],
        ),
        (
            "inquiries/listInquiries.everyKind",
            "/api/inquiries",
            [*(("kind", kind) for kind in kinds), ("limit", "20"), ("offset", "0")],
        ),
        (
            "inquiries/listInquiriesBySeq",
            "/api/inquiries",
            [("kind", "Issue"), ("seq_range", "1..2"), ("limit", "50")],
        ),
        (
            # An Issue key, a Belief key and an edge list: each row keeps the
            # named keys its kind has.
            "inquiries/listInquiries.fields",
            "/api/inquiries",
            [
                ("kind", "Issue"),
                ("kind", "Belief"),
                *(
                    ("fields", name)
                    for name in ("id", "kind", "seq", "title", "priority", "judgement")
                ),
                ("fields", "proved_by"),
                ("limit", "50"),
                ("offset", "0"),
            ],
        ),
        (
            # The child narrows the root, so it alone has an ancestor.
            "inquiries/listInquiries.ancestors",
            "/api/inquiries",
            [
                ("kind", "Issue"),
                *(("fields", name) for name in ("id", "seq", "title")),
                ("ancestors", "narrows"),
                ("limit", "50"),
                ("offset", "0"),
            ],
        ),
        (
            "inquiries/listInquiries.roots",
            "/api/inquiries",
            [
                ("kind", "Issue"),
                ("filter", json.dumps({"field": "narrows", "op": "isnull"})),
                *(("fields", name) for name in ("id", "seq", "title")),
                ("limit", "50"),
                ("offset", "0"),
            ],
        ),
        (
            "search/searchInquiries",
            "/api/web/search",
            [("q", "Child"), ("kind", "Issue"), ("limit", "5")],
        ),
        (
            "search/searchInquiries.fields",
            "/api/web/search",
            [
                ("q", "Child"),
                ("kind", "Issue"),
                ("limit", "5"),
                *(("fields", name) for name in ("id", "kind", "seq", "title")),
            ],
        ),
        (
            "search/searchInquiries.badQuery",
            "/api/web/search",
            [("q", 'title:"unclosed'), ("kind", "Issue"), ("limit", "5")],
        ),
        (
            "changes/listChanges",
            "/api/change_log",
            [("kind", "status"), ("limit", "50")],
        ),
        (
            # The child's description, cleared: the old text cut, the new side
            # unset and so absent.
            "changes/listChanges.brief",
            "/api/change_log",
            [("kind", "description"), ("limit", "50"), ("brief", "true")],
        ),
        (
            # Two kinds in one request, as an Activity tab reads them.
            "changes/listChanges.kinds",
            "/api/change_log",
            [
                ("kind", "status"),
                ("kind", "description"),
                ("limit", "50"),
                ("brief", "true"),
            ],
        ),
        ("graph/getGraph", "/api/web/graph", [("limit", "1000")]),
        # The whole feed: the fixture session's three turns, two of them said.
        ("sessions/readFeedFacets", "/api/web/feed/facets", []),
        (
            # Four quarter hours, all empty; times written as the app writes them.
            "histogram/readHistogram",
            "/api/web/feed/histogram",
            [
                ("kind", "ToolCall"),
                ("since", f"{quarter:%Y-%m-%dT%H:%M:%SZ}"),
                ("until", f"{quarter + timedelta(minutes=45):%Y-%m-%dT%H:%M:%SZ}"),
                ("buckets", "4"),
            ],
        ),
    ):
        _ = rec.call(name, "GET", path, query=query)


def _read_rows(rec: _Recorder, graph: Mapping[str, str]) -> None:
    """Read one row each way: by id, by seq, as a detail, and its evidence."""
    session = graph["session"]
    for name, path, query in (
        ("inquiries/getInquiry", f"/api/inquiries/{graph['child']}", ()),
        ("detail/findRef", "/api/inquiries/Issue/1", ()),
        ("detail/getDetail", f"/api/web/get/{graph['child']}", ()),
        ("detail/getDetail.belief", f"/api/web/get/{graph['belief']}", ()),
        (
            # The child's neighbourhood: its parents, the question it requires,
            # and their own neighbours, each with its distance.
            "graph/getGraph.focus",
            "/api/web/graph",
            (("focus", graph["child"]), ("hops", "2"), ("limit", "60")),
        ),
        (
            "detail/getEvidenceConfidence",
            f"/api/inquiries/{graph['belief']}/confidence",
            (),
        ),
        (
            "metrics/readMetrics",
            f"/api/experiments/{graph['experiment']}/metrics",
            (("limit", "1000"),),
        ),
        ("sessions/listSessionParts", f"/api/sessions/{session}/parts", ()),
        (
            "sessions/readSessionRecords",
            f"/api/sessions/{session}/records",
            (
                ("part", "0"),
                ("after_idx", "-1"),
                ("limit", "200"),
                ("plaintext_only", "true"),
            ),
        ),
    ):
        _ = rec.call(name, "GET", path, query=query)


def _account(rec: _Recorder) -> None:
    """Make, list, change and revoke a token, then sign out."""
    token = _id(
        rec.call(
            "me/createToken",
            "POST",
            "/api/me/tokens",
            body={"name": "fixtures", "role": "viewer"},
        ),
    )
    _ = rec.call("me/listTokens", "GET", "/api/me/tokens")
    _ = rec.call(
        "me/setTokenRole",
        "PUT",
        f"/api/me/tokens/{token}/role",
        body={"role": "writer"},
    )
    _ = rec.call("me/revokeToken", "POST", f"/api/me/tokens/{token}/revoke")
    _ = rec.call("me/signOut", "POST", "/auth/logout")


def _admin(rec: _Recorder) -> None:
    """Change the other admin's role and status, edit the allowlist, delete the user."""
    users = ListCodec.mappings(
        DictCodec.coerce(rec.call("admin/listUsers", "GET", "/api/admin/users")).get(
            "users",
        ),
    )
    user = f"/api/admin/users/{next(_id(u) for u in users if u.get('email') == _OTHER_ADMIN)}"
    entry = "/api/admin/allowlist/ada%40example.com"
    added = {"email_or_pattern": " Ada@Example.com ", "role": "viewer"}
    for name, method, path, body in (
        ("admin/setUserRole", "PUT", f"{user}/role", {"role": "writer"}),
        ("admin/disableUser", "POST", f"{user}/disable", None),
        ("admin/enableUser", "POST", f"{user}/enable", None),
        ("admin/addAllowlistEntry", "POST", "/api/admin/allowlist", added),
        ("admin/addAllowlistEntry.duplicate", "POST", "/api/admin/allowlist", added),
        ("admin/listAllowlist", "GET", "/api/admin/allowlist", None),
        ("admin/setAllowlistRole", "PUT", f"{entry}/role", {"role": "writer"}),
        ("admin/removeAllowlistEntry", "DELETE", entry, None),
        ("admin/deleteUser", "DELETE", user, None),
    ):
        _ = rec.call(name, method, path, body=body)


# One rename, after ``open``: the server listens before it says so, and the client's
# live layer counts on that. A change it dropped would leave this waiting on a frame
# that never comes.
def _stream(rec: _Recorder, *, target: str) -> None:
    """Record the frame the change stream sends when ``target`` changes."""
    with rec.http.stream("GET", "/api/web/subscribe") as response:
        lines = response.iter_lines()
        assert next(lines) == ": open"
        _ = rec.setup(
            "PUT",
            f"/api/inquiries/{target}/title",
            body={"value": "Streamed"},
        )
        frame = next(line for line in lines if target in line)
        rec.exchanges["stream/openStream"] = {
            "request": {"method": "GET", "path": "/api/web/subscribe"},
            "response": _response(response, body=frame),
        }


def _create(rec: _Recorder, kind: str, **body: JSONValue) -> str:
    """Create one ``kind`` row as setup, labelled ``fixture``; return its id."""
    return rec.row(
        _id(
            rec.setup(
                "POST",
                f"/api/inquiries/{kind}",
                body={"labels": ["fixture"], **body},
            ),
        ),
    )


def _id(body: object) -> str:
    return StrCodec.coerce(DictCodec.coerce(body).get("id"), default=None)


def _key(name: str) -> str:
    """Return fixture ``name``'s idempotency key, the same in every run."""
    return str(_uuid_key(name))


def _uuid_key(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"trackinizer-web-app-fixtures/{name}")


def _parsed(response: httpx2.Response) -> MutableJSONValue:
    """Return the body as JSON when the server says it is, else as text."""
    if "json" in response.headers.get("content-type", ""):
        return loads(response.text)
    return response.text


def _response(response: httpx2.Response, *, body: JSONValue) -> dict[str, JSONValue]:
    headers = {
        name: response.headers[name]
        for name in ("content-type", "location")
        if name in response.headers
    }
    return {"status": response.status_code, "headers": headers, "body": body}


def _render(value: JSONValue) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _name(path: Path) -> str:
    return path.relative_to(_FIXTURES).with_suffix("").as_posix()


def _diff(name: str, committed: str, current: str) -> str:
    return "".join(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile=f"fixture {name}",
            tofile=f"server {name}",
        ),
    )[:4000]


def _rewrite(fixtures: Mapping[str, str]) -> None:
    """Write every fixture and delete the ones no call makes any more."""
    for stale in _FIXTURES.rglob("*.json"):
        if _name(stale) not in fixtures:
            stale.unlink()
    for name, text in fixtures.items():
        path = _FIXTURES / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text)


@contextmanager
def _server(tmp_path: Path) -> Generator[str]:
    """Run the server as the end-to-end suite does, on a free port; yield its URL."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    # The caller's own TRACKINIZER_* settings, such as a session embedder, would
    # change what the server answers.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TRACKINIZER_")
    }
    env |= {
        "TRACKINIZER_BOOTSTRAP_ADMIN": _OTHER_ADMIN,
        "TRACKINIZER_BOOTSTRAP_TOKEN_FILE": str(tmp_path / "bootstrap-token"),
    }
    log = tmp_path / "server.log"
    command = [
        sys.executable,
        "-m",
        "trackinizer.server",
        "--ephemeral",
        "--no-auth",
        "--port",
        str(port),
    ]
    with (
        log.open("w") as sink,
        subprocess.Popen(  # noqa: S603 -- fixed interpreter, module path and flags.
            command,
            cwd=_CWD.parents[2],
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
        ) as server,
    ):
        try:
            _wait_for(url, server=server, log=log)
            yield url
        finally:
            server.terminate()


def _wait_for(url: str, *, server: subprocess.Popen[bytes], log: Path) -> None:
    """Return once the server answers; fail with its log if it exits or stalls."""
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        if server.poll() is not None:
            pytest.fail(
                f"The server exited with {server.returncode}:\n{log.read_text()}",
            )
        with suppress(httpx2.TransportError):
            if httpx2.get(f"{url}/api/meta/enums", timeout=1.0).is_success:
                return
        time.sleep(0.1)
    pytest.fail(f"The server did not answer within 90 s:\n{log.read_text()}")


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return cast(tuple[str, int], sock.getsockname())[1]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
