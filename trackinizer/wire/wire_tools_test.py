"""The tools wire contract: what a tool connection offer may carry."""

from __future__ import annotations

import uuid

import pydantic
import pytest

from trackinizer.wire.wire_tools import ToolOffer


def test_a_tool_offer_reads_past_a_field_a_newer_server_added() -> None:
    connection_id = uuid.uuid4()
    offer = ToolOffer.model_validate(
        {"connection_id": str(connection_id), "added_later": 1},
    )
    assert offer == ToolOffer(connection_id=connection_id)


def test_a_tool_offer_without_its_connection_is_refused() -> None:
    with pytest.raises(pydantic.ValidationError):
        ToolOffer.model_validate({})


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
