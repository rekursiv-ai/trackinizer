"""The session-embedder registry: config name -> model entry.

The single place that maps a ``session_embedder`` config value to an embedder
instance and its weight-presence check. Every entry imports its model module
INSIDE its thunk, so importing this registry (which config.py does at config
time) pulls no torch -- only the chosen model's ``build`` reaches transformers.

The registry keys are the STABLE stored identities (``session_embeddings.model``
values): the empty string disables the semantic arm, ``stub``/``stub-<dim>`` are
the deterministic test embedders, and each real model is keyed by its
``<slug>@<dim>`` name. A key rename is data corruption (it orphans every row that
model wrote), so keys are frozen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from difflib import get_close_matches
from typing import TYPE_CHECKING

from trackinizer.server.config import ConfigError
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.tools import bucket_boundaries
from trackinizer.server.tools.bucket_boundaries import TransformerSpec
from trackinizer.types.embedder import QueryEmbedder


if TYPE_CHECKING:
    from trackinizer.server.embedders import (
        jina_v5_text_nano,
        jina_v5_text_small,
        octen_8b,
        qwen3_0p6b,
        qwen3_4b,
        qwen3_8b,
    )
else:
    from wrapt import lazy_import

    # Each model module pulls torch/transformers on FIRST attribute access only;
    # binding them as lazy_import proxies keeps importing this registry (which
    # runs at config time) torch-free while staying top-level (no PLC0415).
    qwen3_0p6b = lazy_import("trackinizer.server.embedders.qwen3_0p6b")
    qwen3_4b = lazy_import("trackinizer.server.embedders.qwen3_4b")
    qwen3_8b = lazy_import("trackinizer.server.embedders.qwen3_8b")
    octen_8b = lazy_import("trackinizer.server.embedders.octen_8b")
    jina_v5_text_nano = lazy_import(
        "trackinizer.server.embedders.jina_v5_text_nano",
    )
    jina_v5_text_small = lazy_import(
        "trackinizer.server.embedders.jina_v5_text_small",
    )


__all__ = [
    "EMBEDDERS",
    "ModelEntry",
    "bucket_specs",
    "build_backfill_embedder",
    "build_session_embedder",
    "is_weightless",
    "resolved_name",
    "weights_present",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelEntry:
    """A registered real model family: how to build it and probe its weights.

    One entry per model SLUG (the bare stored-name prefix). It carries the dim
    contract (:attr:`spec` for a Matryoshka range, or a fixed :attr:`default_dim`)
    so ``(name, dim)`` resolution has a single source, plus dim-aware factories.

    Attributes:
      slug: The bare stored-name prefix (``"qwen3-embedding-4b"``).
      default_dim: The dim a bare name resolves to (the shipped index width).
      spec: The model's :class:`~bucket_boundaries.TransformerSpec` when it is a
        Matryoshka model (its ``min_dim..max_dim`` is the valid ``--dim`` range),
        or ``None`` for a fixed-dim model (only :attr:`default_dim` is valid).
      build: ``dim -> `` fresh CPU embedder at that dim (imports torch inside).
      backfill: ``(device, batch_size, dim) -> `` embedder for the GPU pipeline,
        applying the model's compile policy (Qwen-family compiles the forward on
        cuda -- measured 2.79x on a 5090; Jina does not, its custom ``encode``
        may not route through the forward we compile).
      weights_present: No-network cache check for this model's weights.

    """

    slug: str
    default_dim: int
    spec: TransformerSpec | None
    build: Callable[[int], QueryEmbedder]
    backfill: Callable[[str, int, int], QueryEmbedder]
    weights_present: Callable[[], bool]


def build_session_embedder(
    name: str,
    *,
    dim: int | None = None,
) -> QueryEmbedder | None:
    """Build the session-search embedder for ``name``, or ``None`` when disabled.

    Separate from the Store's ``build_embedder`` because ``session_embeddings``
    is a wide ``halfvec`` surface while ``inquiry_embeddings`` is ``vector(384)``
    and the Store rejects a non-384 embedder. An empty name returns ``None`` so
    the session-search route degrades to full-text only rather than 500, and each
    real model is imported lazily so an unset knob never pulls in torch.

    ``name`` may be a bare slug (``qwen3-embedding-4b``) or the full
    ``slug@dim``; ``dim`` optionally overrides within the model's supported range.
    See :func:`_resolve_model`.

    Args:
      name: ``""`` (disabled), ``"stub"``/``"stub-<dim>"`` (deterministic, for
        tests), a bare slug, or a full ``<slug>@<dim>`` identity.
      dim: Optional output-dim override within the model's supported range;
        ignored for stubs. Must agree with any ``@dim`` suffix in ``name``.

    Returns:
      embedder: The session embedder, or ``None`` when ``name`` is empty.

    Raises:
      ConfigError: ``name`` is non-empty and not a known session embedder.

    """
    if not name:
        return None
    if is_weightless(name):
        return _build_stub(name)
    entry, resolved_dim = _resolve_model(name, dim)
    return entry.build(resolved_dim)


def is_weightless(name: str) -> bool:
    """Whether ``name`` names an embedder with no lazily loaded weights.

    A stub (or the unset name) computes vectors from a hash: nothing to download,
    nothing to warm. A real model has weights and benefits from a background
    warm. Lets the lifespan skip both the cache check and the warm task for
    stubs.

    Args:
      name: A ``session_embedder`` config value.

    Returns:
      weightless: ``True`` for the empty name and any ``stub`` / ``stub-<dim>``.

    """
    return not name or name == "stub" or name.startswith("stub-")


def weights_present(name: str, *, dim: int | None = None) -> bool:
    """Whether the named embedder's weights are cached (no network).

    A weightless embedder (unset, or a stub) is always "present". A real model
    delegates to its module's ``weights_present``. Used by the app lifespan to
    decide degrade-vs-warm without downloading. Weights are shared across a
    model's dims (Matryoshka truncates one checkpoint), so ``dim`` only affects
    which slug resolves, not the cache probe.

    Args:
      name: A ``session_embedder`` config value (bare slug or ``slug@dim``).
      dim: Optional dim override (see :func:`_resolve_model`).

    Returns:
      present: ``True`` when the model needs no download (or has no weights).

    """
    if is_weightless(name):
        return True
    entry, _resolved_dim = _resolve_model(name, dim)
    return entry.weights_present()


def build_backfill_embedder(
    name: str,
    *,
    device: str,
    batch_size: int,
    dim: int | None = None,
) -> QueryEmbedder:
    """Build ``name`` for the GPU backfill pipeline on ``device``.

    Unlike :func:`build_session_embedder` (CPU), this passes the GPU device and
    batch size and applies each model's compile policy (see :class:`ModelEntry`).
    Backfill requires a real model, so a disabled/stub name is a caller error.
    ``name``/``dim`` resolve exactly as in :func:`build_session_embedder`.

    Args:
      name: A real model (bare slug or ``slug@dim``).
      device: Torch device string (``"cuda:0"``, ``"cpu"``).
      batch_size: Texts per forward pass.
      dim: Optional dim override within the model's supported range.

    Returns:
      embedder: The device-configured embedder.

    Raises:
      ConfigError: ``name`` is empty, a stub, or not a known real model.

    """
    if is_weightless(name):
        raise ConfigError(f"backfill needs a real model, not {name!r}")
    entry, resolved_dim = _resolve_model(name, dim)
    return entry.backfill(device, batch_size, resolved_dim)


def _qwen_build(cls: Callable[..., QueryEmbedder], dim: int) -> QueryEmbedder:
    """Construct a Qwen-family CPU embedder at ``dim``."""
    return cls(dim=dim)


def _qwen_backfill(
    cls: Callable[..., QueryEmbedder],
    device: str,
    batch_size: int,
    dim: int,
) -> QueryEmbedder:
    """Construct a Qwen-family embedder at ``dim``, compiling the forward on cuda."""
    return cls(
        device=device,
        batch_size=batch_size,
        dim=dim,
        compile_forward=device.startswith("cuda"),
    )


def _jina_backfill(
    cls: Callable[..., QueryEmbedder],
    device: str,
    batch_size: int,
    dim: int,
) -> QueryEmbedder:
    """Construct a Jina embedder at ``dim`` (no compile flag until measured)."""
    return cls(device=device, batch_size=batch_size, dim=dim)


def _qwen3_0p6b_entry() -> ModelEntry:
    """Build the Qwen3-Embedding-0.6B family entry (loads torch on first use)."""
    return ModelEntry(
        slug="qwen3-embedding-0.6b",
        default_dim=1_024,
        spec=bucket_boundaries.QWEN3_0P6B,
        build=lambda dim: _qwen_build(qwen3_0p6b.Qwen06BEmbedder, dim),
        backfill=lambda device, batch_size, dim: _qwen_backfill(
            qwen3_0p6b.Qwen06BEmbedder,
            device,
            batch_size,
            dim,
        ),
        weights_present=qwen3_0p6b.weights_present,
    )


def _qwen3_4b_entry() -> ModelEntry:
    """Build the Qwen3-Embedding-4B family entry (loads torch on first use)."""
    return ModelEntry(
        slug="qwen3-embedding-4b",
        default_dim=1_024,
        spec=bucket_boundaries.QWEN3_4B,
        build=lambda dim: _qwen_build(qwen3_4b.QwenEmbedder, dim),
        backfill=lambda device, batch_size, dim: _qwen_backfill(
            qwen3_4b.QwenEmbedder,
            device,
            batch_size,
            dim,
        ),
        weights_present=qwen3_4b.weights_present,
    )


def _qwen3_8b_entry() -> ModelEntry:
    """Build the Qwen3-Embedding-8B family entry (loads torch on first use)."""
    return ModelEntry(
        slug="qwen3-embedding-8b",
        default_dim=1_024,
        spec=bucket_boundaries.QWEN3_8B,
        build=lambda dim: _qwen_build(qwen3_8b.Qwen8BEmbedder, dim),
        backfill=lambda device, batch_size, dim: _qwen_backfill(
            qwen3_8b.Qwen8BEmbedder,
            device,
            batch_size,
            dim,
        ),
        weights_present=qwen3_8b.weights_present,
    )


def _octen_8b_entry() -> ModelEntry:
    """Build the Octen-Embedding-8B family entry (loads torch on first use)."""
    return ModelEntry(
        slug="octen-embedding-8b",
        default_dim=1_024,
        spec=bucket_boundaries.OCTEN_8B,
        build=lambda dim: _qwen_build(octen_8b.OctenEmbedder, dim),
        backfill=lambda device, batch_size, dim: _qwen_backfill(
            octen_8b.OctenEmbedder,
            device,
            batch_size,
            dim,
        ),
        weights_present=octen_8b.weights_present,
    )


def _jina_v5_text_nano_entry() -> ModelEntry:
    """Build the jina-embeddings-v5-text-nano entry (fixed dim; loads torch late)."""
    return ModelEntry(
        slug="jina-embeddings-v5-text-nano",
        default_dim=768,
        spec=None,  # Fixed-dim: only the native 768 validates.
        build=lambda dim: jina_v5_text_nano.JinaV5NanoEmbedder(dim=dim),
        backfill=lambda device, batch_size, dim: _jina_backfill(
            jina_v5_text_nano.JinaV5NanoEmbedder,
            device,
            batch_size,
            dim,
        ),
        weights_present=jina_v5_text_nano.weights_present,
    )


def _jina_v5_text_small_entry() -> ModelEntry:
    """Build the jina-embeddings-v5-text-small entry (fixed dim; loads torch late)."""
    return ModelEntry(
        slug="jina-embeddings-v5-text-small",
        default_dim=1_024,
        spec=None,  # Fixed-dim: only the native 1024 validates.
        build=lambda dim: jina_v5_text_small.JinaV5SmallEmbedder(dim=dim),
        backfill=lambda device, batch_size, dim: _jina_backfill(
            jina_v5_text_small.JinaV5SmallEmbedder,
            device,
            batch_size,
            dim,
        ),
        weights_present=jina_v5_text_small.weights_present,
    )


# ``slug`` -> a thunk building that family's ``ModelEntry``. The dict is
# torch-free: the thunks are not called at import, and each imports its model
# module lazily. Keyed by the bare slug (the dim-resolution layer); the frozen
# ``session_embeddings.model`` identities (``slug@default_dim``) derive as
# :data:`EMBEDDERS`. ``stub``/``stub-<dim>`` are handled separately.
_MODELS: dict[str, Callable[[], ModelEntry]] = {
    "qwen3-embedding-0.6b": _qwen3_0p6b_entry,
    "qwen3-embedding-4b": _qwen3_4b_entry,
    "qwen3-embedding-8b": _qwen3_8b_entry,
    "octen-embedding-8b": _octen_8b_entry,
    "jina-embeddings-v5-text-nano": _jina_v5_text_nano_entry,
    "jina-embeddings-v5-text-small": _jina_v5_text_small_entry,
}


# The frozen stored identities (``slug@default_dim``), DERIVED from ``_MODELS`` so
# there is one source of model metadata. These are the ``session_embeddings.model``
# keys the shipped partial HNSW indexes and MODEL_BUCKETS are keyed by; a bare or
# ``@dim`` input resolves through ``_resolve_model``. Values are the same entry
# thunks, so existing ``EMBEDDERS[key]()`` consumers are unchanged.
EMBEDDERS: dict[str, Callable[[], ModelEntry]] = {
    f"{slug}@{thunk().default_dim}": thunk for slug, thunk in _MODELS.items()
}


def bucket_specs() -> dict[str, TransformerSpec]:
    """Return ``stored-name -> spec`` for every model with a Matryoshka spec.

    The single source model_buckets derives its spec map from, so there is no
    second spec table to drift. Fixed-dim models (Jina, ``spec is None``) are
    absent -- they carry no bucket plan.

    Returns:
      specs: ``slug@default_dim -> TransformerSpec`` for spec-bearing models.

    """
    out: dict[str, TransformerSpec] = {}
    for slug, thunk in _MODELS.items():
        entry = thunk()
        if entry.spec is not None:
            out[f"{slug}@{entry.default_dim}"] = entry.spec
    return out


def resolved_name(name: str, dim: int | None = None) -> str:
    """Return the full stored identity ``slug@dim`` for ``(name, dim)``.

    The torch-free way to learn the ``session_embeddings.model`` string a backfill
    worker will write, without building the embedder (which loads weights). The
    scanner uses it to keyset-walk only the rows still pending for THIS model.

    Args:
      name: A bare slug or a full ``slug@dim``.
      dim: Optional dim override within the model's supported range.

    Returns:
      identity: The resolved ``slug@dim`` stored name.

    Raises:
      ConfigError: unknown slug, disagreeing dims, or an unsupported dim.

    """
    entry, resolved_dim = _resolve_model(name, dim)
    return f"{entry.slug}@{resolved_dim}"


# ``name`` is a bare slug (``qwen3-embedding-4b``) or the full ``slug@dim``. A ``@dim``
# suffix and the ``dim`` argument, if both present, must agree. The resolved dim
# defaults to the model's ``default_dim``; an explicit dim is validated against the
# model's supported range (its spec's ``min..max`` for a Matryoshka model, or exactly
# ``default_dim`` for a fixed model) and a violation is a hard error naming the range.
# The stored identity is always the full ``slug@dim`` (resolution is input sugar only).
def _resolve_model(name: str, dim: int | None) -> tuple[ModelEntry, int]:
    """Resolve ``(name, dim)`` to a model entry and its concrete output dim."""
    slug, suffix_dim = _split_name(name)
    thunk = _MODELS.get(slug)
    if thunk is None:
        raise ConfigError(_unknown_message(name))
    entry = thunk()
    if suffix_dim is not None and dim is not None and suffix_dim != dim:
        raise ConfigError(
            f"dim mismatch for {name!r}: suffix @{suffix_dim} vs --dim {dim}",
        )
    chosen = dim if dim is not None else suffix_dim
    if chosen is None:
        return entry, entry.default_dim
    if not _dim_supported(entry, chosen):
        raise ConfigError(_dim_range_message(entry, chosen))
    return entry, chosen


def _split_name(name: str) -> tuple[str, int | None]:
    """Split ``slug@dim`` into ``(slug, dim)``; a bare name has ``dim=None``."""
    slug, sep, dim_text = name.partition("@")
    if not sep:
        return name, None
    try:
        return slug, int(dim_text)
    except ValueError:
        raise ConfigError(f"malformed dim in {name!r}") from None


def _dim_supported(entry: ModelEntry, dim: int) -> bool:
    """Whether ``dim`` is valid for ``entry`` (spec range, or fixed native dim)."""
    if entry.spec is None:
        return dim == entry.default_dim
    return bucket_boundaries.dim_in_range(entry.spec, dim)


def _dim_range_message(entry: ModelEntry, dim: int) -> str:
    """Return a config-error message naming the model's supported dim range."""
    if entry.spec is None:
        return (
            f"{entry.slug} is fixed-dim; dim {dim} is unsupported "
            f"(only {entry.default_dim})"
        )
    return (
        f"{entry.slug} supports dims {entry.spec.min_dim}..{entry.spec.max_dim}; "
        f"dim {dim} is out of range"
    )


# ``get_close_matches`` surfaces the intended slug for a typo. Candidates are the
# bare slugs (the resolver accepts a bare name) plus the stub knobs.
def _unknown_message(name: str) -> str:
    """Return a config-error message naming the nearest valid model."""
    slug, _sep, _dim = name.partition("@")
    candidates = [*_MODELS, "stub", "stub-<dim>"]
    near = get_close_matches(slug, candidates, n=1)
    hint = f" (did you mean {near[0]!r}?)" if near else ""
    return f"unknown session embedder {name!r}{hint}"


def _build_stub(name: str) -> QueryEmbedder:
    """Build a ``StubEmbedder`` from a ``stub`` / ``stub-<dim>`` knob."""
    if name == "stub":
        return StubEmbedder()
    try:
        dim = int(name.removeprefix("stub-"))
    except ValueError:
        raise ConfigError(f"unknown session embedder {name!r}") from None
    return StubEmbedder(dim=dim)
