"""The embedding add-on: its default model, its settings, and what it contributes."""

from __future__ import annotations

import pytest

from trackinizer.addons.embedding.addon import EmbeddingAddon
from trackinizer.addons.embedding.qwen3_0p6b import Qwen06BEmbedder
from trackinizer.addons.embedding.search import Nearest
from trackinizer.addons.embedding.sweep import Sweep
from trackinizer.addons.embedding.units import Settings, settings_key
from trackinizer.sagent.journal.types import (
    AgentMessage,
    first_name,
    stored_names,
)


def test_the_default_model_is_qwen3_0p6b() -> None:
    addon = EmbeddingAddon.Config().make()
    renamed = AgentMessage.__name__
    # Keyed by the name the class first had, so a rename embeds nothing again; its
    # rows are swept under every name they are stored by.
    keyed = ("UserMessage", "AssistantMessage", first_name(renamed))
    swept = ("UserMessage", "AssistantMessage", *stored_names(renamed))
    fields = (
        ("Issue", ("description",)),
        ("Belief", ("title", "description")),
        ("Paper", ("title", "paper_abstract")),
    )

    assert isinstance(addon.model, Qwen06BEmbedder)
    assert addon.settings == Settings(
        key=settings_key(
            model="qwen3-embedding-0.6b@1024",
            dim=1_024,
            chunk_chars=1_500,
            overlap_chars=200,
            session_kinds=keyed,
            inquiry_fields=fields,
            edge_notes=True,
        ),
        dim=1_024,
        chunk_chars=1_500,
        overlap_chars=200,
        session_kinds=swept,
        inquiry_fields=fields,
        edge_notes=True,
    )
    assert not addon.loaded.is_set()


def test_manifest_has_one_sweep_the_migration_and_the_slot() -> None:
    config = EmbeddingAddon.Config()
    config.slice_units = 8
    config.interval_sec = 5.0
    addon = config.make()

    manifest = addon.manifest()

    assert manifest.server_services == [
        Sweep(
            model=addon.model,
            settings=addon.settings,
            slice_units=8,
            interval_sec=5.0,
            loaded=addon.loaded,
        ),
    ]
    assert [name for name, _sql in manifest.migrations] == ["embedding.0001.sql"]
    assert "CREATE TABLE IF NOT EXISTS embedding.vectors" in manifest.migrations[0][1]
    assert manifest.vector_search == Nearest(
        model=addon.model,
        settings=addon.settings,
        loaded=addon.loaded,
    )
    assert (manifest.title, manifest.routers, manifest.access_policy) == (
        "Embedding",
        (),
        None,
    )


@pytest.mark.parametrize(
    ("chunk_chars", "overlap_chars", "slice_units", "message"),
    [
        (100, 100, 1, r"overlap_chars must be in \[0, chunk_chars=100\), got 100"),
        (100, -1, 1, r"overlap_chars must be in \[0, chunk_chars=100\), got -1"),
        (100, 0, 0, "slice_units must be positive, got 0"),
    ],
)
def test_a_setting_that_cannot_chunk_or_slice_is_refused(
    chunk_chars: int,
    overlap_chars: int,
    slice_units: int,
    message: str,
) -> None:
    config = EmbeddingAddon.Config()
    config.chunk_chars = chunk_chars
    config.overlap_chars = overlap_chars
    config.slice_units = slice_units

    with pytest.raises(ValueError, match=message):
        _ = config.make()


def test_the_configured_sources_reach_the_settings() -> None:
    config = EmbeddingAddon.Config()
    config.inquiry_fields = {"Experiment": ("title",)}
    config.edge_notes = False

    settings = config.make().settings

    assert (settings.inquiry_fields, settings.edge_notes) == (
        (("Experiment", ("title",)),),
        False,
    )


def test_an_unstamped_inquiry_field_is_refused() -> None:
    config = EmbeddingAddon.Config()
    config.inquiry_fields = {"Issue": ("description", "labels", "app")}

    with pytest.raises(ValueError, match=r"inquiry_fields names \['app', 'labels'\]"):
        _ = config.make()


def test_the_edges_of_the_settings_are_accepted() -> None:
    config = EmbeddingAddon.Config()
    config.chunk_chars = 10
    config.overlap_chars = 0
    config.slice_units = 1

    assert config.make().settings.overlap_chars == 0
    config.overlap_chars = 9
    assert config.make().settings.overlap_chars == 9


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
