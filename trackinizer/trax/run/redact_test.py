"""Tests for the redactor that keeps delivered secret values out of a transcript."""

from __future__ import annotations

from uuid import UUID

import base64
import json
import time

import pytest

from trackinizer.lib.agent.types.sessions import Attachment, UserMessage
from trackinizer.lib.agent.types.stored import from_stored
from trackinizer.trax.run.redact import (
    Redactor,
    redact_body,
    redactor_from_environ,
)
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_session_ir import RecordBody


def test_a_value_is_replaced_by_its_names_placeholder() -> None:
    redactor = Redactor({"API_KEY": "sk-live-12345678"})
    assert redactor.redact("key=sk-live-12345678;") == "key=[redacted:API_KEY];"


def test_every_occurrence_of_a_value_is_replaced() -> None:
    redactor = Redactor({"TOKEN": "tok-abcdefgh"})
    text = "tok-abcdefgh and tok-abcdefgh, then tok-abcdefghtok-abcdefgh"
    assert redactor.redact(text) == (
        "[redacted:TOKEN] and [redacted:TOKEN], then [redacted:TOKEN][redacted:TOKEN]"
    )


def test_text_without_a_value_is_returned_unchanged() -> None:
    redactor = Redactor({"TOKEN": "tok-abcdefgh"})
    assert redactor.redact("nothing to hide") == "nothing to hide"
    assert Redactor({}).redact("tok-abcdefgh") == "tok-abcdefgh"


def test_a_value_inside_a_longer_value_leaves_no_fragment() -> None:
    redactor = Redactor({"SHORT": "secret123", "LONG": "supersecret123"})
    assert redactor.redact("x supersecret123 y") == "x [redacted:LONG] y"
    assert redactor.redact("x secret123 y") == "x [redacted:SHORT] y"


def test_a_value_that_starts_a_longer_value_leaves_no_fragment() -> None:
    redactor = Redactor({"SHORT": "tok-abcdefgh", "LONG": "tok-abcdefgh-tail"})
    assert redactor.redact("tok-abcdefgh-tail") == "[redacted:LONG]"


def test_values_that_overlap_in_the_text_leave_no_fragment() -> None:
    redactor = Redactor({"FIRST": "abcdefgh", "SECOND": "defghijk"})
    out = redactor.redact("--abcdefghijk--")
    assert out == "--[redacted:FIRST][redacted:SECOND]--"
    assert "ijk" not in out


def test_a_value_that_overlaps_itself_is_redacted_whole() -> None:
    redactor = Redactor({"RUN": "aaaaaaaa"})
    assert redactor.redact("a" * 11) == "[redacted:RUN]"


def test_two_names_with_one_value_are_both_named() -> None:
    redactor = Redactor({"A_KEY": "same-value-1", "B_KEY": "same-value-1"})
    assert redactor.redact("same-value-1") == "[redacted:A_KEY][redacted:B_KEY]"


def test_a_json_escaped_value_is_redacted_too() -> None:
    value = 'line one\n"quoted" back\\slash'
    redactor = Redactor({"PEM": value})
    escaped = r"line one\n\"quoted\" back\\slash"
    assert redactor.redact(f"raw {value} escaped {escaped}") == (
        "raw [redacted:PEM] escaped [redacted:PEM]"
    )


def test_an_empty_value_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="EMPTY"):
        _ = Redactor({"EMPTY": ""})


def test_a_prefix_cut_at_the_end_of_the_text_is_redacted() -> None:
    redactor = Redactor({"KEY": "key-cut-ABCDEFGHIJKLMNOP"})
    cut = ("x" * 1990 + "key-cut-ABCDEFGHIJKLMNOP" + "y" * 100)[:2000]
    out = redactor.redact(cut)
    assert out == "x" * 1990 + "[redacted:KEY]"
    assert "key-cut" not in out


def test_a_prefix_cut_at_the_end_of_a_line_is_redacted() -> None:
    redactor = Redactor({"KEY": "key-cut-ABCDEFGHIJKLMNOP"})
    text = "Preview:\nlead key-cut-ABCD\n...\n</persisted-output>"
    assert redactor.redact(text) == (
        "Preview:\nlead [redacted:KEY]\n...\n</persisted-output>"
    )


