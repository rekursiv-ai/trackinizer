"""The code store's wire bodies: owners compare lowercase, hashes are lowercase hex."""

from __future__ import annotations

from typing import Final

import uuid

import pydantic
import pytest

from trackinizer.wire.wire_code import (
    CodeBlobsPut,
    CodeMissingRequest,
    CodeSnapshotPut,
)


_SHA: Final = "ab" * 32


def test_an_owner_is_kept_lowercase() -> None:
    body = CodeMissingRequest(owner="Ada@Example.COM", sha256=[_SHA])

    assert body.owner == "ada@example.com"


@pytest.mark.parametrize("sha", ["AB" * 32, "ab" * 31, "zz" * 32, f"{_SHA}\n"])
def test_a_hash_is_64_lowercase_hex_digits(sha: str) -> None:
    with pytest.raises(pydantic.ValidationError):
        CodeBlobsPut.model_validate(
            {"owner": "a@x.y", "blobs": [{"sha256": sha, "text": ""}]},
        )


def test_a_snapshot_names_its_privacy_and_parent_and_no_more() -> None:
    files = {"a.py": _SHA}
    parent = uuid.uuid4()

    put = CodeSnapshotPut.model_validate(
        {"owner": "a@x.y", "private": False, "parent_record_id": None, "files": files},
    )

    assert put.partial == ""
    for bad in (
        {"owner": "a@x.y", "parent_record_id": None, "files": files},
        {"owner": "a@x.y", "private": False, "files": files},
        {"owner": "", "private": False, "parent_record_id": None, "files": files},
        {
            "owner": "a@x.y",
            "private": False,
            "parent_record_id": str(parent),
            "files": files,
            "account": "a@x.y",
        },
    ):
        with pytest.raises(pydantic.ValidationError):
            CodeSnapshotPut.model_validate(bad)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
