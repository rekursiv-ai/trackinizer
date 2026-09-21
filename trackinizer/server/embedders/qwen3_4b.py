"""Qwen3-Embedding-4B as a session-search embedder.

Apache-2.0. Ties the MTEB top set at its paper's own noise bound; native 2560
dims, Matryoshka-truncated to :data:`QWEN_TRUNCATED_DIM` (1024) so 4.7M units
index at ~16 GB instead of ~40. Recipe (verified against the HF model card and
QwenLM/Qwen3-Embedding README, 2026-09-19) lives in
:mod:`~trackinizer.server.embedders.qwen_family`: last-token pool,
truncate-then-normalize, bare document / ``Instruct:`` query.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, override

from trackinizer.server.embedders.qwen_family import (
    QwenFamilyEmbedder,
    hf_load,
    weights_cached,
)


if TYPE_CHECKING:
    from transformers import PreTrainedTokenizerBase

    import torch


__all__ = [
    "QWEN_MODEL_ID",
    "QWEN_TRUNCATED_DIM",
    "QwenEmbedder",
    "weights_present",
]


QWEN_MODEL_ID: Final = "Qwen/Qwen3-Embedding-4B"
"""Resolved through the provisioned HF cache (``HF_HOME`` via ops/env); never
pass ``cache_dir=`` -- an explicit dir overrides the shared cache."""

QWEN_TRUNCATED_DIM: Final = 1_024
"""Matryoshka truncation, matching ``session_embeddings`` storage. Qwen3-Embedding
trains 32..2560 nested; 1024 costs <=1 MTEB point vs native 2560 at 40% storage."""


class QwenEmbedder(QwenFamilyEmbedder):
    """Qwen3-Embedding-4B, truncated to 1024 dims, unit-normalized."""

    # ``qwen3-embedding-4b``, NOT ``qwen3-4b``: the latter is the chat LLM, a
    # different model. This slug keys ``session_embeddings`` rows (as
    # ``slug@dim``), so the distinction is load-bearing.
    slug = "qwen3-embedding-4b"
    default_dim = QWEN_TRUNCATED_DIM

    @override
    def _load_model(
        self,
        device: str,
    ) -> tuple[PreTrainedTokenizerBase, torch.nn.Module]:
        # References the module ``_load`` by name so a test patching
        # ``qwen3_4b._load`` is picked up at call time.
        return _load(device)


def weights_present() -> bool:
    """Whether the model's weights are already in the HF cache (no network)."""
    return weights_cached(QWEN_MODEL_ID)


# The module ``_load`` seam: fake-model tests patch this. Delegates to the shared
# loader so only the model id lives here.
def _load(device: str) -> tuple[PreTrainedTokenizerBase, torch.nn.Module]:
    """Build the left-padded tokenizer and the model on ``device``."""
    return hf_load(QWEN_MODEL_ID, device)
