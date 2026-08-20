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
from typing import Any, cast
from uuid import UUID, uuid4

import dataclasses
import decimal
import types
import typing

from fastapi.encoders import jsonable_encoder

import pytest

from trackinizer.server.api._deps import tag_kind
from trackinizer.types import inquiries
from trackinizer.types.cost import Cost
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


_INQUIRIES_NS: dict[str, Any] = dict(vars(inquiries))


def _resolve(annotation: object) -> object:
    """Turn a source-text or aliased annotation into the type it names."""
    if isinstance(annotation, str):
        # ``from __future__ import annotations`` leaves every ``field.type``
        # as source text; evaluate it in the defining module's namespace,
        # which is what ``get_type_hints`` does for the top-level classes.
        annotation = eval(annotation, _INQUIRIES_NS)  # noqa: S307 -- module's own annotations.
    if isinstance(annotation, typing.TypeAliasType):
        # PEP 695 aliases (``type Actor = str``, ``type Status = Literal[...]``)
        # are values rather than types; unwrap to whatever they name.
        return _resolve(annotation.__value__)
    return annotation


def _sample(annotation: object) -> object:
    """One non-``None`` value satisfying an annotation.

    Derived from the annotation rather than hand-written per kind. A field
    added to any Inquiry is populated -- and so serialized, and so compared
    against the oracle -- with no edit here, PROVIDED its type already has an
    arm below. A type with no arm raises rather than silently substituting
    something else, so the failure names the missing case instead of hiding
    it; extending this function is then the deliberate act it should be.
    """
    annotation = _resolve(annotation)
    origin = typing.get_origin(annotation)
    if origin is types.UnionType:
        # Reached only via a nested annotation (a tuple of optionals, say);
        # ``_samples`` is what expands a union at the field level.
        return _sample(
            next(a for a in typing.get_args(annotation) if a is not type(None))
        )
    if origin is tuple:
        return (_sample(typing.get_args(annotation)[0]),)
    if origin is dict:
        # ``Experiment.config`` -- the one JSONB field, whose leaves are
        # caller-supplied and so the only place an unhandled type can enter.
        #
        # The nested value must REQUIRE conversion. A dict of JSON-native
        # leaves is returned unchanged whether or not the serializer recurses
        # into dicts at all, so it cannot tell a working dict arm from a
        # missing one -- verified by deleting the arm and watching this test
        # still pass. A datetime leaf makes the branch observable.
        return {
            "nested": {
                "at": datetime(2026, 8, 19, 10, 12, 23, 55_030, tzinfo=UTC),
                "n": 1,
                "s": "x",
                "none": None,
            }
        }
    if origin is typing.Literal:
        return typing.get_args(annotation)[0]
    if annotation is datetime:
        return datetime(2026, 8, 19, 10, 12, 23, 55_030, tzinfo=UTC)
    if annotation is UUID:
        return uuid4()
    if annotation is decimal.Decimal:
        # No Inquiry field is Decimal-typed today; the arm exists so a union
        # carrying one is sampled rather than raising, which is how the
        # oracle would first see it.
        return decimal.Decimal("12.50")
    if annotation is Cost:
        return Cost(agent_usd=0.5, resource_usd=0.25)
    if annotation is bool:
        return True
    if annotation is int:
        return 7
    if annotation is float:
        return 1.5
    if annotation is str:
        return "sample"
    if dataclasses.is_dataclass(annotation) and isinstance(annotation, type):
        return annotation(
            **{
                field.name: _sample(field.type)
                for field in dataclasses.fields(annotation)
            }
        )
    raise AssertionError(f"no sample value for annotation {annotation!r}")


def _populated[T: Inquiry](subclass: type[T]) -> T:
    """One instance of ``subclass`` with every field set to a real value.

    The kwargs are built from the annotations, so their static type is
    ``object`` and no checker can match them to each field. The construction
    is verified at RUNTIME instead, by ``test_every_field_is_populated``.
    """
    hints = typing.get_type_hints(subclass, _INQUIRIES_NS)
    return subclass(
        **cast(
            "dict[str, Any]",
            {
                field.name: _sample(hints[field.name])
                for field in dataclasses.fields(subclass)
            },
        )
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
        instance = _populated(subclass)
        payload = tag_kind(instance)
        expected = jsonable_encoder(instance)
        expected["kind"] = subclass.__name__
        assert payload == expected

    def test_unhandled_type_raises_here_not_downstream(self) -> None:
        # ``_jsonable`` dispatches on a closed set of types and used to fall
        # through to ``return value`` for anything else. A value it does not
        # convert survives into the payload and raises inside ``json.dumps``
        # while FastAPI is writing the response -- a 500 whose traceback names
        # the encoder, not the field. Failing at the seam names the type.
        experiment = _populated(Experiment)
        with pytest.raises(TypeError, match="set"):
            tag_kind(dataclasses.replace(experiment, config={"tags": {"a"}}))

    def test_unhandled_type_names_the_offending_value(self) -> None:
        experiment = _populated(Experiment)
        with pytest.raises(TypeError, match="bytes"):
            tag_kind(dataclasses.replace(experiment, config={"blob": b"\x00"}))

    def test_container_fields_are_non_empty(self) -> None:
        # An empty tuple serializes to ``[]`` whether or not the serializer
        # converts elements, so a fixture that leaves one empty passes the
        # oracle while exercising nothing.
        instance = _populated(Issue)
        assert instance.produces, "edge tuples must carry an element"
        assert instance.labels, "list-valued columns must carry an element"

    @pytest.mark.parametrize("subclass", _KINDS)
    def test_every_field_is_populated(self, subclass: type[Inquiry]) -> None:
        # The guard on the guard. An oracle that compares two all-``None``
        # payloads passes no matter what the serializer does, which is
        # exactly how the ``Decimal`` and ``dict`` arms of ``_jsonable``
        # shipped untested. Deriving the fixture from ``dataclasses.fields``
        # covers a newly added field of an already-sampled type with no edit
        # here; a NEW type raises in ``_sample`` instead, naming itself.
        instance = _populated(subclass)
        unset = [
            field.name
            for field in dataclasses.fields(instance)
            if getattr(instance, field.name) is None
        ]
        assert not unset, f"{subclass.__name__} left unpopulated: {unset}"


if __name__ == "__main__":
    from trackinizer.lib.testing import test_main

    test_main(__file__)
