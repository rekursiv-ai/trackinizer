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
    from trackinizer.server.embedders import qwen3_0p6b, qwen3_4b
    from trackinizer.server.embedders.qwen3_0p6b import (
        weights_present as qwen3_0p6b_weights_present,
    )
    from trackinizer.server.embedders.qwen3_4b import (
        weights_present as qwen3_4b_weights_present,
    )
else:
    from wrapt import lazy_import

    # Each model module pulls torch/transformers on FIRST attribute access only;
    # binding them as lazy_import proxies keeps importing this registry (which
    # runs at config time) torch-free while staying top-level (no PLC0415).
    qwen3_0p6b = lazy_import("trackinizer.server.embedders.qwen3_0p6b")
    qwen3_4b = lazy_import("trackinizer.server.embedders.qwen3_4b")
    qwen3_0p6b_weights_present = lazy_import(
        "trackinizer.server.embedders.qwen3_0p6b",
        "weights_present",
    )
    qwen3_4b_weights_present = lazy_import(
        "trackinizer.server.embedders.qwen3_4b",
        "weights_present",
    )


__all__ = [
    "EMBEDDERS",
    "ModelEntry",
    "bucket_specs",
    "build_backfill_embedder",
    "build_cached_session_embedder",
    "build_session_embedder",
    "is_weightless",
    "resolved_name",
    "weights_present",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelEntry:
    """A registered real model family: how to build it and probe its weights.

    One entry per model SLUG (the bare stored-name prefix). It carries the dim
    contract (:attr:`spec`, whose ``min_dim..max_dim`` is the Matryoshka range,
    and :attr:`default_dim`) so ``(name, dim)`` resolution has a single source,
    plus dim-aware factories.

    Attributes:
      slug: The bare stored-name prefix (``"qwen3-embedding-4b"``).
      default_dim: The dim a bare name resolves to (the shipped index width).
      spec: The model's :class:`~bucket_boundaries.TransformerSpec`; its
        ``min_dim..max_dim`` is the valid ``--dim`` range.
      build: ``dim -> `` fresh CPU embedder at that dim (imports torch inside).
      backfill: ``(device, batch_size, dim) -> `` embedder for throughput (the GPU
        pipeline, and the follow service on CPU), with the forward compiled --
        measured 2.79x on a 5090, and frozen on CPU 12.0 vs 3.6-6.7 vec/s on 32
        Zen 5 threads.
      weights_present: No-network cache check for this model's weights.

    """

    slug: str
    default_dim: int
    spec: TransformerSpec
    build: Callable[[int], QueryEmbedder]
    backfill: Callable[[str, int, int], QueryEmbedder]
    weights_present: Callable[[], bool]


def build_session_embedder(name: str) -> QueryEmbedder | None:
    """Build the session-search embedder for ``name``, or ``None`` when disabled.

    An empty name returns ``None`` so the session-search route degrades to
    full-text only rather than 500, and each real model is imported lazily so an
    unset knob never pulls in torch.

    ``name`` may be a bare slug (``qwen3-embedding-4b``), which resolves to the
    model's default dim, or the full ``slug@dim``. See :func:`_resolve_model`.

    Args:
      name: ``""`` (disabled), ``"stub"``/``"stub-<dim>"`` (deterministic, for
        tests), a bare slug, or a full ``<slug>@<dim>`` identity.

    Returns:
      embedder: The session embedder, or ``None`` when ``name`` is empty.

    Raises:
      ConfigError: ``name`` is non-empty and not a known session embedder.

    """
    if not name:
        return None
    if is_weightless(name):
        return _build_stub(name)
    entry, resolved_dim = _resolve_model(name, None)
    return entry.build(resolved_dim)


def build_cached_session_embedder(name: str) -> QueryEmbedder | None:
    """Build only a locally cached session embedder; otherwise disable semantics."""
    if not weights_present(name):
        return None
    return build_session_embedder(name)


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


def weights_present(name: str) -> bool:
    """Whether the named embedder's weights are cached (no network).

    A weightless embedder (unset, or a stub) is always "present". A real model
    delegates to its module's ``weights_present``. Used by the app lifespan to
    decide degrade-vs-warm without downloading. Weights are shared across a
    model's dims (Matryoshka truncates one checkpoint), so the ``@dim`` only
    affects which slug resolves, not the cache probe.

    Args:
      name: A ``session_embedder`` config value (bare slug or ``slug@dim``).

    Returns:
      present: ``True`` when the model needs no download (or has no weights).

    """
    if is_weightless(name):
        return True
    entry, _resolved_dim = _resolve_model(name, None)
    return entry.weights_present()


def build_backfill_embedder(
    name: str,
    *,
    device: str,
    batch_size: int,
    dim: int | None = None,
) -> QueryEmbedder:
    """Build ``name`` for throughput on ``device``: the GPU pipeline, the CPU follow.

    Unlike :func:`build_session_embedder` (the server's query embedder), this
    passes the device and batch size and compiles the forward (see
    :class:`ModelEntry`). Backfill requires a real model, so a disabled/stub name
    is a caller error. ``name``/``dim`` resolve as in :func:`resolved_name`.

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
    """Construct a Qwen-family embedder at ``dim`` with its forward compiled."""
    return cls(
        device=device,
        batch_size=batch_size,
        dim=dim,
        compile_forward=True,
    )


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
        weights_present=qwen3_0p6b_weights_present,
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
        weights_present=qwen3_4b_weights_present,
    )


# ``slug`` -> a thunk building that family's ``ModelEntry``. The entry thunks
# defer model access until build/backfill/cache-check: binding weights_present
# directly would load every lazy model while deriving EMBEDDERS below.
# Keyed by the bare slug (the dim-resolution layer); the frozen
# ``session_embeddings.model`` identities (``slug@default_dim``) derive as
# :data:`EMBEDDERS`. ``stub``/``stub-<dim>`` are handled separately.
_MODELS: dict[str, Callable[[], ModelEntry]] = {
    "qwen3-embedding-0.6b": _qwen3_0p6b_entry,
    "qwen3-embedding-4b": _qwen3_4b_entry,
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
    """Return ``stored-name -> spec`` for every registered model.

    The single source model_buckets derives its spec map from, so there is no
    second spec table to drift.

    Returns:
      specs: ``slug@default_dim -> TransformerSpec`` for every model.

    """
    return {name: thunk().spec for name, thunk in EMBEDDERS.items()}


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
# model's Matryoshka range (its spec's ``min..max``) and a violation is a hard error
# naming the range. The stored identity is always the full ``slug@dim`` (resolution
# is input sugar only).
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
    if not bucket_boundaries.dim_in_range(entry.spec, chosen):
        raise ConfigError(
            f"{entry.slug} supports dims {entry.spec.min_dim}..{entry.spec.max_dim}; "
            f"dim {chosen} is out of range",
        )
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
