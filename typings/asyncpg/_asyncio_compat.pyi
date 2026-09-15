from collections.abc import Awaitable
from typing import TypeVar

_T = TypeVar("_T")

async def wait_for(fut: Awaitable[_T], timeout: float | None) -> _T: ...  # noqa: ASYNC109 -- Mirrors upstream timeout parameter.
