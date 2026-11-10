"""What a session's model calls used and cost, from its ``TokenUsage`` records.

Each CLI reports usage its own way, and Chat shows the totals as sagent's status
line does (``tui/status.py``):

- Claude Code writes its call's usage on every line of the answer, so one call's
  usage repeats; a call is counted once, by the request id the lines before it carry.
- Codex reports running totals (``total_token_usage``); a call is what they grew by.
- sagent states what each call cost (``spend_usd``) and no tokens.

A call is priced with sagent's own price tables, for the model the call names, so
Chat's cost and sagent's agree. A call whose model the tables do not know leaves
the cost unknown rather than low.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from pydantic import BaseModel
from treekle import from_plain

from trackinizer.sagent.model.capability import ModelCapability
from trackinizer.sagent.model.cost import TokenCount
from trackinizer.sagent.model.exceptions import UnknownModelError
from trackinizer.sagent.providers import anthropic, openai
from trackinizer.sagent.providers.table import models


if TYPE_CHECKING:
    from collections.abc import Iterable

    from trackinizer.types.session_records import SessionRecordRow


__all__ = ["SessionUsage", "session_usage"]


class SessionUsage(BaseModel):
    """What a session's model calls used and cost, as Chat shows it."""

    input_tokens: int = 0
    """Prompt tokens sent, cached or not."""

    output_tokens: int = 0
    """Tokens generated, reasoning included."""

    cache_read_tokens: int = 0
    """Prompt tokens the provider's cache served."""

    context_tokens: int | None = None
    """The last call's prompt: how full the window is now; None before any call."""

    context_window: int | None = None
    """The most prompt tokens the last call's model takes; None when unknown."""

    usd: float | None = None
    """What the calls cost; None before any, or when one could not be priced."""


def session_usage(
    rows: Iterable[SessionRecordRow],
    *,
    formats: Mapping[int, str],
    today: date,
    capability: Callable[[str, str], ModelCapability | None] | None = None,
) -> SessionUsage:
    """Return what the calls ``rows`` record used and cost.

    Args:
      rows: The session's records, part by part, each in ``idx`` order.
      formats: Each part's format (``claude``, ``codex``, ``sagent``), by part.
      today: The day whose prices apply.
      capability: The model a format names, or None when its tables do not know
        it; sagent's provider tables by default.

    Returns:
      usage: The totals.

    """
    tally = _Tally(today=today, capability=capability or _capability)
    for row in rows:
        tally.read(row, cli=formats.get(row.part, ""))
    return tally.usage()


def _capability(cli: str, model: str) -> ModelCapability | None:
    """Return the model ``cli`` names, from sagent's tables; None for one unknown."""
    if not model or cli not in {"claude", "codex"}:
        return None
    # The catalog sections the CLIs serve; their provider modules do not ship.
    table = (
        models(anthropic, section="claude-code")
        if cli == "claude"
        else models(openai, section="openai-sub")
    )
    try:
        return table.resolve(model)[0]
    except UnknownModelError:
        return None


@dataclass(slots=True, kw_only=True)
class _Tally:
    """The totals so far, and what the records before the next usage said."""

    today: date
    capability: Callable[[str, str], ModelCapability | None]
    sums: SessionUsage = field(default_factory=SessionUsage)
    usd: float = 0.0
    priced: bool = True
    calls: int = 0
    model: str = ""
    request: str = ""
    counted: set[str] = field(default_factory=set[str])
    totals: dict[int, Mapping[str, object]] = field(
        default_factory=dict[int, Mapping[str, object]],
    )

    def read(self, row: SessionRecordRow, *, cli: str) -> None:
        """Take in one record: the model and request it names, or a call's usage.

        Args:
          row: The record.
          cli: The format of the part it is in.

        """
        payload = row.payload
        extra = _fields(payload.get("extra"))
        if row.kind == "TurnContext":
            self.model = from_plain(payload.get("model"), str, default="") or self.model
        message = _fields(extra.get("message"))
        self.model = (
            row.model or from_plain(message.get("model"), str, default="") or self.model
        )
        self.request = (
            from_plain(extra.get("requestId"), str, default="") or self.request
        )
        if row.kind != "TokenUsage":
            return
        info = _fields(payload.get("info"))
        if "spend_usd" in info:
            self.usd += from_plain(info.get("spend_usd"), float, default=0.0)
            self.calls += 1
        elif "total_token_usage" in info:
            self._codex(info, part=row.part, cli=cli)
        elif "input_tokens" in info:
            self._claude(info, cli=cli)

    def usage(self) -> SessionUsage:
        """Return the totals."""
        priced = self.priced and self.calls > 0
        return self.sums.model_copy(update={"usd": self.usd if priced else None})

    def _claude(self, info: Mapping[str, object], *, cli: str) -> None:
        """Count one Claude call, once however many of its lines repeat its usage."""
        if self.request and self.request in self.counted:
            return
        self.counted.add(self.request)
        created = _fields(info.get("cache_creation"))
        hour = _count(created, "ephemeral_1h_input_tokens")
        tokens = TokenCount(
            request=_count(info, "input_tokens"),
            response=_count(info, "output_tokens"),
            cache_write=max(0, _count(info, "cache_creation_input_tokens") - hour),
            cache_write_1h=hour,
            cache_read=_count(info, "cache_read_input_tokens"),
        )
        self._add(tokens, prompt=tokens.prompt, cli=cli)

    def _codex(self, info: Mapping[str, object], *, part: int, cli: str) -> None:
        """Count what Codex's running totals grew by since its last report."""
        total = _fields(info.get("total_token_usage"))
        before = self.totals.get(part, {})
        self.totals[part] = total
        grown = {key: _count(total, key) - _count(before, key) for key in total}
        if not any(grown.values()):
            return
        cached = grown.get("cached_input_tokens", 0)
        tokens = TokenCount(
            request=max(0, grown.get("input_tokens", 0) - cached),
            response=grown.get("output_tokens", 0),
            cache_write=0,
            cache_write_1h=0,
            cache_read=cached,
        )
        last = _fields(info.get("last_token_usage"))
        self._add(tokens, prompt=_count(last, "input_tokens") or tokens.prompt, cli=cli)
        window = _count(info, "model_context_window")
        if window:
            self.sums.context_window = window

    def _add(self, tokens: TokenCount, *, prompt: int, cli: str) -> None:
        """Add one call's tokens, and its price at the model in force."""
        held = self.sums
        self.sums = held.model_copy(
            update={
                "input_tokens": held.input_tokens + tokens.prompt,
                "output_tokens": held.output_tokens + tokens.response,
                "cache_read_tokens": held.cache_read_tokens + tokens.cache_read,
                "context_tokens": prompt,
            },
        )
        self.calls += 1
        model = self.capability(cli, self.model)
        if model is None:
            self.priced = False
            return
        self.usd += (model.prices["auto", prompt, self.today] * tokens).total
        self.sums.context_window = (
            model.limits.max_request_tokens or held.context_window
        )


def _fields(value: object) -> Mapping[str, object]:
    """Return ``value`` as a JSON object; empty for anything else."""
    return from_plain(value, dict[str, object], default={})


def _count(fields: Mapping[str, object], key: str) -> int:
    """Return the token count ``fields`` gives under ``key``; 0 for none."""
    return from_plain(fields.get(key), int, default=0)
