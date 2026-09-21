"""Tests for the v1 footprint policy against the design-doc table."""

from __future__ import annotations

import pytest

from trackinizer.server.semantic_mapper import IndexUnit, SemanticMapper
from trackinizer.server.semantic_mapper_footprint import (
    CHUNK_CHARS,
    CHUNK_OVERLAP_CHARS,
    HEAD_CHARS,
    FootprintMapper,
)


MAPPER = FootprintMapper()


def test_satisfies_the_protocol() -> None:
    assert isinstance(MAPPER, SemanticMapper)
    assert MAPPER.name == "footprint-v1"


class TestProse:
    @pytest.mark.parametrize(
        "kind",
        [
            "UserMessage",
            "AssistantMessage",
            "AgentToAgentMessage",
            "ToolCall",
            "ContextCompaction",
            "WebFetchResult",
            "WebSearchResults",
        ],
    )
    def test_short_prose_is_one_full_embed_unit(self, kind: str) -> None:
        units = MAPPER.units(kind=kind, text="fix the flaky retry test")
        assert units == (IndexUnit(text="fix the flaky retry test"),)
        assert units[0].fts
        assert units[0].embed

    @pytest.mark.parametrize("kind", ["SystemMessage", "Thinking", "IncompleteRecord"])
    def test_fts_only_prose_is_one_unembedded_unit(self, kind: str) -> None:
        # F-only prose (doc: SystemMessage user override 2026-09-20; Thinking;
        # IncompleteRecord): full text on the term surface, no vector.
        units = MAPPER.units(kind=kind, text="near-duplicate system spam")
        assert units == (IndexUnit(text="near-duplicate system spam", embed=False),)
        assert units[0].fts
        assert not units[0].embed

    def test_long_prose_chunks_with_overlap(self) -> None:
        text = "x" * (CHUNK_CHARS * 2)
        units = MAPPER.units(kind="UserMessage", text=text)
        assert len(units) > 1
        assert all(len(unit.text) <= CHUNK_CHARS for unit in units)
        assert [unit.chunk for unit in units] == list(range(len(units)))
        # Consecutive chunks share the overlap window.
        step = CHUNK_CHARS - CHUNK_OVERLAP_CHARS
        assert text[step : step + CHUNK_CHARS] == units[1].text

    def test_only_chunk_zero_joins_fts(self) -> None:
        units = MAPPER.units(kind="UserMessage", text="y" * (CHUNK_CHARS * 3))
        assert [unit.fts for unit in units] == [
            ordinal == 0 for ordinal in range(len(units))
        ]
        assert all(unit.embed for unit in units)

    def test_fts_only_long_prose_never_embeds_any_chunk(self) -> None:
        # A long SystemMessage still chunks (chunk 0 on fts), but no chunk embeds.
        units = MAPPER.units(kind="SystemMessage", text="z" * (CHUNK_CHARS * 3))
        assert len(units) > 1
        assert [unit.fts for unit in units] == [
            ordinal == 0 for ordinal in range(len(units))
        ]
        assert not any(unit.embed for unit in units)


class TestHeaded:
    @pytest.mark.parametrize(
        "kind",
        [
            "ShellCommandResult",
            "UncategorizedToolResult",
            "FileWriteResult",
            "FileEditResult",
            "Stdout",
            "Stderr",
            "Stdin",
        ],
    )
    def test_machine_output_keeps_only_the_fts_head(self, kind: str) -> None:
        text = "exit 1: ModuleNotFoundError\n" + "log spam " * 10_000
        units = MAPPER.units(kind=kind, text=text)
        assert len(units) == 1
        head = units[0]
        assert head.field == "head"
        assert len(head.text) == HEAD_CHARS
        assert head.text.startswith("exit 1: ModuleNotFoundError")
        assert head.fts
        assert not head.embed

    def test_short_output_is_kept_whole(self) -> None:
        units = MAPPER.units(kind="ShellCommandResult", text="ok")
        assert units[0].text == "ok"
        assert not units[0].embed


class TestSilent:
    @pytest.mark.parametrize(
        "kind",
        [
            "AgentStatusResult",
            "FileReadResult",
            "TokenUsage",
            "TurnContext",
            "ContextState",
            "ContextClear",
            "UncategorizedRecord",
            "NeverHeardOfIt",
        ],
    )
    def test_unindexed_kinds_emit_nothing(self, kind: str) -> None:
        assert MAPPER.units(kind=kind, text="some stored text") == ()

    def test_empty_text_emits_nothing_for_any_kind(self) -> None:
        assert MAPPER.units(kind="UserMessage", text="") == ()


