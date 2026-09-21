"""Qwen3-Embedding-8B as a session-search embedder.

Apache-2.0, ~7.6B params, the family's top accuracy tier. Native 4096 dims (MRL
32..4096, verified on the HF model card 2026-09-20), Matryoshka-truncated to
:data:`QWEN_TRUNCATED_DIM` (1024) to fit the session storage width. Same recipe
as the rest of the family (see
:mod:`~trackinizer.server.embedders._base`): last-token pool,
truncate-then-normalize, bare document / ``Instruct:`` query.

Served from the ONNX export mirror; the graph and its external-data sidecar sit
at the repo root (no ``onnx/`` subfolder).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, override

from trackinizer.server.embedders._base import QwenFamilyEmbedder
from trackinizer.server.embedders._onnx import (
    OnnxSource,
    load_onnx_model,
    weights_cached,
)


if TYPE_CHECKING:
    from onnxruntime import InferenceSession
    from tokenizers import Tokenizer


__all__ = [
    "QWEN_ONNX",
    "QWEN_TRUNCATED_DIM",
    "Qwen8BEmbedder",
    "weights_present",
]


QWEN_ONNX: Final = OnnxSource(model_id="onnx-community/Qwen3-Embedding-8B-ONNX")
"""The ONNX export mirror, resolved through the provisioned HF cache (``HF_HOME``
via ops/env); never pass ``cache_dir=`` -- an explicit dir overrides the shared
cache. Graph at the repo root (no subfolder) with a ``model.onnx_data`` sidecar."""

QWEN_TRUNCATED_DIM: Final = 1_024
"""Matryoshka truncation, matching ``session_embeddings`` storage. Qwen3-Embedding-8B
trains 32..4096 nested; 1024 keeps the storage width uniform across the family."""


class Qwen8BEmbedder(QwenFamilyEmbedder):
    """Qwen3-Embedding-8B, truncated to 1024 dims, unit-normalized."""

    slug = "qwen3-embedding-8b"
    default_dim = QWEN_TRUNCATED_DIM

    @override
    def _load_model(self, device: str) -> tuple[Tokenizer, InferenceSession]:
        # References the module ``_load`` by name so a test patching
        # ``qwen3_8b._load`` is picked up at call time.
        return _load(device)


def weights_present() -> bool:
    """Whether the model's ONNX graph is already in the HF cache (no network)."""
    return weights_cached(QWEN_ONNX)


# The module ``_load`` seam: fake-model tests patch this.
def _load(device: str) -> tuple[Tokenizer, InferenceSession]:
    """Build the tokenizer and the ONNX session on ``device``."""
    return load_onnx_model(QWEN_ONNX, device)
