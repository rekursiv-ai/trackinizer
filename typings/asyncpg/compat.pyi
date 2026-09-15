from asyncio import (
    StreamWriter,
    timeout as timeout,
    wait_for as wait_for,
)
from enum import StrEnum as StrEnum
from inspect import markcoroutinefunction as markcoroutinefunction
from pathlib import Path
from typing import Final

SYSTEM: Final[str]

def get_pg_home_directory() -> Path | None: ...
async def wait_closed(stream: StreamWriter) -> None: ...
