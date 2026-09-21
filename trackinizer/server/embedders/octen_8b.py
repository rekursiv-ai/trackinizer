"""Octen-Embedding-8B as a session-search embedder.

Apache-2.0, fine-tuned from Qwen/Qwen3-Embedding-8B (RTEB leaderboard #1 as of
2026-01-12 per the card); native 4096 dims, Matryoshka-truncated to
:data:`OCTEN_TRUNCATED_DIM` (1024). Same family recipe (see
:mod:`~trackinizer.server.embedders._base`) with ONE documented deviation.

Document-side ``"- "`` prefix: the Octen card
(https://huggingface.co/Octen/Octen-Embedding-8B, "Known Issues") reports that
encoding a document with NO instruction prefix triggers unexpected behavior from
an upstream Qwen3-Embedding bug
(https://huggingface.co/Qwen/Qwen3-Embedding-8B/discussions/21), and recommends
prepending ``"- "`` (dash + space) to every document. The card's authors
measured this; we did not, so we follow their recipe rather than the stock
bare-document path. Queries keep the family ``Instruct:`` prefix per the card.

Octen ships no upstream ONNX export, so the graph is converted by the dev-time
``tools/export_onnx.py`` and published under the org ONNX repo below; the
package itself never imports torch.
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
    "OCTEN_ONNX",
    "OCTEN_TRUNCATED_DIM",
    "OctenEmbedder",
    "weights_present",
]


OCTEN_ONNX: Final = OnnxSource(model_id="rekursiv-ai/Octen-Embedding-8B-ONNX")
"""The org-hosted ONNX export (converted from ``Octen/Octen-Embedding-8B`` by
``tools/export_onnx.py``), resolved through the provisioned HF cache (``HF_HOME``
via ops/env); never pass ``cache_dir=`` -- an explicit dir overrides the shared
cache. Graph at the repo root with a ``model.onnx_data`` sidecar."""

OCTEN_TRUNCATED_DIM: Final = 1_024
"""Matryoshka truncation, matching ``session_embeddings`` storage. Octen inherits
Qwen3-Embedding-8B's 4096 native dims; 1024 keeps the family storage width."""

# See the module docstring: the Octen card's documented workaround for an
# upstream Qwen3-Embedding document-encoding bug.
_OCTEN_DOC_PREFIX: Final = "- "


class OctenEmbedder(QwenFamilyEmbedder):
    """Octen-Embedding-8B, truncated to 1024 dims, unit-normalized.

    Documents carry the card's ``"- "`` prefix; queries carry the family
    ``Instruct:`` prefix.
    """

    slug = "octen-embedding-8b"
    default_dim = OCTEN_TRUNCATED_DIM
    doc_prefix = _OCTEN_DOC_PREFIX

    @override
    def _load_model(self, device: str) -> tuple[Tokenizer, InferenceSession]:
        # References the module ``_load`` by name so a test patching
        # ``octen_8b._load`` is picked up at call time.
        return _load(device)


def weights_present() -> bool:
    """Whether the model's ONNX graph is already in the HF cache (no network)."""
    return weights_cached(OCTEN_ONNX)


# The module ``_load`` seam: fake-model tests patch this.
def _load(device: str) -> tuple[Tokenizer, InferenceSession]:
    """Build the tokenizer and the ONNX session on ``device``."""
    return load_onnx_model(OCTEN_ONNX, device)
