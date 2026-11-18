#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Mark a trax run claude ready, bridge its prompts to Chat, and tell it of boxes.

Executable entry point: dispatches to the sibling ``entry`` module's ``main``.
Claude Code runs it once its session starts, around each tool call and before
each prompt; run by hand with ``python -m`` on this package.
'''
# fmt: on

from __future__ import annotations

from trackinizer.trax.run.box_hook.entry import main


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
