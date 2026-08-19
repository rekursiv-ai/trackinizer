"""Entry dispatch: serve, delegate to a daemon, or run in-process.

Imported by ``trax/__main__.py`` INSTEAD of ``trax.cli``, because the choice
must be made before anything expensive loads: importing ``cli`` costs the
~145ms the daemon exists to avoid, so a delegating invocation must never
touch it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import os
import sys

from trackinizer.trax.daemon.client import (
    SERVE_FLAG,
    delegate,
    should_delegate,
)
from trackinizer.trax.daemon.protocol import source_version


def main(argv: Sequence[str] | None = None) -> int:
    """Run one ``trax`` invocation, using the daemon when it can.

    Returns:
      exit_code: The process exit status.

    """
    args = list(sys.argv[1:] if argv is None else argv)
    if SERVE_FLAG in args:
        # Deferred: importing the server pulls in the whole CLI, which is
        # exactly the ~145ms a delegating invocation must not pay. Hoisting
        # either of these imports to module scope defeats the daemon.
        from trackinizer.trax.daemon.server import serve  # noqa: PLC0415

        serve()
        return 0
    if _daemon_enabled() and should_delegate(args):
        response = delegate(args, source_version=_version())
        if response is not None:
            sys.stdout.write(response.stdout)
            sys.stderr.write(response.stderr)
            return response.exit_code
    # No daemon, or a verb that must run here: the original path, unchanged.
    from trackinizer.trax.cli import main as cli_main  # noqa: PLC0415

    cli_main(args)
    return 0


def _daemon_enabled() -> bool:
    """Whether delegation is permitted.

    ``TRAX_NO_DAEMON=1`` forces in-process execution -- the escape hatch for
    debugging a suspected daemon fault without editing code or killing a
    running one.
    """
    return os.environ.get("TRAX_NO_DAEMON", "") not in ("1", "true", "yes")


def _version() -> str:
    """Fingerprint of the CLI source this process was launched from."""
    return source_version(Path(__file__).resolve().parents[1])
