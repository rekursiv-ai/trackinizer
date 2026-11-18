"""Units of a source's text, and the key of the settings they are made under."""

from __future__ import annotations

from typing import Final

import re

from trackinizer.addons.embedding.units import (
    IndexUnit,
    Settings,
    settings_key,
    text_units,
)


_SETTINGS: Final = Settings(
    key="0123456789abcdef",
    dim=4,
    chunk_chars=10,
    overlap_chars=3,
    session_kinds=("UserMessage",),
)


def test_a_short_text_is_one_whole_unit() -> None:
    assert text_units("fits", field="content", settings=_SETTINGS) == (
        IndexUnit(text="fits", field="content"),
    )
    assert text_units("x" * 10, field="text", settings=_SETTINGS) == (
        IndexUnit(text="x" * 10, field="text"),
    )


def test_an_empty_text_has_no_unit() -> None:
    assert text_units("", field="content", settings=_SETTINGS) == ()


def test_a_long_text_is_cut_into_chunks_that_share_the_overlap() -> None:
    assert text_units("abcdefghijklmnopqrstu", field="edge:x", settings=_SETTINGS) == (
        IndexUnit(text="abcdefghij", field="edge:x", chunk=0),
        IndexUnit(text="hijklmnopq", field="edge:x", chunk=1),
        IndexUnit(text="opqrstu", field="edge:x", chunk=2),
    )


def test_the_key_is_sixteen_hex_digits_whatever_the_kinds_order() -> None:
    key = _key()

    assert re.fullmatch(r"[0-9a-f]{16}", key)
    assert key == _key(session_kinds=("AssistantMessage", "UserMessage"))
    assert key == "9f4934e0bffb4f54"


def test_inquiry_kinds_are_sorted_and_their_fields_keep_their_order() -> None:
    fields = (("Paper", ("title", "paper_abstract")), ("Issue", ("description",)))

    assert _key(inquiry_fields=fields) == _key(inquiry_fields=fields[::-1])
    assert _key(inquiry_fields=fields) != _key(
        inquiry_fields=(("Paper", ("paper_abstract", "title")), fields[1]),
    )


def test_every_setting_changes_the_key() -> None:
    keys = {
        _key(),
        _key(model="other@4"),
        _key(dim=8),
        _key(chunk_chars=11),
        _key(overlap_chars=2),
        _key(session_kinds=("UserMessage",)),
        _key(inquiry_fields=(("Issue", ("description",)),)),
        _key(edge_notes=True),
    }

    assert len(keys) == 8


def _key(
    *,
    model: str = "m@4",
    dim: int = 4,
    chunk_chars: int = 10,
    overlap_chars: int = 3,
    session_kinds: tuple[str, ...] = ("UserMessage", "AssistantMessage"),
    inquiry_fields: tuple[tuple[str, tuple[str, ...]], ...] = (),
    edge_notes: bool = False,
) -> str:
    """Return the key of these settings, each defaulted to one fixed value."""
    return settings_key(
        model=model,
        dim=dim,
        chunk_chars=chunk_chars,
        overlap_chars=overlap_chars,
        session_kinds=session_kinds,
        inquiry_fields=inquiry_fields,
        edge_notes=edge_notes,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
