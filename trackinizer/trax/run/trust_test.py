"""Tests for the trust read and the Chat card a run opens for an untrusted folder.

Every config is written under ``tmp_path``, with ``home`` and the environment
injected: no test reads the person's own claude or codex config.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import json
import threading
import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.trax.run.trust import (
    TrustGate,
    TrustScreen,
    claude_remembers,
    claude_trusted,
    codex_trusted,
)
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_session_ir import RecordBody


_CWD: Final = Path(__file__).resolve().parent

if TYPE_CHECKING:
    from collections.abc import Callable

    from trackinizer.lib.agent.types.sessions import SystemMessage


# The trust card as Chat reads it, and the answer Chat posts for it: files the web's
# tests read too, so a change of shape on either side fails both.
_SEAMS: Final = _CWD.parents[1] / "web" / "test" / "seams"


def _claude_config(path: Path, trusted: dict[Path, bool]) -> None:
    """Write a claude global config whose projects hold ``trusted``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    projects = {
        str(folder): {"hasTrustDialogAccepted": accepted}
        for folder, accepted in trusted.items()
    }
    path.write_text(json.dumps({"numStartups": 3, "projects": projects}))


def _codex_config(home: Path, levels: dict[Path, str]) -> None:
    """Write a codex config whose projects hold ``levels``."""
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    body = "".join(
        f'[projects."{folder}"]\ntrust_level = "{level}"\n\n'
        for folder, level in levels.items()
    )
    (home / ".codex" / "config.toml").write_text(f'model = "o3"\n\n{body}')


def _worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Make a main repository and a linked worktree of it; return both roots."""
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n")
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
    return main, worktree


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Return an empty home folder under ``tmp_path``."""
    folder = tmp_path / "home"
    folder.mkdir()
    return folder


class TestClaudeTrusted:
    """The read mirrors claude's own: project key first, then the folders above."""

    def test_the_folders_own_entry(self, tmp_path: Path, home: Path) -> None:
        folder = tmp_path / "proj"
        folder.mkdir()
        _claude_config(home / ".claude.json", {folder: True})

        assert claude_trusted(folder, environ={}, home=home)

    def test_a_repo_subfolder_takes_its_roots_trust(
        self,
        tmp_path: Path,
        home: Path,
    ) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "deep" / "er").mkdir(parents=True)
        _claude_config(home / ".claude.json", {repo: True})

        assert claude_trusted(repo / "deep" / "er", environ={}, home=home)

    def test_a_worktree_takes_its_main_repositorys_trust(
        self,
        tmp_path: Path,
        home: Path,
    ) -> None:
        main, worktree = _worktree(tmp_path)
        _claude_config(home / ".claude.json", {main: True})

        assert claude_trusted(worktree / "src", environ={}, home=home)

    def test_a_folder_outside_any_repo_takes_a_trusted_ancestors(
        self,
        tmp_path: Path,
        home: Path,
    ) -> None:
        folder = tmp_path / "a" / "b"
        folder.mkdir(parents=True)
        _claude_config(home / ".claude.json", {tmp_path / "a": True})

        assert claude_trusted(folder, environ={}, home=home)

    def test_a_trusted_folder_above_an_untrusted_repo_does_not_cover_it(
        self,
        tmp_path: Path,
        home: Path,
    ) -> None:
        repo = tmp_path / "outer" / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src").mkdir()
        _claude_config(home / ".claude.json", {tmp_path / "outer": True})

        assert not claude_trusted(repo / "src", environ={}, home=home)

    def test_home_saved_as_untrusted_is_untrusted(self, home: Path) -> None:
        _claude_config(home / ".claude.json", {home: False})

        assert not claude_trusted(home, environ={}, home=home)

    @pytest.mark.parametrize("body", [None, "{not json", '{"projects": 7}'])
    def test_a_missing_or_garbled_config_trusts_nothing(
        self,
        home: Path,
        body: str | None,
    ) -> None:
        if body is not None:
            (home / ".claude.json").write_text(body)

        assert not claude_trusted(home, environ={}, home=home)

    def test_a_sandboxed_claude_trusts_everything(self, home: Path) -> None:
        assert claude_trusted(home, environ={"CLAUDE_CODE_SANDBOXED": "1"}, home=home)

    def test_the_config_dir_and_the_custom_oauth_suffix_are_honoured(
        self,
        tmp_path: Path,
        home: Path,
    ) -> None:
        folder = tmp_path / "proj"
        folder.mkdir()
        elsewhere = tmp_path / "config"
        _claude_config(elsewhere / ".claude-custom-oauth.json", {folder: True})
        _claude_config(elsewhere / ".claude.json", {folder: False})
        _claude_config(home / ".claude.json", {folder: True})
        custom = {
            "CLAUDE_CONFIG_DIR": str(elsewhere),
            "CLAUDE_CODE_CUSTOM_OAUTH_URL": "https://auth.example",
        }

        assert claude_trusted(folder, environ=custom, home=home)
        assert not claude_trusted(
            folder,
            environ={"CLAUDE_CONFIG_DIR": str(elsewhere)},
            home=home,
        )

    def test_a_non_empty_legacy_config_wins(self, tmp_path: Path, home: Path) -> None:
        folder = tmp_path / "proj"
        folder.mkdir()
        _claude_config(home / ".claude" / ".config.json", {folder: True})
        _claude_config(home / ".claude.json", {folder: False})

        assert claude_trusted(folder, environ={}, home=home)

    def test_claude_keeps_trust_in_home_for_the_run_only(
        self,
        tmp_path: Path,
        home: Path,
    ) -> None:
        assert not claude_remembers(home, home=home)
        assert claude_remembers(tmp_path, home=home)


