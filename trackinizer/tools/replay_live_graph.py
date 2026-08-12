#!/bin/sh
# ruff: noqa: EXE003, D300 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Mirror the live trackinizer graph into a local (ephemeral) server.

Reads every inquiry and edge from the authenticated source server (the saved
trax profile, i.e. ``trackinizer.rekursiv.ai``) and replays them into a target
server via atomic ``submit_batch`` calls, so the graph viz can be exercised on
real data without putting read load on production each time. Node identity is
not preserved (the target mints fresh ids); edges are rewired by replay index.

Reads are paginated per kind; edges are harvested from each node's
``/api/web/get`` projection (outbound ``edges`` only, to avoid double-counting)
and deduplicated. Only the fields the graph view needs are copied.

With ``--traverse`` the replay walks the graph breadth-first from its roots and
inserts ONE node (plus its edges to already-inserted nodes) at a time, pausing
``--delay`` seconds between inserts. The target's SSE stream then pushes each
node to an open ``/graph`` page, so the real graph visibly grows by traversal
order instead of appearing all at once -- the live-growth demo on real data.

Examples:
  ./replay_live_graph.py --target http://127.0.0.1:8767
  ./replay_live_graph.py --target http://127.0.0.1:8767 --limit 400
  ./replay_live_graph.py --target http://127.0.0.1:8767 --traverse --delay 0.15

