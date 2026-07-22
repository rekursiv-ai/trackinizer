"""Ways to address an inquiry row.

A caller names a row either by its per-kind short id (``Issue#7``, a
:class:`SeqRef`) or by its canonical UUID (a :class:`UuidRef`). Both the
HTTP client and the CLI share these forms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import override
from uuid import UUID

from trackinizer.types.inquiries import Inquiry


@dataclass(frozen=True, kw_only=True, slots=True)
class SeqRef:
    """A per-kind short reference, written ``kind#seq``."""

    kind: Inquiry.InquiryKind
    seq: int

    @override
    def __str__(self) -> str:
        return f"{self.kind}#{self.seq}"


@dataclass(frozen=True, kw_only=True, slots=True)
class UuidRef:
    """A canonical UUID reference; the kind is resolved by server lookup.

    Set ``expected_kind`` to have the lookup verify the row's kind and
    reject a mismatch.
    """

    uuid: UUID
    expected_kind: Inquiry.InquiryKind | None = None

    @override
    def __str__(self) -> str:
        return str(self.uuid)


type Ref = SeqRef | UuidRef
