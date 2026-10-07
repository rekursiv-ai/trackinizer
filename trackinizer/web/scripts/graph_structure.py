#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Write a server's graph as structure alone, for the README's large graph.

Reads the server's newest --limit inquiries with one GET of /api/web/graph, the
active trax profile's server by default, and keeps only their shape: each node's
kind and status, in the order the nodes were created, and each edge's ends, kind
and the sign of its valence. Everything else the server answers is dropped: ids,
seqs, titles, times, a Belief's judgement and confidence, and a valence's weight.
Every kind, status and edge kind is checked against trackinizer's own
vocabulary, so no other text can reach the file. seed_graph.py gives the nodes
contents of its own.

The file is graph_structure.json beside this script, which seed_screenshots.py
reads; --out writes elsewhere.

Examples:
  ./graph_structure.py
  ./graph_structure.py --limit 5000 --out /tmp/graph_structure.json

'''
# fmt: on

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast, get_args

import argparse
import json

from trackinizer.client.client import Client, server_url
from trackinizer.lib.codec import from_plain, loads
from trackinizer.trax.profile import load_profile
from trackinizer.types.edges import EDGE_POLICIES, Edge
from trackinizer.types.inquiries import KIND_TO_CLASS, Inquiry
from trackinizer.web.scripts.mirror_local import ReadOnlySource, source_token


_CWD: Final = Path(__file__).resolve().parent

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


GRAPH_STRUCTURE: Final = _CWD / "graph_structure.json"
"""The committed structure seed_screenshots.py seeds the large graph from."""


def main() -> int:
    """Read the source's graph and write its structure.

    Returns:
      result: Process exit code (0 on success).

    """
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 2)[2] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args())
    profile = load_profile()
    url = server_url(flags.source or profile.url, "--from")
    with Client(url, api_key=source_token(url, profile), timeout_sec=120.0) as client:
        graph = from_plain(
            ReadOnlySource(client).get("/api/web/graph", limit=flags.limit),
            dict[str, object],
        )
    structure = scrub(graph)
    flags.out.write_text(dump(structure))
    kinds = Counter(node.kind for node in structure.nodes)
    print(
        f"{flags.out}: {len(structure.nodes)} nodes {dict(kinds)}, "
        f"{len(structure.edges)} edges",
    )
    return 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Node:
    """One inquiry's shape: its kind and status."""

    kind: Inquiry.InquiryKind
    status: Inquiry.Status


@dataclass(frozen=True, slots=True, kw_only=True)
class Link:
    """One edge between two nodes, by their index in creation order.

    It points child to parent, as trackinizer stores edges. ``sign`` is the
    valence's: 1 for, -1 against, 0 for an edge with no valence.
    """

    from_index: int
    to_index: int
    kind: Edge.Kind
    sign: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Structure:
    """A graph's shape: its nodes oldest first, and the edges between them."""

    nodes: tuple[Node, ...]
    edges: tuple[Link, ...]


def scrub(graph: Mapping[str, object]) -> Structure:
    """Keep the shape of ``graph``, as ``GET /api/web/graph`` answers it.

    Args:
      graph: The server's answer: ``nodes`` and ``edges``.

    Returns:
      structure: Its nodes in creation order and its edges, without any text.

    Raises:
      ValueError: A kind, status or edge kind outside trackinizer's vocabulary.

    """
    rows = sorted(
        enumerate(from_plain(graph.get("nodes"), list[dict[str, object]], default=[])),
        key=lambda row: (from_plain(row[1].get("created"), str, default=""), row[0]),
    )
    index = {
        from_plain(row.get("id"), str, default=""): n for n, (_, row) in enumerate(rows)
    }
    nodes = tuple(
        Node(
            kind=_known(row.get("kind"), _KINDS),
            status=_known(row.get("status"), _STATUSES),
        )
        for _, row in rows
    )
    edges = tuple(
        sorted(
            (
                Link(
                    from_index=index[from_plain(edge.get("from_id"), str, default="")],
                    to_index=index[from_plain(edge.get("to_id"), str, default="")],
                    kind=_known(edge.get("edge_kind"), _EDGE_KINDS),
                    sign=_sign(edge.get("valence")),
                )
                for edge in from_plain(
                    graph.get("edges"),
                    list[dict[str, object]],
                    default=[],
                )
            ),
            key=lambda link: (link.to_index, link.from_index, link.kind),
        ),
    )
    return Structure(nodes=nodes, edges=edges)


def dump(structure: Structure) -> str:
    """Return ``structure`` as the compact JSON ``load`` reads.

    Each node is ``[kind, status]`` and each edge ``[from, to, kind, sign]``, the
    kinds and statuses as indexes into the vocabularies the file lists first.

    Args:
      structure: The shape to write.

    Returns:
      text: The JSON, one line per vocabulary, the nodes and the edges.

    """
    columns = {
        "kinds": list(_KINDS),
        "statuses": list(_STATUSES),
        "edge_kinds": list(_EDGE_KINDS),
        "nodes": [
            [_KINDS.index(node.kind), _STATUSES.index(node.status)]
            for node in structure.nodes
        ],
        "edges": [
            [link.from_index, link.to_index, _EDGE_KINDS.index(link.kind), link.sign]
            for link in structure.edges
        ],
    }
    lines = (
        f"{json.dumps(key)}: {json.dumps(value, separators=(',', ':'))}"
        for key, value in columns.items()
    )
    return "{\n" + ",\n".join(lines) + "\n}\n"


def load(text: str) -> Structure:
    """Read a structure ``dump`` wrote.

    Args:
      text: The JSON ``dump`` wrote.

    Returns:
      structure: Its nodes in creation order and its edges.

    Raises:
      ValueError: A kind, status or edge kind outside trackinizer's vocabulary,
        an index outside the list it indexes, or an edge whose end is not a node.

    """
    columns = from_plain(loads(text), dict[str, object])
    kinds = from_plain(columns.get("kinds"), list[str], default=[])
    statuses = from_plain(columns.get("statuses"), list[str], default=[])
    edge_kinds = from_plain(columns.get("edge_kinds"), list[str], default=[])
    nodes = tuple(
        Node(
            kind=_known(_at(kinds, kind, of="kinds"), _KINDS),
            status=_known(_at(statuses, status, of="statuses"), _STATUSES),
        )
        for kind, status in _rows(columns.get("nodes"), width=2)
    )
    ends = range(len(nodes))
    edges = tuple(
        Link(
            from_index=_at(ends, source, of="nodes"),
            to_index=_at(ends, target, of="nodes"),
            kind=_known(_at(edge_kinds, kind, of="edge kinds"), _EDGE_KINDS),
            sign=_known(sign, (-1, 0, 1)),
        )
        for source, target, kind, sign in _rows(columns.get("edges"), width=4)
    )
    return Structure(nodes=nodes, edges=edges)


_KINDS: Final = tuple(KIND_TO_CLASS)
_STATUSES: Final = cast(
    tuple[Inquiry.Status, ...],
    get_args(cast(object, Inquiry.Status.__value__)),
)
_EDGE_KINDS: Final = tuple(EDGE_POLICIES)


class _Flags(Protocol):
    source: str
    limit: int
    out: Path


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument(
        "--from",
        dest="source",
        default="",
        help="Server to read (GET only); default: the active trax profile's.",
    )
    parser.add_argument("--limit", type=int, default=5000, help="Newest nodes.")
    parser.add_argument("--out", type=Path, default=GRAPH_STRUCTURE)


def _known[T: str | int](value: object, vocabulary: Sequence[T]) -> T:
    """Return ``value`` as one of ``vocabulary``'s words, or raise ``ValueError``."""
    for word in vocabulary:
        if value == word:
            return word
    raise ValueError(f"{value!r} is not one of {list(vocabulary)}")


def _sign(valence: object) -> int:
    """Return the sign of ``valence``: 0 when the edge has none or it is neutral."""
    value = from_plain(valence, float, default=0.0)
    return (value > 0) - (value < 0)


def _rows(value: object, *, width: int) -> list[list[int]]:
    """Return ``value``'s rows, each ``width`` ints, or raise ``ValueError``."""
    rows = [from_plain(row, list[int]) for row in from_plain(value, list[object])]
    for row in rows:
        if len(row) != width:
            raise ValueError(f"row {row} is not {width} ints")
    return rows


def _at[T](items: Sequence[T], index: int, *, of: str) -> T:
    """Return ``items[index]``, or raise ``ValueError``: no negative index wraps."""
    if index < 0 or index >= len(items):
        raise ValueError(f"{index} is not one of {len(items)} {of}")
    return items[index]


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
