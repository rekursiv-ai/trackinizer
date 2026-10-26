"""The models one provider serves, looked up by name.

Typical usage example:

  table = ModelTable.from_yaml(path, provider="anthropic")
  table["opus-5.5"]                         # Its exact name.
  table["claude-opus-5-5"]                  # The vendor's id.
  table["default"]                          # A role: best, default, everyday, utility.
  table["opus"], table["opus-5"]            # The newest of the family.
  capability, settings = table.resolve("opus+1m")
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Self, cast, override

import math
import re

from treekle import from_plain

import yaml

from trackinizer.sagent.model.capability import (
    ContextTag,
    ModelCapability,
    ModelLimits,
    ModelSettings,
    ThinkingCapability,
    literal_members,
)
from trackinizer.sagent.model.cost import (
    PriceCatalog,
    PriceKey,
    ServiceTier,
    TokenPrice,
)
from trackinizer.sagent.model.exceptions import UnknownModelError, UnsupportedTagError


if TYPE_CHECKING:
    from importlib.resources.abc import Traversable


__all__ = [
    "ModelTable",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelTable(Mapping[str, ModelCapability]):
    """A vendor's models, by exact name, and reachable by vendor id, role, or family.

    Iterating yields exact names only: a role or a family is a way to name a
    model, not another model.
    """

    rows: tuple[ModelCapability, ...]
    """Every model, newest first within each family."""

    roles: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType[str, str]({}),
    )
    """Each role's family or exact name (``{"default": "opus"}``)."""

    _by_id: Mapping[str, ModelCapability] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Index the rows, and check their order and that each role names one."""
        by_id: dict[str, ModelCapability] = {}
        newest: dict[str, tuple[int, int]] = {}
        for row in self.rows:
            if row.model_id in by_id:
                raise ValueError(f"Model {row.model_id!r} is listed twice.")
            by_id[row.model_id] = row
            # A caller's own id (``local-model``) has no version to order by.
            parsed = _family_version(row.model_id)
            if parsed is None:
                continue
            family, version = parsed
            if family in newest and version >= newest[family]:
                raise ValueError(
                    f"Family {family!r} must list its models newest first;"
                    f" {row.model_id!r} follows an older one.",
                )
            newest[family] = version
        object.__setattr__(self, "_by_id", MappingProxyType(by_id))
        roles = dict(self.roles)
        if "default" in roles:
            for role in ("best", "everyday"):
                roles.setdefault(role, roles["default"])
        object.__setattr__(self, "roles", MappingProxyType(roles))
        for role, target in self.roles.items():
            if self._named(target) is None:
                raise ValueError(f"Role {role!r} names no model: {target!r}.")

    @classmethod
    def from_yaml(cls, path: Traversable, *, provider: str) -> Self:
        """Read the models one provider serves from a vendor's catalog document.

        Layers, lowest first: the reference row, the model's profile, the
        model, the provider's section, and the section's entry for the model.
        Each replaces whole fields, except that the section's ``thinking``
        replaces only the axes it names. A model the section does not list is
        not served.

        Args:
          path: The vendor's YAML document.
          provider: The name of a section under ``providers``.

        Returns:
          table: The section's models, validated, and the document's roles.

        """
        text = path.read_text()
        _validate_yaml(yaml.compose(text, Loader=yaml.SafeLoader), seen=set())
        document = from_plain(yaml.safe_load(text), _CatalogDocument)
        for layer in (document.reference, *document.profiles.values()):
            _require_whole_thinking(layer)
        # Read only to validate: a bad profile fails here, not at the first
        # model that names it.
        from_plain(document.reference, _CapabilityProfile)
        for profile in document.profiles.values():
            from_plain(document.reference | profile, _CapabilityProfile)
        if provider not in document.providers:
            raise ValueError(f"The catalog has no provider {provider!r}.")
        section = dict(document.providers[provider])
        listed = from_plain(
            section.pop("models", {}),
            dict[str, dict[str, object] | None],
        )
        ids = [from_plain(raw.get("model_id"), str) for raw in document.models]
        if unknown := sorted(set(listed) - set(ids)):
            raise ValueError(f"Provider {provider!r} lists unknown models: {unknown}.")
        rows = tuple(
            _model(
                raw,
                document=document,
                section=section,
                entry=listed[model_id] or {},
            )
            for raw, model_id in zip(document.models, ids, strict=True)
            if model_id in listed
        )
        return cls(rows=rows, roles=document.roles)

    @override
    def __getitem__(self, key: str) -> ModelCapability:
        row = self._resolved(key)
        if row is None:
            raise KeyError(key)
        return row

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(self._by_id)

    @override
    def __len__(self) -> int:
        return len(self._by_id)

    @override
    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self._resolved(key) is not None

    def exact(self, name: str) -> ModelCapability | None:
        """Return the row whose exact name or vendor id is ``name``, never a guess.

        For ids a server reports back: billing ``gpt-5`` at the newest
        ``gpt-5.x`` because the family matched would misprice it silently.

        Args:
          name: An exact name or a vendor id.

        Returns:
          row: The row, or ``None`` when no row carries that id.

        """
        row = self._by_id.get(name)
        if row is not None:
            return row
        return next((row for row in self.rows if row.wire_model_id == name), None)

    def resolve(self, model_id: str) -> tuple[ModelCapability, ModelSettings]:
        """Return the model a name means, and settings selecting its context window.

        Args:
          model_id: An exact name, vendor id, role, or family, with any context
            tags trailing it (``opus+1m``).

        Returns:
          capability: The row, with a ``+1m`` window when its default spans one.
          settings: The least-committing settings, selecting the named window.

        Raises:
          UnknownModelError: No row answers to the name.
          UnsupportedTagError: The tags conflict, or name a window it lacks.

        """
        # Trailing tags, in any order and any case: ``opus+1M`` is ``opus`` at ``+1m``.
        base, found = model_id, set[ContextTag]()
        while (tag := _trailing_tag(base)) is not None:
            found.add(tag)
            base = base[: -len(tag)]
        tags = sorted(found)
        if len(tags) > 1:
            raise UnsupportedTagError(
                f"Model {model_id!r} selects conflicting contexts: {', '.join(tags)}.",
            )
        context: ContextTag = tags[0] if tags else ""
        capability = self.get(base)
        if capability is None:
            known = ", ".join([*self, *self.roles])
            raise UnknownModelError(
                f"Unknown model {model_id!r}; known models: {known}.",
            )
        if context in ("+max", "+min"):
            sizes: dict[ContextTag, int] = {
                t: limits.max_request_tokens for t, limits in capability.context.items()
            }
            context = (max if context == "+max" else min)(sizes, key=sizes.__getitem__)
        # A window the default context already spans needs no row of its own.
        if (
            context == "+1m"
            and context not in capability.context
            and capability.context[""].max_request_tokens >= 1_000_000
        ):
            capability = replace(
                capability,
                context=MappingProxyType(
                    {**capability.context, "+1m": capability.context[""]},
                ),
            )
        if context not in capability.context:
            windows = ", ".join(sorted(t for t in capability.context if t)) or "none"
            raise UnsupportedTagError(
                f"Model {model_id!r} has no {context} context; it offers: {windows}.",
            )
        return capability, ModelSettings.narrowest(capability, context=context)

    def _resolved(self, key: str) -> ModelCapability | None:
        """Return the row ``key`` names: by exact name or vendor id, then role or family."""
        row = self.exact(key)
        return row if row is not None else self._named(self.roles.get(key, key))

    # A family is named alone or with a major: ``opus`` and ``opus-5`` name
    # ``opus-*``; ``opu`` names nothing, and ``gemini-flash`` never reaches
    # ``gemini-flash-lite-*``, a family of its own.
    def _named(self, name: str) -> ModelCapability | None:
        """Return the row ``name`` means by exact name, vendor id, or family."""
        row = self.exact(name)
        if row is not None:
            return row
        return next(
            (row for row in self.rows if _in_family(row.model_id, name=name)),
            None,
        )


def _trailing_tag(name: str) -> ContextTag | None:
    """Return the context tag ``name`` ends with, in any case, if any."""
    tags = cast(tuple[ContextTag, ...], literal_members(ContextTag))
    return next((tag for tag in tags if tag and name.lower().endswith(tag)), None)


def _family_version(model_id: str) -> tuple[str, tuple[int, int]] | None:
    """Split ``opus-5.5`` into ``("opus", (5, 5))``; ``None`` for other names."""
    family, _, version = model_id.rpartition("-")
    major, dot, minor = version.partition(".")
    if (
        not dot
        or not major.isdecimal()
        or not minor.isdecimal()
        or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", family) is None
    ):
        return None
    return family, (int(major), int(minor))


def _in_family(model_id: str, *, name: str) -> bool:
    """Whether ``name`` is ``model_id``'s family, alone or with a major version."""
    # A caller's own id (``local-model``) has no family: ``local`` names nothing.
    if _family_version(model_id) is None:
        return False
    family, _, version = model_id.rpartition("-")
    if name == family:
        return True
    head, _, major = name.rpartition("-")
    return head == family and (version == major or version.startswith(f"{major}."))


