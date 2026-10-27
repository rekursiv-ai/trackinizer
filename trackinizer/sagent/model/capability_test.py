"""Tests for what a model offers, the meet with a transport, and settings validation."""

from __future__ import annotations

from dataclasses import fields, replace
from typing import TYPE_CHECKING, Final

import pytest

from trackinizer.sagent.model.capability import (
    ModelCapability,
    ModelLimits,
    ModelSettings,
    PruningPolicy,
    ThinkingCapability,
)
from trackinizer.sagent.model.cost import (
    PriceCatalog,
    PriceKey,
    ServiceTier,
    TokenPrice,
)


if TYPE_CHECKING:
    from trackinizer.sagent.model.capability import ThinkingBudget, ThinkingEffort


def _price(request: float) -> TokenPrice:
    return TokenPrice(
        request=request,
        response=0.0,
        cache_write=0.0,
        cache_write_1h=0.0,
        cache_read=0.0,
    )


def _row() -> ModelCapability:
    return ModelCapability(
        model_id="m",
        limits=ModelLimits(max_request_tokens=1_000_000),
        prices=PriceCatalog({PriceKey("auto"): _price(5.0)}),
        thinking=ThinkingCapability(
            effort=frozenset({"none", "low", "high"}),
            budget=frozenset({"none", "auto"}),
            output=frozenset({"none", "text", "redacted"}),
        ),
        service_tier=frozenset({"auto", "default", "priority"}),
        cache_ttl_sec=frozenset({300.0, 3600.0}),
        manage_context_server_side=frozenset({False, True}),
        retries_internally=True,
    )


_OFF = ModelCapability(
    thinking=ThinkingCapability(
        effort=frozenset({"none"}),
        budget=frozenset({"none"}),
        output=frozenset({"none"}),
    ),
    service_tier=frozenset({"auto"}),
    cache_ttl_sec=frozenset({0.0}),
    manage_context_server_side=frozenset({False}),
)

_UNOFFERED: Final = (
    ("context", -1),
    ("thinking_effort", "medium"),
    ("thinking_budget", "fixed"),
    ("thinking_output", "text"),
    ("service_tier", "flex"),
    ("cache_ttl_sec", 300.0),
    ("manage_context_server_side", True),
)


# ---- the meet --------------------------------------------------------------


def test_every_field_is_default_constructible() -> None:
    assert ModelCapability().model_id == ""
    assert ModelSettings().context == 0


def test_a_bare_transport_keeps_the_axis_a_row_cannot_know() -> None:
    """A row does not know who rolls history, so it must not pin that axis."""
    met = _row() & ModelCapability()
    assert met.manage_context_server_side == frozenset({False, True})
    assert met.thinking.effort == frozenset({"none"})
    assert met.thinking.budget == frozenset({"none"})
    assert met.thinking.output == frozenset({"none"})
    assert met.service_tier == frozenset({"auto", "default"})


def test_the_meet_only_removes() -> None:
    transport = ModelCapability(
        thinking=ThinkingCapability(
            effort=frozenset({"none", "low", "medium", "high", "max"}),
            budget=frozenset({"none", "auto", "fixed"}),
            output=frozenset({"none", "text"}),
        ),
        service_tier=frozenset({"auto", "default", "flex", "priority"}),
        manage_context_server_side=frozenset({True}),
    )
    met = _row() & transport
    assert met.thinking.effort == frozenset({"none", "low", "high"})
    assert met.thinking.budget == frozenset({"none", "auto"})
    assert met.thinking.output == frozenset({"none", "text"})
    assert met.service_tier == frozenset({"auto", "default", "priority"})
    assert met.manage_context_server_side == frozenset({True})


def test_the_transport_declares_the_cache_lifetimes() -> None:
    """Not intersected: only the transport knows if it writes a breakpoint."""
    offered = frozenset({300.0, 7200.0})
    assert (_row() & ModelCapability(cache_ttl_sec=offered)).cache_ttl_sec == offered
    assert (_row() & ModelCapability()).cache_ttl_sec == frozenset({0.0})


