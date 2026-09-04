"""Convert agent sessions between native formats and the normalized form.

Every conversion routes through the normalized model: an input is normalized
to a stream of :class:`~trackinizer.lib.agent.types.sessions.SessionRecord`, then
denormalized to the requested target. Converting to the source's own format is
therefore a losslessness check, which is what ``verify`` reports.

Each path may be a session file or a directory of them, so one command serves
a single file and a whole corpus.

Examples:
  python -m trackinizer.lib.agent.sessions convert session.jsonl --to json
  python -m trackinizer.lib.agent.sessions convert session.json --to claude
  python -m trackinizer.lib.agent.sessions verify ~/captured --workers 5

"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, fields, is_dataclass
from difflib import unified_diff
from io import StringIO
from itertools import repeat
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, TextIO, cast, override

import argparse
import json
import os
import sys
import threading
import time

from trackinizer.lib.agent.sessions import claude, codex, gemini, normalized
from trackinizer.lib.agent.sessions.fuse import chain, fuse, names_of, unfuse
from trackinizer.lib.agent.types.sessions import SessionRecord
from trackinizer.lib.custom_json import DictCodec


type Format = str
# Every format read and written: ``claude`` / ``codex`` (append-only JSONL),
# ``gemini`` (one rewritten document), ``json`` (the IR itself). Repeated as a
# kwarg default rather than held as a module global.
#
# NOT ``sh``. A captured stream is not a CLI dialect -- it is whatever a
# wrapped process printed -- and its reader lives with the tool that spawns
# that process (``trackinizer.trax.run.adapters.scrape``). A converter in
# ``lib`` cannot import it, and should not: converting INTO a scrape was always
# lossy (a stream has no shape for a tool call), and converting out of one
# yields text no CLI will load.


@dataclass(frozen=True, slots=True, kw_only=True)
class FileResult:
    """The outcome of converting one session file."""

    path: Path
    source: Format | None = None
    target: Format | None = None
    text: str = ""
    parts: tuple[tuple[str, str], ...] = ()
    """The files a native conversion writes: ``(name, text)``, in order.

    Empty when the target is the normalized form, which is ONE document by
    definition -- that is what a fused session renders to.
    """
    byte_exact: bool = False
    source_bytes: int = 0
    """Bytes the source files held, summed over every part."""
    output_bytes: int = 0
    """Bytes the rewrite produced, counted even when the text is not kept.

    A byte-exact run makes these equal by definition. They earn their place on
    a run that is NOT exact: a rewrite that lost a whole record and one that
    respelled a separator both report ``not byte-exact``, and the size is what
    tells them apart before a diff is read.
    """
    dropped: tuple[str, ...] = ()
    diff: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Return whether the conversion succeeded."""
        return self.error is None


