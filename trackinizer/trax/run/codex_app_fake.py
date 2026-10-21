"""A scripted stand-in for ``codex app-server``, for tests that must not call a model.

Run it as ``python codex_app_fake.py SCRIPT.json``. It speaks the app-server's stdio
framing, one JSON object per line, and takes everything it says from the script:

- ``replies``: ``{method: result}``. A request the script does not name gets ``{}``.
  A result of ``{"$error": {"code": .., "message": ..}}`` answers with that error, and
  one of ``{"$silent": true}`` does not answer at all.
- ``after``: ``{method: [frame, ...]}``. The frames go out right after the reply to a
  request of that method.
- ``on_response``: ``{id: [frame, ...]}``. The frames go out when the client answers
  the server request with that id.
- ``log``: a file that gets every frame the client sends, one per line.

A frame of ``{"$exit": N}`` ends the process with status ``N``; ``{"$stderr": HEX}``
writes those bytes to stderr; and a frame that is a string goes out as it is, so a
script can send a line that is not JSON. The fake also writes its working directory to
``<log>.cwd``. Standard library only, so it starts fast and needs nothing the
repository imports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, TypedDict, cast

import json
import sys


_ERROR: Final = "$error"
_EXIT: Final = "$exit"
_STDERR: Final = "$stderr"

type _Frame = dict[str, object] | str


def serve(script: Path) -> None:
    """Answer the client's frames from ``script`` until its stdin closes.

    Args:
      script: The JSON file that says what to answer and send.

    """
    plan = cast("_Plan", json.loads(script.read_text()))
    Path(plan["log"] + ".cwd").write_text(str(Path.cwd()))
    with Path(plan["log"]).open("a") as log:
        for line in sys.stdin:
            log.write(line)
            log.flush()
            frame = cast("dict[str, object]", json.loads(line))
            method = frame.get("method")
            if isinstance(method, str) and "id" in frame:
                result = plan["replies"].get(method, {})
                if _ERROR in result:
                    _send({"id": frame["id"], "error": result[_ERROR]})
                elif "$silent" not in result:
                    _send({"id": frame["id"], "result": result})
                _send_all(plan["after"].get(method, []))
            elif method is None:
                _send_all(plan["on_response"].get(str(frame["id"]), []))


def _send_all(frames: list[_Frame]) -> None:
    for frame in frames:
        if isinstance(frame, dict) and _EXIT in frame:
            sys.stdout.flush()
            sys.exit(int(str(frame[_EXIT])))
        if isinstance(frame, dict) and _STDERR in frame:
            sys.stderr.buffer.write(bytes.fromhex(str(frame[_STDERR])))
            sys.stderr.flush()
        else:
            _send(frame)


def _send(frame: _Frame) -> None:
    sys.stdout.write((frame if isinstance(frame, str) else json.dumps(frame)) + "\n")
    sys.stdout.flush()


class _Plan(TypedDict):
    """The script a test writes."""

    log: str
    replies: dict[str, dict[str, object]]
    after: dict[str, list[_Frame]]
    on_response: dict[str, list[_Frame]]


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