def test_the_meet_passes_model_facts_through() -> None:
    row = ModelCapability(
        model_id="m",
        knowledge_cutoff="2026-01",
        effort_as_level=True,
        limits=_row().limits,
        prices=_row().prices,
    )
    met = row & ModelCapability(model_id="t", effort_as_level=False)
    assert met.model_id == "m"
    assert met.knowledge_cutoff == "2026-01"
    assert met.effort_as_level is True
    assert met.limits == _row().limits
    assert met.prices == _row().prices


def test_the_transport_declares_retries_and_auth() -> None:
    """A catalog row cannot know who bills or retries it."""
    row = _row()
    assert (row & ModelCapability(retries_internally=True)).retries_internally is True
    assert (row & ModelCapability()).retries_internally is False
    assert (row & ModelCapability(account_auth=True)).account_auth is True
    assert (row & ModelCapability()).account_auth is False


# ---- assignment validates --------------------------------------------------


def test_a_valid_selection_constructs() -> None:
    settings = ModelSettings(
        capability=_row(),
        context=200_000,
        thinking_effort="high",
        thinking_budget="auto",
        thinking_output="redacted",
        service_tier="priority",
        cache_ttl_sec=300.0,
        manage_context_server_side=True,
    )
    assert settings.thinking_output == "redacted"


def test_assignment_rejects_what_the_model_withholds() -> None:
    settings = ModelSettings.narrowest(_row())
    with pytest.raises(ValueError, match="thinking_effort='medium'"):
        settings.thinking_effort = "medium"
    assert settings.thinking_effort == "none"


def test_construction_rejects_what_the_model_withholds() -> None:
    with pytest.raises(ValueError, match="thinking_effort='medium'"):
        ModelSettings(capability=_row(), cache_ttl_sec=300.0, thinking_effort="medium")


def test_the_cache_axis_validates_by_membership() -> None:
    ModelSettings(capability=_row(), cache_ttl_sec=3600.0)
    with pytest.raises(ValueError, match="not offered by m"):
        ModelSettings(capability=_row(), cache_ttl_sec=1800.0)


def test_the_error_names_the_model_and_what_it_offers() -> None:
    with pytest.raises(ValueError, match="not offered by m") as excinfo:
        ModelSettings(capability=_row(), cache_ttl_sec=300.0, service_tier="flex")
    assert "allowed: 'auto', 'default', 'priority'" in str(excinfo.value)


def test_an_unnamed_model_is_called_this_model() -> None:
    with pytest.raises(ValueError, match="not offered by this model"):
        ModelSettings.narrowest(_OFF).service_tier = "flex"


@pytest.mark.parametrize(("name", "value"), _UNOFFERED, ids=[n for n, _ in _UNOFFERED])
def test_assignment_checks_every_axis(name: str, value: object) -> None:
    settings = ModelSettings.narrowest(_OFF)
    with pytest.raises(ValueError, match=name):
        setattr(settings, name, value)


def test_the_axis_cases_cover_every_offered_axis() -> None:
    """Without this, a new ``ModelSettings`` axis is untested but green."""
    axes = {f.name for f in fields(ModelSettings)} - {"capability", "pruning"}
    assert {n for n, _ in _UNOFFERED} == axes


def test_any_model_takes_a_pruning_policy() -> None:
    """Pruning is the caller's policy, not something a model offers."""
    settings = ModelSettings.narrowest(_OFF)
    policy = PruningPolicy(trigger_tokens=100, target_tokens=50)
    settings.pruning = policy
    assert settings.pruning == policy
    settings.take(pruning=None)
    assert settings.pruning is None


def test_the_capability_itself_is_assignable() -> None:
    settings = ModelSettings.narrowest(_OFF)
    settings.capability = _row()
    settings.thinking_effort = "high"
    assert settings.thinking_effort == "high"


