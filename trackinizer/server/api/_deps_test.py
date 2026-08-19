"""Tests for the shared API dependencies, chiefly :func:`tag_kind`.

``tag_kind`` sits on every inquiry read path, so its output IS the wire
contract. The serialization tests below pin that shape against
``jsonable_encoder``, the reference implementation the route used before the
dataclass fast path replaced it: the encoder is slow (measured 5.3ms of a
9ms 50-row listing) but definitionally correct, so it stays here as the
oracle even though it no longer runs in production.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder

import pytest

from trackinizer.server.api._deps import tag_kind
from trackinizer.types.inquiries import (
    AgentSession,
    Artifact,
    Belief,
    CodeChange,
    Experiment,
    Inquiry,
    InquiryEdge,
    Issue,
    Paper,
    WebResult,
    WebSearch,
)


# Named explicitly rather than walked from ``__subclasses__``: the sub-kinds
# nest under Artifact, and a test session that imports the module twice sees
# every class listed twice.
_KINDS: tuple[type[Inquiry], ...] = (
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


def _issue(
    *,
    subject_id: UUID | None = None,
    produces: tuple[InquiryEdge, ...] = (),
) -> Issue:
    """Build one Issue carrying every scalar type the serializer must handle."""
    return Issue(
        id=uuid4() if subject_id is None else subject_id,
        seq=42,
        account="agent@example.com",
        status="active",
        title="a title",
        created=datetime(2026, 8, 19, 10, 12, 23, 55_030, tzinfo=UTC),
        modified=datetime(2026, 8, 19, 10, 12, 35, 995_201, tzinfo=UTC),
        produces=produces,
    )


class TestTagKind:
    def test_none_passes_through(self) -> None:
        assert tag_kind(None) is None

    def test_adds_kind_discriminator(self) -> None:
        # Clients switch on ``kind``; the column does not carry it, so the
        # serializer synthesizes it from the concrete class.
        payload = tag_kind(_issue())
        assert payload is not None
        assert payload["kind"] == "Issue"

    def test_matches_jsonable_encoder_exactly(self) -> None:
        # The oracle. A faster serializer that changes one field is a
        # protocol break, not an optimization.
        issue = _issue()
        payload = tag_kind(issue)
        expected = jsonable_encoder(issue)
        expected["kind"] = "Issue"
        assert payload == expected

    def test_datetimes_are_iso_8601(self) -> None:
        # ``str(datetime)`` yields a SPACE separator, which parses in Python
        # and breaks every other language's ISO reader. The distinction is
        # invisible until a consumer outside this repo tries to read it.
        payload = tag_kind(_issue())
        assert payload is not None
        assert payload["created"] == "2026-08-19T10:12:23.055030+00:00"

    def test_uuid_is_a_string(self) -> None:
        subject = uuid4()
        payload = tag_kind(_issue(subject_id=subject))
        assert payload is not None
        assert payload["id"] == str(subject)

    def test_nested_edges_are_serialized(self) -> None:
        # Edge tuples are the deep part of the payload -- a shallow
        # conversion leaves dataclass objects that json.dumps then rejects.
        peer = uuid4()
        issue = _issue(
            produces=(InquiryEdge(id=peer, kind="Experiment", note=None, labels=None),)
        )
        payload = tag_kind(issue)
        assert payload is not None
        assert payload["produces"] == [
            {"id": str(peer), "kind": "Experiment", "note": None, "labels": None}
        ]

    @pytest.mark.parametrize("subclass", _KINDS)
    def test_every_kind_matches_the_encoder(self, subclass: type[Inquiry]) -> None:
        # Each sub-kind adds its own fields; the fast path must not be
        # tuned to whichever kind the author happened to test.
        instance = subclass(
            id=uuid4(),
            seq=1,
            account="agent@example.com",
            status="active",
            title="t",
            created=datetime(2026, 1, 1, tzinfo=UTC),
            modified=datetime(2026, 1, 2, tzinfo=UTC),
        )
        payload = tag_kind(instance)
        expected = jsonable_encoder(instance)
        expected["kind"] = subclass.__name__
        assert payload == expected


if __name__ == "__main__":
    from trackinizer.lib.testing import test_main

    test_main(__file__)
