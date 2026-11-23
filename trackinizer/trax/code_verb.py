"""The ``code`` verb: read and write a record's code in the content-addressed store.

This is the agent's half of the code pane. A person imports a folder in the browser, which
files it as that chat session's snapshot; the agent then reads those files, writes them back
and removes them, so the session's research and the code it is about stay one thing.

The store is content-addressed and the snapshot is a whole map of path to blob hash, so
there is no partial write: every mutation here READS the current snapshot, changes one path
in it, stores whatever blob is new, and files the map again. That is why a write needs no
locking to be safe against itself, and why two concurrent writers would still clobber one
another's paths -- the last map filed wins, which is the store's own semantics, not this
verb's choice.

Reads answer ``None`` for a record with no snapshot the caller may read, since the server
answers 404 for an unfiled record and an unreadable one alike.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, ClassVar, Final, cast, override

import argparse
import hashlib
import sys
import uuid

from trackinizer.client.errors import ClientError
from trackinizer.trax.commands import Command, HelpPage, TraxArgumentParser
from trackinizer.trax.context import cwd
from trackinizer.trax.render import echo
from trackinizer.wire import wire_code


if TYPE_CHECKING:
    from trackinizer.client.client import Client


__all__ = ["Code"]


_ACTIONS: Final = ("list", "read", "write", "del", "diff")


def _sha256(text: str) -> str:
    """Return the sha256 hex of ``text``'s UTF-8 bytes, which names its blob."""
    return hashlib.sha256(text.encode()).hexdigest()


def _source_bytes(source: str) -> bytes:
    """Read the bytes of ``-`` (stdin) or ``@FILE``.

    Args:
      source: ``-`` for stdin, or ``@`` and a path.

    Returns:
      data: The bytes read.

    """
    # Bytes, not text: text mode folds ``\r\n`` into ``\n``, which would change the
    # blob's hash against the file on disk.
    if source == "-":
        return cast(bytes, sys.stdin.buffer.read())
    path = source[1:]
    if not path:
        raise ClientError("@ value requires a path")
    try:
        # Resolved against the CALLER's directory: under the daemon the process cwd
        # belongs to whichever shell spawned it.
        return (cwd() / path).read_bytes()
    except OSError as err:
        raise ClientError(
            f"cannot read @{path}: {err.strerror or type(err).__name__}",
        ) from None


def _read_value(source: str) -> str:
    """Resolve a file's text from a literal, ``-`` (stdin) or ``@FILE``.

    Args:
      source: The value as the caller wrote it.

    Returns:
      text: The file's text. Unlike a field value this keeps its trailing newline,
        since a source file's last byte is part of the file.

    """
    if source != "-" and not source.startswith("@"):
        return source
    try:
        return _source_bytes(source).decode()
    except UnicodeDecodeError:
        raise ClientError("the file is not valid UTF-8; the store holds text") from None


