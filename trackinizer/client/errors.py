"""Error raised when a trackinizer HTTP or CLI operation fails."""

from __future__ import annotations

from typing import TypedDict

import json

from treekle import ReadError, json_loads


class ClientError(Exception):
    """An HTTP request or CLI operation failed.

    ``status_code`` and ``code`` are populated for HTTP failures so a
    caller can branch on the server's error (e.g. a 409 conflict).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def server_answer(error: ClientError) -> str:
    """Say what the server answered, without the request's path.

    Args:
      error: The failed client operation.

    Returns:
      answer: The server's refusal, or its status when no detail was readable.

    """
    if error.status_code is None:
        return "the server could not be reached."
    body = str(error).partition(f"-> {error.status_code}: ")[2]
    try:
        detail = json_loads(body, _Failure)["detail"]
    except (json.JSONDecodeError, ReadError):
        return f"the server answered {error.status_code}."
    return f"the server answered {error.status_code}: {detail.removesuffix('.')}."


class _Failure(TypedDict):
    detail: str
