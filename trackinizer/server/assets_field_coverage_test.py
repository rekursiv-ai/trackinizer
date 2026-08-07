"""Every captured session field must be REACHABLE, or say why it is not.

The capture adapters record more than the transcript view drew. An audit of the
message dataclasses against ``server/assets/index.html`` found nine fields that
were stored on every session and rendered nowhere -- including ``attachments``
on three separate message kinds, so an image the model was shown or a PDF a tool
produced existed in the record and was invisible in the only view of it.

That is the project's own recurring defect pointed at itself: **data captured is
not data available**, and a view that silently omits a field is indistinguishable
from a capture that never recorded one. A reader cannot tell "this turn had no
attachment" from "this view does not draw attachments".

**Reachable, not displayed.** This view is meant to be scanned like a CLI, so
the fix is emphatically not to put every field on screen -- that trades an
invisible record for an unreadable one. A field satisfies this test by being
reachable: on the row, behind a disclosure, or in a summary. What it may not be
is absent, with no path from the view to the thing that was captured.

So the audit is a test rather than a one-off sweep. Adding a field to a
:data:`Message` dataclass now forces a choice: make it reachable, or name it
here with a reason. The exemption list is deliberately small and each entry is
justified -- an exemption is a decision, not a default.
"""

from __future__ import annotations

from pathlib import Path

import dataclasses
import re

import pytest

from trackinizer.types import agent_session_events as ev


SPA = Path(__file__).parent / "assets" / "index.html"

MESSAGE_CLASSES = (
    ev.UserMessage,
    ev.AgentSendMessage,
    ev.SystemMessage,
    ev.AssistantMessage,
    ev.ToolResult,
    ev.Compaction,
    ev.SlashCommand,
    ev.UnknownMessage,
)

#: Fields captured on purpose and deliberately NOT drawn, with the reason.
#: Keyed ``(class name, field name)`` so an exemption cannot silently widen to
#: another class that happens to reuse the field name.
NOT_DISPLAYED: dict[tuple[str, str], str] = {
    (
        "AssistantMessage",
        "thinking_signature",
    ): "Opaque provider attestation over the thinking block. Not human-readable, "
    "carries no information a reader of a replay can act on, and is retained "
    "for verification rather than display.",
    (
        "AssistantMessage",
        "thinking_encrypted",
    ): "Provider-encrypted thinking payload. Undecryptable client-side by "
    "construction; rendering it would show ciphertext and imply the view had "
    "failed rather than that the content is sealed.",
    (
        "UnknownMessage",
        "raw",
    ): "Reached the screen through the renderer's JSON fallback, which dumps the "
    "whole message for any kind it does not know -- that fallback is the "
    "display path for this field, not an omission.",
}


@pytest.fixture(scope="module")
def source() -> str:
    return SPA.read_text(encoding="utf-8")


def _is_referenced(field_name: str, source: str) -> bool:
    """Does the SPA mention this field at all?

    Deliberately loose: the point is to catch a field nothing has ever looked
    at, not to police how it is drawn. A false pass here needs someone to have
    written the field name into the view without using it, which review catches;
    a false failure would make the test noise, and noisy tests get deleted.
    """
    return bool(
        re.search(rf"\.{re.escape(field_name)}\b", source)
        or re.search(rf'"{re.escape(field_name)}"', source)
    )


def test_every_message_field_is_reachable_or_documented(source: str) -> None:
    missing: list[str] = []
    for cls in MESSAGE_CLASSES:
        for f in dataclasses.fields(cls):
            key = (cls.__name__, f.name)
            if key in NOT_DISPLAYED:
                continue
            if not _is_referenced(f.name, source):
                missing.append(f"{cls.__name__}.{f.name}")
    assert not missing, (
        "Captured but unreachable: "
        + ", ".join(sorted(missing))
        + ". Make it reachable in server/assets/index.html -- on the row, behind "
        "a disclosure, or in a summary -- or add it to NOT_DISPLAYED with the "
        "reason. A field that is stored and never shown makes 'this turn had "
        "none' indistinguishable from 'this view does not draw it'."
    )


def test_exemptions_name_real_fields() -> None:
    """An exemption for a field that no longer exists is stale permission.

    Left alone it would silently cover a future field of the same name on the
    same class -- the exemption outliving the reason it was granted for.
    """
    real = {
        (cls.__name__, f.name)
        for cls in MESSAGE_CLASSES
        for f in dataclasses.fields(cls)
    }
    stale = sorted(f"{c}.{n}" for (c, n) in NOT_DISPLAYED if (c, n) not in real)
    assert not stale, f"NOT_DISPLAYED names fields that no longer exist: {stale}"


def test_every_exemption_gives_a_reason() -> None:
    """A blank reason is an exemption nobody has to defend."""
    thin = sorted(
        f"{c}.{n}" for (c, n), why in NOT_DISPLAYED.items() if len(why.strip()) < 40
    )
    assert not thin, f"Exemptions without a substantive reason: {thin}"


def test_attachments_are_drawn_for_every_kind_that_can_carry_them(source: str) -> None:
    """The gap that motivated the audit.

    ``BytesAttachment`` / ``FilePath`` / ``WebUrl`` were captured on user turns,
    agent messages and tool results, and drawn on none of them.
    """
    assert "function renderAttachments" in source
    for kind in ("BytesAttachment", "FilePath", "WebUrl"):
        assert kind in source, f"{kind} attachments are not rendered"


def test_an_unknown_attachment_kind_is_still_announced(source: str) -> None:
    """Nothing is dropped for being unrecognised.

    A new attachment kind must degrade to a visible chip, not to silence, or the
    view starts under-reporting the moment the schema grows.
    """
    assert 'String(t || "unknown")' in source


def test_only_raster_images_are_inlined(source: str) -> None:
    """Attachment bytes come from third-party tool output.

    An SVG data URI is not scriptable inside <img> in current browsers, but that
    guarantee is browser-version-dependent, so the preview path is an allowlist
    and everything else stays a chip.
    """
    assert "INLINE_IMAGE_TYPES" in source
    assert "image/svg" not in source


def test_attachments_are_chips_before_they_are_pictures(source: str) -> None:
    """Terse by default is the requirement; reachable is the constraint.

    An image rendered inline puts a 320px picture in the scan path for every
    turn that carried one. It opens on click instead, so the row stays one line
    and the content is still one interaction away.
    """
    assert "attach-preview" in source
    assert "function attachChip" in source


def test_usage_is_reported_per_session_not_per_turn(source: str) -> None:
    """A CLI reports one total.

    A token chip on every assistant turn is chrome on every row for a number
    nobody compares turn-by-turn, so the count is aggregated into the header.
    """
    assert "totalTokens" in source
    assert "turn-tokens" not in source