def test_capability_assignment_renarrows_every_axis_and_keeps_the_size() -> None:
    """The compaction size is the caller's, not the model's to withhold."""
    settings = ModelSettings.narrowest(_row(), context=200_000)
    settings.thinking_effort = "high"
    settings.capability = _OFF
    assert settings.capability == _OFF
    assert settings.context == 200_000
    assert settings.limits == ModelLimits()
    assert settings.thinking_effort == "none"
    assert settings.cache_ttl_sec == 0.0


@pytest.mark.parametrize(
    ("request_tokens", "context_tokens", "shared"),
    [(200_000, 0, 200_000), (272_000, 1_050_000, 1_050_000), (0, 0, 0)],
)
def test_the_context_is_the_request_window_unless_stated(
    request_tokens: int,
    context_tokens: int,
    shared: int,
) -> None:
    limits = ModelLimits(
        max_request_tokens=request_tokens,
        max_context_tokens=context_tokens,
    )
    assert limits.context_tokens == shared


def test_capability_assignment_keeps_what_the_new_one_still_offers() -> None:
    settings = ModelSettings.narrowest(_row(), context=200_000)
    settings.thinking_effort = "high"
    settings.service_tier = "priority"
    settings.capability = _row()
    assert settings.context == 200_000
    assert settings.thinking_effort == "high"
    assert settings.service_tier == "priority"


def test_invalid_capability_assignment_leaves_settings_unchanged() -> None:
    settings = ModelSettings.narrowest(_row(), context=200_000)
    with pytest.raises(ValueError, match="cache_ttl_sec offers nothing"):
        settings.capability = ModelCapability(cache_ttl_sec=frozenset())
    assert settings.capability == _row()
    assert settings.context == 200_000


# ---- take / adopt ----------------------------------------------------------


def test_take_applies_what_is_offered() -> None:
    settings = ModelSettings.narrowest(_row())
    settings.take(thinking_effort="high", thinking_budget="auto")
    assert settings.thinking_effort == "high"
    assert settings.thinking_budget == "auto"


def test_take_falls_back_to_the_lowest_rung_instead_of_raising() -> None:
    settings = ModelSettings.narrowest(_row())
    settings.thinking_effort = "high"
    settings.take(thinking_effort="max", thinking_budget="auto")
    assert settings.thinking_effort == "none"
    assert settings.thinking_budget == "auto"


@pytest.mark.parametrize("name", ["nonsense", "capability"])
def test_take_rejects_a_name_that_is_not_an_axis(name: str) -> None:
    with pytest.raises(ValueError, match=f"not settings axes: {name}"):
        ModelSettings().take(**{name: 1})


def test_take_names_every_unknown_axis_sorted() -> None:
    with pytest.raises(ValueError, match=r"not settings axes: alpha, beta$"):
        ModelSettings().take(beta=1, alpha=2)


def test_take_falls_back_to_the_whole_window_for_a_negative_size() -> None:
    settings = ModelSettings.narrowest(_row(), context=200_000)
    settings.take(context=-1)
    assert settings.context == 0


def test_adopt_carries_the_selection_across_a_swap() -> None:
    old = ModelSettings(
        capability=_row(),
        cache_ttl_sec=3600.0,
        thinking_effort="high",
        service_tier="priority",
    )
    new = ModelSettings(capability=_row(), cache_ttl_sec=300.0)
    new.adopt(old)
    assert new.thinking_effort == "high"
    assert new.service_tier == "priority"
    assert new.cache_ttl_sec == 3600.0


def test_adopt_keeps_the_incoming_size() -> None:
    """The incoming model's name may have chosen its size, as ``opus+small``."""
    old = ModelSettings(capability=_row(), cache_ttl_sec=300.0, context=258_000)
    new = ModelSettings(capability=_row(), cache_ttl_sec=300.0, context=100_000)
    new.adopt(old)
    assert new.context == 100_000


