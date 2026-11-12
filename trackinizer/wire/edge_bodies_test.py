from __future__ import annotations

import uuid

from pydantic import ValidationError

import pytest

from trackinizer.conftest import new_uuid
from trackinizer.wire.bodies import BATCH_MAX_ITEMS
from trackinizer.wire.edge_bodies import (
    CreateEdge,
    CreateEdgeBatch,
    CreateEdgeItem,
)


def test_bare_body_creates_an_unannotated_edge() -> None:
    body = CreateEdge.model_validate({})
    assert (body.priority, body.note, body.valence, body.reason) == (
        None,
        "",
        None,
        "",
    )
    assert body.actor is None


@pytest.mark.parametrize(
    "payload",
    [{"valence": 1.5}, {"valence": -1.01}, {"priority": -1}],
)
def test_out_of_range_annotations_are_refused(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CreateEdge.model_validate(payload)


def _item(**over: object) -> dict[str, object]:
    return {
        "from_id": str(uuid.uuid4()),
        "to_id": str(uuid.uuid4()),
        "edge_kind": "requires",
    } | over


def test_item_restates_identity() -> None:
    item = CreateEdgeItem.model_validate(_item(valence=-1.0, labels=["x"]))
    assert item.edge_kind == "requires"
    assert item.valence == -1.0
    assert item.labels == ["x"]
    with pytest.raises(ValidationError):
        CreateEdgeItem.model_validate(_item(edge_kind="likes"))
    with pytest.raises(ValidationError):
        CreateEdgeItem.model_validate({"edge_kind": "requires"})


def test_batch_size_is_bounded() -> None:
    assert len(CreateEdgeBatch.model_validate({"items": [_item()]}).items) == 1
    with pytest.raises(ValidationError):
        CreateEdgeBatch.model_validate({"items": []})
    with pytest.raises(ValidationError):
        CreateEdgeBatch.model_validate(
            {"items": [_item() for _ in range(BATCH_MAX_ITEMS + 1)]},
        )


class TestEdgeBodies:
    def test_create_edge_labels_default_none(self) -> None:
        """Bare edge create leaves labels unset (NULL), not ``[]`` (F11).

        ``Edge.labels`` defaults to ``None``; the wire body must agree so
        a created edge's labels column matches a Store-created edge's.
        """
        assert CreateEdge().labels is None

    def test_item_priority_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="greater than or equal"):
            CreateEdgeItem(
                from_id=new_uuid(),
                to_id=new_uuid(),
                edge_kind="narrows",
                priority=-3,  # pyrefly: ignore[bad-argument-type] -- The negative test passes the out-of-range value under test.
            )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
