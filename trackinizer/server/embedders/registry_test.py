"""The session-embedder registry: every name builds, names/dims are coherent.

These never load weights: ``build_session_embedder`` constructs the embedder
(the model modules load lazily on first ``embed``), so the whole file runs in
milliseconds with no network. The one contract that matters most here is that
importing the registry stays torch-free -- config.py imports it at config time.
"""

from __future__ import annotations

from pathlib import Path

import importlib
import subprocess
import sys

import pytest

from trackinizer.server.config import ConfigError
from trackinizer.server.embedders import registry


def test_importing_registry_pulls_no_torch() -> None:
    """The registry (and thus config) must import without dragging in torch.

    Probed in a FRESH interpreter, not against this process's ``sys.modules``:
    under xdist a sibling test on the same worker may have imported torch
    already, so an in-process assertion measures worker history, not the
    registry's own import graph. The subprocess resolves the module by its
    import name so the check survives the standalone export's different depth.
    """
    module = registry.__name__
    package = importlib.import_module(module.split(".", 1)[0])
    assert package.__file__ is not None
    source = (
        "import sys; "
        "assert 'torch' not in sys.modules; "
        "assert 'transformers' not in sys.modules; "
        "import MODULE; "
        "assert 'torch' not in sys.modules; "
        "assert 'transformers' not in sys.modules"
    ).replace("MODULE", module)
    probe = subprocess.run(  # noqa: S603 -- argv is this module's own import path.
        [sys.executable, "-c", source],
        cwd=Path(package.__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_every_registry_name_builds() -> None:
    """Each registry key constructs an embedder without loading weights."""
    for name in registry.EMBEDDERS:
        embedder = registry.build_session_embedder(name)
        assert embedder is not None
        # The stored identity the builder returns must equal its registry key --
        # they are the same string everywhere (knob, key, session_embeddings.model,
        # partial-index WHERE); a mismatch would orphan rows.
        assert embedder.name == name


def test_registry_names_are_unique() -> None:
    """No two registry entries share a stored name (they key DB rows)."""
    embedders = [registry.build_session_embedder(name) for name in registry.EMBEDDERS]
    assert all(e is not None for e in embedders)
    names = [e.name for e in embedders if e is not None]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    ("name", "dim"),
    [
        ("qwen3-embedding-0.6b@1024", 1024),
        ("qwen3-embedding-4b@1024", 1024),
        ("qwen3-embedding-8b@1024", 1024),
        ("octen-embedding-8b@1024", 1024),
        ("jina-embeddings-v5-text-nano@768", 768),
        ("jina-embeddings-v5-text-small@1024", 1024),
    ],
)
def test_registry_dim_matches_the_name_suffix(name: str, dim: int) -> None:
    """A model's ``dim`` equals the ``@<dim>`` in its stored name."""
    embedder = registry.build_session_embedder(name)
    assert embedder is not None
    assert embedder.dim == dim
    assert name.endswith(f"@{dim}")


def test_empty_name_disables_the_arm() -> None:
    """An empty name returns ``None`` so the search route degrades to full-text."""
    assert registry.build_session_embedder("") is None


def test_bare_name_resolves_to_the_registered_default_dim() -> None:
    """A bare model name (no ``@dim``) resolves to its registered default.

    A dim that isn't a choice shouldn't need to be specified; today every model
    has exactly one registered dim, so every bare name resolves to it.
    """
    embedder = registry.build_session_embedder("qwen3-embedding-4b")
    assert embedder is not None
    assert embedder.name == "qwen3-embedding-4b@1024"  # Stored identity is full.
    assert embedder.dim == 1_024


def test_resolved_name_returns_the_full_identity_torch_free() -> None:
    """``resolved_name`` gives the stored ``slug@dim`` without building weights.

    The scanner learns the ``session_embeddings.model`` string it must filter on
    without loading torch; a bare name, an explicit dim, and the @-form all map to
    the same full identity.
    """
    assert registry.resolved_name("qwen3-embedding-4b") == "qwen3-embedding-4b@1024"
    assert registry.resolved_name("qwen3-embedding-4b", 512) == "qwen3-embedding-4b@512"
    assert (
        registry.resolved_name("qwen3-embedding-4b@1024") == "qwen3-embedding-4b@1024"
    )


def test_bare_name_with_supported_dim_mints_that_identity() -> None:
    """Name + an in-range Matryoshka dim resolves to ``<name>@<dim>``."""
    embedder = registry.build_session_embedder("qwen3-embedding-4b", dim=512)
    assert embedder is not None
    assert embedder.name == "qwen3-embedding-4b@512"  # A legitimate new identity.
    assert embedder.dim == 512


def test_at_form_still_resolves_as_before() -> None:
    """The full ``<name>@<dim>`` spelling remains valid input (deployed env)."""
    embedder = registry.build_session_embedder("qwen3-embedding-4b@1024")
    assert embedder is not None
    assert embedder.name == "qwen3-embedding-4b@1024"


def test_unsupported_dim_errors_naming_the_range() -> None:
    """A dim the model does not support is a hard error naming the range."""
    with pytest.raises(ConfigError, match=r"32.*2560"):
        registry.build_session_embedder("qwen3-embedding-4b", dim=4_096)


def test_fixed_model_rejects_a_non_native_dim() -> None:
    """A fixed-dim (non-MRL) model accepts only its native dim."""
    with pytest.raises(ConfigError, match="768"):
        registry.build_session_embedder("jina-embeddings-v5-text-nano", dim=512)


def test_backfill_resolves_bare_name_and_dim() -> None:
    """The backfill builder resolves ``(name, dim)`` the same way."""
    embedder = registry.build_backfill_embedder(
        "qwen3-embedding-4b",
        device="cpu",
        batch_size=8,
        dim=512,
    )
    assert embedder.name == "qwen3-embedding-4b@512"
    assert embedder.dim == 512


def test_weights_present_resolves_bare_name() -> None:
    """``weights_present`` accepts a bare name (resolution is shared)."""
    # Stubs are always present; a bare real name resolves then probes its cache.
    assert registry.weights_present("stub")


@pytest.mark.parametrize(
    ("name", "stored_name", "dim"),
    [
        ("stub", "stub", 384),
        ("stub-1024", "stub-1024", 1024),
        # The 384 stub's stored name is the literal "stub" (it keys existing
        # inquiry_embeddings rows), so the knob ``stub-384`` and the stored name
        # deliberately diverge -- a caller wanting the 384 stub writes ``stub``.
        ("stub-384", "stub", 384),
    ],
)
def test_stub_names_build_a_stub(name: str, stored_name: str, dim: int) -> None:
    """The ``stub`` / ``stub-<dim>`` knob builds a deterministic stub."""
    embedder = registry.build_session_embedder(name)
    assert embedder is not None
    assert embedder.name == stored_name
    assert embedder.dim == dim


def test_unknown_name_errors_with_a_did_you_mean() -> None:
    """A genuinely unknown model name errors, naming the nearest real key."""
    with pytest.raises(ConfigError, match="did you mean 'qwen3-embedding-4b"):
        registry.build_session_embedder("qwen3-embeddings-4b")  # typo'd slug.


def test_is_weightless_only_for_stubs_and_empty() -> None:
    """Only the empty name and stubs are weightless; real models are not."""
    assert registry.is_weightless("")
    assert registry.is_weightless("stub")
    assert registry.is_weightless("stub-1024")
    assert not registry.is_weightless("qwen3-embedding-4b@1024")
    assert not registry.is_weightless("jina-embeddings-v5-text-nano@768")


def test_weights_present_is_true_for_weightless() -> None:
    """A stub / unset name is always 'present' (no weights to download)."""
    assert registry.weights_present("")
    assert registry.weights_present("stub-1024")


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
