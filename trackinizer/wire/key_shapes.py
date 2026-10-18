"""The shapes of credentials, masked wherever text is recorded.

A terminal's recording holds what the shell echoed, and a shell can echo a key it
never should have shown. These patterns mask the credentials whose form is fixed,
whatever their value. They are shapes, not secrets, so the recorder needs no list of
the values a host holds.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from typing import Final

import re


__all__ = ["KEY_SHAPES", "MASK", "KeyMasker", "mask_key_shapes"]

MASK: Final = "[redacted:key]"
"""What replaces a credential."""

KEY_SHAPES: Final = (
    re.compile(r"\btrax_[A-Za-z0-9_-]{20,}"),
    re.compile(r"\benr_[0-9a-f]{32}_[A-Za-z0-9_-]{43}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    ),
)
"""Credential forms: trax keys and machine credentials, Anthropic, OpenAI, GitHub,
AWS and Slack keys, bearer tokens, and the first line of a PEM private key."""

_KEY_BEGIN: Final = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_KEY_END: Final = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")


def mask_key_shapes(text: str) -> str:
    """Return ``text`` with every credential-shaped run replaced by :data:`MASK`."""
    for shape in KEY_SHAPES:
        text = shape.sub(MASK, text)
    return text


class KeyMasker:
    """Mask credential shapes line by line, and a PEM private key across its lines.

    A key's body lines carry no marker of their own, so the masker remembers that a
    ``BEGIN`` line opened one and masks each line until the ``END`` line.
    """

    def __init__(self) -> None:
        self._in_key = False

    def mask(self, line: str) -> str:
        """Return ``line`` masked; the line break is kept.

        Args:
          line: One framed line.

        Returns:
          masked: The line with credential shapes replaced.

        """
        ending = "\n" if line.endswith("\n") else ""
        if self._in_key:
            self._in_key = _KEY_END.search(line) is None
            return MASK + ending
        if _KEY_BEGIN.search(line):
            self._in_key = _KEY_END.search(line) is None
        return mask_key_shapes(line)