def test_a_prefix_that_ends_a_json_escaped_value_is_redacted() -> None:
    redactor = Redactor({"PEM": "line one\nline two"})
    assert redactor.redact("raw line one\\nli") == "raw [redacted:PEM]"


def test_a_prefix_shorter_than_four_characters_is_kept() -> None:
    redactor = Redactor({"KEY": "key-cut-ABCDEFGH"})
    assert redactor.redact("ends in key") == "ends in key"


def test_a_prefix_inside_a_line_is_kept() -> None:
    redactor = Redactor({"KEY": "key-cut-ABCDEFGH"})
    assert redactor.redact("key-cut and more\nnext") == "key-cut and more\nnext"


def test_a_non_ascii_value_is_redacted_in_its_ascii_json_form() -> None:
    redactor = Redactor({"PW": "p\u00e4ssw\u00f6rd-123456"})
    escaped = json.dumps({"k": "p\u00e4ssw\u00f6rd-123456"})
    assert "\\u00e4" in escaped
    assert redactor.redact(escaped) == '{"k": "[redacted:PW]"}'


def test_a_number_that_is_a_secret_is_redacted_in_a_json_structure() -> None:
    redactor = Redactor({"PIN": "123456789012"})
    assert redactor.redact_json({"pin": 123_456_789_012, "n": 3, "ok": True}) == {
        "pin": "[redacted:PIN]",
        "n": 3,
        "ok": True,
    }


def test_every_string_in_a_json_structure_is_redacted() -> None:
    redactor = Redactor({"TOKEN": "tok-abcdefgh"})
    value = {
        "tok-abcdefgh": "key",
        "input": {"command": "curl -H tok-abcdefgh"},
        "output": ["a", ["ok tok-abcdefgh", 3, 2.5, True, None]],
    }
    assert redactor.redact_json(value) == {
        "[redacted:TOKEN]": "key",
        "input": {"command": "curl -H [redacted:TOKEN]"},
        "output": ["a", ["ok [redacted:TOKEN]", 3, 2.5, True, None]],
    }


def test_a_json_structure_without_a_value_is_equal_to_the_input() -> None:
    redactor = Redactor({"TOKEN": "tok-abcdefgh"})
    value = {"a": [1, "two", {"three": None}]}
    assert redactor.redact_json(value) == value


def test_no_names_means_no_redactor() -> None:
    assert redactor_from_environ({}) is None
    assert redactor_from_environ({"TRAX_REDACT_NAMES": ""}) is None
    assert redactor_from_environ({"TRAX_REDACT_NAMES": ",", "K": "value-1234"}) is None


def test_the_named_variables_are_redacted() -> None:
    redactor = redactor_from_environ(
        {
            "TRAX_REDACT_NAMES": "A_KEY,B_KEY",
            "A_KEY": "value-1234",
            "B_KEY": "other-5678",
            "UNNAMED": "unnamed-90",
        },
    )
    assert redactor is not None
    assert redactor.redact("value-1234 other-5678 unnamed-90") == (
        "[redacted:A_KEY] [redacted:B_KEY] unnamed-90"
    )


def test_a_named_variable_that_is_absent_exits() -> None:
    environ = {
        "TRAX_REDACT_NAMES": "A_KEY,MISSING_ONE,MISSING_TWO",
        "A_KEY": "value-1234",
    }
    with pytest.raises(SystemExit) as raised:
        _ = redactor_from_environ(environ)
    message = str(raised.value)
    assert "MISSING_ONE, MISSING_TWO" in message
    assert "A_KEY" not in message
    assert "value-1234" not in message


@pytest.mark.parametrize("terminator", ["\n", "\r\n"])
def test_each_line_of_a_multi_line_value_is_redacted_on_its_own(
    terminator: str,
) -> None:
    pem = (
        "-----BEGIN KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
        "AmFrZXNlY3JldGJvZHk=\n-----END KEY-----"
    )
    redactor = Redactor({"PEM": pem})
    for line in pem.split("\n"):
        assert redactor.redact(line + terminator) == "[redacted:PEM]" + terminator
    assert redactor.redact("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\r\nok") == (
        "[redacted:PEM]\r\nok"
    )


