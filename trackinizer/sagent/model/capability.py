"""What a model allows, and what one model instance chose.

model = catalog["anthropic"]["opus"]
model.capability.thinking.effort        # every effort level allowed
model.settings.thinking_effort = "high" # ValueError if not allowed
model.settings.limits.max_request_tokens
"""

from __future__ import annotations

from collections.abc import (
    Collection,
    Sequence,
)
from dataclasses import dataclass, field, fields, replace
from typing import Literal, Self, TypeAliasType, cast, get_args, override

from trackinizer.sagent.model.cost import PriceCatalog, ServiceTier


__all__ = [
    "ContextSize",
    "ModelCapability",
    "ModelLimits",
    "ModelSettings",
    "Permission",
    "PruningPolicy",
    "SummaryKind",
    "ThinkingBudget",
    "ThinkingCapability",
    "ThinkingEffort",
    "ThinkingOutput",
    "literal_members",
]


type ThinkingEffort = Literal[
    "none",
    "min",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
]

type ThinkingBudget = Literal[
    "none",
    "auto",
    "fixed",
]

type ThinkingOutput = Literal[
    "none",
    "text",
    "redacted",
]

type ContextSize = Literal[
    "small",
    "mid",
    "large",
]
"""A context size named by price: below the first band, the second, or the whole window."""

type SummaryKind = Literal[
    "none",
    "auto",
    "concise",
    "detailed",
]