def main(
    argv: Sequence[str] | None = None,
    *,
    formats: tuple[Format, ...] = ("claude", "codex", "gemini", "json"),
) -> int:
    """Convert sessions and return the process exit code."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser, formats=formats)
    args = parser.parse_args(argv)
    paths = tuple(_session_files(args.paths))
    if not paths:
        parser.error("no session files found")
    if args.command == "convert" and args.to is None:
        parser.error("convert requires --to")
    if args.output is not None and len(paths) > 1:
        parser.error("--output takes a single input path")
    results = _convert_all(
        paths,
        workers=args.workers,
        source=args.source,
        target=args.to,
        want_diff=args.diff,
        fail_fast=args.fail_fast,
        destination=_destination_for(args),
    )
    lossy = [r for r in results if r.dropped]
    if lossy and not args.lossy:
        for result in lossy:
            print(  # noqa: T201 -- CLI report.
                f"{result.path}: drops {', '.join(result.dropped)}", file=sys.stderr
            )
        parser.error("conversion drops records; pass --lossy to accept")
    _write(results, output=args.output, out_dir=args.out_dir, stream=sys.stdout)
    _report(
        results,
        output_format=args.format,
        quiet=args.quiet,
        verbose=args.verbose,
        verifying=args.command == "verify",
    )
    return 0 if _passed(results, verifying=args.command == "verify") else 1


def _destination_for(args: argparse.Namespace) -> Path | Literal[False] | None:
    """Return where one run's converted text should go.

    ``verify`` never needs it, and a ``convert`` with a directory writes it
    there; only a convert with nowhere to put it brings the text home.
    """
    if args.command != "convert":
        return False
    # ``Namespace`` attributes are ``Any``; the parser typed this one as Path.
    return cast(Path | None, args.out_dir)


def convert_file(
    path: Path,
    source: Format,
    target: Format | None,
    want_diff: bool,
    destination: Path | Literal[False] | None = None,
    *,
    formats: tuple[Format, ...] = ("claude", "codex", "gemini", "json"),
) -> FileResult:
    """Convert one session and report the outcome.

    A session is a DIRECTORY when the provider spread it over one -- claude
    writes ``<id>.jsonl`` beside an ``<id>/`` holding its subagents, and codex
    chains rollouts by the thread each forked from. The parts are fused into
    one session so the caller sees the conversation a person actually had,
    rather than the several files it landed in.

    Args:
      path: Session file or directory to read.
      source: Input format, or ``auto`` to detect from content.
      target: Output format; ``None`` converts to the source format.
      want_diff: Whether to include a unified diff when output differs.
      destination: Where the converted text goes. A DIRECTORY writes it there
        and returns only the sizes; ``False`` keeps none of it, which is what
        ``verify`` wants -- it asks only whether the bytes agree, and holding a
        273 MB rewrite to answer that costs 2.6 GB. ``None`` returns the text
        on the result, for a caller that has nowhere to put it.
      formats: Session formats this converter recognizes.

    Returns:
      result: The conversion outcome for ``path``.

    """
    parts = _parts_of(path)
    if not parts:
        return FileResult(path=path, error="no session files found")
    streams: list[Sequence[SessionRecord]] = []
    detected: Format = ""
    for part in parts:
        try:
            found = source if source != "auto" else _sniff(part)
            if found not in formats:
                # Opened before the verdict: ``_sniff`` answers ``""`` for a
                # file it could not read as well as for one whose content names
                # no format, and reporting the second when it was the first
                # told an operator their unreadable file was the wrong shape.
                # The open raises, and the handler below names the real fault.
                with part.open(encoding="utf-8") as handle:
                    _ = handle.read(1)
                return FileResult(path=path, error="unrecognized session format")
            detected = detected or found
            # Streamed, never read whole: ONE non-ASCII character makes
            # CPython hold the entire file as 4 bytes per character, so a
            # 273 MB rollout cost 1.09 GB as a string before parsing began.
            # Drained inside the ``with``: ``normalize`` is a generator now, so
            # a stored-but-unconsumed one would resume on a closed handle.
            with part.open(encoding="utf-8") as handle:
                streams.append(list(_adapter(found).normalize(handle)))
        except (OSError, UnicodeDecodeError) as exc:
            return FileResult(path=path, error=f"{type(exc).__name__}: {exc}")
        except (TypeError, ValueError) as exc:
            return FileResult(path=path, error=f"{type(exc).__name__}: {exc}")
    into = target or detected
    ordered = chain(streams) if detected == "codex" else streams
    # Names follow the RECORD STREAMS, not the file list: ``chain`` reorders
    # codex rollouts by the thread each forked from, so pairing name ``i`` with
    # the i-th read file labelled every part with another part's file.
    filed = {id(stream): part.name for stream, part in zip(streams, parts, strict=True)}
    records: Sequence[SessionRecord] = (
        list(fuse(ordered, [filed[id(part)] for part in ordered]))
        if len(ordered) > 1
        else ordered[0]
    )
    try:
        return _compared(
            path,
            records,
            parts,
            detected,
            into,
            want_diff=want_diff,
            keep_text=destination is None,
            out_dir=destination if isinstance(destination, Path) else None,
        )
    except (TypeError, ValueError) as exc:
        return FileResult(path=path, error=f"{type(exc).__name__}: {exc}")


def _compared(
    path: Path,
    records: Sequence[SessionRecord],
    parts: Sequence[Path],
    detected: Format,
    into: Format,
    *,
    want_diff: bool,
    keep_text: bool = True,
    out_dir: Path | None = None,
) -> FileResult:
    """Return how a converted session compares to the bytes it came from.

    Part by part, holding one part's source and one part's rewrite at a time:
    the whole of either is a copy of the session, and a corpus of those is
    what once took 21 GB.
    """
    if out_dir is not None and into == "json":
        # The destination is known, so the conversion goes STRAIGHT there: a
        # corpus whose every session's text came home first reached 3 GB
        # resident before the first byte was written.
        #
        # Only for the normalized target. A native one can DROP records the
        # format cannot express, and the ``--lossy`` gate refuses the run
        # before anything reaches disk -- which a streamed write would have
        # already broken by the time the gate reads the result.
        return _streamed(
            path, records, parts, detected=detected, into=into, out_dir=out_dir
        )
    names = names_of(records)
    written: list[tuple[str, str]] = []
    lost: Counter[str] = Counter()
    byte_exact = True
    diff = ""
    source_bytes = sum(part.stat().st_size for part in parts)
    output_bytes = 0
    # By NAME, not by index: ``chain`` orders codex rollouts by the thread each
    # forked from, while ``parts`` is oldest-first, so a fused session's parts
    # come back in a different order than the files were read in. Comparing
    # position for position then measured every part against the wrong file.
    by_name = {part.name: part for part in parts}
    for index, streamed_part in enumerate(unfuse(records)):
        # Materialized per PART, not per session: this loop rewrites the part
        # and then, when the bytes differ, normalizes the rewrite to measure
        # what was lost -- two passes over one file's records. A part is one
        # file, so the hold is a file rather than the corpus.
        part = list(streamed_part)
        name = names[index] if index < len(names) else ""
        source_path = by_name.get(name) or (
            parts[index] if index < len(parts) else None
        )
        if source_path is not None and detected == into and not keep_text:
            # Verifying only asks whether the bytes match, and the answer is
            # decided by the first line that differs -- so the rewrite streams
            # into the comparison rather than into a 273 MB buffer.
            matched, streamed = _rewrites(part, source_path, into)
            output_bytes += streamed
            if matched:
                written.append((name or source_path.name, ""))
                continue
            byte_exact = False
            text = _write_one(part, into)
        else:
            text = _write_one(part, into)
            output_bytes += len(text.encode("utf-8"))
        same = (
            _matches(source_path, text)
            if source_path is not None and detected == into
            else False
        )
        if not same:
            byte_exact = False
            for dropped in _dropped(part, text, into):
                kind, _, count = dropped.rpartition(":")
                lost[kind] += int(count)
            if want_diff and not diff and source_path is not None:
                diff = _diff(source_path.read_text(encoding="utf-8"), text)
        written.append((name or (source_path.name if source_path else ""), text))
    # Only when the caller wants it: ``verify`` asks whether the bytes agree,
    # and rendering a 273 MB session to answer that costs 2.6 GB.
    text = (
        ""
        if not keep_text
        else _write_one(records, into)
        if into == "json"
        else "".join(part_text for _, part_text in written)
    )
    if into == "json" and keep_text:
        # The normalized form is ONE document, so the per-part sizes describe a
        # rendering that is never emitted; the document itself is the output.
        output_bytes = len(text.encode("utf-8"))
    return FileResult(
        path=path,
        source=detected,
        target=into,
        parts=() if into == "json" else tuple(written),
        text=text,
        byte_exact=byte_exact,
        source_bytes=source_bytes,
        output_bytes=output_bytes,
        dropped=tuple(f"{kind}:{count}" for kind, count in sorted(lost.items())),
        diff=diff,
    )


def _streamed(
    path: Path,
    records: Sequence[SessionRecord],
    parts: Sequence[Path],
    *,
    detected: Format,
    into: Format,
    out_dir: Path,
    suffixes: Mapping[Format, str] = MappingProxyType(
        {
            "claude": ".jsonl",
            "codex": ".jsonl",
            "gemini": ".json",
            "json": ".json",
        }
    ),
) -> FileResult:
    """Write one conversion to ``out_dir``, holding no copy of its text."""
    out_dir.mkdir(parents=True, exist_ok=True)
    source_bytes = sum(part.stat().st_size for part in parts)
    output_bytes = 0
    written: list[tuple[str, str]] = []
    if into == "json":
        # The normalized form is ONE document per session.
        destination = _destination(out_dir, path, suffixes["json"])
        with destination.open("w", encoding="utf-8") as handle:
            _adapter(into).denormalize(records, handle)
        output_bytes = destination.stat().st_size
        written.append((destination.name, ""))
    else:
        # A native target goes back as the files it was spread across, each
        # under its own name, so the source directory is reproduced.
        names = names_of(records)
        for index, part in enumerate(unfuse(records)):
            name = names[index] if index < len(names) else ""
            if not name:
                name = (
                    parts[index].name
                    if index < len(parts)
                    else f"{path.stem}-{index}{suffixes[into]}"
                )
            destination = out_dir / name
            with destination.open("w", encoding="utf-8") as handle:
                _adapter(into).denormalize(part, handle)
            output_bytes += destination.stat().st_size
            written.append((name, ""))
    return FileResult(
        path=path,
        source=detected,
        target=into,
        parts=() if into == "json" else tuple(written),
        byte_exact=detected == into and output_bytes == source_bytes,
        source_bytes=source_bytes,
        output_bytes=output_bytes,
    )


def _destination(out_dir: Path, path: Path, suffix: str) -> Path:
    """Return a unique output path for one session.

    Named for the session's stem, plus as much of its parent chain as it takes
    to be unique: a corpus holds many sessions called ``session.jsonl`` in
    different directories, and naming them all by stem silently wrote one file
    and dropped the rest -- 4 sessions became 1.
    """
    candidate = out_dir / f"{path.stem}{suffix}"
    if not candidate.exists():
        return candidate
    for depth in range(1, len(path.parts)):
        stem = "-".join([*path.parts[-depth - 1 : -1], path.stem])
        candidate = out_dir / f"{stem.replace('/', '-')}{suffix}"
        if not candidate.exists():
            return candidate
    return out_dir / f"{path.stem}-{abs(hash(str(path)))}{suffix}"


class _Comparing(TextIO):
    """A sink that checks what is written against a file, keeping neither.

    ``denormalize`` writes a whole session, and holding that as one string
    cost 2.6 GB on a 273 MB rollout -- one non-ASCII line widens the entire
    buffer. Verification only needs to know whether the bytes agree, so each
    chunk is compared and dropped.
    """

    def __init__(self, path: Path) -> None:
        self._handle = path.open(encoding="utf-8")
        self._pending = ""
        self.matched = True
        self.written = 0

    @override
    def write(self, text: str, /) -> int:
        """Compare one chunk, reading only as much of the file as it needs."""
        # Counted before the match check short-circuits: the size is wanted
        # precisely on the runs that already failed, and a sink that stopped
        # counting at the first difference would report the prefix as the whole.
        self.written += len(text.encode("utf-8"))
        if self.matched and text:
            self._pending += text
            want = self._handle.read(len(self._pending))
            if want != self._pending[: len(want)]:
                self.matched = False
            self._pending = self._pending[len(want) :]
        return len(text)

    @override
    def close(self) -> None:
        """Finish: unread file bytes or unmatched output means a mismatch."""
        if self._pending or self._handle.read(1):
            self.matched = False
        self._handle.close()


def _rewrites(
    records: Sequence[SessionRecord], path: Path, target: Format
) -> tuple[bool, int]:
    """Whether writing ``records`` reproduces ``path``, and the bytes it wrote.

    Args:
      records: The records to write.
      path: The source file to compare against.
      target: Format to write in.

    Returns:
      matched: Whether the rewrite reproduced ``path`` byte for byte.
      written: Bytes the rewrite produced.

    """
    sink = _Comparing(path)
    try:
        _adapter(target).denormalize(records, sink)
    finally:
        sink.close()
    return sink.matched, sink.written


def _matches(path: Path, text: str) -> bool:
    """Whether ``path`` holds exactly ``text``, without reading it whole.

    Line by line: re-materializing a 273 MB transcript to compare against
    cost 1.4 GB, and the answer is decided by the first line that differs.
    """
    if path.stat().st_size != len(text.encode("utf-8")):
        return False
    at = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not text.startswith(line, at):
                return False
            at += len(line)
    return at == len(text)


def _sniff(path: Path) -> Format:
    """Return the format that wrote ``path``, reading only its head.

    Enough lines to find one carrying a key that names the provider, and no
    more: a whole file is megabytes to answer a question its first line
    usually settles.

    A file that cannot be READ names no format, rather than raising. Sniffing
    is what DISCOVERY now uses to tell a gemini document from claude's own
    ``.json`` sidecars, and discovery runs before the per-file error handling
    ``convert_file`` has -- so one non-UTF-8 blob or one unreadable file
    anywhere under a corpus root aborted the whole run.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            head = "".join(line for _, line in zip(range(64), handle, strict=False))
    except (OSError, UnicodeDecodeError):
        return ""
    return detect_format(head)