# The footprint table of ``docs/private/session_indexing.md`` as a literal:
# kind -> (fts, embed, shape). ``shape`` is "full" for whole/chunked prose,
# "head" for a truncated machine-output head, "silent" for no units. This is
# the conformance oracle -- FootprintMapper behavior for every kind must match
# the doc, so a policy edit that forgets to touch one is caught here.
_FOOTPRINT: dict[str, tuple[bool, bool, str]] = {
    # Prose, F+E (embedded).
    "UserMessage": (True, True, "full"),
    "AssistantMessage": (True, True, "full"),
    "AgentToAgentMessage": (True, True, "full"),
    "ToolCall": (True, True, "full"),
    "ContextCompaction": (True, True, "full"),
    "WebFetchResult": (True, True, "full"),
    "WebSearchResults": (True, True, "full"),
    # Prose, F-only (embed=False). SystemMessage: user override 2026-09-20.
    "SystemMessage": (True, False, "full"),
    "Thinking": (True, False, "full"),
    "IncompleteRecord": (True, False, "full"),
    # Machine output: F(head), body blobbed. Streams are machine output too.
    "ShellCommandResult": (True, False, "head"),
    "UncategorizedToolResult": (True, False, "head"),
    "FileWriteResult": (True, False, "head"),
    "FileEditResult": (True, False, "head"),
    "Stdout": (True, False, "head"),
    "Stderr": (True, False, "head"),
    "Stdin": (True, False, "head"),
    # Silent: telemetry, file reads, unrecognized.
    "AgentStatusResult": (False, False, "silent"),
    "FileReadResult": (False, False, "silent"),
    "TokenUsage": (False, False, "silent"),
    "TurnContext": (False, False, "silent"),
    "ContextState": (False, False, "silent"),
    "ContextClear": (False, False, "silent"),
    "UncategorizedRecord": (False, False, "silent"),
}


class TestConformsToDocTable:
    """Every kind's units match the ``session_indexing.md`` footprint table."""

    @pytest.mark.parametrize(("kind", "spec"), _FOOTPRINT.items())
    def test_short_text_matches_the_footprint(
        self,
        kind: str,
        spec: tuple[bool, bool, str],
    ) -> None:
        fts, embed, shape = spec
        units = MAPPER.units(kind=kind, text="short text")
        if shape == "silent":
            assert units == (), kind
            return
        assert len(units) == 1, kind
        unit = units[0]
        assert unit.fts == fts, kind
        assert unit.embed == embed, kind
        assert unit.field == ("head" if shape == "head" else "content"), kind

    def test_indexed_kinds_is_exactly_the_non_silent_set(self) -> None:
        expected = {
            kind for kind, (_f, _e, shape) in _FOOTPRINT.items() if shape != "silent"
        }
        assert set(MAPPER.indexed_kinds) == expected

    def test_embedded_kinds_is_exactly_the_embed_true_set(self) -> None:
        expected = {kind for kind, (_f, embed, _s) in _FOOTPRINT.items() if embed}
        assert set(MAPPER.embedded_kinds) == expected


class TestIndexedKinds:
    """``indexed_kinds`` is exactly the set that yields units (no predicate drift)."""

    def test_every_indexed_kind_yields_a_unit(self) -> None:
        for kind in MAPPER.indexed_kinds:
            assert MAPPER.units(kind=kind, text="non-empty text") != (), kind

    def test_a_kind_outside_the_set_yields_nothing(self) -> None:
        # The backfill scanner's pending predicate binds ``indexed_kinds``; a kind
        # absent here must produce no unit, or the predicate would drift off the
        # mapper and re-open the pending-forever leak (ContextState etc.).
        for kind in ("ContextState", "TokenUsage", "FileReadResult"):
            assert kind not in MAPPER.indexed_kinds
            assert MAPPER.units(kind=kind, text="non-empty text") == ()

    def test_embedded_kinds_is_a_subset_of_indexed_kinds(self) -> None:
        assert MAPPER.embedded_kinds <= MAPPER.indexed_kinds


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
