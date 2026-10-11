"""Bounding one captured record: what is cut, what is kept, and what says it was.

Each test builds a wire body the way capture does -- through
:class:`SessionRecordRow` -- and reads the result back the way the server's
readers do, so a cut that only looked right in memory cannot pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

import json
import re

from treekle import immutable

import httpx2
import pytest

from trackinizer.lib.agent.types.sessions import (
    Attachment,
    ContextClear,
    IncompleteRecord,
    ShellCommandResult,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
)
from trackinizer.lib.agent.types.stored import to_stored
from trackinizer.trax.run.truncate import (
    MAX_RECORD_BYTES,
    MAX_TOOL_RESULT_BYTES,
    NOTE,
    bounded,
    encoded_size,
    was_truncated,
    without_note,
)
from trackinizer.types.session_records import SessionRecordRow, search_text
from trackinizer.types.streams import Stdout
from trackinizer.wire.wire_session_ir import RecordBody


if TYPE_CHECKING:
    from trackinizer.types.streams import TraxRecord


# One cut string: what it kept, how much it says went, and what it kept after.
_CUT: Final = re.compile(
    r"(?P<head>.*)\n\[Trackinizer capture truncated: kept beginning/end; "
    r"omitted (?P<omitted>\d+) UTF-8 bytes\]\n(?P<tail>.*)",
    re.DOTALL,
)


_LOG: Final = (
    '\x1b[2m2026-09-17T22:45:01Z\x1b[0m \x1b[32mINFO\x1b[0m {"step": 120, '
    '"loss": 0.2531} ━━━━ 50%\n'
)
"""One line of a worker log: ANSI escapes, quotes and box drawing all widen on
the wire, which is how ~1 MB of output became 11.39 MB."""


def _shell(output: str) -> ShellCommandResult:
    """Return a shell result holding ``output`` twice, as codex stores one."""
    return ShellCommandResult(
        call_id="c",
        command=("bash", "-lc", "find . -exec sed -n '1,180p' {} \\;"),
        stdout=output,
        exit_code=0,
        extra=immutable({"aggregated_output": output}),
    )


class TestARecordThatFitsIsUntouched:
    """Small results stay byte-identical: nothing is re-encoded on the way."""

    def test_a_tool_result_under_its_budget_sends_the_same_bytes(self) -> None:
        body = _body(ShellCommandResult(call_id="c", stdout="é\x1b[0m" * 5_000))

        assert _wire(bounded(body)) == _wire(body)

    def test_a_message_between_the_two_budgets_is_not_cut(self) -> None:
        """Only a tool result gets the tight budget; prose gets the backstop."""
        body = _body(UserMessage(content="x" * MAX_TOOL_RESULT_BYTES))
        assert MAX_TOOL_RESULT_BYTES < encoded_size(body) < MAX_RECORD_BYTES

        assert _wire(bounded(body)) == _wire(body)


class TestAnOversizedToolResult:
    """The failing shape: one output, stored again in provider metadata."""

    def test_it_fits_its_budget(self) -> None:
        body = _body(_shell(_LOG * 4_000))
        assert encoded_size(body) > 2 * MAX_TOOL_RESULT_BYTES

        assert encoded_size(bounded(body)) <= MAX_TOOL_RESULT_BYTES

    def test_each_copy_keeps_its_beginning_and_end_around_the_marker(self) -> None:
        output = _LOG * 4_000
        record = _read(bounded(_body(_shell(output))))

        assert isinstance(record, ShellCommandResult)
        copy = record.extra["aggregated_output"]
        assert isinstance(copy, str)
        for kept in (record.stdout, copy):
            _assert_cut_from(output, kept)

    def test_the_note_counts_every_byte_left_out(self) -> None:
        output = _LOG * 4_000
        record = _read(bounded(_body(_shell(output))))

        assert isinstance(record, ShellCommandResult)
        copy = record.extra["aggregated_output"]
        assert isinstance(copy, str)
        stated = sum(_omitted(kept) for kept in (record.stdout, copy))
        assert record.extra[NOTE] == immutable({"omitted_bytes": stated})

    def test_search_text_is_derived_from_what_was_kept(self) -> None:
        cut = bounded(_body(_shell(_LOG * 4_000)))

        assert "Trackinizer capture truncated" in cut.text
        assert cut.text == search_text(_read(cut))

    def test_the_envelope_is_kept(self) -> None:
        body = _body(_shell(_LOG * 4_000))

        cut = bounded(body)

        assert cut.payload != body.payload
        assert cut.model_dump(exclude={"payload", "text"}) == body.model_dump(
            exclude={"payload", "text"},
        )


class TestTheCutKeepsTextValid:
    """Valid UTF-8 and valid JSON, whatever sits where the cut lands."""

    @pytest.mark.parametrize(
        "unit",
        [
            pytest.param("aé€😀", id="one-to-four-byte-characters"),
            pytest.param('\x00\x1b[31m\r\n\t\x7f"\\', id="control-characters"),
            pytest.param("\U0001f600", id="four-byte-only"),
        ],
    )
    def test_every_cut_reads_back_as_a_prefix_and_suffix(self, unit: str) -> None:
        output = unit * (MAX_TOOL_RESULT_BYTES // len(unit.encode()) * 2 + 1)
        cut = bounded(_body(_shell(output)))

        reread = RecordBody.model_validate_json(_wire(cut))
        record = _read(reread)

        assert isinstance(record, ShellCommandResult)
        _assert_cut_from(output, record.stdout)

    @pytest.mark.parametrize("offset", range(4))
    def test_no_cut_splits_a_character(self, offset: int) -> None:
        """Every alignment of a four-byte character against the cut."""
        output = "a" * offset + "\U0001f600" * (MAX_TOOL_RESULT_BYTES // 2)

        record = _read(bounded(_body(_shell(output))))

        assert isinstance(record, ShellCommandResult)
        _assert_cut_from(output, record.stdout)


class TestWhatIsNeverCut:
    def test_bytes_survive_whole(self) -> None:
        """An attachment is base64 of bytes: a cut would corrupt both."""
        data = bytes(range(256)) * 1_000
        record = UncategorizedToolResult(
            call_id="c",
            content="x" * MAX_TOOL_RESULT_BYTES,
            attachments=(Attachment(mime_descriptor="image/png", data=data),),
        )

        cut = _read(bounded(_body(record)))

        assert isinstance(cut, UncategorizedToolResult)
        assert cut.attachments[0].data == data
        assert was_truncated(cut)


class TestEveryKindHasABackstop:
    """The incident's class is one record over the limit, not one kind."""

    def test_a_message_over_the_backstop_fits_it(self) -> None:
        body = _body(UserMessage(content=_LOG * 10_000))

        cut = bounded(body)

        assert encoded_size(cut) <= MAX_RECORD_BYTES
        record = _read(cut)
        assert isinstance(record, UserMessage)
        assert record.content is not None
        _assert_cut_from(_LOG * 10_000, record.content)

    def test_a_nested_history_is_reached(self) -> None:
        body = _body(ContextClear(history=(UserMessage(content=_LOG * 10_000),)))

        assert encoded_size(bounded(body)) <= MAX_RECORD_BYTES