def _write_one(records: Sequence[SessionRecord], target: Format) -> str:
    """Return one record stream in ``target`` format."""
    output = StringIO()
    _adapter(target).denormalize(records, output)
    return output.getvalue()


def _parts_of(path: Path) -> list[Path]:
    """Return the transcript files one session is spread across.

    A DIRECTORY is the session: every transcript beneath it, oldest first.
    That is what makes ``/clear`` recoverable -- claude answers it by opening
    a new transcript beside the old one, naming neither, so the directory and
    the order the files were written are the only thing tying them together.

    A lone FILE is a session too, plus the ``<id>/`` beside it when one
    exists, which is where claude puts the subagents that session spawned.
    """
    if path.is_dir():
        return _beneath(path)
    beside = path.with_suffix("")
    return [path, *_beneath(beside)] if beside.is_dir() else [path]


def _transcripts(directory: Path) -> list[Path]:
    """Transcripts sitting DIRECTLY in ``directory``, oldest first.

    Not recursive, which is the whole distinction: "holds transcripts" is the
    test for being a session, and a recursive answer is true of every ancestor
    up to the root -- which is how a whole corpus once fused into one object.
    """
    return _ordered(
        path
        for path in directory.glob("*.jsonl")
        if path.is_file() and not path.name.startswith("._")
    )