def _validate_yaml(node: yaml.Node | None, *, seen: set[int]) -> None:
    """Reject aliases, keys that are not strings, and keys given twice."""
    if node is None:
        return
    if id(node) in seen:
        raise ValueError("Catalogs may not use YAML aliases.")
    seen.add(id(node))
    if isinstance(node, yaml.MappingNode):
        keys: set[str] = set()
        for key, value in node.value:
            if (
                not isinstance(key, yaml.ScalarNode)
                or key.tag != "tag:yaml.org,2002:str"
            ):
                raise ValueError("Catalog mapping keys must be strings.")
            if key.value in keys:
                raise ValueError(f"The catalog gives key {key.value!r} twice.")
            keys.add(key.value)
            _validate_yaml(value, seen=seen)
    elif isinstance(node, yaml.SequenceNode):
        for value in node.value:
            _validate_yaml(value, seen=seen)


@dataclass(frozen=True, slots=True, kw_only=True)
class _CatalogDocument:
    """A vendor's catalog document, before any model is resolved."""

    models: tuple[dict[str, object], ...]
    reference: dict[str, object] = field(default_factory=dict)
    profiles: dict[str, dict[str, object]] = field(default_factory=dict)
    providers: dict[str, dict[str, object]] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)


# Nested objects replace whole, so an omitted axis would become ``none`` rather
# than inherit from the layer below.
def _require_whole_thinking(layer: dict[str, object]) -> None:
    """Reject a ``thinking`` override that omits an axis."""
    thinking = layer.get("thinking")
    if isinstance(thinking, dict) and thinking.keys() != {"effort", "budget", "output"}:
        raise ValueError("A catalog's thinking must state effort, budget, and output.")


