"""Replace secret values in text and JSON before they leave the process.

A :class:`Redactor` is built from the secrets a process was given, as
``{name: value}``, and swaps every occurrence of a value for
``[redacted:<name>]``. It matches the raw value and its JSON-escaped forms, so a
value that a tool printed inside a JSON string is found too.

A value of several lines is also matched line by line, since a pty hands the
sink one line at a time; each line of at least four characters, outside its
indentation, counts as a value of its own.

A value cut off at the end of a line or of the text, as a truncated tool result
is, leaves its first characters behind; those are redacted when at least four
remain. A line ends at a newline or a carriage return, and a ``... (truncated)``
marker that follows the cut counts as the end of the line. A value escaped twice
is not recognised.

The armour of a PEM block (``-----BEGIN KEY-----``) is masked where a whole line of
the value is, but a cut that stops inside it, such as a markdown rule ``-----``,
holds no secret and is kept.

Occurrences that overlap are redacted as one block, so no tail of a longer
secret survives next to the placeholder of a shorter one that sits inside it.

Session records carry bytes, such as an attachment, as ``{"py/b64": ...}``. That
mapping is decoded and its bytes are redacted as text, byte for byte where no
value occurs, so a secret inside an attachment does not leave as base64. Only
UTF-8 text is searched: a value that an attachment holds in another encoding, or
in a compressed or re-encoded form, is not recognised.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final

import base64
import binascii
import json
import re


if TYPE_CHECKING:
    from collections.abc import Iterator

    from trackinizer.lib.codec import PlainTree
    from trackinizer.wire.wire_session_ir import RecordBody


__all__ = ["MIN_SECRET_LENGTH", "Redactor", "redact_body", "redactor_from_environ"]


# What ``LineCapture`` appends to a line it cut at its byte cap.
_TRUNCATED: Final = "... (truncated)"
# A value shorter than this matches ordinary text, and a launch refuses it.
MIN_SECRET_LENGTH: Final = 8
# A line of a multi-line value matches by itself from this length on.
_MIN_LINE_LENGTH: Final = 4
_LINE_END: Final = re.compile(r"[\r\n]")
# A line that opens or closes a PEM block matches this.
_ARMOUR: Final = re.compile(r"-----(?:BEGIN|END) [A-Z0-9 ]+-----")
# The codec tags bytes in a stored record with this key.
_BYTES_TAG: Final = "py/b64"


class Redactor:
    """Replaces secret values with ``[redacted:<name>]``.

    Args:
      secrets: Secret name to its value.

    Raises:
      ValueError: A value is empty, which would match everywhere.

    """

    def __init__(self, secrets: Mapping[str, str]) -> None:
        self._names: dict[str, list[str]] = {}
        for name, value in secrets.items():
            if not value:
                raise ValueError(f"secret {name} has an empty value")
            for part in (value, *_lines_of(value)):
                escaped = (
                    json.dumps(part, ensure_ascii=ascii_only)[1:-1]
                    for ascii_only in (False, True)
                )
                for needle in (part, *escaped):
                    names = self._names.setdefault(needle, [])
                    if name not in names:
                        names.append(name)

    def redact(self, text: str) -> str:
        """Return ``text`` with every secret value replaced.

        Args:
          text: Any text.

        Returns:
          redacted: ``text`` with a placeholder in place of each value.

        """
        found: dict[tuple[int, int], list[str]] = {}
        cuts = _cuts(text)
        for needle, names in self._names.items():
            for start in _starts(text, needle=needle):
                found.setdefault((start, start + len(needle)), []).extend(names)
            for start, end in _cut_prefixes(text, needle=needle, cuts=cuts):
                found.setdefault((start, end), []).extend(names)
        if not found:
            return text
        pieces: list[str] = []
        # Text before ``reach`` is covered by the block being built, whose names
        # are written once the next block, or the end, shows where it stops.
        reach = 0
        block: list[str] = []
        for (start, end), names in sorted(found.items(), key=_longest_first):
            if end <= reach:
                # Inside a longer value already covered: its placeholder names it.
                continue
            if start >= reach:
                pieces.extend(f"[redacted:{name}]" for name in block)
                pieces.append(text[reach:start])
                block = []
            block.extend(name for name in names if name not in block)
            reach = end
        pieces.extend(f"[redacted:{name}]" for name in block)
        pieces.append(text[reach:])
        return "".join(pieces)

    def redact_json(self, value: PlainTree) -> PlainTree:
        """Return ``value`` with every string in it redacted, keys included.

        Args:
          value: Plain data: scalars, lists and mappings, nested. A mapping of
            the one key ``py/b64`` holds bytes, which are redacted as text.

        Returns:
          redacted: A copy of the same shape; a number that is a secret becomes
            its placeholder, other numbers, booleans and nulls are untouched.

        """
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            return self.redact_mapping(value)
        if isinstance(value, Sequence):
            return [self.redact_json(item) for item in value]
        if isinstance(value, int | float) and not isinstance(value, bool):
            text = str(value)
            redacted = self.redact(text)
            return value if redacted == text else redacted
        return value

    def redact_mapping(self, value: Mapping[str, PlainTree]) -> Mapping[str, PlainTree]:
        """Return a JSON object with every key and string value redacted."""
        encoded = value.get(_BYTES_TAG)
        if len(value) == 1 and isinstance(encoded, str):
            return {_BYTES_TAG: self._redact_encoded_bytes(encoded)}
        return {self.redact(key): self.redact_json(item) for key, item in value.items()}

    def _redact_encoded_bytes(self, encoded: str) -> str:
        """Return base64 ``encoded`` with every value in the bytes it holds replaced."""
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return self.redact(encoded)
        # Undecodable bytes survive as lone surrogates, so a binary file keeps every
        # byte that is not part of a value.
        text = data.decode(errors="surrogateescape")
        redacted = self.redact(text)
        if redacted == text:
            # A plain JSON field is not escaped, so this mapping may be user data
            # whose base64-valid string is itself a secret.
            return self.redact(encoded)
        return base64.b64encode(redacted.encode(errors="surrogateescape")).decode()


# ``TRAX_REDACT_NAMES`` lists the delivered secrets' names. A name the environment lacks
# stops the run: it would otherwise upload a transcript that this process cannot vouch
# for.
def redactor_from_environ(environ: Mapping[str, str]) -> Redactor | None:
    """Return the redactor for the secrets named in ``TRAX_REDACT_NAMES``, if any.

    Args:
      environ: The process environment.

    Returns:
      redactor: Masks the value of each named variable; ``None`` when no name is
        listed.

    Raises:
      SystemExit: A named variable is absent from ``environ`` or its value is
        shorter than eight characters; the message names it and never a value.

    """
    stripped = (
        name.strip() for name in environ.get("TRAX_REDACT_NAMES", "").split(",")
    )
    names = [name for name in stripped if name]
    missing = [name for name in names if name not in environ]
    if missing:
        raise SystemExit(
            f"TRAX_REDACT_NAMES names {', '.join(missing)}, "
            "which the environment lacks",
        )
    short = [name for name in names if len(environ[name]) < MIN_SECRET_LENGTH]
    if short:
        raise SystemExit(
            f"TRAX_REDACT_NAMES names {', '.join(short)}, whose values are shorter "
            f"than {MIN_SECRET_LENGTH} characters",
        )
    return Redactor({name: environ[name] for name in names}) if names else None


def redact_body(body: RecordBody, *, redactor: Redactor | None) -> RecordBody:
    """Return ``body`` with every secret value in its payload and text replaced.

    The ciphertext of a reasoning block is encrypted by the provider and carried
    as it is; every other field a secret could be in is redacted.

    Args:
      body: The wire body of one record.
      redactor: Masks secret values in the body; ``None`` keeps them.

    Returns:
      redacted: The body, a copy when a redactor is given.

    """
    if redactor is None:
        return body
    return body.model_copy(
        update={
            "payload": redactor.redact_mapping(body.payload),
            "text": redactor.redact(body.text),
        },
    )


def _lines_of(value: str) -> list[str]:
    """Return the lines of a multi-line ``value`` that are long enough to match."""
    lines = value.splitlines()
    if len(lines) < 2:
        return []
    stripped = (line.strip() for line in lines)
    return [line for line in stripped if len(line) >= _MIN_LINE_LENGTH]


def _cuts(text: str) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` of each line of ``text`` that a cut may end."""
    cuts: list[tuple[int, int]] = []
    start = 0
    for match in _LINE_END.finditer(text):
        cuts.extend(_line_cuts(text, start=start, end=match.start()))
        start = match.end()
    cuts.extend(_line_cuts(text, start=start, end=len(text)))
    return cuts


