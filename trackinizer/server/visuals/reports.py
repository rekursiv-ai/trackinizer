"""Immutable, team-readable report revisions and their graph provenance."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

import hashlib
import json
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trackinizer.lib.custom_json import FloatCodec, IntCodec, StrCodec
from trackinizer.server.notify import notify_after_commit, tx
from trackinizer.wire.bodies import SubmitArtifact


if TYPE_CHECKING:
    from collections.abc import Mapping

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.store.core import Store


class ReportConflictError(Exception):
    """A publication key already names different report content."""


class ReportCitationRef(BaseModel):
    """A graph row and optional signed edge selected for a report citation."""

    model_config = ConfigDict(extra="forbid")

    record_id: uuid.UUID
    claim_id: uuid.UUID | None = None
    edge_kind: Literal["proves", "favors"] | None = None

    @model_validator(mode="after")
    def validate_edge(self) -> ReportCitationRef:
        """Require the two coordinates of an evidence edge together."""
        if (self.claim_id is None) != (self.edge_kind is None):
            raise ValueError("Evidence citations need a claim and edge kind.")
        return self


class ReportCitation(BaseModel):
    """A publication-time snapshot of one row and optional signed edge."""

    record_id: uuid.UUID
    kind: str
    seq: int
    title: str
    claim_id: uuid.UUID | None = None
    claim_kind: str | None = None
    claim_seq: int | None = None
    claim_title: str | None = None
    edge_kind: Literal["proves", "favors"] | None = None
    valence: float | None = Field(default=None, ge=-1, le=1)
    note: str | None = Field(default=None, max_length=4_000)


class ReportFindingDraft(BaseModel):
    """One conclusion with measured outcome and explicit uncertainty."""

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1, max_length=2_000)
    outcome: ReportOutcome
    uncertainty: str = Field(min_length=1, max_length=2_000)
    citations: list[ReportCitationRef] = Field(default_factory=list, max_length=16)

    @field_validator("claim", "uncertainty")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        """Reject prose that renders as an empty finding."""
        if not value.strip():
            raise ValueError("Finding text must not be blank.")
        return value


class ReportSectionDraft(BaseModel):
    """One bounded direction or result, with details closed by default."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(max_length=2_000)
    details: str = Field(max_length=8_000)
    findings: list[ReportFindingDraft] = Field(default_factory=list, max_length=12)

    @field_validator("title", "summary")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        """Reject invisible section headings and summaries."""
        if not value.strip():
            raise ValueError("Section text must not be blank.")
        return value


class ReportFinding(BaseModel):
    """A conclusion whose graph references are frozen at publication."""

    claim: str
    outcome: ReportOutcome
    uncertainty: str
    citations: list[ReportCitation]


class ReportSection(BaseModel):
    """A published section with nested details and evidenced findings."""

    title: str
    summary: str
    details: str
    findings: list[ReportFinding]


class ReportOutcome(BaseModel):
    """A measured result, denominator, and evaluation split."""

    model_config = ConfigDict(extra="forbid")

    result: str = Field(min_length=1, max_length=2_000)
    denominator: int = Field(ge=0, le=9_007_199_254_740_991)
    split: str = Field(min_length=1, max_length=500)

    @field_validator("result", "split")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        """Reject a result or split with no visible text."""
        if not value.strip():
            raise ValueError("Outcome text must not be blank.")
        return value


class PublishReport(BaseModel):
    """A bounded publication request, separate from workspace mutations."""

    model_config = ConfigDict(extra="forbid")

    report_id: uuid.UUID | None = None
    issue_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=4_000)
    format: Literal["html", "structured"]
    html: str | None = Field(default=None, max_length=2_000_000)
    sections: list[ReportSectionDraft] = Field(default_factory=list, max_length=24)
    citations: list[ReportCitationRef] = Field(default_factory=list, max_length=64)

    @field_validator("title", "summary")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        """Reject invisible report headings and summaries."""
        if not value.strip():
            raise ValueError("Report text must not be blank.")
        return value

    @model_validator(mode="after")
    def validate_format(self) -> PublishReport:
        """Keep HTML and structured sections as distinct report formats.

        Returns:
          report: The validated request.

        """
        if self.format == "html" and (not self.html or self.sections):
            raise ValueError("HTML reports need HTML and no structured sections.")
        if self.format == "structured" and (self.html is not None or not self.sections):
            raise ValueError("Structured reports need sections and no HTML.")
        citation_count = len(self.citations) + sum(
            len(finding.citations)
            for section in self.sections
            for finding in section.findings
        )
        if citation_count > 128:
            raise ValueError("Report has more than 128 citations.")
        if len(self.model_dump_json().encode("utf-8")) > 2_200_000:
            raise ValueError("Report payload exceeds 2.2 MB.")
        return self


