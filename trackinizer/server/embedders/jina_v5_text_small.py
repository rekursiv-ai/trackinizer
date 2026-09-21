"""jina-embeddings-v5-text-small as a session-search embedder.

LICENSE: CC BY-NC 4.0 -- NON-COMMERCIAL USE ONLY. Enabling this embedder for a
commercial deployment requires a separate licence from Jina AI.

677M params on a Qwen3-0.6B-Base backbone; native embedding dimension 1024 (MRL
32..1024, verified on the HF model card 2026-02-18) -- the same storage width as
the Qwen family, though it still gets its own per-model partial index (keyed by
name). Loaded via its custom ``encode`` API with ``trust_remote_code`` at a
PINNED revision; recipe in :mod:`~trackinizer.server.embedders.jina`.
"""

from __future__ import annotations

from typing import Final, override

from trackinizer.server.embedders.jina import (
    JinaV5Embedder,
    jina_load,
    weights_cached,
)


__all__ = [
    "JINA_MODEL_ID",
    "JINA_NATIVE_DIM",
    "JinaV5SmallEmbedder",
    "weights_present",
]


JINA_MODEL_ID: Final = "jinaai/jina-embeddings-v5-text-small"
"""Resolved through the provisioned HF cache (``HF_HOME`` via ops/env)."""

# The PINNED commit whose trust_remote_code we execute -- immutable, never
# floating ``main``. Bump deliberately after reviewing the repo's code diff.
_REVISION: Final = "dd76d535f5447ca3897a9c893fb1e612ead98192"

JINA_NATIVE_DIM: Final = 1_024
"""The model's native output dimension (MRL max 1024); stored without truncation."""


class JinaV5SmallEmbedder(JinaV5Embedder):
    """jina-embeddings-v5-text-small at its native 1024 dims, unit-normalized."""

    slug = "jina-embeddings-v5-text-small"
    default_dim = JINA_NATIVE_DIM

    @override
    def _load_model(self, device: str) -> object:
        # References the module ``_load`` by name so a test patching
        # ``jina_v5_text_small._load`` is picked up at call time.
        return _load(device)


def weights_present() -> bool:
    """Whether the pinned weights are already in the HF cache (no network)."""
    return weights_cached(JINA_MODEL_ID, _REVISION)


# The module ``_load`` seam: fake-model tests patch this.
def _load(device: str) -> object:
    """Load the pinned model on ``device`` with ``trust_remote_code``."""
    return jina_load(JINA_MODEL_ID, _REVISION, device)
