"""Qwen3-Embedding-4B as a session-search embedder.

Apache-2.0. Ties the MTEB top set at its paper's own noise bound; native 2560
dims, Matryoshka-truncated to :data:`QWEN_TRUNCATED_DIM` (1024) so 4.7M units
index at ~16 GB instead of ~40. Recipe (verified against the HF model card and
QwenLM/Qwen3-Embedding README, 2026-09-19) lives in
:mod:`~trackinizer.server.embedders._base`: last-token pool,
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
    "QwenEmbedder",
    "weights_present",
]


QWEN_ONNX: Final = OnnxSource(model_id="onnx-community/Qwen3-Embedding-4B-ONNX")
"""The ONNX export mirror, resolved through the provisioned HF cache (``HF_HOME``
via ops/env); never pass ``cache_dir=`` -- an explicit dir overrides the shared
cache. Graph at the repo root (no subfolder) with a ``model.onnx_data`` sidecar."""

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
    def _load_model(self, device: str) -> tuple[Tokenizer, InferenceSession]:
        # References the module ``_load`` by name so a test patching
        # ``qwen3_4b._load`` is picked up at call time.
        return _load(device)


def weights_present() -> bool:
    """Whether the model's ONNX graph is already in the HF cache (no network)."""
    return weights_cached(QWEN_ONNX)


# The module ``_load`` seam: fake-model tests patch this. Delegates to the shared
# loader so only the ONNX source lives here.
def _load(device: str) -> tuple[Tokenizer, InferenceSession]:
    """Build the tokenizer and the ONNX session on ``device``."""
    return load_onnx_model(QWEN_ONNX, device)
