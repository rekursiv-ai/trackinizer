"""Grammar-example regression test.

GRAMMAR.md is the source of truth for what ``trax`` accepts. Every
fenced ``trax`` block must parse and execute against ``FakeClient``;
every fenced ``trax! Exxx`` block must raise ``ClientError``. CI fails
on any drift between the doc and the parser.

This test enforces rules 1 and 2 from GRAMMAR.md section 14. Rules 3-5
(token-table sync, production coverage, help-text derivation) gate at
other layers and are tested elsewhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast, get_args

import re
import shlex

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax import (
    cli,
    grammar as g,
)
from trackinizer.trax.conftest import FakeClient
from trackinizer.trax.grammar import (
    _FIELDS,
    WRITE_FIELDS_CLI,
    _coerce_judgement,
    _coerce_priority,
    _coerce_publication_type,
    _coerce_status,
    cost_key,
    field_value,
    is_issue_kind,
    validate_writable_fields,
)
from trackinizer.types.columns import flat_column_specs
from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import KIND_TO_CLASS, Inquiry
from trackinizer.wire.filters import FILTER_OPS


_GRAMMAR_PATH = Path(__file__).resolve().parent / "docs" / "GRAMMAR.md"

_EXAMPLE_PATTERN = re.compile(
    r"^```trax\n(.*?)^```$",
    re.MULTILINE | re.DOTALL,
)
_SEQUENCE_PATTERN = re.compile(
    r"^```trax-seq\n(.*?)^```$",
    re.MULTILINE | re.DOTALL,
)
_COUNTEREXAMPLE_PATTERN = re.compile(
    r"^```trax!\s+(E\d+)\n(.*?)^```$",
    re.MULTILINE | re.DOTALL,
)


def _split_command(body: str, *, line_no: int) -> list[str]:
    """Split a fenced example body into argv, dropping the leading ``trax``."""
    tokens = shlex.split(body)
    if not tokens or tokens[0] != "trax":
        raise AssertionError(
            f"GRAMMAR.md:{line_no}: example must start with 'trax'; got {body!r}"
        )
    return tokens[1:]


def _examples() -> list[tuple[int, list[str]]]:
    out: list[tuple[int, list[str]]] = []
    text = _GRAMMAR_PATH.read_text(encoding="utf-8")
    for match in _EXAMPLE_PATTERN.finditer(text):
        block_line = text[: match.start()].count("\n") + 1
        for offset, line in enumerate(match.group(1).splitlines()):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            out.append(
                (
                    block_line + 1 + offset,
                    _split_command(stripped, line_no=block_line + 1 + offset),
                )
            )
    return out


def _counterexamples() -> list[tuple[int, str, list[str]]]:
    out: list[tuple[int, str, list[str]]] = []
    text = _GRAMMAR_PATH.read_text(encoding="utf-8")
    for match in _COUNTEREXAMPLE_PATTERN.finditer(text):
        block_line = text[: match.start()].count("\n") + 1
        code = match.group(1)
        for offset, line in enumerate(match.group(2).splitlines()):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            out.append(
                (
                    block_line + 1 + offset,
                    code,
                    _split_command(stripped, line_no=block_line + 1 + offset),
                )
            )
    return out


def _sequences() -> list[tuple[int, list[list[str]]]]:
    """Extract ``trax-seq`` blocks: every line runs in order with shared state.

    Useful for examples that depend on prior commands (e.g. ``profile
    prod url ...`` must run before ``profile prod del``). The whole
    sequence is asserted to parse and execute without error.
    """
    out: list[tuple[int, list[list[str]]]] = []
    text = _GRAMMAR_PATH.read_text(encoding="utf-8")
    for match in _SEQUENCE_PATTERN.finditer(text):
        block_line = text[: match.start()].count("\n") + 1
        commands: list[list[str]] = []
        for offset, line in enumerate(match.group(1).splitlines()):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            commands.append(_split_command(stripped, line_no=block_line + 1 + offset))
        if commands:
            out.append((block_line, commands))
    return out


_EXAMPLES = _examples()
_SEQUENCES = _sequences()
_COUNTEREXAMPLES = _counterexamples()


# Examples that ship in GRAMMAR.md ahead of their parser/runtime support.
# Each entry maps a joined argv string to the refactor step that lands the
# feature. When a step lands, remove the corresponding entry and re-run --
# strict xfail forces the removal (xpassed tests fail strictly).
_KNOWN_FAILING_EXAMPLES: dict[str, str] = {}

_KNOWN_FAILING_COUNTEREXAMPLES: dict[str, str] = {}


def _xfail_mark(reason: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(reason=reason, strict=True, raises=BaseException)


def _example_params() -> list[Any]:
    out: list[Any] = []
    for line_no, argv in _EXAMPLES:
        key = " ".join(argv)
        if key in _KNOWN_FAILING_EXAMPLES:
            out.append(
                pytest.param(
                    line_no,
                    argv,
                    marks=_xfail_mark(_KNOWN_FAILING_EXAMPLES[key]),
                )
            )
        else:
            out.append(pytest.param(line_no, argv))
    return out


def _counterexample_params() -> list[Any]:
    out: list[Any] = []
    for line_no, code, argv in _COUNTEREXAMPLES:
        key = " ".join(argv)
        if key in _KNOWN_FAILING_COUNTEREXAMPLES:
            out.append(
                pytest.param(
                    line_no,
                    code,
                    argv,
                    marks=_xfail_mark(_KNOWN_FAILING_COUNTEREXAMPLES[key]),
                )
            )
        else:
            out.append(pytest.param(line_no, code, argv))
    return out


@pytest.mark.parametrize(
    ("line_no", "argv"),
    _example_params(),
    ids=[f"L{line_no}:{' '.join(argv) or '(bare)'}" for line_no, argv in _EXAMPLES],
)
def test_grammar_example_parses(line_no: int, argv: list[str]) -> None:
    """Every fenced ``trax`` example in GRAMMAR.md parses and runs."""
    client = FakeClient()
    try:
        cli.parse_and_run(argv, client_factory=lambda: cast(Any, client))
    except ClientError as err:
        pytest.fail(f"GRAMMAR.md:{line_no} example rejected: {err}")


@pytest.mark.parametrize(
    ("line_no", "commands"),
    _SEQUENCES,
    ids=[f"L{line_no}:{len(cmds)}cmds" for line_no, cmds in _SEQUENCES],
)
def test_grammar_sequence_parses(line_no: int, commands: list[list[str]]) -> None:
    """A ``trax-seq`` block parses and executes every command in order.

    All commands share one ``FakeClient`` so later commands can depend
    on state established by earlier ones.
    """
    client = FakeClient()
    for offset, argv in enumerate(commands):
        try:
            cli.parse_and_run(argv, client_factory=lambda: cast(Any, client))
        except ClientError as err:
            pytest.fail(
                f"GRAMMAR.md:{line_no} sequence command #{offset + 1}"
                f" ({' '.join(argv)}) rejected: {err}"
            )


@pytest.mark.parametrize(
    ("line_no", "code", "argv"),
    _counterexample_params(),
    ids=[
        f"L{line_no}:{code}:{' '.join(argv)}"
        for line_no, code, argv in _COUNTEREXAMPLES
    ],
)
def test_grammar_counterexample_rejects(
    line_no: int,
    code: str,
    argv: list[str],
) -> None:
    """Every fenced ``trax!`` counterexample raises ``ClientError``.

    Error-code matching is deferred until ``errors.py`` carries the
    codes; today the test only verifies rejection.
    """
    del code, line_no
    client = FakeClient()
    with pytest.raises(ClientError):
        cli.parse_and_run(argv, client_factory=lambda: cast(Any, client))


# Coverage for grammar.py's single-value coerce/lookup helpers.


def test_coerce_priority_accepts_integer_string() -> None:
    assert _coerce_priority("42") == 42


def test_coerce_status_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown status"):
        _coerce_status("flummoxed")


def test_coerce_judgement_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown judgement"):
        _coerce_judgement("ambivalent")


def test_coerce_publication_type_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown publication_type"):
        _coerce_publication_type("PUNCHCARD")


def test_coerce_publication_type_accepts_valid_values() -> None:
    assert _coerce_publication_type("article") == "article"
    assert _coerce_publication_type("inproceedings") == "inproceedings"
    assert _coerce_publication_type("misc") == "misc"


def test_is_issue_kind_true_for_valid_subkind() -> None:
    assert is_issue_kind("bug")
    assert not is_issue_kind("flummoxed")


def test_cost_key_rejects_non_cost_field() -> None:
    with pytest.raises(ClientError, match="unknown cost field"):
        cost_key("title")


def test_field_value_passes_through_unknown_field() -> None:
    assert field_value("definitely_not_a_field", "raw") == "raw"


def test_field_value_wraps_coerce_value_error_as_client_error() -> None:
    with pytest.raises(ClientError, match="unknown status"):
        field_value("status", "flummoxed")


def test_write_fields_cli_matches_server_kind_gate() -> None:
    """The CLI write whitelist must equal the server's per-kind column gate.

    Deriving ``WRITE_FIELDS_CLI`` from ``applies_to_inquiry_kinds`` only
    pays off if it cannot drift from what the server accepts: a scalar or
    list field is writable exactly when its storage column applies to the
    kind. This pins that equivalence so a hand-edit cannot reintroduce the
    drift the derivation removes.
    """
    for kind, cls in KIND_TO_CLASS.items():
        flat = flat_column_specs(cls)
        expected: set[str] = set()
        for spec in _FIELDS:
            if spec.shape == "cost":
                expected.add(spec.cli_name)
                continue
            column = flat.get(spec.payload_key)
            if column is None:
                continue
            applies = column.spec.applies_to_inquiry_kinds
            if applies is None or kind in applies:
                expected.add(spec.cli_name)
        assert set(WRITE_FIELDS_CLI[kind]) == expected, kind


def test_validate_writable_fields_rejects_cross_kind_field() -> None:
    """A field valid on one kind is rejected on a kind it does not apply to."""
    validate_writable_fields("Belief", ("judgement",))
    with pytest.raises(ClientError, match=r"judgement.*not valid.*Issue"):
        validate_writable_fields("Issue", ("judgement",))


def _section_9_tokens(heading: str) -> set[str]:
    """Backtick tokens in the list paragraph under a ``### {heading}``.

    A §9 table is a single paragraph of ``code``-spanned tokens directly
    under its heading; any rationale prose follows a blank line and is
    excluded. Used to pin the hand-maintained §9 tables to ``grammar.py``.
    """
    text = _GRAMMAR_PATH.read_text(encoding="utf-8")
    start = text.index(f"### {heading}")
    body = text[start:].split("\n", 1)[1]
    paragraph = body.split("\n\n", 1)[0]
    return set(re.findall(r"`([^`=]+)`", paragraph))


def _table_sources() -> dict[str, set[str]]:
    """Each §9 table heading mapped to its ``grammar.py`` source set."""
    return {
        "Kinds": set(g.VALID_KINDS),
        "Issue kinds (`issue_kind`)": set(g.ISSUE_KINDS),
        "Editable scalar fields (`EDITABLE_FIELDS`)": set(g.EDITABLE_FIELDS),
        "List fields (`LIST_FIELDS`)": set(g.LIST_FIELDS),
        "Cost fields (`COST_FIELDS`)": set(g.COST_FIELDS),
        "Edge keywords (`EDGE_ALIASES`)": set(g.EDGE_ALIASES),
        "Statuses (`Inquiry.Status`)": set(get_args(Inquiry.Status.__value__)),
        "Sort choices (`SORT_CHOICES`)": set(g.SORT_CHOICES),
        "Filter ops (`FILTER_OPS`)": set(FILTER_OPS),
    }


@pytest.mark.parametrize("heading", list(_table_sources()))
def test_grammar_tables_match_source(heading: str) -> None:
    """Every §9 token table must list exactly its ``grammar.py`` source.

    The tables are hand-maintained, so without this gate they drift: §9
    once omitted ``AgentSession`` from Kinds and the AgentSession fields
    from EDITABLE_FIELDS. Equality (not subset) so a stale leftover token
    fails just as loudly as a missing one.
    """
    listed = _section_9_tokens(heading)
    source = _table_sources()[heading]
    assert listed == source, (
        f"{heading}: missing {sorted(source - listed)}, stale {sorted(listed - source)}"
    )


def test_every_edge_kind_has_both_writable_directions() -> None:
    """Every Edge.Kind must be writable from BOTH endpoints (Issue#425 item 5).

    A relation writable from one side only (e.g. ``narrows`` having only
    ``broadened_by``, no ``narrowed_by``) cannot be authored parent-anchored. The
    invariant: for each ``Edge.Kind`` there is a forward EDGE_ALIASES entry
    (reverse=False) AND a reverse one (reverse=True), so either endpoint can
    anchor the create.
    """
    forward = {e.name for e in g.EDGE_ALIASES.values() if not e.reverse}
    reverse = {e.name for e in g.EDGE_ALIASES.values() if e.reverse}
    kinds = set(get_args(Edge.Kind.__value__))
    assert kinds <= forward, f"no forward writable alias for {sorted(kinds - forward)}"
    assert kinds <= reverse, f"no reverse writable alias for {sorted(kinds - reverse)}"


def test_narrowed_by_produced_by_superseded_by_are_writable_reverses() -> None:
    """The reverse-direction aliases exist and point at the right stored kind.

    ``produced_by`` is now the FORWARD spelling of the stored ``produced_by``
    kind (from=produced -> to=producer); ``produces`` is its reverse.
    """
    assert g.EDGE_ALIASES["narrowed_by"] == g.Edge(name="narrows", reverse=True)
    assert g.EDGE_ALIASES["produced_by"] == g.Edge(name="produced_by")
    assert g.EDGE_ALIASES["produces"] == g.Edge(name="produced_by", reverse=True)
    assert g.EDGE_ALIASES["superseded_by"] == g.Edge(name="supersedes", reverse=True)


def test_taxonomy_edge_has_all_four_verb_forms() -> None:
    """`narrows` is writable via all four English verb forms for one edge.

    The child narrows the parent (stored from=child -> to=parent). The same edge
    reads four ways; narrow-family and broaden-family put opposite ends on the
    from-side:
      child narrows parent    /  parent narrowed_by child   (child is from)
      parent broadens child   /  child broadened_by parent  (parent is from)
    """
    # child on the from-side (forward storage):
    assert g.EDGE_ALIASES["narrows"] == g.Edge(name="narrows")
    assert g.EDGE_ALIASES["broadened_by"] == g.Edge(name="narrows")
    # parent on the from-side (reverse storage):
    assert g.EDGE_ALIASES["narrowed_by"] == g.Edge(name="narrows", reverse=True)
    assert g.EDGE_ALIASES["broadens"] == g.Edge(name="narrows", reverse=True)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