def test_adopt_drops_a_knob_the_new_model_rejects() -> None:
    old = ModelSettings(capability=_row(), cache_ttl_sec=300.0, thinking_effort="high")
    new = ModelSettings.narrowest(_OFF)
    new.adopt(old)
    assert new.thinking_effort == "none"


# ---- narrowest -------------------------------------------------------------


def test_narrowest_is_the_unset_value_of_every_axis_that_offers_one() -> None:
    assert ModelSettings.narrowest(_row()) == ModelSettings(
        capability=_row(),
        cache_ttl_sec=300.0,
    )


def test_narrowest_selects_the_shortest_cache_offered() -> None:
    assert ModelSettings.narrowest(_row()).cache_ttl_sec == 300.0
    assert ModelSettings.narrowest(ModelCapability()).cache_ttl_sec == 0.0


def test_narrowest_carries_the_context_and_capability() -> None:
    settings = ModelSettings.narrowest(_row(), context=200_000)
    assert settings.context == 200_000
    assert settings.capability == _row()


@pytest.mark.parametrize(
    ("default", "effort"),
    [
        (None, "none"),
        ("none", "none"),
        ("high", "high"),
        ("medium", "low"),
        ("min", "low"),
    ],
    ids=["unset", "none", "offered", "nearest-below", "nearest-above"],
)
def test_narrowest_starts_at_the_default_effort_the_row_offers(
    default: ThinkingEffort | None,
    effort: str,
) -> None:
    row = replace(_row(), default_effort=default)
    assert ModelSettings.narrowest(row).thinking_effort == effort


def test_the_meet_keeps_the_rows_default_effort() -> None:
    row = replace(_row(), default_effort="high")
    assert (row & ModelCapability()).default_effort == "high"
    assert hash(row) != hash(_row())


@pytest.mark.parametrize(
    ("default", "budget"),
    [(None, "none"), ("none", "none"), ("auto", "auto"), ("fixed", "fixed")],
    ids=["unset", "none", "auto", "fixed"],
)
def test_narrowest_starts_at_the_default_budget(
    default: ThinkingBudget | None,
    budget: str,
) -> None:
    row = replace(
        _row(),
        thinking=replace(_row().thinking, budget=frozenset({"none", "auto", "fixed"})),
        default_budget=default,
    )
    assert ModelSettings.narrowest(row).thinking_budget == budget


def test_a_default_budget_the_row_lacks_falls_to_the_lowest_it_offers() -> None:
    row = replace(
        _row(),
        thinking=replace(_row().thinking, budget=frozenset({"none", "fixed"})),
        default_budget="auto",
    )
    assert ModelSettings.narrowest(row).thinking_budget == "none"


def test_the_meet_keeps_the_rows_default_budget() -> None:
    row = replace(_row(), default_budget="auto")
    assert (row & ModelCapability()).default_budget == "auto"
    assert hash(row) != hash(_row())


def test_narrowest_climbs_an_axis_that_withholds_its_unset_value() -> None:
    row = ModelCapability(
        thinking=ThinkingCapability(
            effort=frozenset({"low", "high"}),
            budget=frozenset({"fixed", "auto"}),
            output=frozenset({"redacted", "text"}),
        ),
        service_tier=frozenset({"priority", "flex"}),
        manage_context_server_side=frozenset({True}),
    )
    settings = ModelSettings.narrowest(row)
    assert settings.thinking_effort == "low"
    assert settings.thinking_budget == "auto"
    assert settings.thinking_output == "text"
    assert settings.service_tier == "flex"
    assert settings.manage_context_server_side is True