def _beneath(directory: Path) -> list[Path]:
    """Every transcript under ``directory``, nesting included, oldest first."""
    return _ordered(
        path
        for path in directory.rglob("*.jsonl")
        if path.is_file() and not path.name.startswith("._")
    )


def _ordered(paths: Iterable[Path]) -> list[Path]:
    """Sort transcripts oldest first.

    By write time, not by name: a session that resumed another is the one
    written later, and its name is a fresh id that sorts arbitrarily against
    the file it continues.
    """
    return sorted(paths, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _dropped(
    records: Sequence[SessionRecord], text: str, target: Format
) -> tuple[str, ...]:
    """Return the semantic records the target format could not carry.

    Measured, not predicted: the converted text is normalized again and its
    semantic record population compared to the source's. Provider-native
    metadata lives in ``extra`` and does not change the normalized meaning.
    """
    try:
        rebuilt = list(_adapter(target).normalize(StringIO(text)))
    except (TypeError, ValueError):
        return ()
    before = Counter(_semantic_record(record) for record in records)
    after = Counter(_semantic_record(record) for record in rebuilt)
    lost_by_type: Counter[str] = Counter()
    for (name, _), count in (before - after).items():
        lost_by_type[name] += count
    return tuple(f"{name}:{count}" for name, count in sorted(lost_by_type.items()))


def _semantic_record(record: object) -> tuple[str, str]:
    """Return a comparable record identity without provider-native metadata."""
    return type(record).__name__, repr(_semantic_value(record))


def _semantic_value(value: object) -> object:
    """Canonicalize container shapes and omit provider-native metadata."""
    if is_dataclass(value) and not isinstance(value, type):
        return tuple(
            (field.name, _semantic_value(getattr(value, field.name)))
            for field in fields(value)
            # ``context_id`` is an INDEX into the record stream (axiom 5), not
            # a value: a session carrying no ``TurnContext`` gains one when
            # written as claude, so every record's moves ``None`` -> ``0`` and
            # the comparison read a renumbering as the whole session lost.
            if field.name not in {"extra", "context_id"}
        )
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return tuple(
            sorted((repr(key), _semantic_value(item)) for key, item in mapping.items())
        )
    if isinstance(value, (list, tuple)):
        sequence = cast(list[object] | tuple[object, ...], value)
        return tuple(_semantic_value(item) for item in sequence)
    return value


def detect_format(native: str) -> Format:
    """Return the format that wrote ``native``, or an empty string.

    Args:
      native: Session file text.

    Returns:
      source: A member of :data:`FORMATS`, or ``""`` when unrecognized.

    """
    stripped = native.lstrip()
    # An ARRAY: the normalized form is the record stream itself, so a session
    # is the records and nothing wrapping them. It was an object while a
    # ``Session`` existed to hold metadata beside them, and the sniffer went on
    # requiring the brace -- which read every normalized document as no format
    # at all, so ``convert x.json --to codex`` refused a file it had written.
    if stripped.startswith("[") and '"py/object"' in stripped[:200]:
        return "json"
    # Gemini before the line walk: its document is ONE object, so its first
    # line is a fragment that parses as nothing and the walk would fall
    # through to "". Recognized by the pair of keys it always carries.
    if stripped.startswith("{"):
        try:
            document = DictCodec.coerce(json.loads(native))
        except json.JSONDecodeError:
            document = {}
        if "sessionId" in document and "messages" in document:
            return "gemini"
    for line in native.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        keys = set(DictCodec.coerce(record))
        if "payload" in keys:
            return "codex"
        if {"sessionId", "uuid", "parentUuid", "agentId"} & keys:
            return "claude"
    return ""


def _adapter(name: Format) -> _Adapter:
    """Return the read/write adapter for ``name``."""
    if name == "claude":
        return claude
    if name == "codex":
        return codex
    if name == "gemini":
        return gemini
    return normalized


class _Adapter(Protocol):
    """The stream interface every format adapter module provides.

    Both directions stream (axiom 11): ``normalize`` yields each record as its
    line lands, so a session still being written reads as far as it has been
    written, and ``denormalize`` consumes an iterable rather than a container.
    """

    def normalize(self, stream: TextIO, /) -> Iterator[SessionRecord]:
        """Yield the records a native stream states."""
        ...

    def denormalize(self, records: Iterable[SessionRecord], stream: TextIO, /) -> None:
        """Denormalize records to a native stream."""
        ...


def _add_arguments(
    parser: argparse.ArgumentParser, *, formats: tuple[Format, ...]
) -> None:
    """Register flags on ``parser``."""
    parser.add_argument(
        "command",
        choices=("convert", "verify"),
        help="convert writes the target format; verify checks exact recovery.",
    )
    parser.add_argument(
        "paths", nargs="+", type=Path, help="Session files or directories."
    )
    parser.add_argument(
        "--to", choices=formats, help="Output format (required by convert)."
    )
    parser.add_argument(
        "--source",
        choices=("auto", *formats),
        default="auto",
        help="Input format instead of detecting it (default: auto).",
    )
    parser.add_argument("-o", "--output", type=Path, help="Write to this file.")
    parser.add_argument(
        "--out-dir", type=Path, help="Write each converted file into this directory."
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=1,
        help="Worker processes; 0 uses every CPU (default: 1).",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Report format (default: text).",
    )
    parser.add_argument(
        "--lossy",
        action="store_true",
        help="Allow a conversion that drops records the target cannot express.",
    )
    parser.add_argument(
        "--fail-fast", action="store_true", help="Stop after the first failure."
    )
    parser.add_argument(
        "--diff", action="store_true", help="Show the first differing lines."
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Omit the summary line."
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Report every file."
    )


def _convert_all(
    paths: tuple[Path, ...],
    *,
    workers: int,
    source: Format,
    target: Format | None,
    want_diff: bool,
    fail_fast: bool,
    destination: Path | Literal[False] | None = None,
) -> list[FileResult]:
    """Convert every path, in worker processes when more than one is useful."""
    running = _workers(paths=len(paths), workers=workers)
    if running > 1:
        with ProcessPoolExecutor(
            max_workers=running,
            initializer=_die_with_parent,
            initargs=(os.getpid(),),
        ) as executor:
            return _collect(
                executor.map(
                    convert_file,
                    paths,
                    repeat(source),
                    repeat(target),
                    repeat(want_diff),
                    repeat(destination),
                ),
                fail_fast=fail_fast,
            )
    return _collect(
        (convert_file(path, source, target, want_diff, destination) for path in paths),
        fail_fast=fail_fast,
    )


def _die_with_parent(launcher: int) -> None:
    """Exit this worker when the run that started it goes away.

    A SIGKILLed parent runs no cleanup, and CPython leaves the pool behind
    (python/cpython#111873, open, reproduced on Linux and macOS). The workers
    are children of a FORKSERVER whose argv names neither this program nor its
    arguments, so ``pkill -f`` on the obvious pattern misses them and they keep
    whatever they were holding -- measured here at 14 GB across 11 orphans.

    The launcher's pid is passed as a VALUE because nothing about it is
    inherited: under the default ``forkserver`` start method a worker's
    ``getppid()`` is the forkserver, and a pipe fd handed to the initializer
    reads EOF immediately, since the worker was forked from the forkserver
    rather than from the process holding the write end. Both were measured
    before this was written.

    Polling, not signalling: ``prctl(PR_SET_PDEATHSIG)`` is Linux-only and this
    runs on macOS too. The cost is one ``kill(pid, 0)`` per second per worker.
    """

    def _wait() -> None:
        while True:
            time.sleep(1.0)
            try:
                os.kill(launcher, 0)
            except OSError:
                # Gone, or no longer ours to signal. Either way this worker
                # has no one to report to.
                os._exit(1)

    thread = threading.Thread(target=_wait, daemon=True)
    thread.start()


def _workers(*, paths: int, workers: int) -> int:
    """How many processes to run, for ``paths`` sessions and ``workers`` asked.

    Sized by the SESSION count, so a bug that miscounts sessions also turns
    parallelism off -- which is how one 21 GB serial run went unnoticed.
    """
    return max(1, min(paths, workers or os.cpu_count() or 1))


def _collect(compared: Iterable[FileResult], *, fail_fast: bool) -> list[FileResult]:
    """Gather results, stopping early when ``fail_fast`` and one fails."""
    results: list[FileResult] = []
    for result in compared:
        results.append(result)
        if fail_fast and not result.ok:
            break
    return results


def _session_files(paths: Sequence[Path]) -> Iterator[Path]:
    """Yield one path per SESSION, not per file.

    A named DIRECTORY is one session, whatever it holds: that is the unit,
    and it is what makes ``/clear`` recoverable, since the transcript claude
    opens to answer it names nothing but sits in the same directory.

    A named FILE is one session too. Walking a tree yields the innermost
    directories that hold transcripts -- a project directory holds one
    session per conversation, not one per file.
    """
    seen: set[Path] = set()
    for path in paths:
        expanded = path.expanduser()
        for candidate in _roots(expanded):
            if candidate not in seen:
                seen.add(candidate)
                yield candidate


def _roots(path: Path) -> list[Path]:
    """Return the session roots ``path`` names.

    A directory named outright IS one session -- that is the promise of
    pointing at it. Walking a TREE cannot assume the same: a claude project
    directory holds one transcript per conversation, so each is its own
    session, minus the ones another session already claims as its parts.
    """
    if not path.exists():
        return []
    if not path.is_dir():
        return [path]
    if _transcripts(path):
        return [path]
    # ``.json`` too: a gemini session is ONE object rather than a line stream,
    # so a walk that globbed only ``*.jsonl`` reported a directory of them as
    # holding no sessions at all. Not in ``_transcripts``, which asks whether a
    # directory's files are one session -- two gemini documents are two.
    #
    # Sniffed rather than taken on the extension, unlike ``.jsonl``: claude
    # keeps its own sidecars beside the transcripts -- ``sessions-index.json``,
    # ``agent-<id>.meta.json`` -- and claiming those reported 46 files as
    # sessions that failed to convert because they never were any.
    found = sorted(
        candidate
        for pattern in ("*.jsonl", "*.json")
        for candidate in path.rglob(pattern)
        if candidate.is_file()
        and not candidate.name.startswith("._")
        and (candidate.suffix == ".jsonl" or _sniff(candidate))
    )
    claimed = {part for root in found for part in _parts_of(root)[1:]}
    return [root for root in found if root not in claimed]


def _write(
    results: Sequence[FileResult],
    *,
    output: Path | None,
    out_dir: Path | None,
    stream: TextIO,
    suffixes: Mapping[Format, str] = MappingProxyType(
        {
            "claude": ".jsonl",
            "codex": ".jsonl",
            "gemini": ".json",
            "json": ".json",
        }
    ),
) -> None:
    """Write converted text to the chosen destination."""
    converted = [r for r in results if r.ok and r.target is not None]
    if not converted:
        return
    if output is not None:
        # Only when the run actually produced text. ``verify`` keeps none --
        # it asks whether the bytes agree, not for a copy of them -- so an
        # unguarded write emptied whatever ``-o`` named, destroying a file the
        # command never claimed to touch.
        if converted[0].text:
            output.write_text(converted[0].text, encoding="utf-8")
        return
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        for result in converted:
            if not result.text and not result.parts:
                # Already streamed to disk by the conversion itself, which is
                # what leaves a result with neither text nor parts. Skipping on
                # an EMPTY text instead swallowed a target whose rendering is
                # legitimately empty, so a conversion produced no file at all
                # while the run reported success.
                continue
            if result.parts:
                # A session spread over several files goes back as several:
                # its own name for each, so the directory is reproduced.
                for name, text in result.parts:
                    (out_dir / name).write_text(text, encoding="utf-8")
                continue
            suffix = suffixes[result.target or "json"]
            (out_dir / f"{result.path.stem}{suffix}").write_text(
                result.text, encoding="utf-8"
            )
        return
    # No destination and a single input: stdout is the conversion's output.
    if len(converted) == 1 and converted[0].target != converted[0].source:
        stream.write(converted[0].text)


def _passed(results: Sequence[FileResult], *, verifying: bool) -> bool:
    """Return whether the run met its success condition."""
    if verifying:
        return all(result.ok and result.byte_exact for result in results)
    return all(result.ok for result in results)


def _diff(native: str, rebuilt: str) -> str:
    """Return a short unified diff of the first differing lines."""
    lines = unified_diff(
        native.splitlines(),
        rebuilt.splitlines(),
        fromfile="original",
        tofile="converted",
        lineterm="",
        n=0,
    )
    return "\n".join(line[:200] for line in list(lines)[:12])


def _report(
    results: Sequence[FileResult],
    *,
    output_format: str,
    quiet: bool,
    verbose: bool,
    verifying: bool,
) -> None:
    """Print the run report to stderr, leaving stdout for converted text."""
    if output_format == "json":
        print(  # noqa: T201 -- CLI report.
            json.dumps(
                {
                    "files": len(results),
                    "ok": sum(_ok(r, verifying=verifying) for r in results),
                    "results": [_as_json(r) for r in results],
                }
            ),
            file=sys.stderr,
        )
        return
    for result in results:
        if _ok(result, verifying=verifying) and not verbose:
            continue
        print(  # noqa: T201 -- CLI report.
            f"{result.path}: {_status(result, verifying=verifying)}", file=sys.stderr
        )
        if result.diff:
            print(result.diff, file=sys.stderr)  # noqa: T201 -- CLI report.
    if not quiet:
        good = sum(_ok(r, verifying=verifying) for r in results)
        label = "exact" if verifying else "converted"
        source_bytes = sum(r.source_bytes for r in results)
        output_bytes = sum(r.output_bytes for r in results)
        print(  # noqa: T201 -- CLI report.
            f"{good}/{len(results)} {label}; "
            f"{source_bytes} bytes in, {output_bytes} out "
            f"({_ratio(source_bytes, output_bytes)})",
            file=sys.stderr,
        )


def _ok(result: FileResult, *, verifying: bool) -> bool:
    """Return whether one result met the run's success condition."""
    return result.ok and (result.byte_exact or not verifying)


def _as_json(result: FileResult) -> dict[str, object]:
    """Return one result as a JSON-serializable mapping."""
    return {
        "path": str(result.path),
        "source": result.source,
        "target": result.target,
        "byte_exact": result.byte_exact,
        "source_bytes": result.source_bytes,
        "output_bytes": result.output_bytes,
        "dropped": list(result.dropped),
        "error": result.error,
    }


def _sized(result: FileResult) -> str:
    """Return one result's source and output sizes, with their ratio."""
    return (
        f"{result.source_bytes} -> {result.output_bytes} bytes, "
        f"{_ratio(result.source_bytes, result.output_bytes)}"
    )


def _ratio(source_bytes: int, output_bytes: int) -> str:
    """Return output size as a multiple of source size."""
    return f"{output_bytes / source_bytes:.4f}x" if source_bytes else "n/a"


def _status(result: FileResult, *, verifying: bool) -> str:
    """Return a short status for ``result``."""
    if result.error is not None:
        return result.error
    if verifying and not result.byte_exact:
        return f"not byte-exact ({_sized(result)})"
    if result.dropped:
        return f"{result.source} -> {result.target} (drops {', '.join(result.dropped)})"
    return f"{result.source} -> {result.target}"
