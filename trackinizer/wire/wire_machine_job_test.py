"""The machine-job wire contract: what a submit, a host's work and a read may carry."""

from __future__ import annotations

import base64
import uuid

import pydantic
import pytest

from trackinizer.wire import wire_machine_job
from trackinizer.wire.wire_job import MAX_ARGV_CHARS
from trackinizer.wire.wire_machine_job import (
    MAX_CHUNK_BYTES,
    Chunk,
    JobReport,
    MachineJobSubmit,
    ReadReply,
    ReadWork,
    WorkOffer,
    remote_protocol,
    remote_url,
)


_SUBMIT = {
    "machine": "gx10",
    "argv": ["python", "train.py"],
    "gpus": 1,
    "memory_mb": 32_768,
    "minutes": 10,
    "remote": "git@github.com:example/repo.git",
    "ref": "refs/heads/topic",
    "commit": "a" * 40,
}


@pytest.mark.parametrize(
    ("url", "protocol"),
    [
        ("https://github.com/example/repo.git", "https"),
        ("HTTPS://github.com/o/r", "https"),
        ("ssh://git@github.com:22/o/r.git", "ssh"),
        ("git@github.com:example/repo.git", "ssh"),
        ("github.com:o/r.git", "ssh"),
        ("file:///srv/repo.git", "file"),
        ("http://github.com/o/r", None),
        ("git://github.com/o/r", None),
        ("ext::sh -c touch% /tmp/x", None),
        ("ext::true", None),
        ("/srv/repo.git", None),
        ("./repo", None),
        ("-uhttps://x/y", None),
        ("https://github.com/o r", None),
        ("git@github.com::o/r", None),
    ],
)
def test_a_remote_is_reached_by_the_protocol_its_spelling_names(
    url: str,
    protocol: str | None,
) -> None:
    assert remote_protocol(url) == protocol


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/repo.git",
        "ssh://git@github.com/o/r.git",
        "git@github.com:o/r.git",
    ],
)
def test_a_job_names_an_https_or_ssh_remote(url: str) -> None:
    assert remote_url(url) == url


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("file:///srv/repo.git", "https or ssh"),
        ("ext::true", "https or ssh"),
        ("https://ghp_token@github.com/o/r.git", "credential"),
        ("https://user:secret@github.com/o/r.git", "credential"),
        ("ssh://git:secret@github.com/o/r.git", "credential"),
        ("https://github.com", "repository path"),
        ("https://github.com/", "repository path"),
        ("https:///o/r", "host"),
        ("https://github.com/" + "o" * 2_048, "2048"),
    ],
)
def test_a_remote_that_is_not_a_plain_https_or_ssh_url_is_refused(
    url: str,
    why: str,
) -> None:
    with pytest.raises(ValueError, match=why):
        _ = remote_url(url)


def test_a_submit_carries_the_job_and_the_pushed_commit() -> None:
    body = MachineJobSubmit.model_validate(_SUBMIT)

    assert (body.machine, body.gpus, body.label) == ("gx10", 1, "")