@pytest.mark.parametrize(
    ("axis", "capability"),
    [
        (
            "thinking_effort",
            ModelCapability(thinking=ThinkingCapability(effort=frozenset())),
        ),
        (
            "thinking_budget",
            ModelCapability(thinking=ThinkingCapability(budget=frozenset())),
        ),
        (
            "thinking_output",
            ModelCapability(thinking=ThinkingCapability(output=frozenset())),
        ),
        ("service_tier", ModelCapability(service_tier=frozenset())),
        ("cache_ttl_sec", ModelCapability(cache_ttl_sec=frozenset())),
        (
            "manage_context_server_side",
            ModelCapability(manage_context_server_side=frozenset()),
        ),
    ],
)
def test_narrowest_names_an_empty_axis(axis: str, capability: ModelCapability) -> None:
    with pytest.raises(ValueError, match=f"^{axis} offers nothing selectable"):
        ModelSettings.narrowest(capability)


def test_the_limits_are_the_models_whatever_size_is_chosen() -> None:
    row = _row()
    assert ModelSettings.narrowest(row).limits == row.limits
    assert ModelSettings.narrowest(row, context=200_000).limits == row.limits


@pytest.mark.parametrize(
    ("context", "window"),
    [(0, 1_000_000), (258_000, 258_000), (2_000_000, 1_000_000)],
)
def test_the_window_is_the_chosen_size_within_the_models(
    context: int,
    window: int,
) -> None:
    assert ModelSettings.narrowest(_row(), context=context).window == window


def _banded(*cards: tuple[ServiceTier, int]) -> ModelCapability:
    """Return a 1M-token model priced at each ``(tier, threshold)``, base cards too."""
    tiers: set[ServiceTier] = {tier for tier, _ in cards}
    prices = {PriceKey(tier): _price(1.0) for tier in tiers} | {
        PriceKey(tier, threshold): _price(2.0) for tier, threshold in cards
    }
    return ModelCapability(
        limits=ModelLimits(max_request_tokens=1_000_000),
        prices=PriceCatalog(prices),
        service_tier=frozenset({"auto", "default", "priority"}),
    )


def test_the_bands_are_where_a_tiers_price_rises() -> None:
    row = _banded(("auto", 256_000), ("auto", 32_000), ("priority", 272_000))
    assert row.bands("auto") == (32_000, 256_000)
    assert row.bands("priority") == (272_000,)
    # A tier no card names is priced as ``auto``.
    assert row.bands("default") == (32_000, 256_000)
    assert _row().bands("auto") == ()


def test_a_band_at_or_past_the_window_is_none() -> None:
    assert _banded(("auto", 1_000_000), ("auto", 2_000_000)).bands("auto") == ()


@pytest.mark.parametrize(
    ("cards", "small", "mid"),
    [
        ((("auto", 32_000), ("auto", 256_000)), 32_000, 256_000),
        ((("auto", 272_000),), 272_000, 0),
        ((), 0, 0),
    ],
    ids=["two-bands", "one-band", "none"],
)
def test_small_and_mid_are_the_first_and_second_band_else_the_window(
    cards: tuple[tuple[ServiceTier, int], ...],
    small: int,
    mid: int,
) -> None:
    row = _banded(*cards)
    assert row.size("small", tier="auto") == small
    assert row.size("mid", tier="auto") == mid
    assert row.size("large", tier="auto") == 0


@pytest.mark.parametrize(
    ("context", "tag"),
    [
        (0, ""),
        (32_000, "+small"),
        (256_000, "+mid"),
        (258_000, "+258k"),
        (2_000_000, "+2m"),
        (258_123, "+258123"),
    ],
)
def test_the_tag_names_the_size_as_a_model_id_does(context: int, tag: str) -> None:
    row = _banded(("auto", 32_000), ("auto", 256_000))
    assert ModelSettings.narrowest(row, context=context).tag == tag


def test_equal_capabilities_hash_equal() -> None:
    assert hash(_row()) == hash(_row())
    assert hash(_row()) != hash(ModelCapability())


def test_the_meet_cannot_grant() -> None:
    met = ModelCapability(
        thinking=ThinkingCapability(effort=frozenset({"none"})),
    ) & ModelCapability(
        thinking=ThinkingCapability(effort=frozenset({"none", "max"})),
    )
    assert met.thinking.effort == frozenset({"none"})


