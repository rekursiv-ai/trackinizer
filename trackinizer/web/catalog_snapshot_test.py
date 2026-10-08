"""Keep the development preview catalog synchronized with the backend."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import json

from trackinizer.lib.codec import from_plain, loads
from trackinizer.server.visuals.catalog import default_catalog


_CWD: Final = Path(__file__).resolve().parent


def test_preview_catalog_matches_backend() -> None:
    """A dev preview of an older server shows only backend-defined visuals."""
    snapshot = _CWD / "src/visuals/catalog.preview.json"
    assert json.loads(snapshot.read_bytes()) == default_catalog().model_dump(
        mode="json",
    )


def test_every_catalog_visual_has_matching_frontend_renderer() -> None:
    """The build cannot advertise a visual the browser cannot render."""
    manifest = _CWD / "src/visuals/renderer-versions.json"
    versions = {
        key: from_plain(value, int)
        for key, value in from_plain(
            loads(manifest.read_bytes()),
            dict[str, object],
        ).items()
    }
    assert versions == {
        visual.type: visual.version for visual in default_catalog().visuals
    }


def test_subgraph_is_a_record_scoped_catalog_option() -> None:
    """Agents and people can select a graph bound to the viewed record."""
    visuals = {visual.type: visual for visual in default_catalog().visuals}
    subgraph = visuals["trax.subgraph"]
    assert subgraph.version == 1
    assert subgraph.requires == ["record"]
    assert subgraph.default_size == "wide"
    # The window reads with `/api/web/graph?focus=`, which walks at most 3 hops.
    assert sorted(subgraph.parameter_schema) == ["highlight", "hops"]
    assert subgraph.parameter_schema["hops"].maximum == 3


def test_timeline_is_bounded_and_record_scoped() -> None:
    """The timeline advertises the same maxima enforced by its data route."""
    timeline = {visual.type: visual for visual in default_catalog().visuals}[
        "trax.timeline"
    ]
    assert timeline.title == "Lineage and timeline"
    assert timeline.version == 1
    assert timeline.requires == ["record"]
    assert timeline.default_size == "wide"
    assert timeline.parameter_schema["direction_limit"].maximum == 12
    assert timeline.parameter_schema["results_per_direction"].maximum == 5


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
