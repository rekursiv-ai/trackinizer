"""The session-embedder registry: every name builds, names/dims are coherent.

These never load weights: ``build_session_embedder`` constructs the embedder
(the model modules load lazily on first ``embed``), so the whole file runs in
milliseconds with no network. The one contract that matters most here is that
importing the registry stays torch-free -- config.py imports it at config time.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import importlib
import subprocess
import sys

import pytest

from trackinizer.server.config import ConfigError
from trackinizer.server.embedders import registry
from trackinizer.server.embedders.jina import JinaV5Embedder
from trackinizer.server.embedders.qwen_family import QwenFamilyEmbedder


_QWEN_FAMILY = (
    "octen-embedding-8b@1024",
    "qwen3-embedding-0.6b@1024",
    "qwen3-embedding-4b@1024",
    "qwen3-embedding-8b@1024",
)


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


@pytest.mark.parametrize("name", sorted(registry.EMBEDDERS))
def test_every_entry_builds_its_own_model_both_ways(name: str) -> None:
    """The query build and the throughput build make the same model at the dim.

    The throughput build (the GPU pipeline, the CPU follow service) must also take
    its device and batch size, or a worker pinned to a GPU would embed on the CPU.
    Half the default dim, so a builder that dropped the dim would show it.
    """
    entry = registry.EMBEDDERS[name]()
    half = entry.default_dim // 2
    built = entry.build(half)
    throughput = entry.backfill("cuda:1", 3, half)

    assert entry.slug == name.partition("@")[0]
    assert (built.name, throughput.name) == (f"{entry.slug}@{half}",) * 2
    assert type(throughput) is type(built)
    assert isinstance(throughput, QwenFamilyEmbedder | JinaV5Embedder)
    assert (throughput._device, throughput._batch_size) == ("cuda:1", 3)
    assert isinstance(entry.weights_present(), bool)


@pytest.mark.parametrize("name", _QWEN_FAMILY)
def test_only_the_throughput_build_compiles_a_qwen_family_forward(name: str) -> None:
    """The throughput build compiles the forward on every device; the query build not.

    The CPU follow service rests on it: compiled and frozen, the 0.6B embeds
    12.0 vec/s on 32 threads against 3.6-6.7 eager.
    """
    entry = registry.EMBEDDERS[name]()
    built = entry.build(entry.default_dim)
    throughput = entry.backfill("cpu", 8, entry.default_dim)

    assert isinstance(built, QwenFamilyEmbedder)
    assert isinstance(throughput, QwenFamilyEmbedder)
    assert (built._compile_forward, throughput._compile_forward) == (False, True)


@pytest.mark.parametrize("name", sorted(registry.EMBEDDERS))
def test_every_bare_slug_resolves_to_its_stored_name(name: str) -> None:
    """A bare slug resolves to the entry's stored ``slug@default_dim`` identity."""
    assert registry.resolved_name(name.partition("@")[0]) == name


def test_bucket_specs_hold_every_qwen_family_model_with_its_dim_range() -> None:
    """Each Qwen-family model has a spec, so the GPU plan has its buckets.

    The spec is also its Matryoshka range, so each takes a dim below its default.
    """
    specs = registry.bucket_specs()

    assert specs == {name: registry.EMBEDDERS[name]().spec for name in _QWEN_FAMILY}
    assert None not in specs.values()
    for name in specs:
        slug = name.partition("@")[0]
        assert registry.resolved_name(slug, 512) == f"{slug}@512"


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


def test_at_suffix_names_the_dim_without_a_dim_argument() -> None:
    """``slug@dim`` alone resolves to that dim, not the model's default.

    The server and the follow service read their models as ``slug@dim`` from
    ``TRACKINIZER_SESSION_EMBEDDER(S)``, with no dim argument beside them.
    """
    assert registry.resolved_name("qwen3-embedding-4b@512") == "qwen3-embedding-4b@512"


def test_suffix_and_dim_argument_must_agree() -> None:
    """A ``@dim`` suffix and a dim argument resolve when equal and error otherwise."""
    same = registry.resolved_name("qwen3-embedding-4b@512", 512)

    assert same == "qwen3-embedding-4b@512"
    with pytest.raises(ConfigError, match="suffix @1024 vs --dim 512"):
        registry.resolved_name("qwen3-embedding-4b@1024", 512)


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
    with pytest.raises(ConfigError, match=r"32\.\.2560; dim 4096 is out of range"):
        registry.build_session_embedder("qwen3-embedding-4b", dim=4_096)


def test_fixed_model_rejects_a_non_native_dim() -> None:
    """A fixed-dim (non-MRL) model accepts only its native dim."""
    with pytest.raises(ConfigError, match=r"dim 512 is unsupported \(only 768\)"):
        registry.build_session_embedder("jina-embeddings-v5-text-nano", dim=512)


def test_backfill_resolves_bare_name_and_dim() -> None:
    """The backfill builder resolves ``(name, dim)`` the same way, on its device."""
    embedder = registry.build_backfill_embedder(
        "qwen3-embedding-4b",
        device="cuda:1",
        batch_size=8,
        dim=512,
    )
    assert embedder.name == "qwen3-embedding-4b@512"
    assert embedder.dim == 512
    assert isinstance(embedder, QwenFamilyEmbedder)
    assert (embedder._device, embedder._batch_size) == ("cuda:1", 8)


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


def test_cached_builder_never_constructs_uncached_weights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = Mock(return_value=False)
    build = Mock(side_effect=AssertionError("Uncached model constructed"))
    monkeypatch.setattr(registry, "weights_present", check)
    monkeypatch.setattr(registry, "build_session_embedder", build)
    assert registry.build_cached_session_embedder("qwen3-embedding-4b", dim=256) is None
    check.assert_called_once_with("qwen3-embedding-4b", dim=256)
    build.assert_not_called()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