type Permission = Literal[
    "ask",
    "accept_edits",
    "bypass",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelLimits:
    """Hard ceilings of one model's window."""

    max_request_tokens: int = 0
    """Input tokens the model accepts; ``0`` means unknown."""

    max_response_tokens: int = 0
    """Output tokens the model can generate in one response, thinking included."""

    max_context_tokens: int = 0
    """Input and output together; ``0`` means ``max_request_tokens`` bounds both."""

    truncates_at_context: bool | None = None
    """Whether input plus ``max_tokens`` past the context is accepted and the reply
    stopped there (``True``) or the request rejected (``False``); ``None`` is unknown."""

    max_request_bytes: int = 0
    """HTTP wire ceiling, distinct from the token window; ``0`` = none."""

    max_image_edge_px: int = 0
    """Long edge above which the server downscales; ``0`` = no resize."""

    max_image_bytes: int = 0
    """Per-image byte cap after resize; ``0`` = no cap."""

    request_betas: frozenset[str] = frozenset()
    """Vendor beta features every request needs."""

    @property
    def context_tokens(self) -> int:
        """Return what input and output may hold together."""
        return self.max_context_tokens or self.max_request_tokens


@dataclass(frozen=True, slots=True, kw_only=True)
class ThinkingCapability:
    """Reasoning knobs one model or transport accepts, one set per axis."""

    effort: frozenset[ThinkingEffort] = frozenset({"none"})
    """Effort levels accepted."""

    budget: frozenset[ThinkingBudget] = frozenset({"none"})
    """Budget modes accepted."""

    output: frozenset[ThinkingOutput] = frozenset({"none"})
    """Reasoning-visibility modes accepted."""

    def __and__(self, other: ThinkingCapability) -> ThinkingCapability:
        """Narrow each axis to what BOTH offer."""
        return ThinkingCapability(
            effort=self.effort & other.effort,
            budget=self.budget & other.budget,
            output=self.output & other.output,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelCapability:
    """What one model offers: every value a caller may select.

    One catalog row, or one transport's restrictions. ``prices``,
    ``effort_as_level``, and the trailing transport facts have no
    :class:`ModelSettings` counterpart -- nothing selects a price, a wire
    spelling, a retry policy, or an auth mode.

    ``temperature`` is absent: its domain is a continuous range, so it cannot
    join the membership check. A provider that sends one sets it itself.
    """

    model_id: str = ""
    """Our normalized model id, without option tags."""

    wire_model_id: str = ""
    """Vendor id when it differs from :attr:`model_id`; blank means identical."""

    knowledge_cutoff: str | None = None
    """Vendor-published knowledge cutoff, formatted for the system prompt."""

    approx_chars_per_token: float = 4.0
    """Measured text-token estimate for this model's tokenizer."""

    limits: ModelLimits = field(default_factory=ModelLimits)
    """The model's window, whatever share of it a caller chooses to fill."""

    prices: PriceCatalog = field(default_factory=PriceCatalog)
    """USD rates, keyed by service tier and request-size threshold."""

    thinking: ThinkingCapability = field(default_factory=ThinkingCapability)
    """Reasoning effort, budget, and visibility this transport accepts."""

    effort_as_level: bool = False
    """Whether the wire takes a named effort level (Gemini ``thinkingLevel``), not a budget."""

    default_effort: ThinkingEffort | None = None
    """The vendor's effort when none is sent, where a new model starts; ``None`` is the lowest."""

    default_budget: ThinkingBudget | None = None
    """Whether the vendor thinks when told nothing, where a new model starts; ``None`` is the lowest."""

    # Only ``auto``, a request naming no tier, is universal; a transport with no
    # tier parameter narrows the meet to it.
    service_tier: frozenset[ServiceTier] = frozenset({"auto", "default"})
    """Speed and price tiers this transport accepts."""

    # Defaults to BOTH values, not to its unset one: a model row does not know
    # whether the server rolls history, and ``&`` can only remove -- so a row
    # asserting ``{False}`` would pin every transport and empty the meet.
    manage_context_server_side: frozenset[bool] = frozenset({False, True})
    """Whether the server may roll history under quota pressure."""

    # The three below are DECLARED by the transport, not narrowed from the row:
    # a catalog row cannot know who bills it, who retries it, or whether the
    # request path writes a cache breakpoint, so ``&`` takes the transport's
    # answer rather than intersecting with a meaningless one.
    # A set, not a ceiling: vendors sell discrete lifetimes (``5m``, ``1h``). A
    # transport that always caches omits ``0``, so "do not cache" is unselectable.
    cache_ttl_sec: frozenset[float] = frozenset({0.0})
    """Prompt-cache lifetimes this transport accepts, in seconds."""

    retries_internally: bool = False
    """Whether the transport retries transient failures on its own."""

    account_auth: bool = False
    """Whether this transport bills an account rather than an API key."""

    def bands(self, tier: ServiceTier) -> tuple[int, ...]:
        """Return the prompt sizes past which ``tier``'s price rises, within the window.

        Args:
          tier: The service tier; one no card names is priced as ``auto``.

        Returns:
          thresholds: Ascending, each below the window.

        """
        priced = {key.service_tier for key in self.prices}
        rated = tier if tier in priced else "auto"
        window = self.limits.max_request_tokens
        return tuple(
            sorted(
                {
                    key.effective_prompt_tokens
                    for key in self.prices
                    if key.service_tier == rated
                    and key.effective_prompt_tokens > 0
                    and (window <= 0 or key.effective_prompt_tokens < window)
                },
            ),
        )

    def size(self, name: ContextSize, *, tier: ServiceTier) -> int:
        """Return the context ``name`` means at ``tier``: a band's top, else the window.

        Args:
          name: ``small`` below the first band, ``mid`` below the second,
            ``large`` the whole window.
          tier: The service tier whose bands it reads.

        Returns:
          tokens: The size; ``0`` for the whole window.

        """
        bands = self.bands(tier)
        at = ("small", "mid").index(name) if name != "large" else len(bands)
        return bands[at] if at < len(bands) else 0

    def __and__(self, other: ModelCapability) -> Self:
        """Narrow to what BOTH offer -- a catalog row met with a transport.

        Args:
          other: The transport's restrictions.

        Returns:
          narrowed: What survives on both sides.

        """
        # Model facts pass through: a transport
        # restricts which knobs it may send, never which windows a model has
        # or what it costs.
        return replace(
            self,
            thinking=self.thinking & other.thinking,
            service_tier=self.service_tier & other.service_tier,
            manage_context_server_side=(
                self.manage_context_server_side & other.manage_context_server_side
            ),
            cache_ttl_sec=other.cache_ttl_sec,
            retries_internally=other.retries_internally,
            account_auth=other.account_auth,
        )

    @override
    def __hash__(self) -> int:
        """Hash capability values, independent of mapping insertion order."""
        return hash(
            (
                self.model_id,
                self.wire_model_id,
                self.knowledge_cutoff,
                self.approx_chars_per_token,
                self.limits,
                frozenset(self.prices.items()),
                self.thinking,
                self.effort_as_level,
                self.default_effort,
                self.default_budget,
                self.service_tier,
                self.manage_context_server_side,
                self.cache_ttl_sec,
                self.retries_internally,
                self.account_auth,
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PruningPolicy:
    """Which old tool output may be replaced by a stub, to keep the context small.

    The client prunes when the cost says so (``agent.pruner.Pruner``); a server
    that prunes, with ``manage_context_server_side``, goes by the two token marks.
    """

    trigger_tokens: int = 100_000
    """A server that prunes starts once a request's context passes this."""

    target_tokens: int = 40_000
    """A server that prunes stops once the context is back near this."""

    keep_recent: int = 3
    """The newest tool results, which are never pruned."""

    expiry: float = 1.0
    """``λ``: an unused result expires once its rent reaches ``λ`` times the cost of fetching it again."""

    keep_tools: frozenset[str] = frozenset({"Skill"})
    """Tools whose results are never pruned; a skill's steps stay while it runs."""


@dataclass(slots=True, kw_only=True)
class ModelSettings:
    """What one model instance chose, within what its capability offers.

    MUTABLE, and the sole owner of these axes. A caller that keeps its own
    copy of a knob has to re-derive "is this still offered?" on every model
    swap, and each such copy drifted: an ``Agent`` field shadowing
    ``thinking_effort`` was set by ``/effort`` and never read by the wire
    builders, so the selected level silently never shipped.

    Every axis validates ON ASSIGNMENT against :attr:`capability`, so
    ``settings.thinking_budget = "auto"`` raises on a model that cannot
    think. That is why the capability is a field rather than an argument:
    a setter has no other way to reach it, and a caller who had to pass it
    could pass a different one than the model was built from.

    Choices only. A derived value does not belong here: the window the
    context fills is :attr:`window`, not a field.
    """

    # DECLARED FIRST so ``__init__`` binds it before any axis: the setter
    # below reads it to validate, and a later position leaves the first axis
    # assignment reaching an unset attribute.
    capability: ModelCapability = field(default_factory=ModelCapability)
    """What the transport offers. Not an axis -- nothing selects it."""

    context: int = 0
    """Tokens the context may fill before it is compacted; ``0`` for the whole window."""

    thinking_effort: ThinkingEffort = "none"
    """Selected effort level."""

    thinking_budget: ThinkingBudget = "none"
    """Selected budget mode."""

    thinking_output: ThinkingOutput = "none"
    """Selected reasoning visibility."""

    service_tier: ServiceTier = "auto"
    """Selected speed/price tier."""

    cache_ttl_sec: float = 0.0
    """Selected prompt-cache lifetime, in seconds."""

    manage_context_server_side: bool = False
    """Whether the server was asked to roll history."""

    pruning: PruningPolicy | None = PruningPolicy()
    """Which old tool results may be dropped; ``None`` never drops them. Any model takes it."""

    @override
    def __setattr__(self, name: str, value: object) -> None:
        """Reject an axis value :attr:`capability` does not offer.

        Assignment is the API -- ``settings.thinking_budget = "auto"`` --
        so the check lives here rather than in a method a caller can route
        around. ``capability`` itself is exempt: it is the authority, not a
        choice. Swapping it atomically re-narrows every axis and context.

        Args:
          name: Attribute being set.
          value: Its new value.

        Raises:
          ValueError: ``value`` is outside what :attr:`capability` allows.

        """
        if name == "capability" and hasattr(self, "capability"):
            assert isinstance(value, ModelCapability)
            narrowed = type(self).narrowest(value, context=self.context)
            narrowed.adopt(self)
            object.__setattr__(self, name, value)
            for f in fields(self):
                if f.name != "capability":
                    object.__setattr__(self, f.name, getattr(narrowed, f.name))
            return
        if name != "capability":
            _reject_unoffered(self.capability, name, value)
        object.__setattr__(self, name, value)

    def adopt(self, other: Self) -> None:
        """Take every choice of ``other`` this capability still offers.

        The model-swap path: a swap keeps what the user selected. Excludes
        ``context``, because the incoming model id may name its own size
        (``opus+small``) -- overwriting it would undo the selection the swap
        was performed to make.

        Args:
          other: The outgoing model's settings.

        """
        choices: dict[str, object] = {}
        for f in fields(self):
            if f.name not in ("capability", "context"):
                choices[f.name] = getattr(other, f.name)
        self.take(**choices)

    def take(self, **choices: object) -> None:
        """Apply each of ``choices`` this capability offers; drop the rest.

        The lenient counterpart to assignment, and the ONLY way to cross
        capabilities: a knob the incoming model rejects is not merely stale,
        it ships on the next request and earns a 400 -- but a swap or a
        resume must not FAIL on it either. Each unoffered axis falls back to
        the lowest rung this capability does offer.

        Args:
          choices: Field name to desired value.

        Raises:
          ValueError: A name is not an axis of this class.

        """
        names = {f.name for f in fields(self)} - {"capability"}
        unknown = sorted(set(choices) - names)
        if unknown:
            raise ValueError(f"not settings axes: {', '.join(unknown)}")
        narrow = type(self).narrowest(self.capability)
        for name, wanted in choices.items():
            offered = _offers(self.capability, name, wanted)
            setattr(self, name, wanted if offered else getattr(narrow, name))

    @classmethod
    def narrowest(
        cls,
        capability: ModelCapability,
        *,
        context: int = 0,
    ) -> Self:
        """Return the least-committing selection ``capability`` offers.

        The field defaults are not it: a row can withhold the unset value of
        an axis (Model Studio's ``-thinking`` ids reject "do not think", a CLI
        transport requires server-side history), and a default-constructed
        settings object is then unbuildable against that row.

        Args:
          capability: What the transport offers.
          context: The size the model id selected; ``0`` for the whole window.

        Returns:
          settings: The lowest rung of every ladder that survives.

        Raises:
          ValueError: An axis offers nothing at all.

        """
        return cls(
            capability=capability,
            context=context,
            thinking_effort=_lowest(
                "thinking_effort",
                capability.thinking.effort,
                _effort_ladder(
                    capability.default_effort,
                    literal_members(ThinkingEffort),
                ),
            ),
            thinking_budget=_lowest(
                "thinking_budget",
                capability.thinking.budget,
                _default_first(
                    capability.default_budget,
                    literal_members(ThinkingBudget),
                ),
            ),
            thinking_output=_lowest(
                "thinking_output",
                capability.thinking.output,
                literal_members(ThinkingOutput),
            ),
            service_tier=_lowest(
                "service_tier",
                capability.service_tier,
                literal_members(ServiceTier),
            ),
            cache_ttl_sec=_lowest(
                "cache_ttl_sec",
                capability.cache_ttl_sec,
                sorted(capability.cache_ttl_sec),
            ),
            manage_context_server_side=_lowest(
                "manage_context_server_side",
                capability.manage_context_server_side,
                (False, True),
            ),
        )

    @property
    def limits(self) -> ModelLimits:
        """The model's window."""
        return self.capability.limits

    @property
    def window(self) -> int:
        """Tokens the context may fill: the chosen size, within the model's window."""
        window = self.limits.max_request_tokens
        if not self.context:
            return window
        return min(self.context, window) if window > 0 else self.context

    @property
    def tag(self) -> str:
        """The size as a model id names it: ``+small``, ``+258k``; blank for the window."""
        if not self.context:
            return ""
        for name in ("small", "mid"):
            if self.capability.size(name, tier=self.service_tier) == self.context:
                return f"+{name}"
        for unit, scale in (("m", 1_000_000), ("k", 1_000)):
            if self.context % scale == 0:
                return f"+{self.context // scale}{unit}"
        return f"+{self.context}"


def literal_members[T](alias: object) -> tuple[T, ...]:
    """Return a ``type X = Literal[...]`` alias's members, in declaration order.

    ``get_args`` of the alias itself is ``()``: the members live on its
    ``__value__``, which typeshed types as ``Any``.

    Args:
      alias: The alias, such as ``ThinkingEffort``.

    Returns:
      members: Each member, the least committing first where a ladder is meant.

    """
    return cast(
        tuple[T, ...],
        get_args(cast(object, cast(TypeAliasType, alias).__value__)),
    )


def _allowed(capability: ModelCapability, name: str) -> Collection[object]:
    """Return the set ``capability`` offers for the settings axis ``name``."""
    match name:
        case "thinking_effort":
            return capability.thinking.effort
        case "thinking_budget":
            return capability.thinking.budget
        case "thinking_output":
            return capability.thinking.output
        case _:
            return cast(Collection[object], getattr(capability, name))


def _offers(capability: ModelCapability, name: str, value: object) -> bool:
    """Whether ``capability`` allows ``value`` on the axis ``name``."""
    # Pruning and the context's size are the caller's policy, not offers a
    # model makes; a size past the window is held to it.
    if name == "context":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return name == "pruning" or value in _allowed(capability, name)


def _reject_unoffered(capability: ModelCapability, name: str, value: object) -> None:
    """Raise unless ``capability`` offers ``value`` on the axis ``name``."""
    if _offers(capability, name, value):
        return
    if name == "context":
        raise ValueError(
            f"context={value!r} is not offered; a size is tokens, 0 or more",
        )
    model = capability.model_id or "this model"
    # ``repr`` before ``sorted``: these sets mix strings, floats, and bools,
    # which do not order against each other.
    offered = ", ".join(sorted(repr(v) for v in _allowed(capability, name)))
    raise ValueError(f"{name}={value!r} is not offered by {model}; allowed: {offered}")


# A default the row lacks falls to the nearest level below it, then above, and only then
# to ``none``, which stops the reasoning.
def _effort_ladder(
    default: ThinkingEffort | None,
    efforts: tuple[ThinkingEffort, ...],
) -> tuple[ThinkingEffort, ...]:
    """Return ``efforts`` in the order a row's selection tries them."""
    at = efforts.index(default) if default is not None else 0
    return (*efforts[at:0:-1], *efforts[at + 1 :], efforts[0]) if at else efforts


def _default_first[T](default: T | None, ladder: tuple[T, ...]) -> tuple[T, ...]:
    """Return ``ladder`` with ``default``, when one is given, tried first."""
    return ladder if default is None else (default, *ladder)


def _lowest[T](name: str, offered: Collection[T], ladder: Sequence[T]) -> T:
    """Return the first rung of ``ladder`` that ``offered`` contains."""
    for rung in ladder:
        if rung in offered:
            return rung
    raise ValueError(f"{name} offers nothing selectable: {offered!r}")
