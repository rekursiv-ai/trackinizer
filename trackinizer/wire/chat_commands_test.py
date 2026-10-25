"""The slash command rule: which typed text Chat sends as a command, per provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

import pytest

from trackinizer.lib.agent.catalog import models
from trackinizer.wire.chat_commands import (
    COMMAND_CHARS,
    KNOWN_VERBS,
    Command,
    Line,
    Refused,
    Settings,
    chat_models,
    classify,
)


if TYPE_CHECKING:
    from trackinizer.wire.chat_commands import Classified
    from trackinizer.wire.wire_science_chat import ChatProvider


_TOO_LONG: Final = (
    "A command is one line of at most 800 characters; put a longer goal in a file "
    "and write /goal follow <path>"
)
_GOAL_BARE: Final = "Chat shows the goal above the box; /goal clear stops it"
_ASSISTANT: Final = (
    "The assistant runs no slash commands. /goal works in a chat on claude or "
    "codex: pick one in the provider card."
)
_RENAME_BARE: Final = "codex asks for the name in a dialog; type /rename <name>"
_NO_RC: Final = (
    "codex has no remote control for a chat's run: codex remote-control serves "
    "codex's background daemon, not this run"
)


def _menu(verb: str) -> Refused:
    return Refused(
        reason=f"Chat cannot run /{verb}: it opens a menu, changes settings or "
        "switches the session. Use it in the run's own terminal.",
    )


_CLAUDE: Final = models("anthropic")
_CODEX: Final = models("openai")
_ROLES: Final = ("best", "default", "everyday", "utility")
_CLAUDE_ALIASES: Final = list(
    dict.fromkeys(_CLAUDE[role].model_id.rpartition("-")[0] for role in _ROLES),
)
_CODEX_IDS: Final = list(dict.fromkeys(_CODEX[role].wire_model_id for role in _ROLES))
_BARE_MODEL: Final = {
    "claude": "Chat can't open the model menu. Type /model <name> to switch, for "
    f"example /model {_CLAUDE['default'].model_id.rpartition('-')[0]}. Options: {', '.join(_CLAUDE_ALIASES)}, or "
    f"a full id such as {_CLAUDE['default'].wire_model_id}.",
    "codex": "Chat can't open the model menu. Type /model <name> to switch, for "
    f"example /model {_CODEX['default'].wire_model_id}. Options: {', '.join(_CODEX_IDS)}.",
}
_BARE_EFFORT: Final = (
    "Chat can't open the effort menu. Type /effort <level> to switch, for example "
    "/effort high. Options: low, medium, high, xhigh, max."
)


_ONE_MODEL: Final = (
    "/model takes one model name of at most 120 characters, as in /model opus"
)
_ONE_EFFORT: Final = (
    "/effort takes one level of at most 20 characters, as in /effort high"
)


@pytest.mark.parametrize(
    ("text", "provider", "expected"),
    [
        # Only a known verb at the very start of the text is a command.
        ("/etc/hosts is broken", "claude", Line()),
        ("/tmp is full", "claude", Line()),  # noqa: S108 -- A typed line, never opened.
        (" /goal x", "claude", Line()),
        ("ship it /goal x", "codex", Line()),
        ("/review-pr 12", "claude", Line()),
        ("/", "claude", Line()),
        ("", "claude", Line()),
        (" \n ", "codex", Line()),
        ("/etc/hosts is broken", "assistant", Line()),
        # /goal <text> sets a goal, and the verb is read in any case.
        (
            "/goal ship it",
            "claude",
            Command(text="/goal ship it", verb="goal", args="ship it"),
        ),
        (
            "/goal ship it",
            "codex",
            Command(text="/goal ship it", verb="goal", args="ship it"),
        ),
        ("/Goal X", "claude", Command(text="/goal X", verb="goal", args="X")),
        (
            "/goal\tship it ",
            "codex",
            Command(text="/goal ship it", verb="goal", args="ship it"),
        ),
        (
            "/goal ship it\n",
            "claude",
            Command(text="/goal ship it", verb="goal", args="ship it"),
        ),
        # A bare /goal would open claude's panel.
        ("/goal", "claude", Refused(reason=_GOAL_BARE)),
        ("/goal  ", "codex", Refused(reason=_GOAL_BARE)),
        # Every word that clears a goal is typed as /goal clear.
        (
            "/goal stop",
            "claude",
            Command(text="/goal clear", verb="goal", args="clear"),
        ),
        ("/goal OFF", "codex", Command(text="/goal clear", verb="goal", args="clear")),
        (
            "/goal reset",
            "claude",
            Command(text="/goal clear", verb="goal", args="clear"),
        ),
        (
            "/goal none",
            "claude",
            Command(text="/goal clear", verb="goal", args="clear"),
        ),
        (
            "/goal cancel",
            "codex",
            Command(text="/goal clear", verb="goal", args="clear"),
        ),
        (
            "/goal clear",
            "codex",
            Command(text="/goal clear", verb="goal", args="clear"),
        ),
        # A clear word that begins a longer condition sets that condition.
        (
            "/goal stop the leak",
            "claude",
            Command(text="/goal stop the leak", verb="goal", args="stop the leak"),
        ),
        # Claude would take pause as the goal's condition; codex pauses it.
        (
            "/goal pause",
            "claude",
            Refused(reason="claude has no goal pause; /goal clear stops it"),
        ),
        (
            "/goal resume",
            "claude",
            Refused(reason="claude has no goal pause; /goal clear stops it"),
        ),
        (
            "/goal pause",
            "codex",
            Command(text="/goal pause", verb="goal", args="pause"),
        ),
        (
            "/goal Resume",
            "codex",
            Command(text="/goal resume", verb="goal", args="resume"),
        ),
        # /goal edit opens an editor in the terminal.
        (
            "/goal edit",
            "claude",
            Refused(
                reason="Chat cannot run /goal edit: it opens an editor. "
                "Use it in the run's own terminal.",
            ),
        ),
        (
            "/goal edit",
            "codex",
            Refused(
                reason="Chat cannot run /goal edit: it opens an editor. "
                "Use it in the run's own terminal.",
            ),
        ),
        # Claude queues /compact until its turn ends; codex refuses it mid-turn.
        ("/compact", "claude", Command(text="/compact", verb="compact", args="")),
        (
            "/compact keep the plan",
            "claude",
            Command(
                text="/compact keep the plan",
                verb="compact",
                args="keep the plan",
            ),
        ),
        (
            "/compact",
            "codex",
            Refused(reason="codex runs /compact only between turns; attach to run it"),
        ),
        # /model and /effort with an argument change the run's settings; bare, they
        # would open a menu.
        (
            "/model opus",
            "claude",
            Settings(text="/model opus", model="opus", effort=None),
        ),
        (
            "/Model gpt-5.5",
            "codex",
            Settings(text="/model gpt-5.5", model="gpt-5.5", effort=None),
        ),
        (
            "/effort high",
            "claude",
            Settings(text="/effort high", model=None, effort="high"),
        ),
        (
            "/EFFORT\tHigh ",
            "codex",
            Settings(text="/effort high", model=None, effort="high"),
        ),
        ("/model", "codex", Refused(reason=_BARE_MODEL["codex"])),
        ("/model  ", "claude", Refused(reason=_BARE_MODEL["claude"])),
        ("/effort", "claude", Refused(reason=_BARE_EFFORT)),
        ("/effort", "codex", Refused(reason=_BARE_EFFORT)),
        ("/model opus please", "claude", Refused(reason=_ONE_MODEL)),
        ("/effort very high", "codex", Refused(reason=_ONE_EFFORT)),
        (
            "/model " + "m" * 120,
            "claude",
            Settings(
                text="/model " + "m" * 120,
                model="m" * 120,
                effort=None,
            ),
        ),
        ("/model " + "m" * 121, "claude", Refused(reason=_ONE_MODEL)),
        (
            "/effort " + "e" * 20,
            "codex",
            Settings(
                text="/effort " + "e" * 20,
                model=None,
                effort="e" * 20,
            ),
        ),
        ("/effort " + "e" * 21, "codex", Refused(reason=_ONE_EFFORT)),
        # /rename <name> names the session on both; bare, claude names it from the
        # conversation and codex opens a dialog for the name.
        (
            "/rename probe-name",
            "claude",
            Command(text="/rename probe-name", verb="rename", args="probe-name"),
        ),
        (
            "/Rename  the probe ",
            "codex",
            Command(text="/rename the probe", verb="rename", args="the probe"),
        ),
        ("/rename", "claude", Command(text="/rename", verb="rename", args="")),
        ("/rename  ", "codex", Refused(reason=_RENAME_BARE)),
        # Claude's run starts Remote Control, named or not; codex has none for a run.
        ("/rc", "claude", Command(text="/rc", verb="rc", args="")),
        (
            "/RC my phone",
            "claude",
            Command(text="/rc my phone", verb="rc", args="my phone"),
        ),
        (
            "/remote-control",
            "claude",
            Command(text="/remote-control", verb="remote-control", args=""),
        ),
        ("/rc", "codex", Refused(reason=_NO_RC)),
        ("/Remote-Control x", "codex", Refused(reason=_NO_RC)),
        ("/rename x", "assistant", Refused(reason=_ASSISTANT)),
        # Any other known verb opens a menu, changes settings or switches session.
        ("/Resume", "claude", _menu("resume")),
        ("/exit", "codex", _menu("exit")),
        # The assistant runs none of them.
        ("/goal x", "assistant", Refused(reason=_ASSISTANT)),
        ("/compact", "assistant", Refused(reason=_ASSISTANT)),
        ("/model opus", "assistant", Refused(reason=_ASSISTANT)),
        ("/effort", "assistant", Refused(reason=_ASSISTANT)),
        ("/goal x\nand more", "assistant", Refused(reason=_ASSISTANT)),
        # A command is one line: a second line would reach the CLI as a paste.
        ("/goal ship it\nthen test it", "claude", Refused(reason=_TOO_LONG)),
        ("/goal ship it\r\nthen test it", "codex", Refused(reason=_TOO_LONG)),
        ("/model\nopus", "claude", Refused(reason=_TOO_LONG)),
        # A line that only starts with an unknown slash word stays a line.
        ("/tmp is full\nand /goal x", "claude", Line()),  # noqa: S108 -- Never opened.
    ],
)
def test_each_row_of_the_table(
    text: str,
    provider: ChatProvider,
    expected: Classified,
) -> None:
    assert classify(text, provider) == expected


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_command_is_at_most_800_characters(provider: ChatProvider) -> None:
    """Claude makes a longer paste a pill it does not run as a command."""
    longest = "/goal " + "x" * (COMMAND_CHARS - len("/goal "))

    fits = classify(longest, provider)
    over = classify(longest + "x", provider)

    assert fits == Command(
        text=longest,
        verb="goal",
        args=longest.removeprefix("/goal "),
    )
    assert over == Refused(reason=_TOO_LONG)


def test_the_known_verbs_are_the_built_ins_of_claude_and_codex() -> None:
    """A verb missing here would reach the CLI as a line; one added runs as a menu."""
    assert sorted(KNOWN_VERBS) == sorted(
        [
            "goal",
            "compact",
            "clear",
            "new",
            "model",
            "effort",
            "permissions",
            "resume",
            "status",
            "config",
            "exit",
            "quit",
            "init",
            "review",
            "plan",
            "fork",
            "rename",
            "rc",
            "remote-control",
            "diff",
            "mcp",
            "agents",
            "cost",
            "help",
            "hooks",
            "skills",
            "usage",
            "export",
            "copy",
            "theme",
            "vim",
            "ide",
            "side",
            "btw",
            "rewind",
            "undo",
            "cd",
            "pwd",
            "stop",
            "ps",
            "login",
            "logout",
            "memory",
            "context",
            "fast",
            "doctor",
            "feedback",
            "statusline",
        ],
    )


@pytest.mark.parametrize(
    "verb",
    sorted(
        KNOWN_VERBS
        - {"goal", "compact", "model", "effort", "rename", "rc", "remote-control"},
    ),
)
def test_every_other_known_verb_is_refused_on_both_clis(verb: str) -> None:
    assert (
        classify(f"/{verb} now", "claude")
        == classify(f"/{verb}", "codex")
        == _menu(
            verb,
        )
    )


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_bare_model_lists_each_model_once_though_two_roles_name_it(
    provider: ChatProvider,
) -> None:
    refused = classify("/model", provider=provider)
    assert isinstance(refused, Refused)
    options = refused.reason.partition("Options: ")[2].rstrip(".").split(", ")

    assert len(options) == len(set(options))


@pytest.mark.parametrize(
    ("provider", "vendor"),
    [("claude", "anthropic"), ("codex", "openai")],
)
def test_the_picker_offers_what_model_takes_with_each_models_efforts(
    provider: ChatProvider,
    vendor: Literal["anthropic", "openai"],
) -> None:
    listed = chat_models()
    offered = listed.claude if provider == "claude" else listed.codex
    table = models(vendor)

    assert [model.id for model in offered if model.default] == [
        model.id for model in offered if table[model.id] == table["default"]
    ]
    assert len({model.id for model in offered}) == len(offered)
    for model in offered:
        row = table[model.id]
        assert classify(f"/model {model.id}", provider) == Settings(
            text=f"/model {model.id}",
            model=model.id,
            effort=None,
        )
        assert model.label == "{} {}".format(*row.model_id.rsplit("-", 1)).capitalize()
        assert model.efforts == [
            level
            for level in ("low", "medium", "high", "xhigh", "max")
            if level in row.thinking.effort
        ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
