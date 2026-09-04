"""Why a stored session could not be handed back to a CLI.

Their own module because an exception must be IMPORTED to be caught. The
resume path is deferred -- ``trax.run.session`` costs ~324ms of PTY, tail, and
adapter machinery that no other verb needs -- and deferring the exceptions
along with it made ``except`` receive a proxy rather than a class, which raises
``TypeError: catching classes that do not inherit from BaseException`` at the
moment the handler runs. The failure lands only on the error path, so it
replaces the diagnostic the handler existed to print with a traceback.

Nothing here imports anything, so a caller binds these eagerly and defers only
the work.
"""

from __future__ import annotations


__all__ = [
    "CiphertextDroppedError",
    "LossyConversionError",
    "NotResumableError",
]


class NotResumableError(Exception):
    """The requested target CLI cannot be re-entered with a stored session."""


class CiphertextDroppedError(Exception):
    """A record's sealed reasoning is gone, so the file cannot be replayed.

    Raised rather than written with an empty ``encrypted``: the provider
    validates the field, so a silently-hollowed transcript fails at the CLI
    with no indication that retention was the cause.
    """


class LossyConversionError(Exception):
    """The rewrite would drop records the target format cannot express."""
