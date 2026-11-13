"""Upload a session file's records as one part's records, in batches.

Each record a session reader read, from any CLI's file, becomes its wire body at its
position in the part, masked, then bounded by the function the caller passes, since
the bound lives with the caller's capture rules. A process that journals with sagent
reads its journal's lines back with Trackinizer's reader first: the process holds the
real ``sagent`` package's classes and this client its own copy of them.

::

    records ──record_bodies──▶ RecordBody ──send_batches──▶ POST .../records
                                              (each batch through append_batch)

Typical usage example:

  pending = record_bodies(normalize(stream), start=n, redactor=redactor,
                          bounded=bounded)
  send = partial(append_batch, client, session=session_id, name=part, ir_id=ir_id,
                 encoding={}, format="sagent")
  refused = send_batches(pending, send=send)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from treekle import immutable

from trackinizer.runs.redact import redact_body
from trackinizer.runs.refusal import send_isolating
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_session_ir import (
    MAX_RECORD_BATCH,
    ManifestBody,
    RecordBody,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from trackinizer.runs.redact import Redactor
    from trackinizer.types.streams import TraxRecord


__all__ = [
    "RecordAppender",
    "append_batch",
    "record_bodies",
    "record_body",
    "send_batches",
]


class RecordAppender(Protocol):
    """The ``Client`` call that stores one part's records."""

    def append_records(
        self,
        session_id: UUID,
        *,
        name: str,
        manifest: ManifestBody,
        records: Sequence[RecordBody],
    ) -> object:
        """Append one part's records, idempotent on their positions."""
        ...


def record_bodies(
    records: Iterable[TraxRecord],
    *,
    start: int,
    redactor: Redactor | None,
    bounded: Callable[[RecordBody], RecordBody],
) -> list[RecordBody]:
    """Return each record as its body, the first at position ``start`` of its part.

    Args:
      records: Consecutive records of one file, as its format's reader reads them.
      start: The position of the first in its file's records.
      redactor: Masks secret values in each body; ``None`` keeps them.
      bounded: Cuts a masked body to what an upload carries.

    Returns:
      bodies: One body a record, in order.

    """
    return [
        bounded(record_body(record, idx=idx, redactor=redactor))
        for idx, record in enumerate(records, start=start)
    ]


def record_body(
    record: TraxRecord,
    *,
    idx: int,
    redactor: Redactor | None = None,
) -> RecordBody:
    """Return the wire body for ``record`` at position ``idx`` of its part.

    The ciphertext of a reasoning block is encrypted by the provider and carried
    as it is; every other field a secret could be in is masked.

    Args:
      record: The record to send.
      idx: Its position in the part.
      redactor: Masks secret values in the body; ``None`` keeps them.

    Returns:
      body: The record as the wire carries it.

    """
    # ``session_id`` and ``part`` are the server's to assign; the row needs some.
    row = SessionRecordRow.of(session_id=UUID(int=0), part=0, idx=idx, record=record)
    return redact_body(RecordBody.of(row), redactor=redactor)


# ``batch`` second and positional, so ``partial(append_batch, client, session=...)`` is
# the ``send`` :func:`send_batches` calls with each batch.
def append_batch(
    client: RecordAppender,
    batch: Sequence[RecordBody],
    *,
    session: UUID,
    name: str,
    ir_id: UUID,
    encoding: dict[str, object],
    format: str,
) -> None:
    """Append one batch of a part's records; the caller keeps it within the cap.

    Args:
      client: Where the records go.
      batch: At most ``MAX_RECORD_BATCH`` records; the manifest counts up to the
        last one's position.
      session: The session that owns the part.
      name: The part's file name; the server gives each name its own part.
      ir_id: The id the writer minted for that file.
      encoding: The reader's encoding for the prefix sent so far.
      format: The reader the part's file is read with, such as ``sagent``.

    """
    _ = client.append_records(
        session,
        name=name,
        manifest=ManifestBody(
            name=name,
            metadata=immutable(encoding),
            ir_id=ir_id,
            format=format,
            records=batch[-1].idx + 1,
        ),
        records=batch,
    )


# A failure leaves the batch it failed on, and every later one, pending. A record the
# server refuses is no failure: it goes up as a stand-in, since resent it would be
# refused again and hold back every record after it.
def send_batches(
    pending: list[RecordBody],
    *,
    send: Callable[[list[RecordBody]], object],
    batch_size: int = MAX_RECORD_BATCH,
) -> list[RecordBody]:
    """Send ``pending`` in batches, taking each off ``pending`` once it is sent.

    Args:
      pending: The records to send, in position order; what is sent leaves it.
      send: Sends one batch, such as :func:`append_batch` with its part named.
      batch_size: Most records one batch carries; the server takes at most
        ``MAX_RECORD_BATCH``.

    Returns:
      refused: The records the server refused, each sent as its stand-in.

    Raises:
      ClientError: A send failed for a reason other than the records, such as an
        outage; what was not sent stays in ``pending``.

    """
    refused: list[RecordBody] = []
    while pending:
        batch = pending[:batch_size]
        refused += send_isolating(send, records=batch)
        del pending[: len(batch)]
    return refused