class ReportRevision(BaseModel):
    """An immutable report revision shared with authenticated teammates."""

    report_id: uuid.UUID
    revision: int = Field(ge=1)
    artifact_id: uuid.UUID
    issue_id: uuid.UUID
    title: str
    summary: str
    author: str
    created_at: datetime
    scope: Literal["team"] = "team"
    format: Literal["html", "structured"]
    html: str | None
    sections: list[ReportSection]
    citations: list[ReportCitation]


async def publish_report(
    store: Store,
    *,
    user_id: uuid.UUID,
    author: str,
    api_key_id: uuid.UUID | None,
    body: PublishReport,
    key: uuid.UUID,
) -> ReportRevision:
    """Append one report revision and its produced Artifact in one transaction.

    Args:
      store: Shared graph store and database engine.
      user_id: Authenticated publisher.
      author: Server-attested account email.
      api_key_id: Credential used by an agent, if any.
      body: Bounded report content and graph references.
      key: Retry-safe publication key.

    Returns:
      revision: Published or identically replayed immutable revision.

    """
    request_hash = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    async with notify_after_commit(), store.engine.acquire() as conn, tx(conn):
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        if not await conn.fetchval(
            "SELECT 1 FROM users WHERE id = $1 AND status = 'active' FOR UPDATE",
            user_id,
        ):
            raise ValueError("Publisher account is not active.")
        replay = await conn.fetchrow(
            "SELECT revisions.report_id, revisions.revision, revisions.artifact_id, "
            "revisions.content, revisions.created_at, revisions.request_hash, "
            "revisions.author_email AS author, reports.issue_id "
            "FROM visual_report_revisions AS revisions "
            "JOIN visual_reports AS reports ON reports.id = revisions.report_id "
            "WHERE revisions.author_id = $1 AND revisions.publish_key = $2",
            user_id,
            key,
        )
        if replay is not None:
            if replay["request_hash"] != request_hash:
                raise ReportConflictError("Publication key already used.")
            return _revision_from_row(cast("Mapping[str, object]", replay))
        issue = await conn.fetchval(
            "SELECT kind FROM inquiries WHERE id = $1 FOR SHARE",
            body.issue_id,
        )
        if issue != "Issue":
            raise ValueError("Report owner must be an existing Issue.")
        if body.report_id is None:
            report_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO visual_reports (id, issue_id, created_by) "
                "VALUES ($1, $2, $3)",
                report_id,
                body.issue_id,
                user_id,
            )
        else:
            report_id = body.report_id
            report = await conn.fetchrow(
                "SELECT issue_id FROM visual_reports WHERE id = $1 FOR UPDATE",
                report_id,
            )
            if report is None or report["issue_id"] != body.issue_id:
                raise ValueError("Report does not belong to this Issue.")
        content = await _snapshot_content(conn, body)
        revision = IntCodec.coerce(
            await conn.fetchval(
                "SELECT coalesce(max(revision), 0) + 1 "
                "FROM visual_report_revisions WHERE report_id = $1",
                report_id,
            ),
        )
        artifact_id = await store.submit_artifact(
            SubmitArtifact(
                title=f"{body.title} (revision {revision})",
                description=body.summary,
                status="complete",
                account=author,
            ),
            api_key_id=api_key_id,
            actor=author,
            conn=conn,
        )
        cause = await conn.fetchval(
            "SELECT id FROM change_log WHERE subject_id = $1 AND kind = 'created'",
            artifact_id,
        )
        if cause is None:
            raise RuntimeError("Artifact creation audit is missing.")
        await store.insert_edge_and_audit(
            conn,
            subject_id=artifact_id,
            subject_kind="Artifact",
            to_id=body.issue_id,
            edge_kind="produced_by",
            api_key_id=api_key_id,
            actor=author,
            caused_by=uuid.UUID(str(cause)),
        )
        row = await conn.fetchrow(
            "INSERT INTO visual_report_revisions "
            "(report_id, revision, artifact_id, author_id, author_email, content, "
            "publish_key, request_hash) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
            "RETURNING report_id, revision, artifact_id, content, created_at",
            report_id,
            revision,
            artifact_id,
            user_id,
            author,
            content,
            key,
            request_hash,
        )
        if row is None:
            raise RuntimeError("Report revision insert returned no row.")
        return _revision_from_row(
            {**dict(row), "author": author, "issue_id": body.issue_id},
        )


async def read_report_revision(
    store: Store,
    report_id: uuid.UUID,
    revision: int,
) -> ReportRevision | None:
    """Read one exact revision for any authenticated account.

    Args:
      store: Shared graph store and database engine.
      report_id: Stable report identity.
      revision: Immutable revision number.

    Returns:
      report: The requested revision, or None when absent.

    """
    async with store.engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT revisions.report_id, revisions.revision, revisions.artifact_id, "
            "revisions.content, revisions.created_at, "
            "revisions.author_email AS author, "
            "reports.issue_id FROM visual_report_revisions AS revisions "
            "JOIN visual_reports AS reports ON reports.id = revisions.report_id "
            "WHERE revisions.report_id = $1 AND revisions.revision = $2",
            report_id,
            revision,
        )
    return _revision_from_row(cast("Mapping[str, object]", row)) if row else None