def test_a_line_shorter_than_four_characters_is_kept() -> None:
    redactor = Redactor({"PEM": "abcdefgh\nxy\nijklmnop"})
    assert redactor.redact("xy\r\n") == "xy\r\n"
    assert redactor.redact("ijklmnop\r\n") == "[redacted:PEM]\r\n"


def test_a_prefix_cut_before_a_carriage_return_is_redacted() -> None:
    redactor = Redactor({"KEY": "key-cut-ABCDEFGHIJKLMNOP"})
    assert redactor.redact("lead key-cut-ABCD\r\nnext") == "lead [redacted:KEY]\r\nnext"
    assert redactor.redact("lead key-cut-ABCD\rnext") == "lead [redacted:KEY]\rnext"


def test_a_prefix_cut_by_the_line_clamp_is_redacted() -> None:
    redactor = Redactor({"K": "secretvalue1"})
    clamped = "x" * 16_373 + "secretvalue... (truncated)\r\n"
    out = redactor.redact(clamped)
    assert out == "x" * 16_373 + "[redacted:K]... (truncated)\r\n"
    assert "secretvalue" not in out
    assert redactor.redact("a secr... (truncated)") == "a [redacted:K]... (truncated)"


def test_a_prefix_search_over_one_long_line_is_linear() -> None:
    redactor = Redactor({"K": "AAAA1234secret"})
    text = "A" * 1_000_000 + "!"
    started = time.perf_counter()
    assert redactor.redact(text) == text
    assert time.perf_counter() - started < 0.5


def test_a_named_variable_with_a_short_value_exits_naming_it() -> None:
    environ = {
        "TRAX_REDACT_NAMES": "A_SHORT,B_EMPTY,C_OK",
        "A_SHORT": "short-7",
        "B_EMPTY": "",
        "C_OK": "eight-ok",
    }
    with pytest.raises(SystemExit) as raised:
        _ = redactor_from_environ(environ)
    message = str(raised.value)
    assert "A_SHORT, B_EMPTY" in message
    assert "C_OK" not in message
    assert "shorter than 8 characters" in message
    assert "short-7" not in message


def test_a_value_of_eight_characters_is_accepted() -> None:
    redactor = redactor_from_environ({"TRAX_REDACT_NAMES": "K", "K": "eight-ok"})
    assert redactor is not None
    assert redactor.redact("eight-ok") == "[redacted:K]"


def test_names_are_taken_without_surrounding_spaces() -> None:
    environ = {"TRAX_REDACT_NAMES": " A_KEY , B_KEY ", "A_KEY": "value-1234"}
    with pytest.raises(SystemExit, match=r"names B_KEY, which"):
        _ = redactor_from_environ(environ)
    redactor = redactor_from_environ({**environ, "B_KEY": "other-5678"})
    assert redactor is not None
    assert (
        redactor.redact("value-1234 other-5678") == "[redacted:A_KEY] [redacted:B_KEY]"
    )


def test_a_body_is_redacted_in_its_payload_and_text() -> None:
    body = _body("use tok-abcdefgh now")
    assert "tok-abcdefgh" in body.model_dump_json()
    out = redact_body(body, redactor=Redactor({"TOKEN": "tok-abcdefgh"}))
    assert "tok-abcdefgh" not in out.model_dump_json()
    assert out.text == "use [redacted:TOKEN] now"
    assert out.idx == 3


def test_a_body_without_a_redactor_is_kept() -> None:
    body = _body("use tok-abcdefgh now")
    assert redact_body(body, redactor=None) == body


def test_a_secret_in_a_text_attachment_is_redacted_in_its_stored_bytes() -> None:
    canary = "sk-live-12345678"
    message = UserMessage(
        content="see the file",
        attachments=(
            Attachment(mime_descriptor="text/plain", data=f"key={canary}\n".encode()),
            Attachment(mime_descriptor="text/plain", data=b"nothing here"),
        ),
    )
    body = _body_of(message)
    assert canary not in body.model_dump_json()
    out = redact_body(body, redactor=Redactor({"API_KEY": canary}))
    assert from_stored(out.payload, UserMessage) == UserMessage(
        content="see the file",
        attachments=(
            Attachment(mime_descriptor="text/plain", data=b"key=[redacted:API_KEY]\n"),
            Attachment(mime_descriptor="text/plain", data=b"nothing here"),
        ),
    )


