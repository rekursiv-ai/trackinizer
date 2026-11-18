"""Capture identities stay tied to a wrapped process tree and a single run."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING
from uuid import uuid4

import asyncio
import io
import subprocess
import threading

from treekle import immutable, json_loads

import httpx2
import pytest

from trackinizer.client.client import Client
from trackinizer.trax.run import session
from trackinizer.trax.run.adapters.codex import CodexAdapter
from trackinizer.trax.run.adapters.iostream import IOStreamAdapter
from trackinizer.trax.run.sink import FileSink, TrackinizerSink
from trackinizer.wire.wire_session_ir import AppendRecordsRequest


if TYPE_CHECKING:
    from pathlib import Path

    from trackinizer.server.store.core import Store


def _capture(sink: FileSink | TrackinizerSink, lines: tuple[bytes, ...]) -> None:
    stop = threading.Event()
    stop.set()
    asyncio.run(
        session._drain_until_stopped(
            IOStreamAdapter(),
            sink,
            session._Stats(),
            session.RunConfig(cli_name="sh"),
            stop,
            baseline=frozenset(),
            slash_queue=deque(),
            stream_queue=deque(lines),
        ),
    )
    sink.sync()


def test_each_stream_capture_has_a_stable_part_distinct_from_other_runs() -> None:
    captures: list[list[dict[str, object]]] = []
    for lines in ((b"first\n", b"second\n"), (b"third\n",)):
        output = io.StringIO()
        _capture(FileSink(output), lines)
        captures.append(
            [
                json_loads(line, dict[str, object])
                for line in output.getvalue().splitlines()
            ],
        )
    names = [{record["part_name"] for record in records} for records in captures]
    assert all(len(name) == 1 for name in names)
    assert names[0].isdisjoint(names[1])


@pytest.mark.db_postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_attached_stream_runs_preserve_every_stored_line(
    integ_store: Store,
) -> None:
    session_id = uuid4()
    async with integ_store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) VALUES ($1, 'AgentSession', "
            "nextval('seq_agentsession'), 'active', 'tester@example.com', "
            "'attached stream', 'open')",
            session_id,
        )
    uploads: list[AppendRecordsRequest] = []
    transport = httpx2.MockTransport(
        lambda request: _answer(request, session_id=str(session_id), uploads=uploads),
    )
    client = Client("http://capture.test")
    client._http.close()
    client._http = httpx2.Client(base_url=client.base_url, transport=transport)
    with client:
        for lines in ((b"first\n", b"second\n"), (b"third\n",)):
            sink = TrackinizerSink(client, "sh", attach=session_id)
            await asyncio.to_thread(_capture, sink, lines)
    for upload in uploads:
        manifest = upload.manifest
        assert manifest is not None
        part = await integ_store.upsert_session_manifest(
            session_id,
            name=upload.name,
            metadata=immutable(manifest.metadata),
            ir_id=manifest.ir_id,
            format=manifest.format,
            records=manifest.records,
        )
        await integ_store.append_session_records(
            session_id,
            [record.row(session_id, part) for record in upload.records],
            restart=upload.restart,
        )
    lines_read = [
        row.text
        for manifest in await integ_store.read_session_manifests(session_id)
        for row in await integ_store.read_session_records(
            session_id,
            part=manifest.part,
        )
        if row.kind == "Stdout"
    ]
    assert lines_read == ["first\n", "second\n", "third\n"]


def _answer(
    request: httpx2.Request,
    *,
    session_id: str,
    uploads: list[AppendRecordsRequest],
) -> httpx2.Response:
    if request.url.path.endswith("/records"):
        uploads.append(AppendRecordsRequest.model_validate_json(request.content))
        return httpx2.Response(200, json={"written": 0, "skipped": 0, "part": 0})
    return httpx2.Response(200, json={"id": session_id, "seq": 0})


def _child_holds(pid: int, path: Path) -> bool:
    del path
    return pid == 202


def _process_list(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    del args, kwargs
    return subprocess.CompletedProcess(
        args=["/bin/ps"],
        returncode=0,
        stdout="101 1\n202 101\n303 1\n",
    )


def test_rollout_discovery_follows_only_the_wrapped_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / f"rollout-2026-10-20T00-00-00-{uuid4()}.jsonl"
    monkeypatch.setattr(session, "_path_held_in_proc", _child_holds)
    monkeypatch.setattr(session, "_path_held_by_lsof", _child_holds)
    monkeypatch.setattr(subprocess, "run", _process_list)
    assert not session._SessionOwner(child_pid=303).owns(CodexAdapter(), path)
    assert session._SessionOwner(child_pid=101).owns(CodexAdapter(), path)


@pytest.mark.cli_python_subprocess
def test_a_wrapper_owns_its_descendants_rollout_but_not_another_runs(
    tmp_path: Path,
) -> None:
    mine = (
        tmp_path
        / "rollout-2026-10-20T00-00-00-00000000-0000-0000-0000-000000000001.jsonl"
    )
    other = (
        tmp_path
        / "rollout-2026-10-20T00-00-00-00000000-0000-0000-0000-000000000002.jsonl"
    )
    writer = tmp_path / "writer.sh"
    writer.write_text('exec 3>"$1"\nprintf "ready\\n"\nIFS= read -r answer\nexit 7\n')
    with (
        subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                (
                    "/bin/sh writer.sh "
                    "rollout-2026-10-20T00-00-00-00000000-0000-0000-0000-000000000001.jsonl; "
                    'status=$?; exit "$status"'
                ),
            ],
            cwd=tmp_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as launcher,
        subprocess.Popen(
            [
                "/bin/sh",
                "writer.sh",
                "rollout-2026-10-20T00-00-00-00000000-0000-0000-0000-000000000002.jsonl",
            ],
            cwd=tmp_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as stranger,
    ):
        try:
            for process in (launcher, stranger):
                assert process.stdout is not None
                assert process.stdout.readline() == "ready\n"
            owner = session._SessionOwner(child_pid=launcher.pid)
            adapter = CodexAdapter()
            assert not owner.owns(adapter, other)
            assert owner.owns(adapter, mine)
        finally:
            for process in (launcher, stranger):
                assert process.stdin is not None
                process.stdin.write("stop\n")
                process.stdin.flush()
            assert launcher.wait(timeout=3) == 7
            assert stranger.wait(timeout=3) == 7
    assert owner.owns(adapter, mine)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
