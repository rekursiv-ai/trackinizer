"""Report publication rejects misleading or unbounded evidence drafts."""

from __future__ import annotations

import uuid

from pydantic import ValidationError

import pytest

from trackinizer.server.visuals.reports import (
    ArtifactContentRevision,
    ArtifactFindingDraft,
    PublishArtifactContent,
)


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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
