"""Tests for ``ClientError``, the failure a trackinizer client operation raises."""

from __future__ import annotations

import pytest

from trackinizer.client.errors import ClientError


def test_an_http_failure_carries_its_status_and_code() -> None:
    with pytest.raises(ClientError, match=r"^conflict on save$") as caught:
        raise ClientError("conflict on save", status_code=409, code="stale")

    assert (caught.value.status_code, caught.value.code) == (409, "stale")


def test_a_cli_failure_has_no_status_and_an_empty_code() -> None:
    error = ClientError("git not found")

    assert str(error) == "git not found"
    assert (error.status_code, error.code) == (None, "")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
