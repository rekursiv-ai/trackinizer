"""Tests for live graph replay diagnostics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, override

import logging
import uuid


if TYPE_CHECKING:
    import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.tools import replay_live_graph
from trackinizer.types.inquiries import Inquiry


class _FailingInsertClient(Client):
    """Client seam for replay insert diagnostics."""

    def __init__(self) -> None:
        self.base_url = "http://target.test"
        self.submitted: list[tuple[str, object]] = []

    @override
    def submit_batch(
        self,
        items: Sequence[tuple[Inquiry.InquiryKind, Mapping[str, object]]],
        *,
        edges: Sequence[Mapping[str, object]] = (),
    ) -> list[uuid.UUID]:
        del items, edges
        raise ClientError("POST /api/inquiries/batch failed: [Errno 61]")

    @override
    def submit(
        self, kind: Inquiry.InquiryKind, body: Mapping[str, object]
    ) -> uuid.UUID:
        self.submitted.append((kind, body))
        raise ClientError(f"POST /api/inquiries/{kind.lower()} failed: [Errno 61]")


class _MixedInsertClient(_FailingInsertClient):
    @override
    def submit(
        self, kind: Inquiry.InquiryKind, body: Mapping[str, object]
    ) -> uuid.UUID:
        self.submitted.append((kind, body))
        if kind == "Issue":
            return uuid.UUID("11111111-1111-1111-1111-111111111111")
        raise ClientError(f"POST /api/inquiries/{kind.lower()} failed: [Errno 61]")


def _detail(
    *,
    kind: str,
    node_id: str,
    seq: int,
    title: str,
) -> dict[str, Any]:
    return {
        "self": {
            "id": node_id,
            "kind": kind,
            "seq": seq,
            "title": title,
        },
        "edges": {},
        "backlinks": {},
    }


def test_insert_chunk_logs_batch_and_row_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fallback logs enough context to identify the poisoned replay chunk."""
    chunk = [
        _detail(
            kind="WebSearch",
            node_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            seq=12,
            title="fast api search",
        ),
        _detail(
            kind="Paper",
            node_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            seq=7,
            title="attention paper",
        ),
    ]
    caplog.set_level(logging.WARNING, logger=replay_live_graph.__name__)

    replay_live_graph._insert_chunk(_FailingInsertClient(), chunk, {}, set())

    log_text = caplog.text
    assert "batch insert failed" in log_text
    assert "target=http://target.test" in log_text
    assert "WebSearch#12" in log_text
    assert "Paper#7" in log_text
    assert "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in log_text
    assert "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in log_text
    assert "fast api search" in log_text
    assert "attention paper" in log_text


def test_insert_chunk_keeps_successes_after_diagnostic_fallback() -> None:
    """Diagnostics do not change fallback semantics for rows that still insert."""
    target = _MixedInsertClient()
    id_map: dict[str, str] = {}

    replay_live_graph._insert_chunk(
        target,
        [
            _detail(
                kind="Issue",
                node_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                seq=1,
                title="ok",
            ),
            _detail(
                kind="Paper",
                node_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                seq=2,
                title="bad",
            ),
        ],
        id_map,
        set(),
    )

    assert id_map == {
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa": "11111111-1111-1111-1111-111111111111"
    }


def test_detail_order_key_uses_source_created_then_id() -> None:
    """Seed replay writes the crawled component in deterministic authoring order."""
    old_high_id = _detail(
        kind="Issue",
        node_id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        seq=1,
        title="old high id",
    )
    old_low_id = _detail(
        kind="Issue",
        node_id="00000000-0000-0000-0000-000000000000",
        seq=2,
        title="old low id",
    )
    new = _detail(
        kind="Issue",
        node_id="11111111-1111-1111-1111-111111111111",
        seq=3,
        title="new",
    )
    old_high_id["self"]["created"] = "2026-01-01T00:00:00+00:00"
    old_low_id["self"]["created"] = "2026-01-01T00:00:00+00:00"
    new["self"]["created"] = "2026-01-02T00:00:00+00:00"

    ordered = sorted(
        [new, old_high_id, old_low_id], key=replay_live_graph._detail_order_key
    )

    assert [d["self"]["id"] for d in ordered] == [
        "00000000-0000-0000-0000-000000000000",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "11111111-1111-1111-1111-111111111111",
    ]