async def _snapshot_content(conn: Conn, body: PublishReport) -> dict[str, object]:
    """Resolve row titles and signed edges before freezing report content."""
    cache: CitationCache = {}
    citations = [await _snapshot_cached(conn, ref, cache) for ref in body.citations]
    sections = [await _snapshot_section(conn, draft, cache) for draft in body.sections]
    content: dict[str, object] = {
        "title": body.title,
        "summary": body.summary,
        "format": body.format,
        "html": body.html,
        "sections": [section.model_dump(mode="json") for section in sections],
        "citations": [citation.model_dump(mode="json") for citation in citations],
    }
    if len(json.dumps(content, ensure_ascii=False).encode("utf-8")) > 2_200_000:
        raise ValueError("Published report exceeds 2.2 MB.")
    return content


type CitationCache = dict[
    tuple[uuid.UUID, uuid.UUID | None, str | None],
    ReportCitation,
]


async def _snapshot_section(
    conn: Conn,
    draft: ReportSectionDraft,
    cache: CitationCache,
) -> ReportSection:
    """Freeze each finding and citation in one structured section."""
    return ReportSection(
        title=draft.title,
        summary=draft.summary,
        details=draft.details,
        findings=[
            await _snapshot_finding(conn, item, cache) for item in draft.findings
        ],
    )


async def _snapshot_finding(
    conn: Conn,
    draft: ReportFindingDraft,
    cache: CitationCache,
) -> ReportFinding:
    """Freeze a finding and its cited graph evidence."""
    return ReportFinding(
        claim=draft.claim,
        outcome=draft.outcome,
        uncertainty=draft.uncertainty,
        citations=[await _snapshot_cached(conn, ref, cache) for ref in draft.citations],
    )


async def _snapshot_cached(
    conn: Conn,
    ref: ReportCitationRef,
    cache: CitationCache,
) -> ReportCitation:
    """Read each source-edge pair once per report revision."""
    key = (ref.record_id, ref.claim_id, ref.edge_kind)
    if key not in cache:
        cache[key] = await _snapshot_citation(conn, ref)
    return cache[key]


async def _snapshot_citation(conn: Conn, ref: ReportCitationRef) -> ReportCitation:
    """Copy a live row and optional signed edge into immutable report JSON."""
    record = await conn.fetchrow(
        "SELECT kind, seq, title FROM inquiries WHERE id = $1",
        ref.record_id,
    )
    if record is None:
        raise ValueError("Cited record does not exist.")
    citation = ReportCitation(
        record_id=ref.record_id,
        kind=StrCodec.coerce(record["kind"]),
        seq=IntCodec.coerce(record["seq"]),
        title=StrCodec.coerce(record["title"])[:2_000],
    )
    if ref.claim_id is None or ref.edge_kind is None:
        return citation
    claim = await conn.fetchrow(
        "SELECT kind, seq, title FROM inquiries WHERE id = $1",
        ref.claim_id,
    )
    edge = await conn.fetchrow(
        "SELECT valence, note FROM edges WHERE from_id = $1 "
        "AND to_id = $2 AND edge_kind = $3",
        ref.record_id,
        ref.claim_id,
        ref.edge_kind,
    )
    if claim is None or edge is None or edge["valence"] is None:
        raise ValueError("Cited signed evidence edge does not exist.")
    note = cast(str | None, edge["note"])
    if note is not None and len(note) > 4_000:
        raise ValueError("Cited edge note exceeds 4,000 characters.")
    return ReportCitation(
        record_id=citation.record_id,
        kind=citation.kind,
        seq=citation.seq,
        title=citation.title,
        claim_id=ref.claim_id,
        claim_kind=StrCodec.coerce(claim["kind"]),
        claim_seq=IntCodec.coerce(claim["seq"]),
        claim_title=StrCodec.coerce(claim["title"])[:2_000],
        edge_kind=ref.edge_kind,
        valence=FloatCodec.coerce(edge["valence"]),
        note=note,
    )


def _revision_from_row(row: Mapping[str, object]) -> ReportRevision:
    """Validate stored JSON and return its exact publication metadata."""
    content = cast("Mapping[str, object]", row["content"])
    return ReportRevision.model_validate(
        {
            **content,
            "report_id": row["report_id"],
            "revision": row["revision"],
            "artifact_id": row["artifact_id"],
            "issue_id": row["issue_id"],
            "author": row["author"],
            "created_at": row["created_at"],
            "scope": "team",
        },
    )
