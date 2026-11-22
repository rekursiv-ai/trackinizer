"""Creation-only timestamp handling at the trax command boundary."""

from __future__ import annotations

from treekle import from_plain

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax.conftest import FakeClient, run
from trackinizer.trax.grammar import FIELDS_BY_NAME


@pytest.mark.parametrize(
    "argv",
    [
        ["--backfill", "2020-01-02T03:04:05+02:00", "issue", "title", "to", "old"],
        ["issue", "--backfill", "2020-01-02T03:04:05+02:00", "title", "to", "old"],
        ["issue", "title", "to", "old", "--backfill", "2020-01-02T03:04:05+02:00"],
        ["issue", "title", "to", "old", "--backfill=2020-01-02T03:04:05+02:00"],
        [
            "--profile",
            "prod",
            "--show-ids",
            "--backfill=2020-01-02T03:04:05+02:00",
            "issue",
            "title",
            "to",
            "old",
            "--as",
            "archivist",
        ],
    ],
)
def test_backfill_maps_to_creation_body(client: FakeClient, argv: list[str]) -> None:
    run(argv, client)

    batch = next(call for call in client.calls if call[0] == "submit_batch")
    items = from_plain(batch[1][0], tuple[tuple[str, dict[str, object]], ...])
    assert items == (
        (
            "Issue",
            {
                "title": "old",
                "priority": 20,
                "created": "2020-01-02T03:04:05+02:00",
            },
        ),
    )
    if "--as" in argv:
        assert batch[2]["actor"] == "archivist"
    assert "backfill" not in FIELDS_BY_NAME
    assert "created" not in FIELDS_BY_NAME


@pytest.mark.parametrize("backfill", [[], ["--backfill", "2020-01-02T03:04:05Z"]])
def test_creation_context_reaches_every_new_inline_row(
    client: FakeClient,
    backfill: list[str],
) -> None:
    run(
        [
            "issue",
            "title",
            "to",
            "root",
            "requires",
            "issue",
            "title",
            "to",
            "child",
            "requires",
            "issue",
            "title",
            "to",
            "grandchild",
            "--reason",
            "  Imported from a local session; original wording.\n",
            *backfill,
        ],
        client,
    )

    batch = next(call for call in client.calls if call[0] == "submit_batch")
    items = from_plain(batch[1][0], tuple[tuple[str, dict[str, object]], ...])
    assert [body["title"] for _, body in items] == ["root", "child", "grandchild"]
    if backfill:
        assert all(
            body["reason"] == "  Imported from a local session; original wording.\n"
            for _, body in items
        )
        assert all(body["created"] == "2020-01-02T03:04:05+00:00" for _, body in items)
    else:
        assert all("created" not in body for _, body in items)
        assert all("reason" not in body for _, body in items)


@pytest.mark.parametrize("backfill", [[], ["--backfill", "2020-01-02T03:04:05Z"]])
def test_creation_context_preserves_existing_anchor(
    client: FakeClient,
    backfill: list[str],
) -> None:
    anchor = dict(client.rows[0])
    run(
        [
            "issue",
            "1",
            "requires",
            "issue",
            "title",
            "to",
            "child",
            "requires",
            "issue",
            "title",
            "to",
            "grandchild",
            "--reason",
            "Import two historical findings.",
            *backfill,
        ],
        client,
    )

    batch = next(call for call in client.calls if call[0] == "submit_batch")
    items = from_plain(batch[1][0], tuple[tuple[str, dict[str, object]], ...])
    edges = from_plain(batch[2]["edges"], tuple[dict[str, object], ...])
    assert [body["title"] for _, body in items] == ["child", "grandchild"]
    if backfill:
        assert all(
            body["reason"] == "Import two historical findings." for _, body in items
        )
        assert all(body["created"] == "2020-01-02T03:04:05+00:00" for _, body in items)
    else:
        assert all("created" not in body for _, body in items)
        assert all("reason" not in body for _, body in items)
    assert edges[0]["from_id"] == str(client.target_id)
    assert client.rows[0] == anchor
    assert not any(call[0] == "edit" for call in client.calls)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["issue"],
        ["issue", "1"],
        ["issue", "1", "title"],
        ["issue", "1", "title", "to", "changed"],
        ["issue", "1", "del"],
        ["issue", "1", "requires", "issue", "4"],
        ["issue", "title", "re", "row"],
        ["title", "re", "row"],
        ["issue", "1..5", "title", "to", "changed", "--makeitso"],
        ["experiment", "2", "metric", "at", "step", "is", "3", "to", "0.5"],
        ["experiment", "metric", "at", "loss"],
        ["agentsession", "1", "run", "codex"],
        ["recent"],
        ["run", "codex", "--", "prompt"],
    ],
)
def test_backfill_without_creation_fails_before_client_calls(
    client: FakeClient,
    argv: list[str],
) -> None:
    with pytest.raises(ClientError, match=r"--backfill.*create"):
        run(["--backfill", "2020-01-02T03:04:05Z", *argv], client)
    assert client.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["issue"],
        ["issue", "1"],
        ["issue", "1", "title"],
        ["issue", "1", "title", "to", "changed"],
        ["issue", "1", "del"],
        ["issue", "1", "requires", "issue", "4"],
        ["issue", "title", "re", "row"],
        ["title", "re", "row"],
        ["issue", "1..5", "title", "to", "changed", "--makeitso"],
        ["experiment", "2", "metric", "at", "step", "is", "3", "to", "0.5"],
        ["experiment", "metric", "at", "loss"],
    ],
)
def test_local_backfill_without_creation_fails_before_client_calls(
    client: FakeClient,
    argv: list[str],
) -> None:
    with pytest.raises(ClientError, match=r"--backfill.*create"):
        run([*argv, "--backfill", "2020-01-02T03:04:05Z"], client)
    assert client.calls == []


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("invalid", "ISO 8601"),
        ("2020-01-02", "timezone"),
        ("2020-01-02T03:04:05", "timezone"),
        ("2020-02-30T03:04:05Z", "ISO 8601"),
        ("9999-01-01T00:00:00Z", "past"),
        ("", "ISO 8601"),
    ],
)
def test_invalid_backfill_fails_before_client_calls(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
    value: str,
    message: str,
) -> None:
    with pytest.raises(SystemExit) as failure:
        run(["issue", "title", "to", "old", "--backfill", value], client)
    assert failure.value.code == 2
    error = capsys.readouterr().err
    assert "--backfill" in error
    assert message in error
    assert client.calls == []


def test_backfill_requires_timestamp(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as failure:
        run(["issue", "title", "to", "old", "--backfill"], client)
    assert failure.value.code == 2
    assert "--backfill: expected one argument" in capsys.readouterr().err
    assert client.calls == []


def test_creation_context_does_not_leak_to_next_command(client: FakeClient) -> None:
    run(
        [
            "issue",
            "title",
            "to",
            "old",
            "--backfill",
            "2020-01-02T03:04:05Z",
            "--reason",
            "Imported from history.",
        ],
        client,
    )
    client.calls.clear()
    run(["issue", "title", "to", "current"], client)
    batch = next(call for call in client.calls if call[0] == "submit_batch")
    items = from_plain(batch[1][0], tuple[tuple[str, dict[str, object]], ...])
    assert items == (("Issue", {"title": "current", "priority": 20}),)
    client.calls.clear()
    run(["issue", "1", "title", "to", "updated"], client)
    edits = [call for call in client.calls if call[0] == "edit"]
    assert len(edits) == 1
    assert edits[0][1][1:] == ("title", "updated")
    assert edits[0][2]["reason"] == ""


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