def _line_cuts(text: str, *, start: int, end: int) -> Iterator[tuple[int, int]]:
    yield start, end
    # ``LineCapture`` appends the marker after cutting, so the cut sits before it.
    if text.endswith(_TRUNCATED, start, end):
        yield start, end - len(_TRUNCATED)


def _starts(text: str, *, needle: str) -> Iterator[int]:
    """Yield every index where ``needle`` occurs in ``text``, overlaps included."""
    index = text.find(needle)
    while index >= 0:
        yield index
        index = text.find(needle, index + 1)


def _cut_prefixes(
    text: str,
    *,
    needle: str,
    cuts: Sequence[tuple[int, int]],
    min_length: int = 4,
) -> Iterator[tuple[int, int]]:
    """Yield the span of each proper prefix of ``needle`` that ends a line."""
    head = needle[:min_length]
    armour = _ARMOUR.match(needle)
    # A prefix that stops inside a PEM block's opening line holds no secret.
    framing = armour.end() if armour else 0
    for line_start, cut in cuts:
        # A proper prefix is shorter than the needle, so it starts within
        # ``len(needle) - 1`` characters of the cut; searching only there keeps the
        # work per line bounded however often ``head`` repeats.
        start = text.find(head, max(line_start, cut - len(needle) + 1), cut)
        while start >= 0:
            if cut - start > framing and needle.startswith(text[start:cut]):
                yield start, cut
            start = text.find(head, start + 1, cut)


def _longest_first(item: tuple[tuple[int, int], list[str]]) -> tuple[int, int]:
    """Order spans by start, and the longer of two spans that start together first."""
    (start, end), _ = item
    return start, -end