def _record(value: str, what: str) -> uuid.UUID:
    """Parse a record id, naming what was expected when it is not one."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ClientError(f"{what} must be a record UUID, not {value!r}") from None


def _checked_path(path: str) -> str:
    """Return ``path`` as a snapshot path, refusing one the store would reject.

    Args:
      path: A relative POSIX path.

    Returns:
      path: The same path, once it is one a snapshot may map.

    """
    if not path:
        raise ClientError("a path is required")
    if len(path) > wire_code.MAX_PATH_CHARS:
        raise ClientError(f"path is over {wire_code.MAX_PATH_CHARS} characters")
    if path.startswith("/"):
        raise ClientError(f"path must be relative, not {path!r}")
    segments = path.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ClientError(f"path must have no empty, . or .. segment: {path!r}")
    if "\0" in path:
        raise ClientError("path must hold no NUL")
    return path


class Code(Command):
    """Read or write a record's code in the content-addressed store."""

    names = ("code",)
    help: ClassVar[str | HelpPage] = HelpPage(
        usage=(
            "trax code RECORD_UUID "
            "[list | read PATH | write PATH to TEXT|@FILE|- | del PATH | diff RECORD_UUID]"
        ),
        summary="List, read, write or remove the files of a record's code snapshot.",
        arguments=(
            ("RECORD_UUID", "The record the code is filed against, usually a chat session."),
            ("PATH", "A relative POSIX path inside the snapshot (`src/app.ts`)."),
        ),
        options=(
            ("--owner EMAIL", "File under another account; staff only. Defaults to the caller's."),
            ("--public", "Let any reader of the record read the snapshot; private by default."),
            (
                "--parent RECORD_UUID",
                "The record this snapshot builds on, so the store can diff them. Kept as it was when omitted.",
            ),
            ("--context N", "With `diff`, lines of context around each change (default 3)."),
        ),
        examples=(
            "trax code 11111111-1111-1111-1111-111111111111",
            "trax code RECORD_UUID read src/app.ts",
            "trax code RECORD_UUID write src/app.ts to @src/app.ts",
            "trax code RECORD_UUID write notes.md to - < notes.md",
            "trax code RECORD_UUID del src/dead.ts",
            "trax code RECORD_UUID diff OTHER_RECORD_UUID",
        ),
        notes=(
            (
                "A snapshot is a whole map of path to blob hash, so every write reads the "
                "current map, changes one path and files it again. The last map filed wins: "
                "two agents writing the same snapshot at once can lose each other's paths."
            ),
            (
                "A blob is one text file named by the sha256 of its UTF-8 bytes and stored "
                "once per owner, so writing a file the store already holds uploads nothing."
            ),
            (
                "Text only, at most 1 MiB a file. A record with no snapshot, and one whose "
                "snapshot the caller may not read, both answer the same: no code is filed."
            ),
        ),
    )

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        """Build the parser for ``trax code``.

        Returns:
          parser: The argparse parser.

        """
        parser = TraxArgumentParser(prog="trax code", description=cls.__doc__)
        parser.add_argument("record_id", nargs="?")
        parser.add_argument("action", nargs="?", metavar="|".join(_ACTIONS))
        parser.add_argument("subject", nargs="*")
        parser.add_argument("--owner")
        parser.add_argument("--public", action="store_true")
        parser.add_argument("--parent")
        parser.add_argument(
            "--context",
            type=int,
            default=wire_code.DEFAULT_DIFF_CONTEXT,
            choices=range(wire_code.MAX_DIFF_CONTEXT + 1),
            metavar="N",
        )
        return parser

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        """Dispatch one ``trax code`` invocation.

        Args:
          verb: The matched verb name.
          args: Parsed local arguments.
          client_factory: Callable returning an authenticated client.

        """
        del verb
        if not args.record_id:
            raise ClientError("trax code needs a record UUID; see trax code help")
        record = _record(cast(str, args.record_id), "record")
        action = cast("str | None", args.action) or "list"
        if action not in _ACTIONS:
            raise ClientError(f"unknown code action {action!r}; one of {', '.join(_ACTIONS)}")
        subject = cast("list[str]", args.subject)
        client = client_factory()
        if action == "list":
            cls._list(client, record)
        elif action == "read":
            cls._read(client, record, subject)
        elif action == "write":
            cls._write(client, record, subject, args)
        elif action == "del":
            cls._delete(client, record, subject, args)
        else:
            cls._diff(client, record, subject, cast(int, args.context))

    @classmethod
    def _list(cls, client: Client, record: uuid.UUID) -> None:
        """Print the snapshot's files, a path and its size a line."""
        snapshot = client.code_snapshot(record)
        if snapshot is None:
            echo(f"No code is filed for {record}.")
            return
        echo(
            f"{snapshot.file_count} files, {snapshot.total_bytes} bytes"
            f"{' (private)' if snapshot.private else ''}"
            f"{f', on {snapshot.parent_record_id}' if snapshot.parent_record_id else ''}",
        )
        if snapshot.partial:
            echo(f"Partial: {snapshot.partial}")
        for file in snapshot.files:
            echo(f"{file.size:>9}  {file.path}")

    @classmethod
    def _read(cls, client: Client, record: uuid.UUID, subject: Sequence[str]) -> None:
        """Print one file's text, with no added newline, so it round-trips byte for byte."""
        if len(subject) != 1:
            raise ClientError("read takes one PATH")
        path = _checked_path(subject[0])
        file = client.code_file(record, path=path)
        if file is None:
            raise ClientError(f"no file at {path!r} in the code of {record}")
        echo(file.text, nl=False)

    @classmethod
    def _write(
        cls,
        client: Client,
        record: uuid.UUID,
        subject: Sequence[str],
        args: argparse.Namespace,
    ) -> None:
        """Create or replace one path, storing its blob and filing the new map."""
        if len(subject) != 3 or subject[1] != "to":
            raise ClientError("write takes PATH to TEXT|@FILE|-")
        path = _checked_path(subject[0])
        text = _read_value(subject[2])
        size = len(text.encode())
        if size > wire_code.MAX_BLOB_BYTES:
            raise ClientError(
                f"{path} is {size} bytes, over the {wire_code.MAX_BLOB_BYTES}-byte limit for one file",
            )
        if "\0" in text:
            raise ClientError(f"{path} holds a NUL byte; the store holds text")
        current = client.code_snapshot(record)
        files = {file.path: file.sha256 for file in current.files} if current else {}
        if current is None and len(files) >= wire_code.MAX_SNAPSHOT_FILES:
            raise ClientError(f"a snapshot maps at most {wire_code.MAX_SNAPSHOT_FILES} files")
        sha = _sha256(text)
        owner = cls._owner(client, args)
        # Only what the store lacks is sent, so rewriting a file to its own content
        # uploads nothing and still refiles the map.
        if client.code_missing(owner, hashes=[sha]):
            client.code_put_blobs(owner, blobs={sha: text})
        existed = path in files
        if not existed and len(files) + 1 > wire_code.MAX_SNAPSHOT_FILES:
            raise ClientError(f"a snapshot maps at most {wire_code.MAX_SNAPSHOT_FILES} files")
        files[path] = sha
        head = client.code_put_snapshot(
            record,
            owner=owner,
            private=cls._private(current, args),
            parent_record_id=cls._parent(current, args),
            files=files,
            partial=current.partial if current else "",
        )
        echo(
            f"{'Replaced' if existed else 'Wrote'} {path} ({size} bytes) in {record}; "
            f"{head.file_count} files now.",
        )

    @classmethod
    def _delete(
        cls,
        client: Client,
        record: uuid.UUID,
        subject: Sequence[str],
        args: argparse.Namespace,
    ) -> None:
        """Remove one path from the snapshot, leaving its blob in the store."""
        if len(subject) != 1:
            raise ClientError("del takes one PATH")
        path = _checked_path(subject[0])
        current = client.code_snapshot(record)
        if current is None:
            raise ClientError(f"no code is filed for {record}")
        files = {file.path: file.sha256 for file in current.files}
        if path not in files:
            raise ClientError(f"no file at {path!r} in the code of {record}")
        del files[path]
        head = client.code_put_snapshot(
            record,
            owner=cls._owner(client, args),
            private=cls._private(current, args),
            parent_record_id=cls._parent(current, args),
            files=files,
            partial=current.partial,
        )
        echo(f"Removed {path} from {record}; {head.file_count} files now.")

    @classmethod
    def _diff(
        cls,
        client: Client,
        record: uuid.UUID,
        subject: Sequence[str],
        context: int,
    ) -> None:
        """Print the unified diff of two records' snapshots, oldest side first."""
        if len(subject) != 1:
            raise ClientError("diff takes one RECORD_UUID")
        other = _record(subject[0], "the record to diff against")
        diff = client.code_diff(record, other, context=context)
        if diff is None:
            echo(f"No code is filed for {record} or {other}.")
            return
        if not diff.files:
            echo("No file differs.")
            return
        for file in diff.files:
            echo(f"{file.change:>8}  {file.path}")
            echo(file.diff, nl=False)
        if diff.more:
            echo("More files differ than are shown here.")

    @classmethod
    def _owner(cls, client: Client, args: argparse.Namespace) -> str:
        """Return the account to file under: the flag, else the caller's own."""
        named = cast("str | None", args.owner)
        return named if named else client.my_email()

    @classmethod
    def _private(cls, current: object, args: argparse.Namespace) -> bool:
        """Whether the snapshot stays private: ``--public`` opens it, else it keeps its state.

        A first snapshot with no flag is private, which is the safe default for a
        folder someone dropped off their own machine.
        """
        if cast(bool, args.public):
            return False
        return getattr(current, "private", True)

    @classmethod
    def _parent(cls, current: object, args: argparse.Namespace) -> uuid.UUID | None:
        """The record this snapshot builds on: the flag, else the one it already names."""
        named = cast("str | None", args.parent)
        if named:
            return _record(named, "the parent record")
        return cast("uuid.UUID | None", getattr(current, "parent_record_id", None))