'''
# fmt: on

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, cast, get_args

import argparse
import logging
import time
import uuid

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.lib.custom_json import dict_val, str_val
from trackinizer.trax.profile import load_profile
from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import Inquiry


_log = logging.getLogger(__name__)

# One ``web_get`` detail's ``edges``/``backlinks`` projection: edge-kind -> the
# peer rows on that edge. The ``Client.get`` JSON is typed ``Any``, so narrowing
# to this shape at the read boundary restores the peer-row types downstream.
type _PeerMap = Mapping[str, Sequence[Mapping[str, object]]]


def _peer_map(detail: Mapping[str, object], key: str) -> _PeerMap:
    """Return one detail's ``edges``/``backlinks`` peer map (empty when absent)."""
    return cast("_PeerMap", detail.get(key) or {})


def _opt_float(value: object) -> float | None:
    """Coerce a JSON edge ``valence`` to ``float`` (``None`` stays ``None``)."""
    return None if value is None else float(cast("float", value))


# The subset of fields the graph view (and a faithful-enough replay) needs,
# per kind. Everything else on the live row is dropped: the demo only renders
# kind, title, status, and the typed edges.
_KIND_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "Issue": ("title", "status", "issue_kind", "priority"),
    "Belief": ("title", "status", "judgement", "confidence"),
    "Experiment": ("title", "status", "outcome"),
    "Paper": ("title", "status", "publication_type", "source"),
    "CodeChange": ("title", "status", "sha"),
    "WebResult": ("title", "status", "url"),
    "WebSearch": ("title", "status", "provider", "query"),
    "AgentSession": ("title", "status", "cli"),
    "Artifact": ("title", "status"),
}

_DEFAULT_SOURCE: Final = "https://trackinizer.rekursiv.ai"
"""The live trackinizer the replay reads from unless ``--source`` overrides it.
Auth (the API key) still comes from the saved trax profile."""


def main() -> int:
    """The main function. Return the process exit code."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n", 2)[2],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # The httpx per-request INFO lines (one per node fetch) drown the replay's
    # own progress; quiet them to WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    source = _source_client(args.source)
    target = Client(args.target, author="replay")

    if args.seed:
        # Crawl the connected subgraph from the seeds and insert each node into
        # the target the moment it is discovered -- the graph grows live as the
        # crawl walks it, no bulk pull.
        count = _crawl_and_insert(source, target, args.seed, delay=args.delay)
        _log.info("[replay] inserted %d nodes from seeds %s", count, args.seed)
        _log.info("[replay] done -> %s", target.base_url)
        return 0

    nodes = _pull_nodes(source, limit=args.limit)
    _log.info("[replay] pulled %d nodes from %s", len(nodes), source.base_url)
    edges = _pull_edges(source, [n["id"] for n in nodes])
    _log.info("[replay] pulled %d edges", len(edges))

    if args.traverse:
        _replay_traversal(target, nodes, edges, delay=args.delay)
    else:
        _replay(target, nodes, edges)
    _log.info("[replay] done -> %s", target.base_url)
    return 0


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument(
        "--target",
        required=True,
        help="Base URL of the ephemeral server to populate.",
    )
    parser.add_argument(
        "--source",
        default=_DEFAULT_SOURCE,
        help="Base URL of the live trackinizer to read from "
        f"(default {_DEFAULT_SOURCE}). The API key still comes from the saved "
        "trax profile.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap total nodes pulled (0 = all). Useful for a quick smaller run.",
    )
    parser.add_argument(
        "--seed",
        action="append",
        default=[],
        metavar="REF",
        help="Seed the pull from one or more inquiry refs (e.g. 'Issue#552'); "
        "repeatable. Pulls ONLY the connected subgraph reachable from the seeds "
        "(a BFS over the live edges), so the replay shows a chosen story -- a "
        "root issue with its sub-issues, beliefs, evidence, and citations -- "
        "and grows until that component is exhausted. Pair with --traverse.",
    )
    parser.add_argument(
        "--traverse",
        action="store_true",
        help="Insert one node at a time in breadth-first order (live-growth "
        "demo) instead of bulk batches.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.01,
        help="Rate-limit pause (seconds) between write batches in --seed mode, "
        "so the embedded pglite target is not bursted (default 0.01; batching "
        "already keeps writes to a handful of transactions).",
    )


def _source_client(source_url: str) -> Client:
    """Client for ``source_url``, authed with the saved trax profile's key.

    The URL is an explicit argument (``--source``); only the credential is
    taken from the profile, so the source server can be pointed elsewhere
    without rewriting the profile.
    """
    profile = load_profile()
    if not profile.api_key:
        raise SystemExit(
            "no api_key in the active trax profile; cannot read the source server"
        )
    return Client(
        source_url, author=profile.author or "replay", api_key=profile.api_key
    )


def _pull_nodes(source: Client, *, limit: int) -> list[dict[str, Any]]:
    """Every inquiry across all kinds, trimmed to the graph-relevant fields.

    Under ``--limit`` the kinds are ROUND-ROBINED rather than taken in enum
    order, so a small cap samples every kind (Issue, Belief, Paper, ...) instead
    of filling up entirely on the first kind. This keeps the replayed graph
    diverse -- and keeps cross-kind edges (citations, provenance) -- at any size.
    """
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for kind in get_args(Inquiry.InquiryKind.__value__):
        rows = source.list_kind_all(kind)
        by_kind[kind] = [_node_from_row(row, kind) for row in rows]

    if not limit:
        return [node for nodes in by_kind.values() for node in nodes]

    out: list[dict[str, Any]] = []
    cursors = dict.fromkeys(by_kind, 0)
    while len(out) < limit and any(cursors[k] < len(by_kind[k]) for k in by_kind):
        for kind, nodes in by_kind.items():
            if cursors[kind] < len(nodes):
                out.append(nodes[cursors[kind]])
                cursors[kind] += 1
                if len(out) >= limit:
                    break
    return out


def _node_from_row(row: dict[str, Any], kind: str) -> dict[str, Any]:
    """Trim a live inquiry row to the graph-relevant fields.

    ``created`` is carried for traversal ordering, not replayed (it is
    server-stamped on insert); ``_node_body`` drops it.
    """
    node: dict[str, Any] = {
        "id": row["id"],
        "kind": kind,
        "created": row.get("created"),
    }
    for field in _KIND_FIELDS[kind]:
        value = row.get(field)
        if value is not None:
            node[field] = value
    return node


def _pull_edges(
    source: Client, node_ids: list[str]
) -> list[tuple[str, str, str, float | None]]:
    """Outbound edges for each node, as ``(from_id, to_id, kind, valence)``.

    Only the outbound ``edges`` projection is read (the inbound ``backlinks``
    would re-report the same row from the other endpoint), so the set is
    naturally deduplicated across nodes.
    """
    valid_kinds = set(get_args(Edge.Kind.__value__))
    known = set(node_ids)
    out: list[tuple[str, str, str, float | None]] = []
    for index, node_id in enumerate(node_ids):
        detail = cast("Mapping[str, object]", source.get(f"/api/web/get/{node_id}"))
        for edge_kind, peers in _peer_map(detail, "edges").items():
            if edge_kind not in valid_kinds:
                continue
            # A guarded, multi-value append inside two loops; a comprehension
            # (PERF401) would be less readable than the explicit filter here.
            out.extend(
                (node_id, str(peer["id"]), edge_kind, _opt_float(peer.get("valence")))
                for peer in peers
                if peer["id"] in known
            )
        if (index + 1) % 100 == 0:
            _log.info("[replay]   edges: %d/%d nodes", index + 1, len(node_ids))
    return out


def _crawl_and_insert(
    source: Client,
    target: Client,
    seeds: list[str],
    *,
    delay: float,
    # Insert granularity: how many nodes per ``submit_batch`` during the
    # incremental, oldest-first stream. Small so the graph grows visibly
    # node-group by node-group (and the SSE stream pushes each chunk live), but
    # >1 so the embedded pglite target is never bursted by per-row writes.
    chunk: int = 5,
) -> int:
    """Crawl the connected subgraph from ``seeds`` and stream it in, in order.

    The crawl INTERLEAVES discovery and insertion: as the BFS from the seeds
    reaches each node it is inserted right away (in small ``chunk`` batches),
    so the target -- and an open ``/graph`` page via SSE -- starts filling
    almost immediately instead of waiting for the whole component to be read
    first. Each chunk is sorted by source ``created`` before insert, so the
    write order is locally deterministic; the web page re-sorts by ``created``
    for its own replay animation, so exact authoring order is preserved THERE.
    A node that arrives before its peer is not stranded: the viz buffers an
    edge whose other endpoint has not landed yet and attaches it when it does.

    Small batches (not one row at a time) keep the embedded pglite target
    healthy -- per-row writes burst it into 500s and orphaned sockets. ``delay``
    rate-limits between chunks. Returns the inserted node count.
    """
    valid_kinds = set(get_args(Edge.Kind.__value__))
    seen: dict[str, dict[str, Any]] = {}
    id_map: dict[str, str] = {}

    frontier: list[str] = []
    for ref in seeds:
        seed_id = _resolve_seed(source, ref)
        if seed_id not in seen:
            seen[seed_id] = dict_val(source.get(f"/api/web/get/{seed_id}"))
            frontier.append(seed_id)

    pending: list[dict[str, Any]] = []

    def flush() -> None:
        if not pending:
            return
        pending.sort(key=_detail_order_key)
        _insert_chunk(target, list(pending), id_map, valid_kinds)
        _log.info("[replay]   inserted %d nodes so far", len(id_map))
        pending.clear()
        if delay > 0:
            time.sleep(delay)

    while frontier:
        current = frontier.pop(0)
        detail = seen[current]
        pending.append(detail)
        for peer_id in _detail_peers(detail):
            if peer_id not in seen:
                seen[peer_id] = dict_val(source.get(f"/api/web/get/{peer_id}"))
                frontier.append(peer_id)
        if len(pending) >= chunk:
            flush()
    flush()

    return len(id_map)


def _detail_order_key(detail: dict[str, Any]) -> tuple[str, str]:
    """Deterministic replay order: source creation time, then source id."""
    self_view = detail["self"]
    return (str(self_view.get("created") or ""), str(self_view["id"]))


def _insert_chunk(
    target: Client,
    chunk: list[dict[str, Any]],
    id_map: dict[str, str],
    valid_kinds: set[str],
) -> None:
    """Batch-insert a chunk's nodes, then write their edges to present peers."""
    items = [(d["self"]["kind"], _node_from_detail(d["self"])) for d in chunk]
    try:
        new_ids = target.submit_batch(items)
    except ClientError as exc:
        _log.warning(
            "[replay]   batch insert failed target=%s rows=%s: %s",
            target.base_url,
            ", ".join(_detail_label(d) for d in chunk),
            exc,
        )
        # A bad row poisons the atomic batch; fall back to per-row so the good
        # rows still land and only the bad one is skipped.
        new_ids = _insert_one_by_one(target, chunk)
    for d, rid in zip(chunk, new_ids, strict=True):
        if rid is not None:
            id_map[d["self"]["id"]] = str(rid)
    # Write each just-inserted node's edges to any already-present peer, so the
    # nodes are connected immediately rather than stranded.
    for d in chunk:
        sid = d["self"]["id"]
        if sid not in id_map:
            continue
        for from_id, to_id, kind, valence in _detail_edges(sid, d, valid_kinds):
            _write_edge(target, id_map, from_id, to_id, kind, valence=valence)


def _detail_label(detail: dict[str, Any]) -> str:
    """Human-readable node context for replay diagnostics."""
    self_view = detail["self"]
    return (
        f"{self_view['kind']}#{self_view.get('seq', '?')}"
        f" {self_view['id']} {self_view.get('title', '')!r}"
    )


def _insert_one_by_one(
    target: Client, chunk: list[dict[str, Any]]
) -> list[object | None]:
    """Per-row insert fallback; ``None`` for a row the target rejects."""
    out: list[object | None] = []
    for d in chunk:
        try:
            out.append(target.submit(d["self"]["kind"], _node_from_detail(d["self"])))
        except ClientError as exc:
            _log.warning("[replay]   skipped %s node: %s", d["self"]["kind"], exc)
            out.append(None)
    return out


def _resolve_seed(source: Client, ref: str) -> str:
    """Resolve a seed ref (``Issue#552`` or a bare id) to a source node id."""
    text = ref.strip()
    if "#" in text:
        kind, _, seq = text.partition("#")
        row = dict_val(source.get(f"/api/inquiries/{kind}/{seq}"))
        return str_val(row.get("id"))
    return text


def _detail_edges(
    node_id: str,
    detail: dict[str, Any],
    valid_kinds: set[str],
) -> list[tuple[str, str, str, float | None]]:
    """Every edge touching ``node_id``, oriented from_id -> to_id.

    Outbound ``edges`` are ``node -> peer``; inbound ``backlinks`` are
    ``peer -> node``. Each edge is collected from BOTH endpoints during the
    crawl, but ``_write_edge`` is an upsert and the viz dedups, so a repeat is
    harmless.
    """
    out: list[tuple[str, str, str, float | None]] = []
    for kind, peers in _peer_map(detail, "edges").items():
        if kind in valid_kinds:
            out.extend(
                (node_id, str(p["id"]), kind, _opt_float(p.get("valence")))
                for p in peers
            )
    for kind, peers in _peer_map(detail, "backlinks").items():
        if kind in valid_kinds:
            out.extend(
                (str(p["id"]), node_id, kind, _opt_float(p.get("valence")))
                for p in peers
            )
    return out


def _detail_peers(detail: dict[str, Any]) -> list[str]:
    """The ids of every node adjacent to this one (both edge directions)."""
    out: list[str] = []
    for key in ("edges", "backlinks"):
        for peers in _peer_map(detail, key).values():
            out.extend(str(p["id"]) for p in peers)
    return out


def _node_from_detail(self_view: dict[str, Any]) -> dict[str, Any]:
    """A submit body from a ``web_get`` ``self`` view: graph-relevant fields."""
    kind = self_view["kind"]
    body: dict[str, Any] = {}
    for field in _KIND_FIELDS.get(kind, ("title", "status")):
        value = self_view.get(field)
        if value is not None:
            body[field] = value
    return body


def _replay(
    target: Client,
    nodes: list[dict[str, Any]],
    edges: list[tuple[str, str, str, float | None]],
    *,
    batch: int = 200,
) -> None:
    """Create every node, then every edge, on the target via batched submits.

    Nodes are created first (in chunks) to learn their new target ids; edges
    then reference those ids. Splitting nodes and edges avoids the index
    bookkeeping of a single mixed batch while staying just a handful of
    requests.
    """
    id_map: dict[str, str] = {}
    for start in range(0, len(nodes), batch):
        chunk = nodes[start : start + batch]
        items = [(n["kind"], _node_body(n)) for n in chunk]
        new_ids = target.submit_batch(items)
        for old, new in zip(chunk, new_ids, strict=True):
            id_map[old["id"]] = str(new)
        _log.info("[replay]   nodes %d/%d", start + len(chunk), len(nodes))

    written = 0
    for from_id, to_id, kind, valence in edges:
        if _write_edge(target, id_map, from_id, to_id, kind, valence=valence):
            written += 1
            if written % batch == 0:
                _log.info("[replay]   edges %d/%d", written, len(edges))


def _write_edge(
    target: Client,
    id_map: dict[str, str],
    from_id: str,
    to_id: str,
    kind: str,
    *,
    valence: float | None,
) -> bool:
    """Create one edge on the target, rewired to the replayed ids.

    Returns ``False`` (and writes nothing) if an endpoint was not replayed
    (e.g. dropped by ``--limit``). Edges go through the dedicated edge route,
    not the inquiry-batch route -- that route requires at least one item and
    rejects an edges-only batch.
    """
    if from_id not in id_map or to_id not in id_map:
        return False
    try:
        target.add_edge(
            uuid.UUID(id_map[from_id]),
            uuid.UUID(id_map[to_id]),
            kind,
            actor="replay",
            valence=valence,
        )
    except ClientError as exc:
        # The target enforces invariants the source already satisfied but our
        # replay order can momentarily violate -- most notably a provenance
        # cycle (``produced_by`` is acyclic, and auto-inferred edges plus our
        # both-directions crawl can present one out of order). Skip the
        # offending edge rather than abort the whole replay; the structure it
        # would have added is cosmetic for the demo.
        _log.warning("[replay]   skipped %s edge: %s", kind, exc)
        return False
    return True


def _replay_traversal(
    target: Client,
    nodes: list[dict[str, Any]],
    edges: list[tuple[str, str, str, float | None]],
    *,
    delay: float,
) -> None:
    """Insert nodes one at a time in creation-time order, pausing between each.

    Replays the graph's real authoring history: nodes are sorted by ``created``
    and inserted oldest-first, so the viz grows exactly as the knowledge was
    built. This also makes every edge land cleanly -- edges are stored
    younger(child) -> older(parent), so inserting oldest-first guarantees a
    node's parents already exist when it arrives, and its edges to them fire
    immediately. The SSE stream pushes each insert to an open ``/graph`` page,
    so the real graph visibly forms over time.
    """
    edges_from: dict[str, list[tuple[str, str, str, float | None]]] = {
        n["id"]: [] for n in nodes
    }
    for edge in edges:
        # Index each edge under BOTH endpoints; on insert we emit only the ones
        # whose other endpoint is already present, so each edge fires once.
        edges_from[edge[0]].append(edge)
        edges_from[edge[1]].append(edge)

    order = sorted(nodes, key=lambda n: (n.get("created") or "", n["id"]))
    id_map: dict[str, str] = {}
    for index, node in enumerate(order):
        new_id = target.submit(node["kind"], _node_body(node))
        id_map[node["id"]] = str(new_id)
        for edge in edges_from[node["id"]]:
            _write_edge(target, id_map, edge[0], edge[1], edge[2], valence=edge[3])
        if (index + 1) % 50 == 0:
            _log.info("[replay]   inserted %d/%d nodes", index + 1, len(order))
        if delay > 0:
            time.sleep(delay)


def _node_body(node: dict[str, Any]) -> dict[str, Any]:
    """A submit body from a pulled node: graph-relevant fields only.

    Drops ``id`` and ``kind`` (the route's own discriminators) and ``created``
    (server-stamped on insert; carried only for traversal ordering).
    """
    return {k: v for k, v in node.items() if k not in ("id", "kind", "created")}


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
