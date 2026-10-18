"""Credential shapes are masked; ordinary text is not."""

from __future__ import annotations

import pytest

from trackinizer.wire.key_shapes import MASK, KeyMasker, mask_key_shapes


@pytest.mark.parametrize(
    "secret",
    [
        "trax_" + "a" * 40,
        "trax_machine_" + "0" * 32 + "_" + "b" * 43,
        "enr_" + "0" * 32 + "_" + "c" * 43,
        "sk-ant-api03-" + "d" * 30,
        "sk-" + "e" * 40,
        "ghp_" + "f" * 36,
        "github_pat_" + "g" * 60,
        "AKIA" + "H" * 16,
        "xoxb-1234567890-abcdef",
        "Bearer " + "i" * 30,
    ],
)
def test_a_credential_shape_is_masked_inside_a_line(secret: str) -> None:
    assert mask_key_shapes(f"export K={secret} # note") == f"export K={MASK} # note"


def test_a_private_key_line_is_masked_to_its_end() -> None:
    one = "-----BEGIN RSA PRIVATE KEY-----MIIE-----END RSA PRIVATE KEY-----"

    assert mask_key_shapes(f"a {one} b") == f"a {MASK} b"
    assert mask_key_shapes("x -----BEGIN PRIVATE KEY-----MIIE") == f"x {MASK}"


def test_the_masker_hides_a_private_key_across_its_lines_and_then_stops() -> None:
    masker = KeyMasker()
    lines = [
        "cat key\n",
        "-----BEGIN OPENSSH PRIVATE KEY-----\n",
        "b3BlbnNzaC1rZXk\n",
        "-----END OPENSSH PRIVATE KEY-----\n",
        "$ ",
    ]

    assert [masker.mask(line) for line in lines] == [
        "cat key\n",
        f"{MASK}\n",
        f"{MASK}\n",
        f"{MASK}\n",
        "$ ",
    ]


def test_the_masker_hides_an_unterminated_key_line_without_adding_a_break() -> None:
    masker = KeyMasker()

    assert masker.mask("-----BEGIN PRIVATE KEY-----\n") == f"{MASK}\n"
    assert masker.mask("MIIEvQ") == MASK
    assert masker.mask("-----END PRIVATE KEY-----") == MASK
    assert masker.mask("plain") == "plain"


@pytest.mark.parametrize("text", ["ls -la", "trax run claude", "sk-short", "AKIAshort"])
def test_ordinary_text_is_unchanged(text: str) -> None:
    assert mask_key_shapes(text) == text


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
