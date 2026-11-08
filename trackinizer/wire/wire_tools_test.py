"""The tools wire contract: what a pipe offer may carry."""

from __future__ import annotations

import uuid

import pydantic
import pytest

from trackinizer.wire.wire_tools import ToolOffer


def test_a_tool_offer_reads_past_a_field_a_newer_server_added() -> None:
    pipe_id = uuid.uuid4()
    offer = ToolOffer.model_validate({"pipe_id": str(pipe_id), "added_later": 1})
    assert offer == ToolOffer(pipe_id=pipe_id)


def test_a_tool_offer_without_its_pipe_is_refused() -> None:
    with pytest.raises(pydantic.ValidationError):
        ToolOffer.model_validate({})


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