class TestTheLossyNote:
    """A cut record says so wherever the record can hold it."""

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param(_shell(_LOG * 4_000), id="extra"),
            pytest.param(
                UncategorizedRecord(kind="k", payload=immutable({"x": _LOG * 12_000})),
                id="payload",
            ),
            pytest.param(IncompleteRecord(text=_LOG * 10_000), id="marker-only"),
            pytest.param(Stdout(text=_LOG * 10_000), id="stream"),
        ],
    )
    def test_a_cut_record_reports_it(self, record: TraxRecord) -> None:
        assert not was_truncated(record)

        assert was_truncated(_read(bounded(_body(record))))

    def test_a_record_that_fits_reports_nothing(self) -> None:
        assert not was_truncated(_read(bounded(_body(_shell("short")))))

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param(_shell(_LOG * 4_000), id="extra"),
            pytest.param(
                UncategorizedRecord(kind="k", payload=immutable({"x": _LOG * 12_000})),
                id="payload",
            ),
        ],
    )
    def test_the_note_comes_off_and_the_marker_stays(self, record: TraxRecord) -> None:
        """A writer replays an uncategorized payload verbatim, note and all."""
        cut = _read(bounded(_body(record)))

        stored = json.dumps(to_stored(without_note(cut)))

        assert NOTE not in stored
        assert "Trackinizer capture truncated" in stored


def test_the_size_is_what_the_http_client_sends() -> None:
    """Measured as ``httpx2`` encodes ``json=``: compact, non-ASCII kept raw."""
    body = _body(
        ShellCommandResult(
            call_id="c",
            stdout='é€😀\x00\x1b[31m"\\\n',
            extra=immutable({"duration": 0.1, "nested": [{"a": None, "b": True}]}),
        ),
    )

    sent = httpx2.Request("POST", "http://server/", json=body.model_dump(mode="json"))

    assert encoded_size(body) == len(sent.content)


def _body(record: TraxRecord) -> RecordBody:
    """Return the wire body capture builds for ``record``, before any cut."""
    return RecordBody.of(
        SessionRecordRow.of(session_id=UUID(int=0), part=0, idx=3, record=record),
    )


def _read(body: RecordBody) -> TraxRecord:
    """Return the record a server reader decodes from ``body``."""
    return body.row(UUID(int=0), 0).record()


def _wire(body: RecordBody) -> bytes:
    """Return ``body`` as the HTTP client encodes it."""
    return httpx2.Request(
        "POST",
        "http://server/",
        json=body.model_dump(mode="json"),
    ).content


def _assert_cut_from(original: str, kept: str) -> None:
    """Fail unless ``kept`` is ``original``'s start and end around a true count."""
    match = _CUT.fullmatch(kept)
    assert match is not None, "no marker between a beginning and an end"
    head, tail = match["head"], match["tail"]
    assert head
    assert tail
    assert original.startswith(head)
    assert original.endswith(tail)
    omitted = len(original.encode()) - len(head.encode()) - len(tail.encode())
    assert int(match["omitted"]) == omitted
    assert kept.encode().decode() == kept


def _omitted(kept: str) -> int:
    """Return the byte count the marker in ``kept`` states."""
    match = _CUT.fullmatch(kept)
    assert match is not None
    return int(match["omitted"])


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