@dataclass(frozen=True, slots=True, kw_only=True)
class _CapabilityProfile:
    """Model facts several rows share; no identity, prices, or profile of its own."""

    knowledge_cutoff: str | None = None
    approx_chars_per_token: float = 4.0
    default_context: str = "max"
    context: dict[str, ModelLimits] = field(default_factory=dict)
    thinking: ThinkingCapability = field(default_factory=ThinkingCapability)
    effort_as_level: bool = False
    cache_ttl_sec: frozenset[float] = frozenset({0.0})
    manage_context_server_side: frozenset[bool] = frozenset({False, True})
    retries_internally: bool = False
    account_auth: bool = False
    service_tier: frozenset[ServiceTier] | None = None
    """Tiers offered; ``None`` offers every tier the row prices."""


def _model(
    raw: dict[str, object],
    *,
    document: _CatalogDocument,
    section: dict[str, object],
    entry: dict[str, object],
) -> ModelCapability:
    """Return one model's row, its layers merged, as one provider serves it."""
    values = dict(raw)
    _require_whole_thinking(values)
    _require_whole_thinking(entry)
    profile = from_plain(values.pop("profile", ""), str)
    if profile and profile not in document.profiles:
        raise ValueError(f"The catalog has no profile {profile!r}.")
    values = document.reference | document.profiles.get(profile, {}) | values
    axes = from_plain(section.get("thinking", {}), dict[str, object])
    if axes.keys() - {"effort", "budget", "output"}:
        raise ValueError(f"Unknown thinking axes: {sorted(axes)}.")
    values |= {key: value for key, value in section.items() if key != "thinking"}
    values["thinking"] = (
        from_plain(values.get("thinking", {}), dict[str, object]) | axes
    )
    values |= entry
    row = from_plain(values, _ModelRow)
    if _family_version(row.model_id) is None:
        raise ValueError(f"Model id {row.model_id!r} is not family-major.minor.")
    context = _contexts(row)
    if (
        not row.prices
        or not row.thinking.effort
        or not row.thinking.budget
        or not row.thinking.output
        or row.approx_chars_per_token <= 0
        or not math.isfinite(row.approx_chars_per_token)
    ):
        raise ValueError(
            f"Catalog model {row.model_id!r} is missing a price or setting.",
        )
    catalog = _prices(row)
    return ModelCapability(
        model_id=row.model_id,
        wire_model_id=row.wire_model_id,
        knowledge_cutoff=row.knowledge_cutoff,
        approx_chars_per_token=row.approx_chars_per_token,
        context=MappingProxyType(context),
        thinking=row.thinking,
        effort_as_level=row.effort_as_level,
        prices=catalog,
        service_tier=catalog.service_tiers
        if row.service_tier is None
        else row.service_tier,
        cache_ttl_sec=row.cache_ttl_sec,
        manage_context_server_side=row.manage_context_server_side,
        retries_internally=row.retries_internally,
        account_auth=row.account_auth,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _PriceRow(TokenPrice):
    """One rate card, and the conditions that select it."""

    service_tier: ServiceTier = "auto"
    effective_prompt_tokens: int = 0
    effective_date: str | date = "0001-01-01"


@dataclass(frozen=True, slots=True, kw_only=True)
class _ModelRow(_CapabilityProfile):
    """One model's document, its profile applied."""

    model_id: str
    prices: tuple[_PriceRow, ...]
    wire_model_id: str = ""


def _contexts(row: _ModelRow) -> dict[ContextTag, ModelLimits]:
    """Return the row's windows by tag: the default as ``""``, the other by size."""
    if any(tag not in {"max", "min"} for tag in row.context):
        raise ValueError(
            f"Model {row.model_id!r} names a context other than max or min.",
        )
    if row.default_context not in row.context:
        raise ValueError(f"Model {row.model_id!r} has no default context.")
    if (
        "max" in row.context
        and "min" in row.context
        and row.context["max"].max_request_tokens
        < row.context["min"].max_request_tokens
    ):
        raise ValueError(
            f"Model {row.model_id!r} has a max context smaller than its min.",
        )
    sizes: dict[int, ContextTag] = {
        200_000: "+200k",
        272_000: "+272k",
        1_000_000: "+1m",
    }
    context: dict[ContextTag, ModelLimits] = {}
    for preset, limits in row.context.items():
        if (
            min(
                limits.max_request_tokens,
                limits.max_response_tokens,
                limits.max_request_bytes,
                limits.max_image_edge_px,
                limits.max_image_bytes,
            )
            < 0
        ):
            raise ValueError(f"Model {row.model_id!r} has a negative limit.")
        if preset == row.default_context:
            context[""] = limits
        else:
            fallback: ContextTag = "+max" if preset == "max" else "+min"
            context[sizes.get(limits.max_request_tokens, fallback)] = limits
    return context


def _prices(row: _ModelRow) -> PriceCatalog:
    """Return the row's rate cards, keyed by tier, prompt band, and start date."""
    prices: dict[PriceKey, TokenPrice] = {}
    for card in row.prices:
        rates = (
            card.request,
            card.response,
            card.cache_write,
            card.cache_write_1h,
            card.cache_read,
        )
        if (
            card.tokens_per_unit <= 0
            or card.effective_prompt_tokens < 0
            or card.service_tier == "default"
            or isinstance(card.effective_date, datetime)
            or any(rate < 0 or not math.isfinite(rate) for rate in rates)
        ):
            raise ValueError(f"Model {row.model_id!r} has an invalid price.")
        key = PriceKey(
            card.service_tier,
            card.effective_prompt_tokens,
            date.fromisoformat(card.effective_date)
            if isinstance(card.effective_date, str)
            else card.effective_date,
        )
        if key in prices:
            raise ValueError(f"Model {row.model_id!r} prices {key!r} twice.")
        prices[key] = TokenPrice(
            request=card.request,
            response=card.response,
            cache_write=card.cache_write,
            cache_write_1h=card.cache_write_1h,
            cache_read=card.cache_read,
            tokens_per_unit=card.tokens_per_unit,
        )
    # ``rate`` raises for a request no card covers; only an undated card with
    # no prompt band covers every prompt on every day.
    tiers: set[ServiceTier] = {key.service_tier for key in prices}
    for tier in tiers:
        if PriceKey(tier) not in prices:
            raise ValueError(
                f"Model {row.model_id!r} has no base card for tier {tier!r}"
                " (no prompt band, no effective date).",
            )
    return PriceCatalog(prices)
