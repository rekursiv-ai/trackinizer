"""Frozen normalized-JSON bytes for each captured session fixture.

``normalized_test.py`` proves a session survives the JSON round trip, and
``testdata_test.py`` proves it returns to its provider's bytes. Both pass just
as well after the reader and writer shift together, so neither can see the
STORED form move. This freezes that form.

The normalized JSON is what a session is archived and shipped as -- the
PostgreSQL payload and the cross-provider handoff -- so a byte that moves here
is a stored-session compatibility break, not a stale expectation. Regenerate
only alongside that decision::

    SESSIONS_REGENERATE_GOLDEN=1 uv --quiet run --frozen pytest \
        trackinizer/lib/agent/sessions/normalized_golden_test.py
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Final

import os

import pytest

from trackinizer.lib.agent.sessions import claude, codex, normalized
from trackinizer.lib.agent.sessions.convert import _Adapter
from trackinizer.lib.agent.types.sessions import SessionRecord


_TESTDATA: Final = Path(__file__).resolve().parent / "testdata"
_ENV_REGENERATE: Final = "SESSIONS_REGENERATE_GOLDEN"


def fixtures() -> list[Path]:
    """Return every captured session fixture."""
    return sorted(_TESTDATA.glob("*.jsonl"))


def golden_path(fixture: Path) -> Path:
    """Return the golden holding a fixture's normalized JSON."""
    return fixture.with_suffix(".normalized.json")


def rendered(fixture: Path) -> str:
    """Return the normalized JSON a fixture encodes to."""
    adapter: _Adapter = claude if fixture.name.startswith("claude") else codex
    stream = StringIO()
    normalized.denormalize(
        adapter.normalize(StringIO(fixture.read_text(encoding="utf-8"))), stream
    )
    return stream.getvalue()


@pytest.mark.parametrize("fixture", fixtures(), ids=lambda p: p.stem)
def test_a_fixture_normalizes_to_its_frozen_bytes(fixture: Path) -> None:
    wire = rendered(fixture)

    if os.environ.get(_ENV_REGENERATE) == "1":
        golden_path(fixture).write_text(wire, encoding="utf-8")
        pytest.skip(f"regenerated {golden_path(fixture).name}")
    assert wire == golden_path(fixture).read_text(encoding="utf-8")


@pytest.mark.parametrize("fixture", fixtures(), ids=lambda p: p.stem)
def test_the_frozen_bytes_still_rebuild_the_provider_file(fixture: Path) -> None:
    # A golden nothing can read back would freeze a broken format. Reading the
    # STORED bytes -- not a fresh encode -- is what proves an archived session
    # still returns to the file it came from.
    adapter: _Adapter = claude if fixture.name.startswith("claude") else codex
    stored = golden_path(fixture).read_text(encoding="utf-8")

    rebuilt = StringIO()
    adapter.denormalize(normalized.normalize(StringIO(stored)), rebuilt)

    assert rebuilt.getvalue() == fixture.read_text(encoding="utf-8")


@pytest.mark.parametrize("fixture", fixtures(), ids=lambda p: p.stem)
def test_normalizing_twice_produces_the_same_bytes(fixture: Path) -> None:
    # The IR is a mapping from the source, so reading one file twice must give
    # one answer. A session id defaulting to ``uuid4()`` that no adapter
    # assigned made every parse mint a fresh id and the encoding differ run to
    # run; this is what fails if a field goes unsourced again.
    assert rendered(fixture) == rendered(fixture)


def test_every_fixture_has_a_golden() -> None:
    # Without this, adding a fixture and forgetting its golden makes the
    # parametrized tests above fail on a missing file rather than name the gap,
    # and deleting testdata/ turns them into no-ops that report success.
    assert fixtures()
    assert [p for p in fixtures() if not golden_path(p).exists()] == []


def test_a_golden_carries_every_record_type_the_fixture_holds() -> None:
    # The goldens defend only the record kinds they contain, so this reports
    # what that set is rather than leaving it implicit.
    kinds = {
        type(record).__name__ for fixture in fixtures() for record in _session(fixture)
    }

    assert kinds == {
        "AgentStatusResult",
        "AssistantMessage",
        # Every session opens with one: the launch line states the context the
        # model starts from, which is the clear's own subject.
        "ContextClear",
        "ContextState",
        "FileEditResult",
        "FileReadResult",
        "FileWriteResult",
        "ShellCommandResult",
        "SystemMessage",
        "Thinking",
        "TokenUsage",
        "ToolCall",
        "TurnContext",
        "UncategorizedRecord",
        "UncategorizedToolResult",
        "UserMessage",
        "WebFetchResult",
        "WebSearchResults",
    }


def _session(fixture: Path) -> list[SessionRecord]:
    """Return the normalized records for one fixture."""
    adapter: _Adapter = claude if fixture.name.startswith("claude") else codex
    return list(adapter.normalize(StringIO(fixture.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
