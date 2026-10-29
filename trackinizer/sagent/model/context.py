"""A model context's size: the price band it falls in, its tokens, and how it reads.

size = model.settings.context_size
size.context_name                       # small, mid, or large
size.tokens
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


__all__ = [
    "ContextSize",
    "ContextSizeName",
]


type ContextSizeName = Literal[
    "small",
    "mid",
    "large",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextSize:
    """A context's size: the price band it falls in, its tokens, and how it reads."""

    context_name: ContextSizeName = "small"
    tokens: int = 0
    display_name: str = "0"
