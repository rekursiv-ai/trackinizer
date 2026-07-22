"""Error raised when a trackinizer HTTP or CLI operation fails."""

from __future__ import annotations


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
