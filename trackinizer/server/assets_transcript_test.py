"""The transcript view must nest tool results without hiding any.

``server/assets/index.html`` renders a captured :class:`AgentSession` as a list
of turns. A tool call and its output arrive as two separate events -- the call
nested in an ``AssistantMessage``, the output as a standalone ``ToolResult`` --
so rendering them flat left the reader to re-pair them by eye, and a long result
buried the conversation it belonged to.

They are now joined on ``ToolCall.id == ToolResult.call_id``, a provider-assigned
key, and drawn as one collapsible unit.

**The part these tests exist to protect is the leftovers.** Nesting is a filter,
and a filter that silently drops what it cannot place renders a tidier session
than the one that happened. A ``ToolResult`` whose ``call_id`` matches no
captured call -- because the assistant turn was truncated, or the capture began
mid-conversation -- must still appear, and must say why it is unattached. The
invariant is that every ``ToolResult`` is rendered exactly once: nested under its
call, or surfaced as an orphan. Never zero times.

Static scan, in the idiom of :mod:`assets_drift_test`: the SPA is plain HTML and
cannot be imported, so the contract is asserted against its source. That is
weaker than executing it and is stated plainly rather than papered over -- the
behavioural verification lives in the change's own review evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest


SPA = Path(__file__).parent / "assets" / "index.html"


@pytest.fixture(scope="module")
def source() -> str:
    return SPA.read_text(encoding="utf-8")


def test_results_are_joined_on_the_providers_own_call_id(source: str) -> None:
    """Adjacency is not a join.

    Pairing a result with "the call just above it" would be an inference, and it
    breaks the moment a model requests several tools in one turn -- which is
    exactly when a reader most needs the pairing to be right.
    """
    assert 'ev.kind === "ToolResult"' in source
    assert "results.set(cid, ev)" in source
    assert "results.get(tc.id)" in source


def test_an_unmatched_result_is_still_rendered(source: str) -> None:
    """The invariant: rendered exactly once, never zero times.

    ``consumed`` records the results that were actually drawn inside a call, and
    the second pass renders everything it does not contain. Deleting either half
    would make the view silently lossy while every visible turn still looked
    correct -- the failure mode is invisible by construction, which is why it is
    pinned here.
    """
    assert "const consumed = new Set();" in source
    assert "consumed.has(ev)" in source
    assert "turn-orphan" in source
    assert "tool-orphan-banner" in source


def test_a_call_with_no_result_says_so(source: str) -> None:
    """A blank body would read as "the tool returned nothing".

    That is a different claim from "no result was captured", and the difference
    matters: the first is a fact about the tool, the second a fact about the
    capture.
    """
    assert "No ToolResult was captured for this call id." in source
    assert "no result captured" in source


def test_failures_are_not_collapsed(source: str) -> None:
    """A collapsed error is an error nobody reads."""
    assert "if (err) box.open = true;" in source


def test_the_flat_rendering_is_gone(source: str) -> None:
    """Guards against a revert that leaves the new code in place beside the old.

    Both paths rendering would double-draw every tool call, which reads as a
    duplicated transcript rather than as a bug.
    """
    assert "`${tc.name}(${JSON.stringify(tc.args || {})})`" not in source
