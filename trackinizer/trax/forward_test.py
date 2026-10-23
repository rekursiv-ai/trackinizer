from __future__ import annotations

import pytest

from trackinizer.trax import forward


@pytest.mark.parametrize("form", ["run", "machine", "job", "agentsession run"])
def test_a_moved_form_exits_2_with_one_line_saying_it_moved(
    capsys: pytest.CaptureFixture[str],
    form: str,
) -> None:
    with pytest.raises(SystemExit) as stopped:
        forward.moved(form)

    assert stopped.value.code == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith(f"trax {form} moved out of trax")
    assert err.count("\n") == 1


def test_the_moved_verbs_are_run_machine_and_job() -> None:
    assert {"run", "machine", "job"} == forward.MOVED_VERBS


def test_the_resumable_clis_are_claude_and_codex() -> None:
    assert {"claude", "codex"} == forward.RUN_CLIS


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
