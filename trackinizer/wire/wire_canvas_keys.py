"""A canvas's key: the short name a web app's addresses give a canvas.

A canvas's id is a random UUID, too long to read in an address. The address
names the canvas by the id's first 12 hex digits instead, as ``docker ps``
names a container by the first 12 of its id: ``/app/c/c103f7c70ef7/ref/Issue/7``.
The server finds the canvas whose id starts with them
(``GET /api/canvases/key/{key}``). Those 48 random bits make two canvases
sharing a key unlikely until millions exist; a key that names two is refused,
and the app then shows the page alone, as for a canvas that is gone.
"""

from __future__ import annotations

from typing import Final

import uuid


__all__ = ["CANVAS_KEY_PATTERN", "canvas_key", "key_bounds"]


_DIGITS: Final = 12
"""How many of the id's 32 hex digits a key keeps."""

CANVAS_KEY_PATTERN: Final = rf"^[0-9a-f]{{{_DIGITS}}}$"
"""A canvas key, in the lower case ids are written in."""


def canvas_key(canvas: uuid.UUID) -> str:
    """Return the key the app's addresses name ``canvas`` by."""
    return canvas.hex[:_DIGITS]


def key_bounds(key: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Return the lowest and the highest id that start with ``key``."""
    rest = 32 - len(key)
    return uuid.UUID(key + "0" * rest), uuid.UUID(key + "f" * rest)