def test_the_meet_passes_limits_and_prices_through() -> None:
    met = _row() & ModelCapability()
    assert met.limits == _row().limits
    assert met.prices == _row().prices


def test_the_meet_keeps_the_rows_effort_spelling() -> None:
    """A model fact: the transport can neither grant nor revoke it."""
    level_row = ModelCapability(effort_as_level=True)
    assert (level_row & ModelCapability()).effort_as_level is True
    assert (_row() & ModelCapability(effort_as_level=True)).effort_as_level is False


def test_defaults_construct_against_a_default_capability() -> None:
    ModelSettings(capability=ModelCapability())


def test_a_negative_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="context=-1 is not offered"):
        ModelSettings(capability=_row(), cache_ttl_sec=300.0, context=-1)


def test_a_boolean_axis_validates_by_membership() -> None:
    row = ModelCapability(manage_context_server_side=frozenset({False}))
    ModelSettings(capability=row, manage_context_server_side=False)
    with pytest.raises(ValueError, match="manage_context_server_side=True"):
        ModelSettings(capability=row, manage_context_server_side=True)


def test_take_drops_what_is_not_offered_instead_of_raising() -> None:
    """A swap or resume must not FAIL on a knob the new model withholds."""
    settings = ModelSettings.narrowest(_row())
    settings.take(thinking_effort="max", thinking_budget="auto")
    assert settings.thinking_effort == "none"
    assert settings.thinking_budget == "auto"


def test_narrowest_selects_the_cache_the_wire_already_ships() -> None:
    """``0.0`` claimed no caching while a ``5m`` breakpoint shipped anyway."""
    assert ModelSettings.narrowest(_row()).cache_ttl_sec == 300.0
    assert ModelSettings.narrowest(ModelCapability()).cache_ttl_sec == 0.0
    only_long = ModelCapability(cache_ttl_sec=frozenset({3600.0}))
    assert ModelSettings.narrowest(only_long).cache_ttl_sec == 3600.0


def test_narrowest_carries_the_size_the_id_selected() -> None:
    assert ModelSettings.narrowest(_row(), context=100_000).context == 100_000


def test_narrowest_carries_the_capability_it_narrowed() -> None:
    """Without it every later write would validate against nothing."""
    assert ModelSettings.narrowest(_row()).capability == _row()


@pytest.mark.parametrize(
    ("models", "transport"),
    [(_row(), ModelCapability()), (_row(), _row())],
    ids=["bare", "self"],
)
def test_narrowest_builds_against_what_it_narrowed(
    models: ModelCapability,
    transport: ModelCapability,
) -> None:
    """Construction validates, so an unselectable narrowest would raise here."""
    capability = models & transport
    assert ModelSettings.narrowest(capability).capability == capability


def test_narrowest_rejects_an_empty_axis() -> None:
    """An empty meet is a model nothing can be requested from -- say so."""
    with pytest.raises(ValueError, match="thinking_effort offers nothing"):
        ModelSettings.narrowest(
            ModelCapability(thinking=ThinkingCapability(effort=frozenset())),
        )


def test_settings_over_an_empty_axis_cannot_be_constructed() -> None:
    """So ``take`` never meets a capability whose ``narrowest`` raises."""
    with pytest.raises(ValueError, match=r"cache_ttl_sec=0\.0 is not offered"):
        ModelSettings(capability=ModelCapability(cache_ttl_sec=frozenset()))


def test_take_rejects_capability_as_a_non_axis() -> None:
    with pytest.raises(ValueError, match="not settings axes: capability"):
        ModelSettings().take(capability=ModelCapability())


def test_equal_capabilities_are_hashable_independent_of_mapping_order() -> None:
    assert hash(ModelCapability()) == hash(ModelCapability())
    assert hash(_row()) == hash(_row())


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
