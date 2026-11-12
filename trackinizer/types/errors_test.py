from __future__ import annotations

import pytest

from trackinizer.types.errors import (
    ChatDeletedError,
    ConflictError,
    NotFoundError,
    ValidationError,
)


def test_codes_name_each_error() -> None:
    assert ConflictError.code == "conflict"
    assert NotFoundError.code == "not_found"
    assert ValidationError.code == "validation"
    assert ChatDeletedError.code == "chat_deleted"


def test_not_found_is_caught_as_conflict() -> None:
    with pytest.raises(ConflictError) as raised:
        raise NotFoundError("gone")
    assert raised.value.code == "not_found"


def test_validation_and_chat_deleted_are_not_conflicts() -> None:
    assert not issubclass(ValidationError, ConflictError)
    assert not issubclass(ChatDeletedError, ConflictError)
    assert str(ValidationError("bad")) == "bad"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
