"""Shared dependencies for API route modules."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi.encoders import jsonable_encoder

from trackinizer.lib.custom_json import MutableJSON
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.types.inquiries import Inquiry


if TYPE_CHECKING:
    from fastapi import Request


def get_store(request: Request) -> Store:
    """Return the Store held on the FastAPI app state."""
    return cast(Store, request.app.state.store)


def get_inbound(request: Request) -> InboundQueue:
    """Return the inbound-message queue held on the FastAPI app state."""
    return cast(InboundQueue, request.app.state.inbound)


def tag_kind(inquiry: Inquiry | None) -> MutableJSON | None:
    """Serialize one Inquiry, adding an explicit ``kind`` discriminator field."""
    if inquiry is None:
        return None
    payload = cast(MutableJSON, jsonable_encoder(inquiry))
    payload["kind"] = type(inquiry).__name__
    return payload
