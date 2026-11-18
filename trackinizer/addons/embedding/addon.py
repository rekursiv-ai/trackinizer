"""The embedding add-on: vectors for sessions, inquiries and edge notes.

A deployment that names it serves the vector arm of search through the
``vector_search`` slot, keeps every vector current with one server service
(:class:`~trackinizer.addons.embedding.sweep.Sweep`), and owns the
``embedding`` Postgres schema through its migrations. A deployment without it
searches by keyword alone.
"""

from __future__ import annotations

from dataclasses import field
from pathlib import Path
from typing import Final

import asyncio

from configgle import Fig, Makeable

from trackinizer.addons.addon import AddonManifest
from trackinizer.addons.embedding.model import Embedder
from trackinizer.addons.embedding.qwen3_0p6b import Qwen06BEmbedder
from trackinizer.addons.embedding.search import Nearest
from trackinizer.addons.embedding.sweep import Sweep
from trackinizer.addons.embedding.units import Settings, settings_key
from trackinizer.server.store.search import INQUIRY_TEXT_COLUMNS


__all__ = ["EmbeddingAddon"]


_CWD: Final[Path] = Path(__file__).resolve().parent


class EmbeddingAddon:
    """The embedding add-on.

    Args:
      config: The model, and what it embeds.

    Raises:
      ValueError: A chunk is not longer than its overlap, a slice holds no text, or
        ``inquiry_fields`` names a column the stamp trigger does not watch.

    """

    class Config(Fig["EmbeddingAddon"]):
        model: Makeable[Embedder] = field(default_factory=Qwen06BEmbedder.Config)
        """The model that turns text into vectors: Qwen3-Embedding-0.6B unless the
        site's factory names its own model's config here."""

        chunk_chars: int = 1_500
        """Characters per chunk of a long text; each chunk gets one vector."""

        overlap_chars: int = 200
        """Characters adjacent chunks share, so a sentence cut at a boundary is
        whole in one of them."""

        session_kinds: tuple[str, ...] = (
            "UserMessage",
            "AssistantMessage",
            "AgentToAgentMessage",
        )
        """The session record kinds that get vectors. Tool calls and their results
        stay keyword-only."""

        inquiry_fields: dict[str, tuple[str, ...]] = field(
            default_factory=lambda: {
                "Issue": ("description",),
                "Belief": ("title", "description"),
                "Paper": ("title", "paper_abstract"),
            },
        )
        """Per inquiry kind, the columns embedded together as one text, in this
        order; a kind not named gets no vector."""

        edge_notes: bool = True
        """Whether each edge's note gets a vector, owned by the inquiry the edge
        starts from, a session included."""

        slice_units: int = 32
        """Texts the server's sweep hands the model per call. Small, so one call
        holds the server's CPU threads for seconds, not minutes."""

        interval_sec: float = 60.0
        """How long the sweep waits after a round that found nothing to embed."""

    def __init__(self, config: Config) -> None:
        if config.overlap_chars < 0 or config.overlap_chars >= config.chunk_chars:
            raise ValueError(
                f"overlap_chars must be in [0, chunk_chars={config.chunk_chars}), "
                f"got {config.overlap_chars}",
            )
        if config.slice_units < 1:
            raise ValueError(f"slice_units must be positive, got {config.slice_units}")
        unstamped = sorted(
            {column for columns in config.inquiry_fields.values() for column in columns}
            - set(INQUIRY_TEXT_COLUMNS),
        )
        if unstamped:
            raise ValueError(
                f"inquiry_fields names {unstamped}, which the inquiries_stamp_change "
                f"trigger does not watch; an edit to them would never re-embed. "
                f"Embeddable columns: {list(INQUIRY_TEXT_COLUMNS)}.",
            )
        self.config = config
        self.model = config.model.make()
        inquiry_fields = tuple(
            (kind, tuple(columns)) for kind, columns in config.inquiry_fields.items()
        )
        self.settings = Settings(
            key=settings_key(
                model=self.model.name,
                dim=self.model.dim,
                chunk_chars=config.chunk_chars,
                overlap_chars=config.overlap_chars,
                session_kinds=config.session_kinds,
                inquiry_fields=inquiry_fields,
                edge_notes=config.edge_notes,
            ),
            dim=self.model.dim,
            chunk_chars=config.chunk_chars,
            overlap_chars=config.overlap_chars,
            session_kinds=config.session_kinds,
            inquiry_fields=inquiry_fields,
            edge_notes=config.edge_notes,
        )
        # Set once the sweep has loaded the model, so a search never loads it.
        self.loaded = asyncio.Event()

    def manifest(self) -> AddonManifest:
        """Declare the sweep, the migrations and the vector arm of search.

        Returns:
          manifest: One server service, the ``embedding.NNNN.sql`` migrations in
            order, and the vector search slot.

        """
        return AddonManifest(
            title="Embedding",
            description="Vectors for sessions, inquiries and edge notes, searched "
            "beside keywords.",
            server_services=[
                Sweep(
                    model=self.model,
                    settings=self.settings,
                    slice_units=self.config.slice_units,
                    interval_sec=self.config.interval_sec,
                    loaded=self.loaded,
                ),
            ],
            migrations=[
                (path.name, path.read_text(encoding="utf-8"))
                for path in sorted((_CWD / "schema").glob("embedding.*.sql"))
            ],
            vector_search=Nearest(
                model=self.model,
                settings=self.settings,
                loaded=self.loaded,
            ),
        )
