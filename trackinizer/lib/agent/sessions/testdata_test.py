"""Round-trip tests against captured live CLI sessions.

The fixtures are real CLI output, so they carry record kinds a synthesized
sample would not.

Regenerate after a CLI version bump:
  sh trackinizer/lib/agent/sessions/testdata/capture.py
"""

from __future__ import annotations

from collections.abc import Iterable
from io import StringIO
from pathlib import Path

import pytest

from trackinizer.lib.agent.sessions import claude, codex
from trackinizer.lib.agent.sessions.convert import _Adapter, detect_format
from trackinizer.lib.agent.sessions.testdata.capture import (
    _SECRETS,
    _emit,
    _scrub_secrets,
)
from trackinizer.lib.agent.types.sessions import SessionRecord, UncategorizedRecord


_TESTDATA = Path(__file__).resolve().parent / "testdata"


def _fixtures() -> list[Path]:
    """Return every captured session fixture, newest layout first."""
    return sorted(_TESTDATA.glob("*.jsonl"))


def _adapter_for(path: Path) -> _Adapter:
    """Return the adapter matching a fixture's detected format."""
    return claude if path.name.startswith("claude") else codex


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_a_captured_session_round_trips_byte_for_byte(path: Path) -> None:
    native = path.read_text(encoding="utf-8")
    adapter = _adapter_for(path)

    records = list(adapter.normalize(StringIO(native)))
    rebuilt = StringIO()
    adapter.denormalize(records, rebuilt)

    assert rebuilt.getvalue() == native


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_a_captured_session_is_detected_as_its_own_format(path: Path) -> None:
    expected = "claude" if path.name.startswith("claude") else "codex"

    assert detect_format(path.read_text(encoding="utf-8")) == expected


UNCATEGORIZED = {
    "claude_main": [
        "ai-title",
        "atis-latch",
        "bridge-session",
        "file-history-snapshot",
        "last-prompt",
        "mode",
        "permission-mode",
        "queue-operation",
    ],
    "claude_sidechain": [],
    "codex_main": [
        "event_msg/item_completed/AgentMessage",
        "event_msg/item_completed/Reasoning",
        "event_msg/item_completed/UserMessage",
        "event_msg/task_complete",
        "event_msg/task_started",
    ],
}
"""What each fixture leaves uncategorized, and therefore what it does not
claim to understand. A kind leaving this list is the IR learning something;
a kind joining it is the IR giving something up."""


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_a_captured_session_maps_every_record_kind(path: Path) -> None:
    # The point of capturing live output: a kind the CLI emits but no adapter
    # maps lands in UncategorizedRecord, and this is what reports it. A
    # synthesized corpus cannot fail here, because its author only writes
    # kinds they already know about.
    records = _adapter_for(path).normalize(StringIO(path.read_text(encoding="utf-8")))

    assert _unmapped(records) == UNCATEGORIZED[path.stem]


def _unmapped(records: Iterable[SessionRecord]) -> list[str]:
    """Return the record kinds that reached the uncategorized fallback."""
    return sorted({r.kind for r in records if isinstance(r, UncategorizedRecord)})


def test_the_fixture_directory_is_populated() -> None:
    # Without this, deleting testdata/ would silently turn every
    # parametrized test above into a no-op that reports success.
    assert _fixtures(), f"no session fixtures in {_TESTDATA}"


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_a_captured_session_carries_no_credential(path: Path) -> None:
    # A capture drives real tools against a real environment, so the model can
    # echo a live secret into its own transcript: one codex run wrote a 1900
    # character `sk-` key into its rollout. That fixture must never be
    # committed, so the check runs here as well as in the capture script.
    text = path.read_text(encoding="utf-8")

    assert [p.pattern for p in _SECRETS if p.search(text)] == []


def test_the_capture_script_refuses_to_write_a_leaked_fixture(
    tmp_path: Path,
) -> None:
    # Proves the guard bites: without it, `_emit` writes whatever the CLI
    # produced, and a key reaches disk with nothing to catch it.
    source = tmp_path / "rollout.jsonl"
    source.write_text(
        '{"type":"event_msg","payload":{"text":"export KEY=sk-' + "A" * 40 + '"}}\n',
        encoding="utf-8",
    )
    target = tmp_path / "out.jsonl"

    _emit(source, target, home=tmp_path / "home", work=tmp_path / "work")

    written = target.read_text(encoding="utf-8")
    assert "A" * 40 not in written
    # Same-shape filler, not a short token: the fixture keeps exercising the
    # parser against a field of realistic length, and stays byte-comparable.
    assert "sk-deadbeef" in written
    assert len(written) == len(source.read_text(encoding="utf-8"))


@pytest.mark.parametrize("length", [20, 23, 40, 41, 1900])
def test_filler_repeats_to_the_exact_length_it_replaces(length: int) -> None:
    # Length consistency is the point of the filler: a replacement that is
    # shorter or longer changes the field the parser is tested against, and a
    # naive repeat-count overshoots on any length not divisible by 8.
    secret = "sk-" + "A" * length

    scrubbed = _scrub_secrets(secret)

    assert len(scrubbed) == len(secret)
    assert scrubbed == "sk-" + ("deadbeef" * length)[:length]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
