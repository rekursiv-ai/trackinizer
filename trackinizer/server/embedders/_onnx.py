"""Shared ONNX Runtime + tokenizer loading for the session embedders.

One place that turns a Hugging Face repo id into a ready
``(tokenizer, session)`` pair: it resolves the model's snapshot in the
provisioned HF cache (``HF_HOME`` via ops/env -- never a passed ``cache_dir``,
which would make the shared cache inert), loads the ONNX graph onto the
device's execution provider, and loads the ``tokenizer.json`` with the
``tokenizers`` library.

ONNX layout varies per repo: optimum's ``main_export`` writes ``model.onnx``
under an ``onnx/`` subfolder, while the ``onnx-community`` mirrors place it at
the repo root. A large graph also ships its weights in an external-data sidecar
(``model.onnx_data``) that ONNX Runtime loads by a path RELATIVE to the graph
file, so both files must land in the same cached directory -- the loader fetches
the sidecar first when the repo has one, then the graph. The per-model
:class:`OnnxSource` names the subfolder and whether a sidecar exists.

The heavy ``onnxruntime`` / ``huggingface_hub`` imports are function-local so
importing this module (which the registry does at config time) pulls neither
until a model actually loads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from tokenizers import Tokenizer


if TYPE_CHECKING:
    from onnxruntime import InferenceSession


__all__ = ["OnnxSource", "load_onnx_model", "weights_cached"]


_ONNX_FILENAME: Final = "model.onnx"


@dataclass(frozen=True, slots=True, kw_only=True)
class OnnxSource:
    """Where a model's ONNX graph and tokenizer live in its HF repo.

    Attributes:
      model_id: The Hugging Face repo id hosting the ONNX export.
      subfolder: Repo-relative directory holding ``model.onnx`` (``""`` when the
        graph sits at the repo root, as the ``onnx-community`` 4B/8B mirrors do).
      has_sidecar: Whether the graph ships external weights (``model.onnx_data``)
        that must be co-located with the graph in the cache.
      revision: Optional pinned commit; ``None`` resolves the default snapshot.

    """

    model_id: str
    subfolder: str = ""
    has_sidecar: bool = True
    revision: str | None = None


def load_onnx_model(
    source: OnnxSource,
    device: str,
) -> tuple[Tokenizer, InferenceSession]:
    """Load ``source``'s ONNX graph and tokenizer on ``device`` (no network).

    Fetches the external-data sidecar (when present) and the graph into the same
    cached directory, loads the graph onto the execution provider ``device``
    selects (CUDA for ``cuda*``, else CPU), and loads ``tokenizer.json``. Reads
    the provisioned HF cache; passes no ``cache_dir``.

    Args:
      source: The model's ONNX repo location.
      device: Inference device string (``"cpu"``, ``"cuda:0"``); selects the
        ONNX Runtime execution provider.

    Returns:
      tokenizer: The repo's ``tokenizers`` tokenizer.
      session: The ONNX Runtime inference session on the chosen provider.

    """
    import huggingface_hub  # noqa: PLC0415 -- deferred so config-time imports never pull huggingface_hub.
    import onnxruntime  # noqa: PLC0415 -- deferred so config-time imports never pull onnxruntime.

    if source.has_sidecar:
        # ONNX Runtime resolves the sidecar by a path relative to the graph, so
        # it must be cached alongside; download it first for that co-location.
        _ = huggingface_hub.hf_hub_download(
            source.model_id,
            "model.onnx_data",
            subfolder=source.subfolder or None,
            revision=source.revision,
        )
    onnx_path = huggingface_hub.hf_hub_download(
        source.model_id,
        _ONNX_FILENAME,
        subfolder=source.subfolder or None,
        revision=source.revision,
    )
    tokenizer_path = huggingface_hub.hf_hub_download(
        source.model_id,
        "tokenizer.json",
        revision=source.revision,
    )
    session = onnxruntime.InferenceSession(onnx_path, providers=_providers(device))
    return Tokenizer.from_file(tokenizer_path), session


def weights_cached(source: OnnxSource) -> bool:
    """Whether ``source``'s ONNX graph is already in the HF cache (no network).

    Consults ``try_to_load_from_cache`` for the graph file -- a cache HIT means
    the snapshot was downloaded, so the lazy load will not reach the network.
    Used at startup to decide degrade-vs-warm, never to trigger a download.
    Reads ``HF_HOME`` (ops/env); passes no ``cache_dir``.

    Args:
      source: The model's ONNX repo location.

    Returns:
      present: ``True`` when the model's ONNX graph is already cached.

    """
    import huggingface_hub  # noqa: PLC0415 -- deferred so config-time imports never pull huggingface_hub.

    graph = (
        f"{source.subfolder}/{_ONNX_FILENAME}" if source.subfolder else _ONNX_FILENAME
    )
    cached = huggingface_hub.try_to_load_from_cache(
        source.model_id,
        graph,
        revision=source.revision,
    )
    return isinstance(cached, str)


def _providers(device: str) -> list[str]:
    """Return the ONNX Runtime provider chain for ``device``."""
    if device.startswith("cuda"):
        # Fall back to CPU if the CUDA provider is unavailable at runtime, so a
        # session built for a GPU device still loads on a CPU-only host.
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]
