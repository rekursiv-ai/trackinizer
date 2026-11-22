"""Exercise the real host-job Client methods at the HTTP boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

import httpx2
import pytest

from trackinizer.client.client import Client
from trackinizer.wire.wire_host_job import (
    CancelWork,
    Chunk,
    HostJobList,
    HostJobSubmit,
    HostJobView,
    JobReport,
    ReadReply,
    ResultManifest,
    WorkOffer,
)


if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def wire(request: pytest.FixtureRequest) -> Iterator[_Wire]:
    api_key = cast(object, request.param)
    assert isinstance(api_key, str)
    found = _Wire(api_key=api_key)
    try:
        yield found
    finally:
        found.client.close()


@pytest.mark.parametrize("wire", ["person-key"], indirect=True)
def test_person_host_job_methods_match_route_paths_queries_and_bodies(
    wire: _Wire,
) -> None:
    body = HostJobSubmit(
        host="gx10",
        argv=["python", "train.py"],
        gpus=1,
        memory_mb=2_048,
        minutes=5,
        label="smoke",
        remote="https://example.invalid/o/r.git",
        ref="refs/heads/main",
        commit="a" * 40,
    )
    view = HostJobView(
        id=9,
        state="completed",
        exit_code=0,
        reason="",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        queued_at=None,
        started_at=None,
        ended_at=None,
        host=body.host,
        argv=body.argv,
        gpus=body.gpus,
        memory_mb=body.memory_mb,
        minutes=body.minutes,
        label=body.label,
        remote=body.remote,
        ref=body.ref,
        commit=body.commit,
    )
    chunk = ReadReply(reply=Chunk(data_b64="aGk=", offset=5, size=7, ended=True))
    manifest = ReadReply(reply=ResultManifest(entries=[], total_bytes=0))
    wire.responses = [
        httpx2.Response(201, json=view.model_dump(mode="json")),
        httpx2.Response(200, json=HostJobList(jobs=[view]).model_dump(mode="json")),
        *[httpx2.Response(200, json=view.model_dump(mode="json")) for _ in range(2)],
        httpx2.Response(200, json=chunk.model_dump(mode="json")),
        httpx2.Response(200, json=manifest.model_dump(mode="json")),
        httpx2.Response(200, json=chunk.model_dump(mode="json")),
    ]

    assert wire.client.host_job_submit(body) == view
    assert wire.client.host_job_list() == HostJobList(jobs=[view])
    assert wire.client.host_job_view(9) == view
    assert wire.client.host_job_cancel(9) == view
    assert wire.client.host_job_log(9, offset=5, wait=20) == chunk
    assert wire.client.host_job_manifest(9) == manifest
    assert wire.client.host_job_file(9, "out/deep/a b.bin", offset=3) == chunk

    assert [(r.method, r.url.path, dict(r.url.params)) for r in wire.requests] == [
        ("POST", "/api/addons/jobs/host-jobs", {}),
        ("GET", "/api/addons/jobs/host-jobs", {}),
        ("GET", "/api/addons/jobs/host-jobs/9", {}),
        ("POST", "/api/addons/jobs/host-jobs/9/cancel", {}),
        ("GET", "/api/addons/jobs/host-jobs/9/log", {"offset": "5", "wait": "20"}),
        ("GET", "/api/addons/jobs/host-jobs/9/manifest", {}),
        (
            "GET",
            "/api/addons/jobs/host-jobs/9/file",
            {"path": "out/deep/a b.bin", "offset": "3"},
        ),
    ]
    assert HostJobSubmit.model_validate_json(wire.requests[0].content) == body
    assert all(r.content in (b"", b"null") for r in wire.requests[1:])
    assert all(r.headers["Authorization"] == "Bearer person-key" for r in wire.requests)
    assert wire.responses == []


@pytest.mark.parametrize("wire", ["host-credential"], indirect=True)
def test_host_host_job_methods_match_route_paths_auth_and_models(
    wire: _Wire,
) -> None:
    host, instance, read_id = UUID(int=41), UUID(int=7), UUID(int=8)
    report = JobReport(state="completed", exit_code=0, reason="finished")
    reply = ReadReply(reply=Chunk(data_b64="aGk=", offset=5, size=7, ended=True))
    offer = WorkOffer(work=CancelWork(job_id=9))
    wire.responses = [
        httpx2.Response(200, json=offer.model_dump(mode="json")),
        httpx2.Response(204),
        httpx2.Response(204),
    ]

    assert wire.client.wait_host_work(host, instance=instance, wait_sec=25) == offer
    wire.client.report_host_job(host, 9, report)
    wire.client.post_host_read(host, read_id, reply)

    assert [(r.method, r.url.path, dict(r.url.params)) for r in wire.requests] == [
        (
            "GET",
            f"/api/addons/jobs/hosts/{host}/work",
            {"instance": str(instance), "wait_sec": "25"},
        ),
        ("POST", f"/api/addons/jobs/hosts/{host}/jobs/9", {}),
        ("POST", f"/api/addons/jobs/hosts/{host}/reads/{read_id}", {}),
    ]
    assert JobReport.model_validate_json(wire.requests[1].content) == report
    assert ReadReply.model_validate_json(wire.requests[2].content) == reply
    assert wire.requests[0].content in (b"", b"null")
    assert all(
        r.headers["Authorization"] == "Bearer host-credential" for r in wire.requests
    )
    assert wire.responses == []


class _Wire:
    def __init__(self, *, api_key: str) -> None:
        self.requests: list[httpx2.Request] = []
        self.responses: list[httpx2.Response] = []
        self.client = Client("https://example.invalid", api_key=api_key)
        self.client._http.close()
        self.client._http = httpx2.Client(
            base_url=self.client.base_url,
            headers=dict(self.client._http.headers),
            transport=httpx2.MockTransport(self),
        )

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.responses.pop(0)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
