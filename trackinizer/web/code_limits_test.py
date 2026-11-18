"""Keep the browser's copy of the code store's bounds equal to the wire contract.

The uploader must know these before it sends: it pages the missing-blobs question and packs
blob requests so none is ever exceeded, because a request over any bound is a 422 that loses
the whole import. They are module constants in :mod:`trackinizer.wire.wire_code`, not part of
the OpenAPI schema, so codegen cannot reach them and ``src/code/limits.ts`` holds them by
hand. This test is what keeps that copy honest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import re

from trackinizer.wire.wire_code import (
    MAX_BLOB_BYTES,
    MAX_BLOBS_PER_REQUEST,
    MAX_MISSING_HASHES,
    MAX_PATH_CHARS,
    MAX_REQUEST_TEXT_BYTES,
    MAX_SNAPSHOT_FILES,
)


_LIMITS: Final = Path(__file__).resolve().parent / "src/code/limits.ts"


def _declared() -> dict[str, int]:
    """Read every ``export const NAME = <number>;`` from the browser's limits module.

    Returns:
      limits: Each exported constant's name and its value, underscores in numeric
        separators removed, and an expression such as ``8 * MAX_BLOB_BYTES`` evaluated
        against the constants already read.

    """
    found: dict[str, int] = {}
    for name, value in re.findall(
        r"^export const (\w+) = ([^;]+);$",
        _LIMITS.read_text(),
        re.MULTILINE,
    ):
        text = value.strip()
        # Underscores are stripped from the NUMBER alone; stripping the whole expression
        # would turn `MAX_BLOB_BYTES` into `MAXBLOBBYTES` and lose the reference.
        number = re.fullmatch(r"[\d_]+", text)
        if number:
            found[name] = int(text.replace("_", ""))
            continue
        # The one derived bound: `8 * MAX_BLOB_BYTES`, kept as an expression there so
        # the relation between the two is visible rather than a second magic number.
        product = re.fullmatch(r"([\d_]+) \* (\w+)", text)
        assert product, f"{name} is neither a number nor a product of one: {value!r}"
        found[name] = int(product[1].replace("_", "")) * found[product[2]]
    return found


def test_the_browsers_code_limits_match_the_wire_contract() -> None:
    """Every bound the uploader splits its requests by is the one the server enforces."""
    assert _declared() == {
        "MAX_BLOB_BYTES": MAX_BLOB_BYTES,
        "MAX_REQUEST_TEXT_BYTES": MAX_REQUEST_TEXT_BYTES,
        "MAX_BLOBS_PER_REQUEST": MAX_BLOBS_PER_REQUEST,
        "MAX_MISSING_HASHES": MAX_MISSING_HASHES,
        "MAX_SNAPSHOT_FILES": MAX_SNAPSHOT_FILES,
        "MAX_PATH_CHARS": MAX_PATH_CHARS,
    }
