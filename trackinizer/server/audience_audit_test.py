"""The guard that finds a reader of session content nobody gated.

A session says who may read its transcript, so every function that reads or writes
the tables holding it must say how it is gated. Three tests hold that line. The
first parses the whole ``trackinizer`` package, wire and client included, and names
every function with SQL on one of those tables that carries neither
``# Audience: gated`` (a route or caller checks the viewer's audience before it
runs) nor ``# Audience: internal`` (it has no caller-visible output: a sweep, a
maintenance run, a fragment its caller completes). The second names every server
function that calls a store method on session content without calling
``require_session_access``, because the marker on the store method promises a check
nothing else enforces. The third lists every route that reaches session content in
the registry the wire package keeps, so a new route has to be placed there, where a
review asks which check it carries. A fourth holds the aggregate readers (feed,
search, record filters, export) to ``gated``: each filters by its caller's audiences,
so none may be passed off as ``internal``. A fifth fails a function that stores
session content and queues a notification without the session's audience, because a
live stream holds a ping back from a viewer only by the audience the ping names.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import ast
import bisect
import dataclasses
import functools
import gc
import re

from fastapi import FastAPI

import pytest

from trackinizer.server import web
from trackinizer.server.api.app import app
from trackinizer.server.route_iter import iter_routes
from trackinizer.wire.audience import SESSION_CONTENT_ROUTES


_CWD: Final = Path(__file__).resolve().parent

if TYPE_CHECKING:
    from collections.abc import Iterator


_SERVER: Final = _CWD
_TRACKINIZER: Final = _SERVER.parent

_TABLES: Final = (
    "session_records",
    "session_manifests",
    "session_slash_commands",
    "session_embeddings",
    "session_ciphertext",
    "session_bodies",
)

_TABLE_NAME: Final = re.compile("|".join(_TABLES))

# A SQL keyword in any case, then a session table (bare, quoted, or under ``public``)
# or a name only known at run time: ``\0`` from a run-time part of an f-string or
# concatenation, ``{name}`` for ``str.format`` and ``%s`` for ``%``. Only string
# constants are searched, which already leaves prose in comments out. A table in a
# variable is flagged because it may be any of these.
_NAMES_A_TABLE: Final = re.compile(
    r"\b(?<!DISTINCT )(?:FROM|JOIN|INTO|UPDATE)\s+(?:ONLY\s+)?"
    r'(?:"?public"?\.)?(?:"?(?:' + "|".join(_TABLES) + r')"?|\0|\{\w*\}|%s)',
    re.IGNORECASE,
)
# A line where a keyword ends a string and the table follows from a variable, which
# the table names above cannot find. It only chooses which functions to walk; the
# walk itself decides.
_DYNAMIC_TABLE: Final = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s*(?:\{|%s|[\"']\s*(?:[,)+]|$))",
    re.IGNORECASE,
)
# An unindented line that opens a function, a class or a decorator.
_CUT: Final = re.compile(r"^(?:@|(?:async[ \t]+)?def\b|class\b)", re.MULTILINE)
# The store methods that read or write a session's content for any id they are given.
_CONTENT_METHODS: Final = frozenset(
    {
        "append_session_records",
        "read_recent_session_turns",
        "read_session_manifests",
        "read_session_records",
        "read_session_slash_commands",
        "upsert_session_manifest",
    },
)
_SQL_VERB: Final = re.compile(
    r"\b(?:select|insert|update|delete|with)\b",
    re.IGNORECASE,
)
_MARKER: Final = re.compile(r"#\s*Audience:\s*(?:gated|internal)\b")
_GATED: Final = re.compile(r"#\s*Audience:\s*gated\b")

# The aggregate readers: each returns or counts what many sessions hold, and filters
# by the audiences its caller reads. They are gated, never internal, and
# ``audience_aggregates_pglite_test`` reads through each as a caller that reads only
# ``open`` and as one that reads ``staff`` too. A reader added to the audit without a
# marker fails the first test below; one listed here fails the last test if it is
# renamed, removed or marked internal.
_ENFORCED_AGGREGATES: Final = frozenset(
    {
        ("server/store/export.py", "_read_table"),
        ("server/store/read.py", "_record_texts"),
        ("server/store/session.py", "_SessionMixin.read_feed"),
        ("server/store/session_feed.py", "_SessionFeedMixin.read_feed_facets"),
        ("server/store/session_feed.py", "_SessionFeedMixin.read_feed_histogram"),
        ("server/store/session_feed.py", "_count_buckets"),
        ("server/store/session_search.py", "_fts_arm"),
        ("server/store/session_search.py", "_semantic_arm"),
        # The SQL-lowered form of a record filter: an EXISTS over the records of
        # ``inquiries.id``, which would let a viewer probe a hidden transcript were
        # ``list_kind`` not to keep hidden sessions out of the rows it meets.
        ("wire/column_shapes.py", "_RECORDS_WHERE"),
        # Not an aggregate but a probe of one transcript: whether a fork point is a
        # line. Its yes or no was the answer to a question about a hidden session,
        # and a yes let the assistant copy that session's lines into a fork.
        ("server/visuals/science_chats.py", "science_chat_exists_at"),
        # Whether anyone but a chat's starter posted in it, which lets the starter
        # delete it: read only off the sessions the caller reads, a hidden one
        # counting as someone else's.
        ("server/visuals/science_chats.py", "_HEAD"),
        # A writer whose commit pings every live stream. The ping names the
        # session's audience, so a stream holds it back from a viewer who does not
        # read it; ``untagged_notifiers`` fails a writer that pings without one.
        ("server/store/session_ir.py", "_SessionIRMixin.append_session_records"),
    },
)

_UNGATED: Final = """
class Reader:
    async def read(self, conn):
        return await conn.fetch("SELECT text FROM session_records WHERE idx = 0")
