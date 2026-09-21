"""Qwen3-Embedding-0.6B as a session-search embedder.

Apache-2.0, ~0.6B params -- the lightweight member of the family for
edge/resource-constrained hosts. Its NATIVE embedding dimension is 1024 (MRL
32..1024, default 1024, verified on the HF model card 2026-09-20), which already
matches the session storage width, so :data:`QWEN_TRUNCATED_DIM` == the native
dim and the base's truncate step is a no-op slice. Same recipe as the rest of the
family (see :mod:`~trackinizer.server.embedders._base`): last-token pool,
truncate-then-normalize, bare document / ``Instruct:`` query.

Served from the ONNX export mirror; the graph and its external-data sidecar sit
under the repo's ``onnx/`` subfolder.
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
    "Qwen06BEmbedder",
    "weights_present",
]


QWEN_ONNX: Final = OnnxSource(
    model_id="onnx-community/Qwen3-Embedding-0.6B-ONNX",
    subfolder="onnx",
)
"""The ONNX export mirror, resolved through the provisioned HF cache (``HF_HOME``
via ops/env); never pass ``cache_dir=`` -- an explicit dir overrides the shared
cache. Graph under ``onnx/`` with a ``model.onnx_data`` sidecar."""

QWEN_TRUNCATED_DIM: Final = 1_024
"""The model's NATIVE dimension (MRL max 1024), so this is the full vector, not
a truncation -- the base slice ``[:1024]`` is a no-op. Kept explicit so the
stored ``name`` (``@1024``) and the storage width read together."""


class Qwen06BEmbedder(QwenFamilyEmbedder):
    """Qwen3-Embedding-0.6B at its native 1024 dims, unit-normalized."""

    slug = "qwen3-embedding-0.6b"
    default_dim = QWEN_TRUNCATED_DIM

    @override
    def _load_model(self, device: str) -> tuple[Tokenizer, InferenceSession]:
        # References the module ``_load`` by name so a test patching
        # ``qwen3_0p6b._load`` is picked up at call time.
        return _load(device)


def weights_present() -> bool:
    """Whether the model's ONNX graph is already in the HF cache (no network)."""
    return weights_cached(QWEN_ONNX)


# The module ``_load`` seam: fake-model tests patch this.
def _load(device: str) -> tuple[Tokenizer, InferenceSession]:
    """Build the tokenizer and the ONNX session on ``device``."""
    return load_onnx_model(QWEN_ONNX, device)