@pytest.mark.parametrize(
    "fields",
    [
        {"machine": "GX10"},
        {"argv": []},
        {"argv": ["a\x00b"]},
        {"argv": ["x" * MAX_ARGV_CHARS]},
        {"gpus": -1},
        {"gpus": 9},
        {"memory_mb": 63},
        {"memory_mb": 4_194_305},
        {"minutes": 0},
        {"minutes": 1_441},
        {"label": "a;b"},
        {"remote": "https://t@github.com/o/r"},
        {"commit": "A" * 40},
        {"extra": 1},
    ],
)
def test_a_submit_outside_the_limits_is_refused(fields: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        _ = MachineJobSubmit.model_validate(_SUBMIT | fields)


@pytest.mark.parametrize(
    "ref",
    [
        "agent/x",
        "refs/heads/",
        "refs/heads/a..b",
        "refs/heads/a//b",
        "refs/heads/a/",
        "refs/heads/a.lock",
        "refs/heads/a.lock/b",
        "refs/heads/.a",
        "refs/heads/a/.b",
        "refs/heads/-a",
        "refs/heads/a:b",
        "refs/tags/v1",
    ],
)
def test_a_ref_that_is_not_a_branch_git_takes_is_refused(ref: str) -> None:
    with pytest.raises(
        pydantic.ValidationError,
        match=r"Value error, the ref must be a branch, refs/heads/NAME \[",
    ):
        _ = MachineJobSubmit.model_validate(_SUBMIT | {"ref": ref})


def test_the_boundaries_of_a_submit_are_accepted() -> None:
    body = MachineJobSubmit.model_validate(
        _SUBMIT
        | {
            "gpus": 0,
            "memory_mb": 64,
            "minutes": 1_440,
            "argv": ["x" * (MAX_ARGV_CHARS - 1)],
            "ref": "refs/heads/a.b/c-d_e+f@g",
        },
    )

    assert (body.gpus, body.memory_mb, body.minutes) == (0, 64, 1_440)


def test_a_host_is_offered_nothing_a_start_a_cancel_or_a_read() -> None:
    read_id = uuid.uuid4()
    spec = {
        "id": 7,
        "owner": "jan@example.com",
        "argv": ["true"],
        "remote": "https://github.com/o/r.git",
        "ref": "refs/heads/main",
        "commit": "b" * 40,
        "gpus": 0,
        "memory_mb": 1_024,
        "minutes": 5,
        "label": "",
    }

    offers = [
        WorkOffer.model_validate_json(text)
        for text in (
            "{}",
            f'{{"work": {{"kind": "start", "job": {spec!s}}}}}'.replace("'", '"'),
            '{"work": {"kind": "cancel", "job_id": 7}}',
            (
                f'{{"work": {{"kind": "read", "read_id": "{read_id}", "job_id": 7, '
                '"what": "file", "path": "a/b.txt", "offset": 9}}'
            ),
        )
    ]

    assert offers[0].work is None
    assert [type(each.work).__name__ for each in offers[1:]] == [
        "StartWork",
        "CancelWork",
        "ReadWork",
    ]
    read = offers[3].work
    assert isinstance(read, ReadWork)
    assert (read.read_id, read.what, read.path, read.offset) == (
        read_id,
        "file",
        "a/b.txt",
        9,
    )


def test_a_read_holds_at_most_twenty_seconds_and_a_path_is_bounded() -> None:
    fields = {"read_id": str(uuid.uuid4()), "job_id": 7, "what": "log"}
    for bad in ({"wait_sec": 20.5}, {"offset": -1}, {"path": "p" * 4_097}):
        with pytest.raises(pydantic.ValidationError):
            _ = ReadWork.model_validate(fields | bad)


def test_a_report_names_a_state_the_host_may_put_a_job_in() -> None:
    assert JobReport(state="completed", exit_code=0).reason == ""
    for bad in ({"state": "requested"}, {"state": "failed", "exit_code": 256}):
        with pytest.raises(pydantic.ValidationError):
            _ = JobReport.model_validate(bad)


def test_a_chunk_carries_at_most_one_mebibyte() -> None:
    full = base64.b64encode(bytes(MAX_CHUNK_BYTES)).decode()
    over = base64.b64encode(bytes(MAX_CHUNK_BYTES + 1)).decode()

    chunk = Chunk(data_b64=full, offset=0, size=MAX_CHUNK_BYTES, ended=True)

    assert len(base64.b64decode(chunk.data_b64)) == MAX_CHUNK_BYTES
    with pytest.raises(pydantic.ValidationError):
        _ = Chunk(data_b64=over, offset=0, size=MAX_CHUNK_BYTES + 1, ended=True)


def test_a_reply_is_a_chunk_a_manifest_or_a_refusal() -> None:
    texts = (
        (
            '{"reply": {"kind": "chunk", "data_b64": "aGk=", "offset": 0, "size": 2, '
            '"ended": false}}'
        ),
        (
            '{"reply": {"kind": "manifest", "entries": [{"path": "a", "size": 1, '
            '"sha256": null}], "total_bytes": 1}}'
        ),
        '{"reply": {"kind": "refused", "reason": "too large"}}',
    )

    kinds = [ReadReply.model_validate_json(text).reply.kind for text in texts]

    assert kinds == ["chunk", "manifest", "refused"]


def test_a_manifest_lists_at_most_ten_thousand_files() -> None:
    entries = [{"path": f"f{n}", "size": 0, "sha256": None} for n in range(10_001)]

    with pytest.raises(pydantic.ValidationError):
        _ = wire_machine_job.ResultManifest.model_validate(
            {"entries": entries, "total_bytes": 0},
        )


@pytest.mark.parametrize(("length", "count"), [(4_097, 1), (2_048, 1_000)])
def test_manifest_paths_and_aggregate_bytes_are_bounded(
    length: int,
    count: int,
) -> None:
    entries = [{"path": "p" * length, "size": 0, "sha256": None}] * count

    with pytest.raises(pydantic.ValidationError):
        wire_machine_job.ResultManifest.model_validate(
            {"entries": entries, "total_bytes": 0},
        )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