"""


_PREFIXES: Final = (
    "/api/sessions/",
    "/api/web/feed",
    "/api/messages",
    "/api/terminals",
)
_EXACT: Final = (
    "/api/web/search_sessions",
    "/api/inquiries",
    "/api/export",
    "/api/machines/{machine_id}/terminals/wait",
)


def unmarked_readers(source: str) -> list[str]:
    """Return the qualified name of each unmarked function with SQL on a session table.

    Args:
      source: A module's text.

    Returns:
      names: ``Class.method`` or ``function``, the name a module-level assignment
        binds, or ``<line N>`` for any other module-level statement, in source
        order. A function counts as marked when ``# Audience: gated`` or
        ``# Audience: internal`` is a comment inside it or in the comment block
        directly above it. A function nested in another is read as part of the
        outer one.

    """
    return [reader.name for reader in _classified(source) if not reader.marked]


def gated_readers(source: str) -> list[str]:
    """Return the qualified name of each reader marked ``# Audience: gated``.

    A reader marked ``# Audience: internal`` is not one: it says nothing it returns
    reaches a caller, which is false of an aggregate.

    Args:
      source: A module's text.

    Returns:
      names: Named as :func:`unmarked_readers` names them, in source order.

    """
    return [reader.name for reader in _classified(source) if reader.gated]


def unchecked_callers(source: str) -> list[str]:
    """Return each function that calls a store method on session content unchecked.

    The store methods answer for any session id, so the function that calls one has
    to decide whether the caller may read that session. It does when it calls
    ``require_session_access`` (or says why nothing reaches a caller with
    ``# Audience: internal``). Without this a new route that forgets the check is
    invisible to the SQL audit, because the marker sits on the store method.

    Args:
      source: A module's text.

    Returns:
      names: ``Class.method`` or ``function`` of each unchecked caller, in source
        order.

    """
    if not any(reader in source for reader in _CONTENT_METHODS):
        return []
    lines = source.split("\n")
    return [
        name
        for scope, name in _scopes(ast.parse(source).body, prefix="")
        if _calls(scope, _CONTENT_METHODS)
        and not _calls(scope, frozenset({"require_session_access"}))
        and not _is_marked(scope, lines)
    ]


def untagged_notifiers(source: str) -> list[str]:
    """Return each function with SQL on a session table that pings without an audience.

    A function that stores session content and then queues a notification wakes every
    live stream. A stream holds a ping back from a viewer who does not read the
    session only when the ping names the session's audience, so a writer that leaves
    it out tells every viewer when a hidden session is written.

    Args:
      source: A module's text.

    Returns:
      names: Named as :func:`unmarked_readers` names them, in source order.

    """
    return [reader.name for reader in _classified(source) if reader.pings_bare]


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class _Reader:
    """What the audits ask of one scope with SQL on a session table."""

    name: str
    marked: bool
    gated: bool
    pings_bare: bool


# Cached by text so the audits that ask of the same module (unmarked, gated, untagged)
# parse and walk it once between them. The cache keeps the answers, not the trees: a
# tree held across modules makes the collector rescan every one.
@functools.cache
def _classified(source: str) -> tuple[_Reader, ...]:
    """Return each scope of ``source`` with SQL on a session table, classified."""
    lines = source.split("\n")
    # Only a scope with a line that could name a table is walked: most of a module
    # does not, and walking all of it dominates the cost of the audit.
    mentions = [
        number
        for number, line in enumerate(lines, start=1)
        if _could_name_a_table(line)
    ]
    if not mentions:
        return ()
    notifies = "_buffer_notification" in source
    return tuple(
        _Reader(
            name=name,
            marked=_is_marked(scope, lines),
            gated=_is_marked(scope, lines, marker=_GATED),
            pings_bare=notifies and _pings_bare(scope),
        )
        for scope, name in _scopes(_body_near(source, mentions), prefix="")
        if _has_a_mention(mentions, scope) and _reads_a_table(scope)
    )


# ``source`` is cut ahead of each unindented ``def``, ``class`` or decorator, and only a
# piece with a mention (a line number of ``mentions``, ascending) is parsed: most of a
# module has none, and parsing it dominates the cost of the audit. A cut inside a
# statement leaves a piece that does not parse, so this falls back to the whole module
# rather than read a wrong one.
def _body_near(source: str, mentions: list[int]) -> list[ast.stmt]:
    """Return the module-level statements of ``source`` that may hold a mention."""
    cuts = _cuts(source)
    ends = [*cuts[1:], (len(source), len(source))]
    body: list[ast.stmt] = []
    try:
        for (start, first), (stop, after) in zip(cuts, ends, strict=True):
            at = bisect.bisect_left(mentions, first)
            if at < len(mentions) and mentions[at] < after:
                # Blank lines ahead keep each statement on its line of ``source``.
                body.extend(ast.parse("\n" * (first - 1) + source[start:stop]).body)
    except SyntaxError:
        return ast.parse(source).body
    return body


def _cuts(source: str) -> list[tuple[int, int]]:
    """Return the offset and line where each piece of ``source`` starts."""
    cuts = [(0, 1)]
    line = 1
    seen = 0
    decorated = -2
    for found in _CUT.finditer(source):
        line += source.count("\n", seen, found.start())
        seen = found.start()
        if line != decorated + 1 and found.start():
            cuts.append((found.start(), line))
        decorated = line if found.group() == "@" else -2
    return cuts


def _pings_bare(scope: ast.stmt) -> bool:
    """Say whether ``scope`` queues a notification that names no audience."""
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_buffer_notification"
        and all(keyword.arg != "audience" for keyword in node.keywords)
        for node in ast.walk(scope)
    )


def _calls(scope: ast.stmt, names: frozenset[str]) -> bool:
    """Say whether ``scope`` calls a function or method called one of ``names``."""
    return any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr in names)
            or (isinstance(node.func, ast.Name) and node.func.id in names)
        )
        for node in ast.walk(scope)
    )


# A table name in a comment is prose, so a line must hold a quote as well.
def _could_name_a_table(line: str) -> bool:
    """Say whether a source line may hold SQL naming a session table."""
    return ('"' in line or "'" in line) and (
        _TABLE_NAME.search(line) is not None or _DYNAMIC_TABLE.search(line) is not None
    )


def _has_a_mention(mentions: list[int], scope: ast.stmt) -> bool:
    """Say whether any line of ``mentions`` (ascending) falls inside ``scope``."""
    at = bisect.bisect_left(mentions, _first_line(scope))
    return at < len(mentions) and mentions[at] <= (scope.end_lineno or 0)


def _scopes(body: list[ast.stmt], *, prefix: str) -> Iterator[tuple[ast.stmt, str]]:
    """Yield each function (with its classes) and each other module-level statement."""
    for statement in body:
        match statement:
            case ast.ClassDef(name=name, body=members):
                yield from _scopes(members, prefix=f"{prefix}{name}.")
            case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
                yield statement, f"{prefix}{name}"
            case (
                ast.Assign(targets=[ast.Name(id=name)])
                | ast.AnnAssign(
                    target=ast.Name(id=name),
                )
            ):
                yield statement, name
            case _:
                yield statement, f"<line {statement.lineno}>"


def _first_line(scope: ast.stmt) -> int:
    """Return the line a scope starts on, counting its decorators."""
    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return min([scope.lineno, *(d.lineno for d in scope.decorator_list)])
    return scope.lineno


def _reads_a_table(scope: ast.stmt) -> bool:
    """Say whether any string expression inside ``scope`` names a session table."""
    parents: dict[ast.AST, ast.AST] = {}
    nodes: list[ast.AST] = [scope]
    for node in nodes:
        for child in ast.iter_child_nodes(node):
            parents[child] = node
            nodes.append(child)
    return any(_sql_text(node, parents) for node in nodes)


def _sql_text(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Say whether ``node`` is a string expression naming a session table in SQL."""
    if not isinstance(node, ast.Constant | ast.JoinedStr | ast.BinOp | ast.Call):
        return False
    # A docstring describes SQL; it does not run it. A part of a larger string
    # expression is read through that expression.
    if _is_a_docstring_or_part(parents.get(node)):
        return False
    text = _render(node)
    found = _NAMES_A_TABLE.search(text)
    if found is None:
        return False
    # Lower case counts only in a string that also holds a statement verb, so that
    # prose such as "stored from session_records" is still not a reader.
    return found.group().split()[0].isupper() or _SQL_VERB.search(text) is not None


def _is_a_docstring_or_part(parent: ast.AST | None) -> bool:
    """Say whether a string node under ``parent`` is not read on its own."""
    match parent:
        case ast.Expr() | ast.JoinedStr() | ast.FormattedValue():
            return True
        case ast.BinOp(op=ast.Add()):
            return True
        case ast.Call(func=ast.Name(id="vetted_sql")):
            return True
        case _:
            return False


def _render(node: ast.AST) -> str:
    r"""Concatenate the text of a string expression; a run-time part is ``\\0``."""
    match node:
        case ast.Constant(value=str() as text):
            return text
        case ast.JoinedStr(values=parts):
            return "".join(_render(part) for part in parts)
        case ast.BinOp(left=left, op=ast.Add(), right=right):
            return _render(left) + _render(right)
        case ast.Call(func=ast.Name(id="vetted_sql"), args=args):
            return "".join(_render(arg) for arg in args)
        case _:
            return "\0"


def _is_marked(
    scope: ast.stmt,
    lines: list[str],
    *,
    marker: re.Pattern[str] = _MARKER,
) -> bool:
    """Say whether the scope, or the comment block above it, carries ``marker``."""
    above = _first_line(scope) - 1
    while above >= 1 and lines[above - 1].lstrip().startswith("#"):
        above -= 1
    span = lines[above : (scope.end_lineno or scope.lineno)]
    return any(marker.search(line) for line in span)


def _modules(root: Path) -> Iterator[Path]:
    """Yield each non-test Python module under ``root``."""
    for path in sorted(root.rglob("*.py")):
        if path.name.endswith("_test.py") or path.name == "conftest.py":
            continue
        if "_test_support" in path.name:
            continue
        yield path


# The whole package is read, not the server alone: the wire package
# lowers a record filter to SQL over the same tables.
@functools.cache
def _repository_scopes() -> tuple[tuple[str, _Reader], ...]:
    """Return ``(path, reader)`` for each scope with SQL on a session table."""
    # The trees die with each module; the collector would rescan the live ones
    # again and again, which is half the cost of the audit.
    gc.disable()
    try:
        return tuple(
            (path.relative_to(_TRACKINIZER).as_posix(), reader)
            for path in _modules(_TRACKINIZER)
            for reader in _classified(path.read_text(encoding="utf-8"))
        )
    finally:
        gc.enable()


def _repository_readers() -> frozenset[tuple[str, str]]:
    """Return ``(path, name)`` for each unmarked reader anywhere in the package."""
    return frozenset(
        (path, reader.name)
        for path, reader in _repository_scopes()
        if not reader.marked
    )


def _repository_gated() -> frozenset[tuple[str, str]]:
    """Return ``(path, name)`` for each reader marked gated anywhere in the package."""
    return frozenset(
        (path, reader.name) for path, reader in _repository_scopes() if reader.gated
    )


def _repository_untagged() -> frozenset[tuple[str, str]]:
    """Return ``(path, name)`` for each session writer in the server that pings bare."""
    server = _SERVER.relative_to(_TRACKINIZER).as_posix() + "/"
    return frozenset(
        (path, reader.name)
        for path, reader in _repository_scopes()
        if reader.pings_bare and path.startswith(server)
    )


@functools.cache
def _repository_callers() -> frozenset[tuple[str, str]]:
    """Return ``(path, name)`` for each server caller of a content method unchecked."""
    found: set[tuple[str, str]] = set()
    for path in _modules(_SERVER):
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(_TRACKINIZER).as_posix()
        found.update((relative, name) for name in unchecked_callers(source))
    return frozenset(found)


def test_every_function_on_session_tables_says_how_it_is_gated() -> None:
    unclassified = _repository_readers()

    assert not unclassified, (
        "functions with SQL on a session-content table and no marker; add "
        "'# Audience: gated' if the caller checks the viewer's audience, or "
        "'# Audience: internal' if nothing they return reaches a caller:\n"
        + "\n".join(f"  {path}  {name}" for path, name in sorted(unclassified))
    )


def test_every_enforced_aggregate_is_still_a_reader_marked_gated() -> None:
    lost = _ENFORCED_AGGREGATES - _repository_gated()

    assert not lost, (
        "aggregate readers renamed, removed or no longer marked '# Audience: gated' "
        "(an aggregate returns what many sessions hold, so 'internal' is false of it):\n"
        + "\n".join(f"  {path}  {name}" for path, name in sorted(lost))
    )


def test_every_session_writer_that_pings_a_stream_names_the_audience() -> None:
    bare = _repository_untagged()

    assert not bare, (
        "functions that store session content and queue a notification without "
        "audience=: a stream cannot hold the ping back from a viewer who does not "
        "read the session:\n"
        + "\n".join(f"  {path}  {name}" for path, name in sorted(bare))
    )


_NOTIFYING: Final = """
class Writer:
    async def write(self, conn, session_id, audience):
        await conn.execute("INSERT INTO session_records (session_id) VALUES ($1)", session_id)
        self._buffer_notification(session_id{audience})

    async def touch(self, session_id):
        self._buffer_notification(session_id)
"""


def test_a_session_writer_that_pings_without_the_audience_is_named() -> None:
    bare = _NOTIFYING.format(audience="")
    named = _NOTIFYING.format(audience=", audience=audience")

    # ``touch`` pings too but stores no session content, so it is not a session writer.
    assert untagged_notifiers(bare) == ["Writer.write"]
    assert untagged_notifiers(named) == []


def test_the_audit_names_a_reader_nobody_gated() -> None:
    assert unmarked_readers(_UNGATED) == ["Reader.read"]


def test_only_a_gated_marker_makes_a_reader_gated() -> None:
    gated = _UNGATED.replace(
        "    async def read",
        "    # Audience: gated -- the caller filters.\n    async def read",
    )
    internal = _UNGATED.replace(
        "    async def read",
        "    # Audience: internal -- a sweep.\n    async def read",
    )

    assert gated_readers(gated) == ["Reader.read"]
    assert gated_readers(internal) == []
    assert gated_readers(_UNGATED) == []


def test_a_marker_inside_or_above_the_function_gates_it() -> None:
    inside = _UNGATED.replace(
        "        return await",
        "        # Audience: gated -- the route checks.\n        return await",
    )
    above = _UNGATED.replace(
        "    async def read",
        "    # Audience: internal -- a sweep.\n    async def read",
    )

    assert unmarked_readers(inside) == []
    assert unmarked_readers(above) == []


def test_the_audit_sees_through_concatenation_vetted_sql_and_f_strings() -> None:
    source = (
        "def a(c):\n"
        '    c.execute("DELETE FROM " + "session_ciphertext")\n'
        "def b(c):\n"
        '    c.execute(vetted_sql("SELECT 1 JOIN ", "session_manifests m"))\n'
        "def c(c, table):\n"
        '    c.execute(f"SELECT 1 FROM {table}")\n'
        "def d(c):\n"
        '    c.execute("SELECT 1 FROM inquiries")\n'
    )

    assert unmarked_readers(source) == ["a", "b", "c"]


@pytest.mark.parametrize(
    "statement",
    [
        'c.execute("SELECT text FROM {t}".format(t=table))',
        'c.execute("SELECT text FROM %s" % table)',
        'c.execute("SELECT text FROM public.session_records")',
        "c.execute('SELECT text FROM \"session_records\"')",
        'c.execute("select text from session_records")',
        'c.execute("select text from \\"public\\".\\"session_manifests\\"")',
    ],
)
def test_the_audit_sees_every_spelling_of_a_session_table(statement: str) -> None:
    source = f"def a(c, table):\n    {statement}\n"

    assert unmarked_readers(source) == ["a"]


def test_distinct_from_is_a_comparison_not_a_table() -> None:
    source = 'def a():\n    return "x IS DISTINCT FROM {p}"\n'

    assert unmarked_readers(source) == []


def test_a_docstring_or_prose_about_a_table_is_not_a_reader() -> None:
    source = (
        "def a():\n"
        '    """Reads FROM session_records for the caller."""\n'
        '    return "stored from session_records"\n'
    )

    assert unmarked_readers(source) == []


def test_a_module_level_statement_is_gated_by_the_comment_above_it() -> None:
    source = (
        "# Audience: internal -- no content leaves.\n"
        '_SQL = "SELECT 1 FROM session_records"\n'
        '_OTHER = "SELECT 1 FROM session_manifests"\n'
    )

    assert unmarked_readers(source) == ["_OTHER"]


_ROUTE: Final = """
async def route(store, identity, session_id):
    session = await require_session(store, session_id)
    {check}
    return await store.read_session_records(session_id, part=0)
"""


def test_a_caller_of_a_content_method_without_an_access_check_is_named() -> None:
    unchecked = _ROUTE.format(check="pass")
    checked = _ROUTE.format(
        check="require_session_access(identity, session=session, mode='read')",
    )
    internal = _ROUTE.format(check="pass").replace(
        "    return await",
        "    # Audience: internal -- a maintenance run.\n    return await",
    )

    assert unchecked_callers(unchecked) == ["route"]
    assert unchecked_callers(checked) == []
    assert unchecked_callers(internal) == []


def test_every_server_caller_of_a_content_method_checks_access() -> None:
    callers = _repository_callers()

    assert not callers, (
        "functions that call a store method on session content without "
        "require_session_access; add the check, or '# Audience: internal' if "
        "nothing they return reaches a caller:\n"
        + "\n".join(f"  {path}  {name}" for path, name in sorted(callers))
    )


def _session_content_routes() -> set[tuple[str, str]]:
    """Return the ``(method, path)`` of each route the full app serves on the paths."""
    # The feed and search routes live on the web surface, the rest on the API app;
    # attach both, as the live server does.
    full_app = FastAPI()
    for route in app.routes:
        full_app.router.routes.append(route)
    web.attach(full_app)
    return {
        (method, path)
        for path, methods in iter_routes(full_app)
        if path.startswith(_PREFIXES) or path in _EXACT
        for method in methods
        if method not in {"HEAD", "OPTIONS"}
    }


def test_every_route_that_reaches_session_content_is_registered() -> None:
    served = _session_content_routes()

    assert served - SESSION_CONTENT_ROUTES == set(), (
        "routes on a session-content path missing from SESSION_CONTENT_ROUTES in "
        "wire/audience.py; add each and say which audience check it carries"
    )
    assert SESSION_CONTENT_ROUTES - served == set(), (
        "registered routes that do not exist"
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
