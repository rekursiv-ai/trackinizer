"""jina-embeddings-v5-text-small as a session-search embedder.

LICENSE: CC BY-NC 4.0 -- NON-COMMERCIAL USE ONLY. Enabling this embedder for a
commercial deployment requires a separate licence from Jina AI.

677M params on a Qwen3-0.6B-Base backbone; native embedding dimension 1024 (MRL
32..1024, verified on the HF model card 2026-02-18) -- the same storage width as
the Qwen family, though it still gets its own per-model partial index (keyed by
name). Served from the task-specific ``-retrieval`` ONNX export (retrieval
adapter merged into the base weights); recipe in
:mod:`~trackinizer.server.embedders._jina`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, override

from trackinizer.server.embedders._jina import (
    JinaV5Embedder,
    jina_weights_cached,
    load_jina_onnx,
)
from trackinizer.server.embedders._onnx import OnnxSource


if TYPE_CHECKING:
    from onnxruntime import InferenceSession
    from tokenizers import Tokenizer


__all__ = [
    "JINA_NATIVE_DIM",
    "JINA_ONNX",
    "JinaV5SmallEmbedder",
    "weights_present",
]


# The retrieval-task ONNX export: its adapter is merged into the base weights, so
# the graph is plain (no PEFT, no trust_remote_code). Pinned revision -- bump
# deliberately after reviewing the repo diff.
JINA_ONNX: Final = OnnxSource(
    model_id="jinaai/jina-embeddings-v5-text-small-retrieval",
    subfolder="onnx",
    revision="dd76d535f5447ca3897a9c893fb1e612ead98192",
)
"""Resolved through the provisioned HF cache (``HF_HOME`` via ops/env)."""

JINA_NATIVE_DIM: Final = 1_024
"""The model's native output dimension (MRL max 1024); stored without truncation."""


class JinaV5SmallEmbedder(JinaV5Embedder):
    """jina-embeddings-v5-text-small at its native 1024 dims, unit-normalized."""

    slug = "jina-embeddings-v5-text-small"
    default_dim = JINA_NATIVE_DIM
    onnx = JINA_ONNX

    @override
    def _load_model(self, device: str) -> tuple[Tokenizer, InferenceSession]:
        # References the module ``_load`` by name so a test patching
        # ``jina_v5_text_small._load`` is picked up at call time.
        return _load(device)


def weights_present() -> bool:
    """Whether the ONNX graph is already in the HF cache (no network)."""
    return jina_weights_cached(JINA_ONNX)


# The module ``_load`` seam: fake-model tests patch this.
def _load(device: str) -> tuple[Tokenizer, InferenceSession]:
    """Load the tokenizer and ONNX session on ``device``."""
    return load_jina_onnx(JINA_ONNX, device)
