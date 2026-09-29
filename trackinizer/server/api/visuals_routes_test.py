"""Contract tests for the backend-owned visual catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.lib.custom_json import DictCodec, ListCodec, StrCodec, loads
from trackinizer.server.visuals.catalog import BrowseVisual, ChatVisual, Workspace


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


def test_visual_catalog_has_default_browse_chat_and_context_graph(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    """A signed-in browser learns its visual choices and default from one route."""
    client, _, _ = route_client
    response = client.get("/api/visuals")
    assert response.status_code == 200
    body = DictCodec.coerce(loads(response.content))
    assert StrCodec.coerce(body["default_visual"]) == "trax.browse"
    visuals = [DictCodec.coerce(item) for item in ListCodec.coerce(body["visuals"])]
    by_type = {StrCodec.coerce(visual["type"]): visual for visual in visuals}
    assert set(by_type) == {
        "trax.browse",
        "trax.chat",
        "trax.subgraph",
        "trax.timeline",
    }
    assert by_type["trax.browse"]["version"] == 1
    assert by_type["trax.chat"]["requires"] == ["session"]
    assert by_type["trax.subgraph"]["requires"] == ["record"]
    assert by_type["trax.timeline"]["requires"] == ["record"]
    direction_schema = DictCodec.coerce(
        DictCodec.coerce(by_type["trax.timeline"]["parameter_schema"])[
            "direction_limit"
        ],
    )
    assert direction_schema == {
        "type": "integer",
        "default": 8,
        "minimum": 1,
        "maximum": 12,
        "max_length": None,
    }
    assert by_type["trax.browse"]["parameter_schema"] == {}


def test_visual_config_composes_titles_and_rejects_invalid_identity() -> None:
    """A contributor can configure modules without publishing duplicate IDs."""
    configured = Workspace.Config(
        visuals=[BrowseVisual.Config(title="Explore"), ChatVisual.Config()],
        default_visual="trax.browse",
    ).make()
    assert configured.catalog().visuals[0].title == "Explore"

    with pytest.raises(ValueError, match="unique"):
        Workspace.Config(visuals=[BrowseVisual.Config(), BrowseVisual.Config()]).make()
    with pytest.raises(ValueError, match="registered"):
        Workspace.Config(visuals=[ChatVisual.Config()]).make()


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