class TestCodexTrusted:
    """A saved decision, either way, means codex asks nothing."""

    @pytest.mark.parametrize("level", ["trusted", "untrusted"])
    def test_the_folders_own_decision(
        self,
        tmp_path: Path,
        home: Path,
        level: str,
    ) -> None:
        _codex_config(home, {tmp_path: level})

        assert codex_trusted(tmp_path, environ={}, home=home)

    def test_a_repo_roots_decision(self, tmp_path: Path, home: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src").mkdir()
        _codex_config(home, {repo: "trusted"})

        assert codex_trusted(repo / "src", environ={}, home=home)

    def test_a_worktrees_main_root(self, tmp_path: Path, home: Path) -> None:
        main, worktree = _worktree(tmp_path)
        _codex_config(home, {main: "trusted"})

        assert codex_trusted(worktree, environ={}, home=home)

    def test_codex_home_is_honoured(self, tmp_path: Path, home: Path) -> None:
        _codex_config(tmp_path / "other", {tmp_path: "trusted"})

        assert codex_trusted(
            tmp_path,
            environ={"CODEX_HOME": str(tmp_path / "other" / ".codex")},
            home=home,
        )

    @pytest.mark.parametrize("body", [None, "projects = [", 'model = "o3"\n'])
    def test_no_decision_or_no_readable_config(
        self,
        tmp_path: Path,
        home: Path,
        body: str | None,
    ) -> None:
        if body is not None:
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text(body)

        assert not codex_trusted(tmp_path, environ={}, home=home)


class _Keys:
    """Records the keys typed into the CLI and whether it was stopped."""

    def __init__(self) -> None:
        self.typed: list[bytes] = []
        self.terminated = 0
        self.on_write: Callable[[], None] = _nothing

    def write(self, data: bytes) -> None:
        self.typed.append(data)
        self.on_write()

    def terminate(self) -> None:
        self.terminated += 1


def _nothing() -> None:
    """Do nothing, as a CLI does on most keys."""


class _Card:
    """A gate with its collaborators faked: no real time, no terminal."""

    def __init__(self, cli: str = "claude", *, ready: bool = False) -> None:
        self.records: list[SystemMessage] = []
        self.pending = threading.Event()
        self.keys = _Keys()
        self.slept: list[float] = []
        self.rearmed = 0
        self.is_ready = ready
        self.gate = TrustGate(
            emit=self.records.append,
            pending=self.pending,
            ready=lambda: self.is_ready,
            rearm=self._rearm,
            relay=self.keys,
            sleep=self.slept.append,
            clock=lambda: datetime(2026, 10, 20, tzinfo=UTC),
        )
        self.gate.open(cli, "~/proj", machine="gx10", remembered=cli == "codex")
        card = self.records[0].extra["$interaction"]
        assert isinstance(card, dict)
        self.id = card["id"]

    def answer(self, decision: str, *, prompt_id: object = None) -> bool:
        return self.gate.answer(
            {
                "prompt_id": self.id if prompt_id is None else prompt_id,
                "decision": decision,
                "updated_input": None,
                "message": "",
            },
        )

    def resolved(self) -> list[object]:
        return [
            record.extra["$interaction_resolved"]
            for record in self.records
            if record.subtype == "interaction_resolved"
        ]

    def trusted_in_the_terminal(self, seconds: float) -> None:
        """Wait out the key gap, during which the person accepts at the terminal."""
        self.slept.append(seconds)
        self.is_ready = True

    def _rearm(self) -> None:
        self.rearmed += 1


class TestTrustGate:
    """One card: the first answer wins, and only the dialog's own keys are typed."""

    def test_open_writes_one_trust_card_and_holds_the_lines(self) -> None:
        card = _Card()
        card.gate.open("claude", "~/other", machine=None, remembered=True)

        [record] = card.records
        assert record.subtype == "interaction"
        assert record.content == "claude waits for trust in ~/proj"
        assert record.timestamp == "2026-10-20T00:00:00+00:00"
        assert record.extra["$interaction"] == {
            "id": card.id,
            "type": "trust",
            "tool": "claude",
            "input": {"folder": "~/proj"},
            "questions": None,
            "plan": None,
            "state": "pending",
            "cli": "claude",
            "folder": "~/proj",
            "machine": "gx10",
            "remembered": False,
        }
        assert card.pending.is_set()
        assert card.gate.holds(card.id)
        assert not card.gate.holds("another")

    def test_claude_allow_moves_down_then_enters(self) -> None:
        card = _Card()

        assert card.answer("allow")

        assert card.keys.typed == [b"\x1b[B", b"\r"]
        assert card.slept == [0.3]
        assert card.resolved() == [{"id": card.id, "decision": "allow", "by": "chat"}]
        assert card.records[-1].content == "Trusted from Chat"
        assert card.records[-1].timestamp == "2026-10-20T00:00:00+00:00"
        assert not card.pending.is_set()
        assert card.rearmed == 1

    def test_claude_trusted_in_the_terminal_meanwhile_gets_no_enter(self) -> None:
        card = _Card()
        card.gate.sleep = card.trusted_in_the_terminal

        assert not card.answer("allow")

        assert card.keys.typed == [b"\x1b[B"]
        assert card.resolved() == [
            {"id": card.id, "decision": "allow", "by": "terminal"},
        ]
        assert card.rearmed == 0

    def test_codex_allow_enters_only(self) -> None:
        card = _Card("codex")

        assert card.answer("allow")

        assert card.keys.typed == [b"\r"]
        assert card.slept == []
        assert card.resolved() == [{"id": card.id, "decision": "allow", "by": "chat"}]

    def test_deny_stops_the_run(self) -> None:
        card = _Card()

        assert card.answer("deny")

        assert (card.keys.typed, card.keys.terminated) == ([], 1)
        assert card.resolved() == [{"id": card.id, "decision": "deny", "by": "chat"}]
        assert card.records[-1].content == "Not trusted from Chat: the run stopped"
        assert not card.pending.is_set()

    def test_the_cli_saying_it_is_ready_means_the_terminal_trusted_it(self) -> None:
        card = _Card()
        card.gate.check(ready=False)
        unanswered = card.resolved()
        card.gate.check(ready=True)

        assert unanswered == []
        assert card.resolved() == [
            {"id": card.id, "decision": "allow", "by": "terminal"},
        ]
        assert card.records[-1].content == "Trusted in the terminal"

    @pytest.mark.parametrize(
        ("exited", "decision", "by"),
        [(True, "deny", "terminal"), (False, "released", "run")],
    )
    def test_a_card_open_at_the_end_is_closed(
        self,
        *,
        exited: bool,
        decision: str,
        by: str,
    ) -> None:
        card = _Card()
        card.gate.close(cli_exited=exited)
        card.gate.close(cli_exited=exited)

        assert card.resolved() == [{"id": card.id, "decision": decision, "by": by}]
        assert not card.pending.is_set()

    def test_a_second_answer_types_nothing(self) -> None:
        card = _Card()
        assert card.answer("allow")
        card.keys.typed.clear()

        assert not card.answer("allow")
        assert not card.answer("deny")
        assert (card.keys.typed, card.keys.terminated) == ([], 0)
        assert len(card.resolved()) == 1

    def test_an_answer_for_another_card_types_nothing(self) -> None:
        card = _Card()

        assert not card.answer("allow", prompt_id="another")
        assert not card.answer("allow", prompt_id=7)
        assert card.keys.typed == []
        assert card.pending.is_set()

    def test_no_card_is_open_before_open(self) -> None:
        records: list[SystemMessage] = []
        gate = TrustGate(
            emit=records.append,
            pending=threading.Event(),
            ready=lambda: False,
            rearm=lambda: None,
        )
        gate.check(ready=True)
        gate.close(cli_exited=True)

        assert not gate.holds(None)
        assert not gate.answer({"prompt_id": None, "decision": "allow"})
        assert records == []

    def test_a_decision_that_is_neither_types_nothing(self) -> None:
        card = _Card()

        assert not card.answer("maybe")
        assert card.keys.typed == []
        assert card.pending.is_set()

    def test_a_cli_that_quits_as_the_enter_lands_loses_the_chat_answer(self) -> None:
        """The card closes as refused in the terminal, and nothing is rearmed."""
        card = _Card("codex")
        card.keys.on_write = lambda: card.gate.close(cli_exited=True)

        assert not card.answer("allow")

        assert card.resolved() == [
            {"id": card.id, "decision": "deny", "by": "terminal"},
        ]
        assert card.rearmed == 0


class TestTheCardChatShows:
    """The card the gate writes is the one Chat renders, and Chat's answer wins it."""

    def test_the_record_is_the_one_the_web_renders(self) -> None:
        card = _Card()
        [record] = card.records
        row = SessionRecordRow.of(
            session_id=uuid.UUID(int=0),
            part=1,
            idx=1,
            record=record,
        )
        stored = json.dumps(RecordBody.of(row).model_dump(mode="json"))

        assert json_loads(stored.replace(str(card.id), "card-1")) == json_loads(
            (_SEAMS / "trustCard.json").read_text(encoding="utf-8"),
        )

    def test_the_answer_chat_posts_for_it_trusts_the_folder(self) -> None:
        posted = json_loads(
            (_SEAMS / "trustAnswer.json").read_text(encoding="utf-8"),
            dict[str, object],
        )
        payload = from_plain(posted["payload"], dict[str, object])
        card = _Card()

        assert posted["kind"] == "answer"
        assert payload["prompt_id"] == "card-1"
        assert card.gate.answer({**payload, "prompt_id": card.id})
        assert card.keys.typed == [b"\x1b[B", b"\r"]
        assert card.resolved() == [{"id": card.id, "decision": "allow", "by": "chat"}]


class TestTrustScreen:
    """Codex's dialog is found on a screen whose words are placed by cursor moves."""

    def test_the_measured_screen_is_found_once(self) -> None:
        seen: list[bool] = []
        screen = TrustScreen(lambda: seen.append(True))

        screen.output(b"\x1b[5;3H\x1b[22mTrust\x1b[5;9Hthis\x1b[5;14Hfolder?")
        screen.output(b"\x1b[5;3HTrust\x1b[5;9Hthis\x1b[5;14Hfolder?")

        assert seen == [True]

    def test_a_screen_split_across_reads_is_found(self) -> None:
        seen: list[bool] = []
        screen = TrustScreen(lambda: seen.append(True))

        screen.output(b"Trust\x1b[5")
        before = list(seen)
        screen.output(b";9Hthis\x1b[5;14Hfolder?")

        assert (before, seen) == ([], [True])

    def test_other_output_is_not_the_dialog(self) -> None:
        seen: list[bool] = []
        screen = TrustScreen(lambda: seen.append(True))

        screen.output(b"Trust\x1b[5;9Hthat folder? " + b"x" * 1_000)
        screen.output(b"XTrust\x1b[5;9Hthis\x1b[5;14Hfolder?")

        assert seen == []


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