def test_a_secret_in_binary_attachment_bytes_is_replaced_and_the_rest_kept() -> None:
    canary = "sk-live-12345678"
    message = UserMessage(
        attachments=(
            Attachment(
                mime_descriptor="image/png",
                data=b"\x89PNG\xff\x00" + canary.encode() + b"\xfe\x80end",
            ),
        ),
    )
    out = redact_body(_body_of(message), redactor=Redactor({"API_KEY": canary}))
    assert from_stored(out.payload, UserMessage) == UserMessage(
        attachments=(
            Attachment(
                mime_descriptor="image/png",
                data=b"\x89PNG\xff\x00[redacted:API_KEY]\xfe\x80end",
            ),
        ),
    )


def test_attachment_bytes_without_a_secret_are_kept_as_they_are() -> None:
    message = UserMessage(
        attachments=(Attachment(mime_descriptor="image/png", data=b"\x00\xff\x80"),),
    )
    out = redact_body(_body_of(message), redactor=Redactor({"K": "sk-live-12345678"}))
    assert from_stored(out.payload, UserMessage) == message


def test_a_bytes_tag_that_is_not_base64_is_redacted_as_text() -> None:
    redactor = Redactor({"TOKEN": "tok-abcdefgh"})
    assert redactor.redact_json({"py/b64": "tok-abcdefghi!"}) == {
        "py/b64": "[redacted:TOKEN]i!",
    }


def test_a_bytes_tag_with_stray_characters_is_not_decoded_leniently() -> None:
    redactor = Redactor({"TOKEN": "tok-abcdefgh"})
    blob = base64.b64encode(b"tok-abcdefgh").decode() + "!"
    assert redactor.redact_json({"py/b64": blob}) == {"py/b64": blob}


def test_a_value_that_is_valid_base64_under_a_bytes_tag_is_redacted_as_text() -> None:
    value = "Zm9vYmFyYmF6cXV4"
    assert base64.b64decode(value, validate=True)
    redactor = Redactor({"TOKEN": value})
    assert redactor.redact_json({"arguments": {"py/b64": value}}) == {
        "arguments": {"py/b64": "[redacted:TOKEN]"},
    }


def test_a_bytes_tag_beside_other_keys_is_an_ordinary_mapping() -> None:
    redactor = Redactor({"TOKEN": "tok-abcdefgh"})
    blob = base64.b64encode(b"tok-abcdefgh").decode()
    assert redactor.redact_json({"py/b64": blob, "note": "tok-abcdefgh"}) == {
        "py/b64": blob,
        "note": "[redacted:TOKEN]",
    }


def test_a_cut_inside_the_armour_of_a_pem_value_is_kept() -> None:
    pem = "-----BEGIN KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END KEY-----"
    redactor = Redactor({"PEM": pem})
    text = "-----\nrule -----\n-----BEGIN KEY\n-----END"
    assert redactor.redact(text) == text
    assert redactor.redact(f"x {pem} y") == "x [redacted:PEM] y"
    assert redactor.redact("-----BEGIN KEY-----\n") == "[redacted:PEM]\n"


def test_a_pem_cut_after_its_armour_is_redacted() -> None:
    redactor = Redactor({"PEM": "-----BEGIN KEY-----\nMIIEvQIBADANBgkqhkiG9w0B"})
    assert redactor.redact("raw -----BEGIN KEY-----\\nMIIE") == "raw [redacted:PEM]"
    assert redactor.redact("raw -----BEGIN KE") == "raw -----BEGIN KE"


def _body(content: str) -> RecordBody:
    return _body_of(UserMessage(content=content))


def _body_of(record: UserMessage) -> RecordBody:
    row = SessionRecordRow.of(
        session_id=UUID(int=0),
        part=0,
        idx=3,
        record=record,
    )
    return RecordBody.of(row)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
