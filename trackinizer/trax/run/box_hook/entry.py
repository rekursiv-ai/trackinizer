"""The hook as Claude runs it: its flags from the process, the client as ``trax`` has it."""

from __future__ import annotations

import sys

from trackinizer.trax.cli import connect
from trackinizer.trax.run.box_hook import hook


def main() -> int:
    """Run the hook on ``sys.argv``; return the process exit code."""
    return hook.main(sys.argv[1:], connect=connect)
