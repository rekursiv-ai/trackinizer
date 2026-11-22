"""Keep a record the server refuses from holding back the rest of a transcript.

The server refuses a record it can never store -- a NUL byte in a tool's output,
which Postgres text cannot hold -- and refuses every resend of it the same way.
Retried like an outage, the batch holding it is sent forever, and every record
after it waits behind it. :func:`send_isolating` finds the refused records
instead, sends a stand-in at each one's position, and sends the rest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from trackinizer.client.errors import ClientError
from trackinizer.sagent.session.types import UncategorizedRecord
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_session_ir import RecordBody


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


__all__ = ["REFUSED_KIND", "is_refusal", "send_isolating", "stand_in"]


REFUSED_KIND: Final = "RefusedRecord"
"""The ``kind`` a stand-in's payload names, so a reader can tell it from a record."""


def is_refusal(error: BaseException) -> bool:
    """Say whether the server refused what a request carried, so a resend fails too.

    400 and 422 name the request's content; 413 its size, which a smaller batch
    escapes. A 408, 409 or 429, an outage or a 5xx says nothing about the records.

    Args:
      error: What a send raised.

    Returns:
      refused: True for a client error with one of those statuses.

    """
    return isinstance(error, ClientError) and error.status_code in {400, 413, 422}


def send_isolating(
    send: Callable[[list[RecordBody]], object],
    *,
    records: Sequence[RecordBody],
) -> list[RecordBody]:
    """Send ``records``, standing in for each one the server refuses.

    A refused batch is halved until each refused record is alone. That record is
    sent as its :func:`stand_in`, every other record as it is. A failure that is
    not a refusal, an outage included, propagates for the caller to retry.

    Args:
      send: Sends one batch, in position order.
      records: The batch.

    Returns:
      refused: The records the server refused, in position order.

    """
    try:
        _ = send(list(records))
    except ClientError as error:
        if not is_refusal(error):
            raise
        if len(records) == 1:
            _ = send([stand_in(records[0], reason=str(error))])
            return [records[0]]
        half = len(records) // 2
        return [
            *send_isolating(send, records=records[:half]),
            *send_isolating(send, records=records[half:]),
        ]
    return []


# The stand-in keeps the position, so the part's positions stay contiguous: a reader
# replaying the part meets a record that says what is missing and why, not a gap that
# silently shortens the conversation. The refused content is left out, since it is
# what the server could not store.
def stand_in(body: RecordBody, *, reason: str) -> RecordBody:
    """Return the record that holds ``body``'s position and says why it is missing.

    Args:
      body: The record the server refused.
      reason: What the server answered, kept in the stand-in.

    Returns:
      stand_in: An :class:`UncategorizedRecord` of kind :data:`REFUSED_KIND` at
        ``body``'s position, with its context, model and time.

    """
    record = UncategorizedRecord(
        context_id=body.context_id,
        kind=REFUSED_KIND,
        payload={"refused_kind": body.kind, "reason": reason},
    )
    row = SessionRecordRow.of(
        session_id=UUID(int=0),
        part=0,
        idx=body.idx,
        record=record,
        model=body.model,
    )
    return RecordBody.of(row).model_copy(update={"timestamp": body.timestamp})
