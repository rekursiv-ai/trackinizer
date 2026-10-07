"""Report publication rejects misleading or unbounded evidence drafts."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Self, cast
from unittest.mock import AsyncMock

import hashlib
import json
import re
import uuid

from pydantic import ValidationError

import pytest

from trackinizer.conftest import make_store, queue_field_rows
from trackinizer.lib.codec import from_plain
from trackinizer.lib.postgres import Conn
from trackinizer.server.store.change_id_slot import (
    _peek_client_change_id,
    set_client_change_id,
)
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.reports import (
    ArtifactCitationRef,
    ArtifactContentConflictError,
    ArtifactContentRevision,
    ArtifactFindingDraft,
    PublishArtifactContent,
    publish_artifact_content,
    read_artifact_content,
    read_artifact_content_on_conn,
)
from trackinizer.wire.bodies import SubmitArtifact


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


_ACTIVE: Final = "SELECT 1 FROM users WHERE id = $1 AND status = 'active' FOR UPDATE"
_REPLAY: Final = (
    "SELECT revisions.report_id, revisions.revision, revisions.artifact_id, "
    "revisions.content, revisions.created_at, revisions.request_hash, "
    "revisions.author_email AS author, reports.issue_id "
    "FROM visual_report_revisions AS revisions "
    "JOIN visual_reports AS reports ON reports.id = revisions.report_id "
    "WHERE revisions.author_id = $1 AND revisions.publish_key = $2"
)
_ISSUE: Final = "SELECT kind FROM inquiries WHERE id = $1 FOR SHARE"
_NEW_REPORT: Final = (
    "INSERT INTO visual_reports (id, issue_id, created_by) VALUES ($1, $2, $3)"
)
_PREVIOUS: Final = (
    "SELECT reports.id, reports.issue_id FROM visual_reports AS reports "
    "JOIN visual_report_revisions AS revisions "
    "ON revisions.report_id = reports.id "
    "WHERE revisions.artifact_id = $1 FOR UPDATE OF reports"
)
_USED: Final = (
    "SELECT coalesce(sum(content_bytes), 0)::bigint "
    "FROM visual_report_revisions WHERE author_id = $1"
)
_NEXT_REVISION: Final = (
    "SELECT coalesce(max(revision), 0) + 1 "
    "FROM visual_report_revisions WHERE report_id = $1"
)
_CAUSE: Final = "SELECT id FROM change_log WHERE subject_id = $1 AND kind = 'created'"
_INSERT_REVISION: Final = (
    "INSERT INTO visual_report_revisions "
    "(report_id, revision, artifact_id, author_id, author_email, content, "
    "content_bytes, publish_key, request_hash) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
    "RETURNING report_id, revision, artifact_id, content, created_at"
)
_RECORD: Final = "SELECT kind, seq, title FROM inquiries WHERE id = $1"
_EDGE: Final = (
    "SELECT valence, note FROM edges WHERE from_id = $1 "
    "AND to_id = $2 AND edge_kind = $3"
)
_READ: Final = (
    "SELECT revisions.report_id, revisions.revision, revisions.artifact_id, "
    "CASE WHEN $2 THEN revisions.content ELSE revisions.content - 'html' END "
    "AS content, revisions.created_at, "
    "revisions.author_email AS author, "
    "reports.issue_id FROM visual_report_revisions AS revisions "
    "JOIN visual_reports AS reports ON reports.id = revisions.report_id "
    "WHERE revisions.artifact_id = $1"
)
_AUTHOR: Final = "publisher@example.com"
_CREATED_AT: Final = datetime(2026, 10, 1, tzinfo=UTC)
_PLAIN_CITATION: Final[dict[str, object]] = {
    "claim_id": None,
    "claim_kind": None,
    "claim_seq": None,
    "claim_title": None,
    "edge_kind": None,
    "valence": None,
    "note": None,
}


def _finding() -> dict[str, object]:
    return {
        "claim": "Wider features improve score",
        "outcome": {"result": "12 wins", "denominator": 16, "split": "held-out"},
        "uncertainty": "Small sample.",
        "citations": [{"record_id": str(uuid.uuid4())}],
    }


def _draft() -> dict[str, object]:
    return {
        "issue_id": str(uuid.uuid4()),
        "title": "ARC3 directions",
        "summary": "One measured outcome.",
        "format": "structured",
        "sections": [
            {
                "title": "Scaling",
                "summary": "Measured on the held-out split.",
                "details": "Matched runs.",
                "findings": [_finding()],
            },
        ],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [("title", "   "), ("summary", "   ")],
)
def test_blank_report_heading_is_rejected(field: str, value: str) -> None:
    """Publication needs a visible title and summary."""
    with pytest.raises(ValidationError):
        PublishArtifactContent.model_validate({**_draft(), field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("claim", "   "),
        ("uncertainty", ""),
        (
            "outcome",
            {
                "result": "12 wins",
                "denominator": 9_007_199_254_740_993,
                "split": "held-out",
            },
        ),
    ],
)
def test_unreadable_finding_is_rejected(field: str, value: object) -> None:
    """The browser must display a claim, uncertainty, and exact denominator."""
    with pytest.raises(ValidationError):
        ArtifactFindingDraft.model_validate({**_finding(), field: value})


def test_finding_requires_cited_evidence() -> None:
    """A published conclusion must link at least one graph reference."""
    with pytest.raises(ValidationError):
        ArtifactFindingDraft.model_validate({**_finding(), "citations": []})


def test_measured_outcome_requires_positive_denominator() -> None:
    """A zero denominator cannot describe a measured result."""
    with pytest.raises(ValidationError):
        ArtifactFindingDraft.model_validate(
            {
                **_finding(),
                "outcome": {"result": "0 wins", "denominator": 0, "split": "held-out"},
            },
        )


def test_historical_zero_denominator_report_remains_readable() -> None:
    """An old immutable revision must survive stricter rules for new drafts."""
    report = ArtifactContentRevision.model_validate(
        {
            "revision": 1,
            "artifact_id": uuid.uuid4(),
            "issue_id": uuid.uuid4(),
            "title": "Earlier direction",
            "summary": "No measured trials.",
            "author": "researcher@example.com",
            "created_at": "2026-09-01T00:00:00Z",
            "format": "structured",
            "html": None,
            "citations": [],
            "sections": [
                {
                    "title": "Attempted direction",
                    "summary": "Trial did not start.",
                    "details": "Historical record.",
                    "findings": [
                        {
                            "claim": "No result",
                            "outcome": {
                                "result": "0 trials",
                                "denominator": 0,
                                "split": "unrun",
                            },
                            "uncertainty": "No estimate.",
                            "citations": [],
                        },
                    ],
                },
            ],
        },
    )
    assert report.sections[0].findings[0].outcome.denominator == 0


def test_html_limit_counts_utf8_bytes() -> None:
    """Unicode cannot bypass the upload byte budget."""
    with pytest.raises(ValidationError):
        PublishArtifactContent.model_validate(
            {
                "issue_id": str(uuid.uuid4()),
                "title": "Large atlas",
                "summary": "Visual report.",
                "format": "html",
                "html": "😀" * 7_500_001,
            },
        )


@pytest.mark.asyncio
async def test_read_artifact_content_returns_the_stored_revision_or_none() -> None:
    """A read resolves one exact revision, with its HTML, on one connection."""
    store, engine = make_store()
    artifact_id = uuid.uuid4()
    queue_field_rows(
        engine.conn,
        {
            "report_id": uuid.uuid4(),
            "revision": 2,
            "artifact_id": artifact_id,
            "content": {
                "title": "Site",
                "summary": "A published site.",
                "format": "html",
                "html": "<h1>Hi</h1>",
                "sections": [],
                "citations": [],
            },
            "created_at": "2026-10-01T00:00:00Z",
            "author": "publisher@example.com",
            "issue_id": uuid.uuid4(),
        },
        None,
    )
    revision = await read_artifact_content(store, artifact_id)
    assert revision is not None
    assert (revision.revision, revision.html) == (2, "<h1>Hi</h1>")
    sql, *params = engine.conn.fetchrow.call_args.args
    assert sql == _READ
    assert params == [artifact_id, True]
    assert await read_artifact_content(store, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_read_without_html_keeps_metadata_and_nulls_html() -> None:
    """A workspace read skips the HTML body but keeps every other field."""
    artifact_id = uuid.uuid4()
    issue_id = uuid.uuid4()
    content: dict[str, object] = {
        "title": "Site",
        "summary": "A published site.",
        "format": "html",
        "sections": [],
        "citations": [],
    }
    row = {
        "report_id": uuid.uuid4(),
        "revision": 4,
        "artifact_id": artifact_id,
        "content": content,
        "created_at": _CREATED_AT,
        "author": "publisher@example.com",
        "issue_id": issue_id,
    }
    db = _Db(answers={_READ: [row]})
    revision = await read_artifact_content_on_conn(
        cast(Conn, db.conn),
        artifact_id,
        include_html=False,
    )
    assert revision == ArtifactContentRevision.model_validate(
        {
            **content,
            "html": None,
            "revision": 4,
            "artifact_id": artifact_id,
            "issue_id": issue_id,
            "author": "publisher@example.com",
            "created_at": _CREATED_AT,
        },
    )
    assert db.calls == [("fetchrow", _READ, (artifact_id, False))]


def test_nested_citations_have_a_global_limit() -> None:
    """An otherwise small request cannot trigger thousands of graph reads."""
    finding = {**_finding(), "citations": [{"record_id": str(uuid.uuid4())}] * 16}
    section = {
        "title": "Direction",
        "summary": "Measured outcome.",
        "details": "Details.",
        "findings": [finding] * 9,
    }
    with pytest.raises(ValidationError, match="128 citations"):
        PublishArtifactContent.model_validate({**_draft(), "sections": [section]})


@pytest.mark.asyncio
async def test_first_revision_creates_report_artifact_and_issue_edge() -> None:
    """A first publish writes one report, Artifact, edge, and exact revision."""
    run = _Run()
    record_id = uuid.uuid4()
    body = _html_body(run, citations=[ArtifactCitationRef(record_id=record_id)])
    run.db.answers[_RECORD] = [{"kind": "Paper", "seq": 3, "title": "Source"}]
    # A key left by an earlier submit must not name this Artifact's creation.
    set_client_change_id(uuid.uuid4())
    revision = await run.publish(body)
    assert _peek_client_change_id() is None

    report_id = run.db.calls[4][2][0]
    assert isinstance(report_id, uuid.UUID)
    citation = {
        **_PLAIN_CITATION,
        "record_id": str(record_id),
        "kind": "Paper",
        "seq": 3,
        "title": "Source",
    }
    content = {
        "title": "Atlas é",
        "summary": "One direction.",
        "format": "html",
        "html": "<h1>Atlas</h1>",
        "sections": [],
        "citations": [citation],
    }
    content_bytes = len(json.dumps(content, ensure_ascii=False).encode())
    request_hash = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    assert run.db.calls == [
        ("execute", "BEGIN", ()),
        ("fetchval", _ACTIVE, (run.user_id,)),
        ("fetchrow", _REPLAY, (run.user_id, run.key)),
        ("fetchval", _ISSUE, (run.issue_id,)),
        ("execute", _NEW_REPORT, (report_id, run.issue_id, run.user_id)),
        ("fetchrow", _RECORD, (record_id,)),
        ("fetchval", _USED, (run.user_id,)),
        ("fetchval", _NEXT_REVISION, (report_id,)),
        ("fetchval", _CAUSE, (run.store.artifact_id,)),
        (
            "fetchrow",
            _INSERT_REVISION,
            (
                report_id,
                1,
                run.store.artifact_id,
                run.user_id,
                _AUTHOR,
                content,
                content_bytes,
                run.key,
                request_hash,
            ),
        ),
        ("execute", "COMMIT", ()),
    ]
    assert run.store.submitted == [
        (
            SubmitArtifact(
                title="Atlas é (revision 1)",
                description="One direction.",
                status="complete",
                account=_AUTHOR,
            ),
            {"api_key_id": run.api_key_id, "actor": _AUTHOR, "conn": run.db.conn},
        ),
    ]
    assert run.store.edges == [run.edge(to_id=run.issue_id, edge_kind="produced_by")]
    assert revision == ArtifactContentRevision.model_validate(
        {
            **content,
            "revision": 1,
            "artifact_id": run.store.artifact_id,
            "issue_id": run.issue_id,
            "author": _AUTHOR,
            "created_at": _CREATED_AT,
        },
    )


@pytest.mark.asyncio
async def test_next_revision_appends_to_report_and_supersedes_previous() -> None:
    """A revision reuses the report, numbers itself, and links its predecessor."""
    run = _Run()
    previous_id = uuid.uuid4()
    report_id = uuid.uuid4()
    run.db.answers[_PREVIOUS] = [{"id": str(report_id), "issue_id": run.issue_id}]
    run.db.answers[_NEXT_REVISION] = [3]
    revision = await run.publish(_html_body(run, previous_artifact_id=previous_id))

    assert revision.revision == 3
    assert run.store.submitted[0][0].title == "Atlas é (revision 3)"
    assert [call[:2] for call in run.db.calls[3:6]] == [
        ("fetchval", _ISSUE),
        ("fetchrow", _PREVIOUS),
        ("fetchval", _USED),
    ]
    assert run.db.calls[4][2] == (previous_id,)
    assert run.db.calls[6] == ("fetchval", _NEXT_REVISION, (report_id,))
    assert run.db.calls[8][2][:2] == (report_id, 3)
    assert run.store.edges == [
        run.edge(to_id=run.issue_id, edge_kind="produced_by"),
        run.edge(to_id=previous_id, edge_kind="supersedes"),
    ]


@pytest.mark.asyncio
async def test_structured_content_freezes_cited_rows_and_signed_edges() -> None:
    """Each distinct source-edge pair is read once and frozen with its edge."""
    run = _Run()
    record_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    plain = ArtifactCitationRef(record_id=record_id)
    favors = ArtifactCitationRef(
        record_id=record_id,
        claim_id=claim_id,
        edge_kind="favors",
    )
    proves = ArtifactCitationRef(
        record_id=record_id,
        claim_id=claim_id,
        edge_kind="proves",
    )
    outcome = {"result": "12 wins", "denominator": 16, "split": "held-out"}
    body = PublishArtifactContent.model_validate(
        {
            "issue_id": run.issue_id,
            "title": "Directions",
            "summary": "Measured.",
            "format": "structured",
            "citations": [plain, favors],
            "sections": [
                {
                    "title": "Scaling",
                    "summary": "Held-out split.",
                    "details": "Matched runs.",
                    "findings": [
                        {
                            "claim": "Wider helps",
                            "outcome": outcome,
                            "uncertainty": "Small sample.",
                            "citations": [plain, proves, favors],
                        },
                    ],
                },
            ],
        },
    )
    paper = {"kind": "Paper", "seq": 3, "title": "P" * 2_001}
    belief = {"kind": "Belief", "seq": 5, "title": "B" * 2_001}
    run.db.answers[_RECORD] = [paper, paper, belief, paper, belief]
    run.db.answers[_EDGE] = [
        {"valence": 0.75, "note": "n" * 4_000},
        {"valence": -1, "note": None},
    ]
    await run.publish(body)

    reads = [call[1:] for call in run.db.calls if call[1] in {_RECORD, _EDGE}]
    assert reads == [
        (_RECORD, (record_id,)),
        (_RECORD, (record_id,)),
        (_RECORD, (claim_id,)),
        (_EDGE, (record_id, claim_id, "favors")),
        (_RECORD, (record_id,)),
        (_RECORD, (claim_id,)),
        (_EDGE, (record_id, claim_id, "proves")),
    ]
    source = {
        **_PLAIN_CITATION,
        "record_id": str(record_id),
        "kind": "Paper",
        "seq": 3,
        "title": "P" * 2_000,
    }
    signed = {
        **source,
        "claim_id": str(claim_id),
        "claim_kind": "Belief",
        "claim_seq": 5,
        "claim_title": "B" * 2_000,
    }
    favors_json = {
        **signed,
        "edge_kind": "favors",
        "valence": 0.75,
        "note": "n" * 4_000,
    }
    proves_json = {**signed, "edge_kind": "proves", "valence": -1.0}
    content = run.db.calls[-2][2][5]
    assert content == {
        "title": "Directions",
        "summary": "Measured.",
        "format": "structured",
        "html": None,
        "sections": [
            {
                "title": "Scaling",
                "summary": "Held-out split.",
                "details": "Matched runs.",
                "findings": [
                    {
                        "claim": "Wider helps",
                        "outcome": outcome,
                        "uncertainty": "Small sample.",
                        "citations": [source, proves_json, favors_json],
                    },
                ],
            },
        ],
        "citations": [source, favors_json],
    }


@pytest.mark.asyncio
async def test_matching_replay_returns_the_stored_revision_unchanged() -> None:
    """A retry with the same key and body reads back, writing nothing."""
    run = _Run()
    body = _html_body(run)
    content: dict[str, object] = {
        "title": "Stored",
        "summary": "Earlier.",
        "format": "html",
        "html": "<p>old</p>",
        "sections": [],
        "citations": [],
    }
    stored = {
        "report_id": uuid.uuid4(),
        "revision": 2,
        "artifact_id": uuid.uuid4(),
        "content": content,
        "created_at": _CREATED_AT,
        "request_hash": hashlib.sha256(body.model_dump_json().encode()).hexdigest(),
        "author": "original@example.com",
        "issue_id": run.issue_id,
    }
    run.db.answers[_REPLAY] = [stored]
    revision = await run.publish(body)
    assert revision == ArtifactContentRevision.model_validate(
        {
            **content,
            "revision": 2,
            "artifact_id": stored["artifact_id"],
            "issue_id": run.issue_id,
            "author": "original@example.com",
            "created_at": _CREATED_AT,
        },
    )
    assert [call[1] for call in run.db.calls] == ["BEGIN", _ACTIVE, _REPLAY, "COMMIT"]
    assert run.store.submitted == []


@pytest.mark.asyncio
async def test_storage_quota_allows_exactly_500_mb() -> None:
    """Prior bytes plus this revision may reach, but not pass, 500 MB."""
    probe = _Run()
    await probe.publish(_html_body(probe))
    content_bytes = from_plain(probe.db.calls[-2][2][6], int)
    at_limit = _Run()
    at_limit.db.answers[_USED] = [500_000_000 - content_bytes]
    assert (await at_limit.publish(_html_body(at_limit))).revision == 1
    over = _Run()
    over.db.answers[_USED] = [500_000_001 - content_bytes]
    with pytest.raises(
        ValueError,
        match=_exact("Publisher Artifact storage exceeds 500 MB."),
    ):
        await over.publish(_html_body(over))
    assert over.db.calls[-1] == ("fetch", "ROLLBACK", ())


@pytest.mark.parametrize(
    ("sql", "answer", "error", "message"),
    [
        (_ACTIVE, None, ValueError, "Publisher account is not active."),
        (_ISSUE, "Belief", ValueError, "Artifact source must be an existing Issue."),
        (_CAUSE, None, RuntimeError, "Artifact creation audit is missing."),
        (
            _INSERT_REVISION,
            None,
            RuntimeError,
            "Artifact revision insert returned no row.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_publish_rejects_a_missing_precondition(
    sql: str,
    answer: object,
    error: type[Exception],
    message: str,
) -> None:
    """Each missing row aborts the transaction with its own reason."""
    run = _Run()
    run.db.answers[sql] = [answer]
    with pytest.raises(error, match=_exact(message)):
        await run.publish(_html_body(run))
    assert run.db.calls[-1] == ("fetch", "ROLLBACK", ())


@pytest.mark.asyncio
async def test_replay_with_a_different_body_conflicts() -> None:
    """A reused key cannot publish different content."""
    run = _Run()
    run.db.answers[_REPLAY] = [{"request_hash": "0" * 64}]
    with pytest.raises(
        ArtifactContentConflictError,
        match=_exact("Publication key already used."),
    ):
        await run.publish(_html_body(run))


@pytest.mark.parametrize("report_issue", [None, "other"])
@pytest.mark.asyncio
async def test_previous_artifact_must_belong_to_the_issue(
    report_issue: str | None,
) -> None:
    """A revision cannot attach to a missing or foreign report."""
    run = _Run()
    run.db.answers[_PREVIOUS] = [
        None
        if report_issue is None
        else {"id": uuid.uuid4(), "issue_id": uuid.uuid4()},
    ]
    with pytest.raises(
        ValueError,
        match=_exact("Previous Artifact does not belong to this Issue."),
    ):
        await run.publish(_html_body(run, previous_artifact_id=uuid.uuid4()))


@pytest.mark.parametrize(
    ("record", "claim", "edge", "message"),
    [
        (None, None, None, "Cited record does not exist."),
        ("row", None, "edge", "Cited signed evidence edge does not exist."),
        ("row", "row", None, "Cited signed evidence edge does not exist."),
        ("row", "row", "unsigned", "Cited signed evidence edge does not exist."),
        ("row", "row", "long note", "Cited edge note exceeds 4,000 characters."),
    ],
)
@pytest.mark.asyncio
async def test_unresolvable_citation_is_rejected(
    record: str | None,
    claim: str | None,
    edge: str | None,
    message: str,
) -> None:
    """A citation must name an existing row and signed edge with a short note."""
    run = _Run()
    row = {"kind": "Paper", "seq": 1, "title": "Row"}
    run.db.answers[_RECORD] = [row if record else None, row if claim else None]
    run.db.answers[_EDGE] = [
        {
            "edge": {"valence": 0.5, "note": None},
            "unsigned": {"valence": None, "note": None},
            "long note": {"valence": 0.5, "note": "n" * 4_001},
            None: None,
        }[edge],
    ]
    ref = ArtifactCitationRef(
        record_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        edge_kind="proves",
    )
    with pytest.raises(ValueError, match=_exact(message)):
        await run.publish(_html_body(run, citations=[ref]))


def _exact(message: str) -> str:
    return f"^{re.escape(message)}$"


def _html_body(
    run: _Run,
    *,
    previous_artifact_id: uuid.UUID | None = None,
    citations: list[ArtifactCitationRef] | None = None,
) -> PublishArtifactContent:
    return PublishArtifactContent(
        previous_artifact_id=previous_artifact_id,
        issue_id=run.issue_id,
        title="Atlas é",
        summary="One direction.",
        format="html",
        html="<h1>Atlas</h1>",
        citations=citations or [],
    )


@dataclass(slots=True, kw_only=True)
class _Db:
    """Connection double that answers only the exact SQL ``reports`` sends."""

    answers: dict[str, list[object]]
    calls: list[tuple[str, str, tuple[object, ...]]] = field(default_factory=list)
    conn: AsyncMock = field(init=False)

    def __post_init__(self) -> None:
        self.conn = AsyncMock(
            execute=AsyncMock(side_effect=self.execute),
            fetch=AsyncMock(side_effect=self.fetch),
            fetchval=AsyncMock(side_effect=self.fetchval),
            fetchrow=AsyncMock(side_effect=self.fetchrow),
        )

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append(("execute", sql, args))
        return "OK"

    async def fetch(self, sql: str, *args: object) -> list[object]:
        self.calls.append(("fetch", sql, args))
        return []

    async def fetchval(self, sql: str, *args: object) -> object:
        self.calls.append(("fetchval", sql, args))
        return self.answers[sql].pop(0)

    async def fetchrow(self, sql: str, *args: object) -> object:
        self.calls.append(("fetchrow", sql, args))
        answer = self.answers[sql].pop(0)
        if sql == _INSERT_REVISION and answer is not None:
            return {
                "report_id": args[0],
                "revision": args[1],
                "artifact_id": args[2],
                "content": args[5],
                "created_at": answer,
            }
        return answer


@dataclass(slots=True, kw_only=True)
class _FakeStore:
    """The two ``Store`` writes ``publish_artifact_content`` delegates."""

    db: _Db
    artifact_id: uuid.UUID = field(default_factory=uuid.uuid4)
    submitted: list[tuple[SubmitArtifact, dict[str, object]]] = field(
        default_factory=list,
    )
    edges: list[dict[str, object]] = field(default_factory=list)

    @property
    def engine(self) -> Self:
        return self

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[AsyncMock]:
        yield self.db.conn

    async def submit_artifact(
        self,
        req: SubmitArtifact,
        *,
        api_key_id: uuid.UUID | None,
        actor: str,
        conn: AsyncMock,
    ) -> uuid.UUID:
        self.submitted.append(
            (req, {"api_key_id": api_key_id, "actor": actor, "conn": conn}),
        )
        return self.artifact_id

    async def insert_edge_and_audit(self, conn: AsyncMock, **kwargs: object) -> bool:
        self.edges.append({"conn": conn, **kwargs})
        return True


@dataclass(slots=True, kw_only=True)
class _Run:
    """One publication against doubles primed for the success path."""

    user_id: uuid.UUID = field(default_factory=uuid.uuid4)
    issue_id: uuid.UUID = field(default_factory=uuid.uuid4)
    api_key_id: uuid.UUID = field(default_factory=uuid.uuid4)
    key: uuid.UUID = field(default_factory=uuid.uuid4)
    cause: uuid.UUID = field(default_factory=uuid.uuid4)
    db: _Db = field(init=False)
    store: _FakeStore = field(init=False)

    def __post_init__(self) -> None:
        self.db = _Db(
            answers={
                _ACTIVE: [1],
                _REPLAY: [None],
                _ISSUE: ["Issue"],
                _USED: [0],
                _NEXT_REVISION: [1],
                _CAUSE: [str(self.cause)],
                _INSERT_REVISION: [_CREATED_AT],
            },
        )
        self.store = _FakeStore(db=self.db)

    async def publish(self, body: PublishArtifactContent) -> ArtifactContentRevision:
        return await publish_artifact_content(
            cast(Store, self.store),
            user_id=self.user_id,
            author=_AUTHOR,
            api_key_id=self.api_key_id,
            body=body,
            key=self.key,
        )

    def edge(self, *, to_id: uuid.UUID, edge_kind: str) -> dict[str, object]:
        return {
            "conn": self.db.conn,
            "subject_id": self.store.artifact_id,
            "subject_kind": "Artifact",
            "to_id": to_id,
            "edge_kind": edge_kind,
            "api_key_id": self.api_key_id,
            "actor": _AUTHOR,
            "caused_by": self.cause,
        }


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
