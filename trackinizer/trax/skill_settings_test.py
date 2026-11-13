"""The trax skill names every setting a canvas's agent may change for its owner."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from trackinizer.server.preferences import ASSISTANT_KEYS


_CWD: Final[Path] = Path(__file__).resolve().parent


def test_the_trax_skill_names_every_setting_a_canvas_agent_may_change() -> None:
    skill = (_CWD / "docs" / "skills" / "trax" / "SKILL.md").read_text()
    assert [key for key in sorted(ASSISTANT_KEYS) if f"`{key}`" not in skill] == []


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
