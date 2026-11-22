"""The files document: a record's files and what changed in them, with diffs.

    FilesDoc     # the tree's paths, and each changed file with its diff
    dump_files   # ... as the JSON text an Artifact's description holds
    load_files   # ... read back

A producer files the document as an Artifact labelled :data:`FILES_LABEL` and
``files:of:<record id>`` (:data:`FILES_OF`) for each record it describes, and adds a
``produced_by`` edge to each. A reader finds a record's document by those two labels,
keeps the rows filed by the operator's service accounts or by the account the record
itself is filed under, and shows the newest. A ``files:of:`` label is a link that
every source of the record's documents writes, never a row key: a producer that
refreshes its row in place finds it by a label only it writes, or it would overwrite
another source's document. The shape names no source, so a work tree, a branch or a
pull request is filed alike and read by one visual. Untracked files that are, byte for
byte, a file of the base's tree are grouped as copied trees and never listed as
changes, so a copied package does not bury the files a change made. It holds paths
and diffs only, never a file's content, and no time, so an unchanged tree writes the
same text.

This package is part of the publishable client distribution, so it must not
import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final, Literal

from treekle import json_dumps, json_loads


__all__ = [
    "FILES_LABEL",
    "FILES_OF",
    "Change",
    "ChangedFile",
    "CopiedTree",
    "FilesDoc",
    "Note",
    "dump_files",
    "load_files",
]


FILES_LABEL: Final = "files"
"""The label of every files document."""

FILES_OF: Final = "files:of:"
"""The prefix of the label that links a document to a record it describes, by id."""


type Change = Literal["added", "modified", "deleted"]
"""How a file differs from the document's base."""

type Note = Literal["", "clipped", "withheld", "omitted"]
"""Why a changed file's diff is cut or missing; empty when it is whole.

``clipped``: the diff is cut at a line end. ``withheld``: the source's own check held
it back. ``omitted``: it was not read, being too large, over the budget, or failed.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangedFile:
    """One file that differs from the base.

    Attributes:
      path: The file's path, relative and ``/``-separated.
      change: How it differs from the base.
      diff: Git's unified diff of this one path, header lines then hunks; empty
        exactly when ``note`` is ``withheld`` or ``omitted``.
      note: Why the diff is cut or missing.

    """

    path: str
    change: Change
    diff: str
    note: Note = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class CopiedTree:
    """Untracked files that are, byte for byte, a file of the base's tree, grouped.

    Attributes:
      path: The directory holding them, ending in ``/``, or the one file's own path.
      files: How many of them lie under ``path``; at least one.

    """

    path: str
    files: int

    def __post_init__(self) -> None:
        """Refuse a group of no files."""
        if self.files < 1:
            raise ValueError(f"files must be at least 1, not {self.files}.")


@dataclass(frozen=True, slots=True, kw_only=True)
class FilesDoc:
    """A tree's files and its changes against a base, read-only.

    A deleted file is among ``changed``, never among ``files``.

    Attributes:
      base: What ``changed`` is measured against, as the source names it
        (``origin/main``, a sha); empty when unknown.
      files: The paths present in the tree: relative, ``/``-separated, sorted,
        unique, and possibly capped.
      more_files: Whether more paths exist than ``files`` lists, or some were not
        read.
      changed: The files that differ from ``base``, sorted by path, unique.
      more_changes: Whether more changed files exist than ``changed`` lists.
      hidden: How many paths the source withheld from both lists by its own rule;
        never negative.
      copied: The untracked files that are, byte for byte, a file of ``base``'s
        tree, grouped under the shallowest directory that holds no other untracked
        file; they are among ``files``, never among ``changed``.
      version: The shape's version; a reader refuses any other, as the page does.

    """

    base: str
    files: tuple[str, ...]
    more_files: bool
    changed: tuple[ChangedFile, ...]
    more_changes: bool
    hidden: int
    copied: tuple[CopiedTree, ...] = ()
    version: Literal[1] = 1
    kind: Literal["files"] = "files"

    def __post_init__(self) -> None:
        """Refuse a negative count of withheld paths."""
        if self.hidden < 0:
            raise ValueError(f"hidden must not be negative, not {self.hidden}.")


def dump_files(doc: FilesDoc) -> str:
    """Return the document as the compact JSON text an Artifact's description holds.

    Args:
      doc: The document.

    Returns:
      text: Plain objects, arrays and scalars with no type tags, so a page reads it
        as it is.

    """
    return json_dumps(doc, target=FilesDoc, sort=False, separators=(",", ":"))


def load_files(text: str) -> FilesDoc:
    """Read a files document back.

    Args:
      text: What :func:`dump_files` wrote.

    Returns:
      doc: The document.

    Raises:
      ValueError: ``text`` is not a files document, such as one with a field it
        does not know, another version, a negative ``hidden``, or a copied tree of
        no files.

    """
    doc = json_loads(text, FilesDoc)
    # A read restores state as written; building again runs each bound check.
    return replace(doc, copied=tuple(replace(tree) for tree in doc.copied))
