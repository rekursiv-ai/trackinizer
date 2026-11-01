"""Find the modules whose SQL reads ``inquiries`` or ``change_log``, for guard tests.

Read filtering is per SQL site: a reader the access policy is not wired into shows
an addon's rows, an AccessGroup's included, to every caller. Each guard names every
production module under its root that reads either table, with why it may; this
module finds them and reports the ones a guard has not classified.

The scan reads string constants other than docstrings: a ``FROM`` or ``JOIN``
naming either table, or a ``FROM`` / ``JOIN`` whose table is composed at run time
(``" FROM ", table``). Parsing is the cost, so a module is parsed only when its
text could hold such a string, and a guard checks each classified module on its
own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

import ast
import os
import re


if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path


__all__ = [
    "Class",
    "calls_policy",
    "candidates",
    "misfiling",
    "reader",
    "readers",
    "unclassified",
]


type Class = Literal["policy", "audience", "owned", "trusted", "fragment"]
"""``policy``: filters every caller-facing read through the access policy.
``audience``: reads only session or chat rows, gated by audience, membership or the
opening credential, which the policy derives its session grants from.
``owned``: reads only rows the caller owns.
``trusted``: no request principal reaches the read (bootstrap, workers, operator
tools, write-path locks and existence checks, authorization lookups).
``fragment``: SQL text whose consumers bind the policy's filter into it."""

# Upper case only: the code spells SQL keywords upper case, and prose saying "read
# from inquiries" is no query.
_READS: Final = re.compile(r"\b(?:FROM|JOIN)\s+(?:inquiries|change_log)\b")
_COMPOSED: Final = re.compile(r"\b(?:FROM|JOIN)\s*$")
_POLICY_FILTERS: Final = frozenset(
    {"inquiry_read_filter", "change_read_filter", "read_filter"},
)

# A module's text holds either read only where FROM or JOIN is followed, past
# whitespace or escapes, by a table's name or by where its literal ends (a quote, or
# an f-string's field). Escapes can hide the boundary a constant's value has, so the
# keyword needs none here.
_MAY_READ: Final = re.compile(
    r"(?:FROM|JOIN)"
    r"(?:\s|\\(?:x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|N\{[^}]*\}|[\s\S]))*"
    r"(?:inquiries\b|change_log\b|[\"'{])",
)


def candidates(root: Path, *, skip: frozenset[str] = frozenset()) -> tuple[str, ...]:
    """Return each production module under ``root`` whose text could hold a read.

    Args:
      root: The package directory to walk.
      skip: Top-level directories under ``root`` to leave out.

    Returns:
      found: Each module's POSIX path relative to ``root``, sorted.

    """
    found = []
    for path in _modules(root, skip=skip):
        text = path.read_text()
        # Most modules hold no upper-case keyword at all.
        if ("FROM" in text or "JOIN" in text) and _MAY_READ.search(text):
            found.append(path.relative_to(root).as_posix())
    return tuple(sorted(found))


def reader(path: Path) -> ast.Module | None:
    """Return the module at ``path``'s tree if its SQL reads either table, else None."""
    text = path.read_text()
    tree = ast.parse(text, filename=str(path))
    if any(_READS.search(s) or _COMPOSED.search(s) for s in _strings(tree)):
        return tree
    return None


def readers(root: Path, *, skip: frozenset[str] = frozenset()) -> dict[str, ast.Module]:
    """Return each production module under ``root`` whose SQL reads either table.

    Args:
      root: The package directory to walk.
      skip: Top-level directories under ``root`` to leave out.

    Returns:
      found: Each module's tree, by its POSIX path relative to ``root``.

    """
    return {
        module: tree
        for module in candidates(root, skip=skip)
        if (tree := reader(root / module)) is not None
    }


def unclassified(
    root: Path,
    classified: Mapping[str, tuple[Class, str]],
    *,
    skip: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Return each module under ``root`` that reads either table and is not classified.

    Args:
      root: The package directory to walk.
      classified: Each module a guard names, relative to ``root``, its class and why.
      skip: Top-level directories under ``root`` to leave out.

    Returns:
      found: The unclassified readers' POSIX paths relative to ``root``, sorted.

    """
    return tuple(
        module
        for module in candidates(root, skip=skip)
        if module not in classified and reader(root / module) is not None
    )


def misfiling(path: Path, kind: Class) -> Literal["stale", "unwired"] | None:
    """Say how a module classified ``kind`` disagrees with what it does, if it does.

    Args:
      path: The classified module.
      kind: Its class.

    Returns:
      misfiling: ``stale`` when it is gone or reads neither table; ``unwired`` when
        it is a ``policy`` reader calling none of the policy's filters; else None.

    """
    tree = reader(path) if path.is_file() else None
    if tree is None:
        return "stale"
    if kind == "policy" and not calls_policy(tree):
        return "unwired"
    return None


def calls_policy(tree: ast.AST) -> bool:
    """Say whether ``tree`` names one of the policy's read filters."""
    return any(
        (isinstance(node, ast.Name) and node.id in _POLICY_FILTERS)
        or (isinstance(node, ast.Attribute) and node.attr in _POLICY_FILTERS)
        for node in ast.walk(tree)
    )


def _modules(root: Path, *, skip: frozenset[str]) -> Iterator[Path]:
    """Yield each production module under ``root``: no test, conftest or hidden path."""
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if directory != root or entry.name not in skip:
                        pending.append(directory / entry.name)
                elif (
                    entry.name.endswith(".py")
                    and not entry.name.endswith(("_test.py", "_test_support.py"))
                    and entry.name != "conftest.py"
                ):
                    yield directory / entry.name


def _strings(tree: ast.AST) -> Iterator[str]:
    """Yield every string constant in ``tree`` but a docstring, f-string parts too."""
    prose = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in prose
        ):
            yield node.value
