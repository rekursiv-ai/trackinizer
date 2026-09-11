#!/bin/sh
# ruff: noqa: EXE003, D300 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen python3 "$0" "$@"
Runnable wrapper for the trackinizer server; the implementation lives in
``server/server.py``.
'''
# fmt: on

# The exec line above omits ``--no-sync`` on purpose: a remote redeploy pulls
# new source and relies on ``uv run`` syncing the venv on first start.
# house-lint: ignore[cli-shape]

from __future__ import annotations

from trackinizer.server.server import main


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    raise SystemExit(main())
# vim: ft=python
