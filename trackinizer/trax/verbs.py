"""The inquiry-kind verbs (``issue``, ``belief``, ...) and the helper commands.

The 8 kind names share one ``Kind`` command; ``search``, ``recent``, ``next``,
``blocked``, ``graph``, ``board``, and ``cost`` each get their own class.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import ClassVar, cast, get_args, override

import argparse
import math
import os
import sys
import uuid

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.trax import render as fmt
from trackinizer.trax.commands import Command, HelpPage
from trackinizer.trax.grammar import (
    COST_FIELDS,
    EDGE_ALIASES,
    FIELDS_BY_NAME,
    KIND_LOWER,
    REF_FIELD_BY_PAYLOAD,
    RELATION_ALIASES,
    SORT_CHOICES,
    Action,
    AddCost,
    AddList,
    BulkApply,
    DeleteRow,
    EdgeAction,
    Field,
    InlineCreate,
    ListQuery,
    MetricAction,
    MetricMask,
    ReadField,
    RelationAction,
    RemoveList,
    SetField,
    cost_key,
    parse_kind,
    validate_writable_fields,
)
from trackinizer.trax.parser import (
    consume_ref,
    parse_actions,
    parse_bulk_apply,
    parse_list_query,
    parse_metric_action,
    parse_subject_list,
    starts_with_ref,
)
from trackinizer.trax.render import (
    add_write_flags,
    echo,
    format_field_value,
    print_rows,
    show_ids,
    table_cell,
    table_width,
)
from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.refs import Ref, SeqRef, UuidRef
from trackinizer.wire.routes import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
)
from trackinizer.wire.wire_metrics import MetricPoint
from trackinizer.wire.wire_metrics_query import (
    MetricCompareOp,
    MetricMaskClause,
    MetricRankRow,
    MetricReduce,
)


# The ``(from_label, to_label)`` endpoint roles per edge kind, keyed by the
# closed :data:`Edge.Kind` literal. A unit test pins the key set to
# ``get_args(Edge.Kind)`` so adding an edge kind forces a label entry here
# rather than raising a raw ``KeyError`` deep in ``_edge_payload`` (I4).
LABELS_BY_EDGE_KIND: Mapping[Edge.Kind, tuple[str, str]] = {
    # ``(from_label, to_label)`` per stored edge kind. Every edge is stored
    # child -> parent, so the from-side is the younger/dependent vertex.
    "narrows": ("narrower", "broader"),
    "requires": ("requirer", "prerequisite"),
    "produced_by": ("produced", "producer"),
    # proves/favors store evidence (the citing Artifact) -> claim (Belief or
    # Experiment). For-vs-against is the valence sign, not a separate kind.
    "proves": ("citing artifact", "proven claim"),
    "favors": ("citing artifact", "favored claim"),
    "supersedes": ("successor", "predecessor"),
    # cites_paper stores citing paper -> cited paper (historical bibliography).
    "cites_paper": ("citing paper", "cited paper"),
}

# The against-citation spelling for each citation kind. For-vs-against is the
# SIGN of valence, not a separate stored kind, so the CLI re-derives the dis*
# title/labels from a negative valence -- one place the polarity convention is
# applied for display.
_NEGATIVE_CITATION_TITLE: Mapping[Edge.Kind, str] = {
    "proves": "disproves",
    "favors": "disfavors",
}
_NEGATIVE_CITATION_LABELS: Mapping[Edge.Kind, tuple[str, str]] = {
    "proves": ("citing artifact", "disproven claim"),
    "favors": ("citing artifact", "disfavored claim"),
}


def _is_against_citation(edge_kind: str, valence: object) -> bool:
    """Whether a citation edge carries a negative (against) valence."""
    return (
        edge_kind in _NEGATIVE_CITATION_TITLE
        and isinstance(valence, (int, float))
        and not isinstance(valence, bool)
        and valence < 0
    )


def _positive_int(value: str) -> int:
    """Argparse ``type`` for ``--limit``: an integer ``>= 1``.

    A zero or negative limit silently returned no rows; reject it at the
    parser so every ``--limit`` site shares one rule and the user gets a
    clear error instead of an empty result.
    """
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return parsed


class Kind(Command):
    """The 8 inquiry-kind names, each acting as a verb.

    One parser handles every shape; :meth:`run` routes by looking at the
    trailing positionals:

      * ``trax <kind>`` -- list rows of that kind
      * ``trax <kind> field to value ...`` -- create a row
      * ``trax <kind> <seq>`` -- show one row
      * ``trax <kind> <seq> field to value ...`` -- edit fields
      * ``trax <kind> <seq> <edge> <kind> <seq>`` -- add an edge

    The only multi-verb class; the kinds genuinely share one parser.
    """

    names = tuple(KIND_LOWER)
    field_help: ClassVar[HelpPage] = HelpPage(
        usage="trax <kind> <seq> FIELD [to VALUE]",
        summary="Bare FIELD projects it; FIELD to VALUE replaces it.",
        examples=("trax issue 7 title", "trax issue 7 title to 'New title'"),
    )
    field_set_help: ClassVar[HelpPage] = HelpPage(
        usage="trax <kind> <seq> FIELD to VALUE [FIELD to VALUE ...]",
        summary="Mutates the selected field or fields.",
        examples=("trax belief 3 judgement to proven confidence to 0.95",),
    )

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="trax <kind>",
            description=(
                "List, show, edit, or add an outbound edge on an inquiry. "
                "The kind name itself is the verb."
            ),
        )
        parser.add_argument(
            "rest",
            nargs="*",
            metavar="POS",
            help="[seq] [verb object]; use strict SVO for row-local mutations",
        )
        parser.add_argument(
            "--format",
            default="table",
            dest="format_",
            choices=("table", "json", "ids"),
        )
        parser.add_argument("--limit", type=_positive_int, default=DEFAULT_LIST_LIMIT)
        parser.add_argument("--sort", choices=SORT_CHOICES, default="priority")
        parser.add_argument("--width", type=int, default=None)
        parser.add_argument("--changes", action="store_true")
        parser.add_argument(
            "--makeitso",
            action="store_true",
            help="apply a bulk field mutation that matches more than one row",
        )
        add_write_flags(parser)
        return parser

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        kind = KIND_LOWER[verb]
        rest = cast(Sequence[str], args.rest)
        # The ``metric`` grid tail is Experiment-only and can appear after a ref
        # (single experiment), after a list query (cross-experiment rank), or
        # after create fields (create+log fusion). Intercept it before the
        # generic list/create dispatch, which does not know the ``metric`` word.
        if kind == "Experiment" and (split := _split_metric_tail(rest)) is not None:
            return cls.run_metric(split[0], split[1], args, client_factory)
        if not rest:
            return run_list_query(
                ListQuery(kinds=(kind,), ranges={}, filters=()),
                args,
                client_factory,
            )
        if bulk := parse_bulk_apply(kind, rest):
            return run_bulk_apply(bulk, args, client_factory)
        if query := parse_list_query(kind, rest):
            return run_list_query(query, args, client_factory)
        if subjects := parse_subject_list(rest, default_kind=kind):
            for subject in subjects:
                run_show(subject, args, client_factory)
            return None
        if not starts_with_ref(rest):
            actions = _resolve_stdin_actions(parse_actions(rest))
            cls.run_create(kind, actions, args, client_factory)
            return None
        ref, consumed = consume_ref(rest, 0, kind_hint=kind)
        after = rest[consumed:]
        if not after:
            return run_show(ref, args, client_factory)
        return run_actions(
            ref,
            _resolve_stdin_actions(parse_actions(after)),
            args,
            client_factory,
            kind=kind,
        )

    @classmethod
    def run_relation(
        cls,
        ref: Ref,
        relation: tuple[str, bool],
        tokens: Sequence[str],
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
        *,
        against: bool = False,
    ) -> None:
        if len(tokens) > 1:
            raise ClientError(
                f"unexpected positional after relation index: {tokens[1:]!r}"
            )
        client = client_factory()
        _kind, _target_id, payload = client.get_inquiry(ref)
        edge_kind, _inbound = relation
        rows = cls._relation_rows(payload, relation, against=against)
        rows = cls._sort_relation_rows(rows, args.sort)
        hydrated = cls._hydrate_relation_rows(client, rows)
        if not tokens:
            print_rows(hydrated, args.format_, width=args.width)
            return
        row = cls._select_relation_row(rows, edge_kind, tokens[0])
        cls._print_edge_payload(client, ref, row, relation, args)

    @classmethod
    def run_metric(
        cls,
        before: Sequence[str],
        tail: Sequence[str],
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        """Dispatch a ``metric`` grid tail (``docs/metric-grammar.md``).

        ``before`` is everything left of the ``metric`` keyword; ``tail`` is the
        mask/write/read-opts to its right. ``before`` selects the experiment(s):

          * a leading ref (``experiment 42 metric ...``) -> one experiment's grid
            (read the masked cells, or ``to`` write them);
          * create fields (``experiment title to "x" metric ...``) -> create the
            Experiment, then log the metric writes into it (create+log fusion);
          * empty or a list query (``experiment [label ml] metric ...``) -> a
            cross-experiment masked read/rank (the leaderboard surface).
        """
        action = parse_metric_action(tail)
        if starts_with_ref(before):
            cls._run_metric_single(before, action, args, client_factory)
        elif not before or parse_list_query("Experiment", before) is not None:
            cls._run_metric_cross(before, action, args, client_factory)
        else:
            cls._run_metric_create(before, action, args, client_factory)

    @classmethod
    def _run_metric_single(
        cls,
        before: Sequence[str],
        action: MetricAction,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        """``experiment <ref> metric ...``: read or write one experiment's grid."""
        ref, consumed = consume_ref(before, 0, kind_hint="Experiment")
        if consumed != len(before):
            raise ClientError(
                f"unexpected tokens before metric: {list(before[consumed:])!r}"
            )
        client = client_factory()
        _kind, exp_id = client.resolve_id(ref)
        if action.write is not None:
            written = cls._write_masked(client, exp_id, action, bulk_ok=args.makeitso)
            echo(f"written: {written}")
            return
        points = client.query_metrics(
            exp_id,
            masks=_mask_clauses(action.masks),
            sort=action.sort,
            limit=action.limit,
        )
        cls._render_metric_points(points, args)

    @classmethod
    def _run_metric_cross(
        cls,
        before: Sequence[str],
        action: MetricAction,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        """Cross-experiment masked read/rank over the experiments ``before`` selects."""
        if action.write is not None:
            raise ClientError("a cross-experiment metric tail cannot write; name a ref")
        query = parse_list_query("Experiment", before) or ListQuery(
            kinds=("Experiment",), ranges={}, filters=()
        )
        client = client_factory()
        rows = _query_rows(query, client, limit=MAX_LIST_LIMIT)
        exp_ids = [uuid.UUID(cast(str, row["id"])) for row in rows]
        if not exp_ids:
            echo("(no experiments)")
            return
        ranked = client.rank_metrics(
            exp_ids,
            masks=_mask_clauses(action.masks),
            sort=action.sort,
            limit=action.limit,
        )
        cls._render_metric_rank(ranked, rows, args)

    @classmethod
    def _run_metric_create(
        cls,
        before: Sequence[str],
        action: MetricAction,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        """``experiment title to "x" metric ...``: create the Experiment then log."""
        if action.write is None:
            raise ClientError(
                "create+log requires a metric write (a 'to' value); "
                "read an existing experiment by ref instead"
            )
        actions = _resolve_stdin_actions(parse_actions(before))
        # ``run_create`` returns the server-minted id of the row it just created,
        # so the write targets THAT experiment -- not "the newest", which a
        # concurrent create could shadow. The create body carries no metric, so
        # the write lands as a second step: not atomic, matching every other
        # create-then-annotate path (the create itself is atomic).
        exp_id = cls.run_create("Experiment", actions, args, client_factory)
        client = client_factory()
        written = cls._write_masked(client, exp_id, action, bulk_ok=args.makeitso)
        echo(f"written: {written}")

    @classmethod
    def _write_masked(
        cls,
        client: Client,
        exp_id: uuid.UUID,
        action: MetricAction,
        *,
        bulk_ok: bool,
    ) -> int:
        """Coerce and apply a masked ``to`` write, guarding a bulk blast radius.

        The ``to`` value is a finite float; ``step`` must be masked (a metric
        point has no default step). A mask that resolves to more than one cell is
        a bulk write and requires ``--makeitso`` -- the count is discovered by a
        dry read first, mirroring the inquiry bulk-edit guard.
        """
        assert action.write is not None
        value = _finite_float(action.write)
        masks = _mask_clauses(action.masks)
        if not any(m.axis == "step" for m in masks):
            raise ClientError("a metric write must mask 'step' (no default step)")
        if not bulk_ok:
            # Discover the blast radius before writing: a mask selecting more
            # than one cell needs explicit --makeitso, so a fat-fingered
            # ``at step gt 0 to 0`` cannot silently overwrite a whole run.
            hits = client.query_metrics(exp_id, masks=masks, sort=None, limit=None)
            if len(hits) > 1:
                raise ClientError(
                    f"would write {len(hits)} cells; pass --makeitso for a bulk write"
                )
        return client.write_metrics_masked(exp_id, masks=masks, value=value)

    @classmethod
    def _render_metric_points(
        cls, points: Sequence[MetricPoint], args: argparse.Namespace
    ) -> None:
        """Print masked cells in ``(key, step)`` order, or JSON."""
        if args.format_ == "json":
            echo(fmt.format_json([p.model_dump(mode="json") for p in points]), nl=False)
            return
        if not points:
            echo("(no metrics)")
            return
        for p in points:
            echo(f"{p.key:24} {p.step:>10} {p.value:>16.6g}")

    @classmethod
    def _render_metric_rank(
        cls,
        ranked: Sequence[MetricRankRow],
        rows: Sequence[Mapping[str, object]],
        args: argparse.Namespace,
    ) -> None:
        """Print a cross-experiment read: each cell tagged with its experiment."""
        if args.format_ == "json":
            echo(
                fmt.format_json(
                    [
                        {
                            "experiment_id": str(r.experiment_id),
                            **r.point.model_dump(mode="json"),
                        }
                        for r in ranked
                    ]
                ),
                nl=False,
            )
            return
        if not ranked:
            echo("(no metrics)")
            return
        seq_by_id = {
            str(row.get("id")): row.get("seq", "?") for row in rows if row.get("id")
        }
        for r in ranked:
            seq = seq_by_id.get(str(r.experiment_id), "?")
            echo(
                f"experiment {seq!s:>4}  {r.point.key:20} "
                f"{r.point.step:>10} {r.point.value:>16.6g}"
            )

    @classmethod
    def _relation_rows(
        cls,
        payload: Mapping[str, object],
        relation: tuple[str, bool],
        *,
        against: bool = False,
    ) -> list[dict[str, object]]:
        edge_kind, inbound = relation
        bucket = "backlinks" if inbound else "edges"
        rows = list(
            cast(
                Sequence[dict[str, object]],
                cast(Mapping[str, object], payload.get(bucket) or {}).get(edge_kind)
                or (),
            )
        )
        if against:
            # A ``dis*`` spelling selects the negative-valence (against) subset of
            # the shared citation kind: for-vs-against is the valence sign.
            rows = [
                r for r in rows if _is_against_citation(edge_kind, r.get("valence"))
            ]
        return rows

    @classmethod
    def _print_edge_payload(
        cls,
        client: Client,
        subject: Ref,
        peer_row: dict[str, object],
        relation: tuple[str, bool],
        args: argparse.Namespace,
    ) -> None:
        _subject_kind, _subject_id, subject_payload = client.get_inquiry(subject)
        _peer_kind, _peer_id, peer_payload = client.get_inquiry(
            UuidRef(uuid=uuid.UUID(cast(str, peer_row["id"])))
        )
        edge_payload = cls._edge_payload(
            subject_payload, peer_payload, peer_row, relation
        )
        if args.format_ == "json":
            echo(fmt.format_json(edge_payload), nl=False)
        else:
            echo(
                fmt.format_edge(edge_payload, changes=args.changes),
                nl=False,
            )

    @classmethod
    def _edge_payload(
        cls,
        subject_payload: Mapping[str, object],
        peer_payload: Mapping[str, object],
        peer_row: Mapping[str, object],
        relation: tuple[str, bool],
    ) -> dict[str, object]:
        edge_kind, inbound = relation
        subject = cast(Mapping[str, object], subject_payload["self"])
        peer = cast(Mapping[str, object], peer_payload["self"])
        source, target = (peer, subject) if inbound else (subject, peer)
        # For-vs-against is the sign of valence: a negative-valence citation reads
        # with the dis* spelling, not the plain kind name.
        against = _is_against_citation(edge_kind, peer_row.get("valence"))
        typed_kind = cast(Edge.Kind, edge_kind)
        title = (
            _NEGATIVE_CITATION_TITLE[typed_kind]
            if against
            else edge_kind.replace("_", " ")
        )
        source_label, target_label = (
            _NEGATIVE_CITATION_LABELS[typed_kind]
            if against
            else LABELS_BY_EDGE_KIND[typed_kind]
        )
        source_payload = peer_payload if inbound else subject_payload
        target_id = str(target.get("id") or "")
        changes = [
            change
            for change in cast(
                Sequence[dict[str, object]], source_payload.get("changes") or ()
            )
            if any(
                cast(Mapping[str, object], snapshot or {}).get("peer_edge_kind")
                == edge_kind
                and cast(Mapping[str, object], snapshot or {}).get("peer_id")
                == target_id
                for snapshot in (change.get("old"), change.get("new"))
            )
        ]
        return {
            "title": title,
            "endpoints": (
                dict(source, label=source_label),
                dict(target, label=target_label),
            ),
            "edge": cls._relation_edge_metadata(peer_row),
            "changes": changes,
        }

    @classmethod
    def _sort_relation_rows(
        cls,
        rows: Sequence[dict[str, object]],
        sort: str,
    ) -> list[dict[str, object]]:
        if sort == "seq":
            return sorted(rows, key=lambda row: int(cast(int, row.get("seq") or 0)))
        if sort == "recent":
            return sorted(
                rows, key=lambda row: str(row.get("created") or ""), reverse=True
            )
        if sort == "oldest":
            return sorted(rows, key=lambda row: str(row.get("created") or ""))
        if sort == "valence":
            return sorted(
                rows,
                key=lambda row: float(cast(float, row.get("valence") or 0)),
                reverse=True,
            )
        return sorted(
            rows,
            key=lambda row: (
                str(row.get("status") or "") != "active",
                # A P0 row has priority 0, which is falsy -- ``0 or 20`` would
                # sort it as medium, below P1 (F33). Only an absent/None priority
                # defaults to 20; an explicit 0 is preserved.
                _priority_or_default(row.get("priority")),
                int(cast(int, row.get("seq") or 0)),
            ),
        )

    @classmethod
    def _select_relation_row(
        cls,
        rows: Sequence[dict[str, object]],
        edge_kind: str,
        token: str,
    ) -> dict[str, object]:
        if not token.isdigit():
            raise ClientError(
                f"relation index must be a positive integer, got {token!r}"
            )
        seq_matches = [row for row in rows if str(row.get("seq") or "") == token]
        if len(seq_matches) == 1:
            return seq_matches[0]
        index = int(token)
        if index < 1 or index > len(rows):
            raise ClientError(
                f"relation index {index} out of range; {len(rows)} {edge_kind} rows"
            )
        return rows[index - 1]

    @classmethod
    def _hydrate_relation_rows(
        cls,
        client: Client,
        rows: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]:
        hydrated: list[dict[str, object]] = []
        for row in rows:
            _kind, _target_id, payload = client.get_inquiry(
                UuidRef(uuid=uuid.UUID(cast(str, row["id"])))
            )
            self_row = cast(dict[str, object], payload["self"])
            hydrated.append(dict(self_row, **cls._relation_edge_metadata(row)))
        return hydrated

    @classmethod
    def _relation_edge_metadata(
        cls,
        edge_row: Mapping[str, object],
    ) -> dict[str, object]:
        metadata: dict[str, object] = {}
        if "valence" in edge_row:
            metadata["edge_valence"] = edge_row["valence"]
        if "priority" in edge_row:
            metadata["edge_priority"] = edge_row["priority"]
        if labels := edge_row.get("labels"):
            metadata["edge_labels"] = labels
        if note := edge_row.get("note"):
            metadata["edge_note"] = note
        return metadata

    @classmethod
    def run_relation_add_edge(
        cls,
        subject: Ref,
        relation: tuple[str, bool],
        source: Ref,
        target: Ref,
        args: argparse.Namespace,
        *,
        client_factory: Callable[[], Client],
    ) -> None:
        client = client_factory()
        _source_kind, src_id = client.resolve_id(source)
        _target_kind, tgt_id = client.resolve_id(target)
        actor = resolve_actor(args.actor, client)
        result = client.add_edge(src_id, tgt_id, relation[0], actor=actor)
        verb = "added" if result.created else "exists"
        echo(f"{verb}: {source} {relation[0]} {target}")
        _kind, _target_id, payload = client.get_inquiry(subject)
        rows = cls._relation_rows(payload, relation)
        # The stored edge is from=src_id -> to=tgt_id (set at add_edge above);
        # this only picks which PEER to re-display. ``relation[1]`` is the
        # reverse/inbound flag: when reversed, the subject is the stored ``to``
        # side, so the peer to look up is the ``from`` side (src_id); otherwise
        # the peer is the ``to`` side (tgt_id). Display-only -- it does not
        # affect the stored direction.
        target_id = str(src_id if relation[1] else tgt_id)
        peer = next(
            (row for row in rows if str(row.get("id") or "") == target_id), None
        )
        if peer is not None:
            cls._print_edge_payload(client, subject, peer, relation, args)

    @classmethod
    def run_add_edge(
        cls,
        source: Ref,
        edge_kind: str,
        target: Ref,
        metadata: Mapping[str, object],
        args: argparse.Namespace,
        *,
        client_factory: Callable[[], Client],
    ) -> None:
        """Link an edge that carries metadata, upserting it in one call.

        The metadata path only: plain (metadata-less) links go through
        :meth:`run_relation_add_edge`. The grammar's ``EDGE TARGET FIELD to
        VALUE`` is one upsert: a brand-new edge carries the metadata, and an
        existing edge has it applied -- the server does both in
        :meth:`Client.add_edge`. The echo distinguishes the two ("added" vs
        "annotated") from the returned :class:`EdgeWrite`.
        """
        client = client_factory()
        _, src_id = client.resolve_id(source)
        _, tgt_id = client.resolve_id(target)
        actor = resolve_actor(args.actor, client)
        priority = cast(int | None, metadata.get("priority"))
        note = cast(str | None, metadata.get("note"))
        valence = cast(float | None, metadata.get("valence"))
        # An empty ``labels`` list PRESENT in metadata is an explicit
        # clear-to-empty (``label del`` emptied it); absent means "no labels
        # arg". ``None`` threads the clear through ``add_edge`` to the labels
        # route that writes NULL; ``()`` leaves stored labels untouched
        # (TRAX-CLI-004).
        labels = cast("Sequence[str] | None", metadata.get("labels"))
        edge_labels: Sequence[str] | None
        if "labels" in metadata and not labels:
            edge_labels = None
        else:
            edge_labels = labels or ()
        result = client.add_edge(
            src_id,
            tgt_id,
            edge_kind,
            actor=actor,
            priority=priority,
            note=note or "",
            valence=valence,
            labels=edge_labels,
        )
        if result.created:
            echo(f"added: {source} {edge_kind} {target}")
        elif result.changed:
            echo(f"annotated: {source} {edge_kind} {target}")

    @classmethod
    def run_create(
        cls,
        kind: Inquiry.InquiryKind,
        actions: Sequence[Action],
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> uuid.UUID:
        """Create a row (plus any inline edge subtree) and return the root id.

        Returns the server-minted id of the root row so a caller that must act
        on the just-created row (the ``metric`` create+log fusion) targets that
        exact row rather than re-reading "the newest", which a concurrent create
        could shadow.
        """
        client = client_factory()
        actor = resolve_actor(args.actor, client)
        create_actions, edge_actions, cost_actions = cls._split_create_actions(actions)
        # Reject kind-invalid create fields before any request so a bad body
        # cannot land an inline-create edge target and then orphan it on the
        # root submit's 409 -- the same up-front gate the edit path applies.
        validate_writable_fields(kind, tuple(action.field for action in create_actions))
        # One atomic batch: item 0 is the root row. The edge tree is flattened by
        # a DEEP cursor (Issue#425 item 6): each edge links its SOURCE node (the
        # cursor) to its target; an inline-create target is appended as a new item
        # and BECOMES the cursor for its own nested edges, so a chain descends and
        # a `begin ... end` group fans out. The whole create commits or rolls back
        # together, so a failed edge or target can never orphan the root.
        items: list[tuple[Inquiry.InquiryKind, Mapping[str, object]]] = [
            (kind, cls._create_body(kind, create_actions, actor, client))
        ]
        edges: list[dict[str, object]] = []
        # The edges in flatten (creation) order, each tagged with its SOURCE node's
        # batch index and the target ref it already pointed at (None = freshly
        # inline-created), so the echo below can resolve both endpoints from the
        # minted ids regardless of nesting depth and print a directional triple.
        flat_edges: list[tuple[EdgeAction, int, Ref | None]] = []
        # Cost deltas keyed by the batch index of the node they land on: the root
        # (0) plus any inline-created node carrying ``agent-cost``/``resource-cost``.
        # Applied after the atomic create, so a non-root cost hits the right node.
        deferred_costs: list[tuple[int, AddCost]] = [(0, cost) for cost in cost_actions]

        # Resolve every existing-ref edge target in one batch, then flatten the
        # whole subtree off that pre-resolved stream (F8/F14).
        resolved = iter(
            target_id
            for _kind, target_id in client.resolve_ids(
                cls._collect_existing_refs(edge_actions)
            )
        )
        cls._flatten_inline_tree(
            edge_actions,
            from_index=0,
            actor=actor,
            client=client,
            items=items,
            edges=edges,
            deferred_costs=deferred_costs,
            flat_edges=flat_edges,
            resolved=resolved,
        )
        ids = client.submit_batch(items, edges=edges)
        # Cost columns are flattened, so a delta cannot ride the create body;
        # each is applied right after the atomic create lands, on its OWN node,
        # through the same signed-delta setter a standalone ``agent-cost add`` uses.
        for node_index, cost in deferred_costs:
            client.add_cost(
                ids[node_index],
                cost_key(cost.field),
                cost.value,
                actor=actor,
                reason=args.reason,
            )
        bare_ids = args.format_ == "ids"
        new_ref = _submitted_ref(ids[0], client)
        # Under ``--format ids`` stdout carries only the bare UUIDs so a
        # ``$(...)`` capture stays clean; the human ``created:``/``added:``
        # lines are suppressed entirely.
        echo(str(ids[0]) if bare_ids else _created_line(new_ref, ids[0]))
        # Cache each batch index's user-facing ref so a node sourcing several
        # edges resolves once; index 0 is the root, the rest are inline targets
        # in the order ``flatten`` appended them (so the Nth inline target is
        # ``ids[N]``).
        ref_by_index: dict[int, Ref] = {0: new_ref}
        next_inline_index = 1
        for action, from_index, existing_ref in flat_edges:
            if existing_ref is None:
                target_id = ids[next_inline_index]
                if bare_ids:
                    echo(str(target_id))
                    next_inline_index += 1
                    continue
                shown_ref: Ref = _submitted_ref(target_id, client)
                ref_by_index[next_inline_index] = shown_ref
                next_inline_index += 1
                echo(_created_line(shown_ref, target_id))
            elif bare_ids:
                continue
            else:
                shown_ref = existing_ref
            anchor = ref_by_index.get(from_index, new_ref)
            # Print the logical triple (source EDGE target); a reverse alias
            # stores target -> source, so swap the shown endpoints to match the
            # direction the user wrote -- mirroring the row-local edit echo.
            source, target = (
                (shown_ref, anchor) if action.edge.reverse else (anchor, shown_ref)
            )
            echo(f"added: {source} {action.edge.name} {target}")
        return ids[0]

    @classmethod
    def _batch_edge(
        cls,
        action: EdgeAction,
        *,
        from_index: int | None = None,
        from_id: uuid.UUID | None = None,
        to_index: int | None = None,
        to_id: uuid.UUID | None = None,
    ) -> dict[str, object]:
        """Map one create edge action to a ``BatchEdge`` payload dict.

        The SOURCE node is named by EITHER ``from_index`` (a batch item -- the
        deep cursor, the node this edge hangs off) OR ``from_id`` (an existing
        row, e.g. the leading subject of a row-local edit). The TARGET likewise
        by ``to_index`` (a new inline item) or ``to_id`` (an existing row).
        ``action.edge.reverse`` flips which endpoint is the source: a reverse
        edge stores ``target -> source``.
        """
        src: dict[str, object] = (
            {"from_index": from_index}
            if from_index is not None
            else {"from_id": str(from_id)}
        )
        peer: dict[str, object] = (
            {"to_index": to_index} if to_index is not None else {"to_id": str(to_id)}
        )
        # A reverse alias (e.g. ``blocked_by``) stores the edge as peer -> source;
        # swap the endpoint keys so the wire direction matches storage.
        if action.edge.reverse:
            src, peer = _swap_edge_endpoints(src, peer)
        edge: dict[str, object] = {**src, **peer, "edge_kind": action.edge.name}
        for key in ("priority", "note", "valence", "labels"):
            value = action.metadata.get(key)
            if value is not None:
                edge[key] = value
        return edge

    @classmethod
    def _split_create_actions(
        cls,
        actions: Sequence[Action],
    ) -> tuple[
        Sequence[SetField | AddList | RemoveList],
        Sequence[EdgeAction],
        Sequence[AddCost],
    ]:
        """Partition create actions into body, edge, and cost groups.

        Returns:
          body_actions: Field/list mutations forming the create body.
          edge_actions: Non-remove edge links (each may inline-create a target).
          cost_actions: Signed cost deltas applied after the row is created.

        """
        body_actions: list[SetField | AddList | RemoveList] = []
        edge_actions: list[EdgeAction] = []
        cost_actions: list[AddCost] = []
        for action in actions:
            if isinstance(action, SetField | AddList | RemoveList):
                body_actions.append(action)
            elif isinstance(action, EdgeAction) and not action.remove:
                edge_actions.append(action)
            elif isinstance(action, AddCost):
                cost_actions.append(action)
            else:
                raise ClientError(
                    "create supports field/list actions, edge actions, and cost adds"
                )
        return body_actions, edge_actions, cost_actions

    @classmethod
    def _collect_existing_refs(cls, actions: Sequence[EdgeAction]) -> list[Ref]:
        """Every existing-ref edge target in the subtree, in flatten (DFS) order.

        An inline-create target mints a new row, so only non-inline targets need
        resolving; collecting them all up front lets the caller resolve the whole
        tree in one ``resolve_ids`` batch instead of N sequential round-trips
        (F14). The order matches :meth:`_flatten_inline_tree`'s walk, so the
        resolved ids zip back positionally.
        """
        refs: list[Ref] = []
        for action in actions:
            if isinstance(action.target, InlineCreate):
                refs.extend(cls._collect_existing_refs(action.target.edges))
            else:
                refs.append(action.target)
        return refs

    @classmethod
    def _flatten_inline_tree(
        cls,
        actions: Sequence[EdgeAction],
        *,
        from_index: int,
        actor: Inquiry.Actor,
        client: Client,
        items: list[tuple[Inquiry.InquiryKind, Mapping[str, object]]],
        edges: list[dict[str, object]],
        deferred_costs: list[tuple[int, AddCost]],
        flat_edges: list[tuple[EdgeAction, int, Ref | None]],
        resolved: Iterator[uuid.UUID],
    ) -> None:
        """Flatten an inline-create edge tree into batch ``items`` and ``edges``.

        Shared by :meth:`run_create` (root-anchored at batch item 0) and
        :meth:`_run_anchored_inline_subtree` (anchored at an existing row by id):
        both descend the DEEP cursor identically, appending each inline target as
        a new item that becomes the cursor for its own nested edges, so the whole
        subtree lands in one ``submit_batch`` (F8). Existing-ref targets pull
        their pre-resolved uuid from ``resolved`` (the batch from
        :meth:`_collect_existing_refs`) rather than a per-edge round-trip (F14).
        ``flat_edges`` records ``(action, source_index, existing_ref_or_None)``
        in creation order so the caller can echo a directional triple per edge.
        """
        for action in actions:
            if isinstance(action.target, InlineCreate):
                target = action.target
                validate_writable_fields(
                    target.kind, tuple(f.field for f in target.fields)
                )
                items.append((target.kind, _inline_create_body(target, actor, client)))
                new_index = len(items) - 1
                edges.append(
                    cls._batch_edge(action, from_index=from_index, to_index=new_index)
                )
                flat_edges.append((action, from_index, None))
                deferred_costs.extend((new_index, cost) for cost in target.costs)
                cls._flatten_inline_tree(
                    target.edges,
                    from_index=new_index,
                    actor=actor,
                    client=client,
                    items=items,
                    edges=edges,
                    deferred_costs=deferred_costs,
                    flat_edges=flat_edges,
                    resolved=resolved,
                )
            else:
                target_id = next(resolved)
                edges.append(
                    cls._batch_edge(action, from_index=from_index, to_id=target_id)
                )
                flat_edges.append((action, from_index, action.target))

    @classmethod
    def _create_body(
        cls,
        kind: Inquiry.InquiryKind,
        actions: Sequence[SetField | AddList | RemoveList],
        actor: Inquiry.Actor,
        client: Client,
    ) -> dict[str, object]:
        body: dict[str, object] = {"owner": actor}
        for action in actions:
            if isinstance(action, SetField):
                # A ref-list `... to KIND SEQ` resolves to its wire shape via
                # _resolve_set_value (a bare id for the monomorphic ``codechanges``
                # field; trax #419); plain values pass through.
                body[action.field] = _resolve_set_value(action, client)
            else:
                spec = FIELDS_BY_NAME[action.field]
                if spec.ref_kind is not None:
                    raise ClientError(
                        f"create does not support {spec.cli_name} add/del; use to"
                    )
                values = list(cast(Sequence[str], body.get(spec.payload_key) or ()))
                if isinstance(action, AddList):
                    values.append(action.value)
                else:
                    values = [value for value in values if value != action.value]
                body[spec.payload_key] = tuple(values)
        _apply_create_defaults(kind, body)
        return body

    @classmethod
    def run_purge(
        cls,
        ref: Ref,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        client = client_factory()
        kind, target_id = client.resolve_id(ref)
        client.purge(
            target_id,
            actor=resolve_actor(args.actor, client),
            reason=args.reason or "del",
        )
        echo(f"deleted: {kind} {target_id}" if show_ids() else f"deleted: {kind} {ref}")

    @classmethod
    def run_remove_edge(
        cls,
        source: Ref,
        edge_kind: str,
        target: Ref,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        client = client_factory()
        _, src_id = client.resolve_id(source)
        _, tgt_id = client.resolve_id(target)
        client.remove_edge(
            src_id, tgt_id, edge_kind, actor=resolve_actor(args.actor, client)
        )
        echo(f"removed: {source} {edge_kind} {target}")

    @classmethod
    def run_edge_action(
        cls,
        ref: Ref,
        action: EdgeAction,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        # Any inline-create target -- flat (fields only) or a deep/wide subtree
        # (its own nested edges or costs) -- is built as one atomic batch
        # anchored at the existing leading subject, so the new row and its
        # anchor edge land or roll back together (I1/TRAX-425-007). Routing the
        # flat case through the same path closes the orphan window the old
        # two-call tail (``submit`` then ``add_edge``) left when the edge POST
        # failed after the row committed.
        if isinstance(action.target, InlineCreate):
            cls._run_anchored_inline_subtree(ref, action, args, client_factory)
            return
        target_ref: Ref = action.target
        source, target = (target_ref, ref) if action.edge.reverse else (ref, target_ref)
        if action.remove:
            cls.run_remove_edge(source, action.edge.name, target, args, client_factory)
            return
        if not action.annotate:
            cls.run_relation_add_edge(
                ref,
                (action.edge.name, action.edge.reverse),
                source,
                target,
                args,
                client_factory=client_factory,
            )
            return
        cls.run_add_edge(
            source,
            action.edge.name,
            target,
            action.metadata,
            args,
            client_factory=client_factory,
        )

    @classmethod
    def _run_anchored_inline_subtree(
        cls,
        ref: Ref,
        action: EdgeAction,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        """Build a deep/wide inline-create subtree anchored at an existing row.

        The mirror of :meth:`run_create` for the row-local edit path: the leading
        subject ``ref`` already exists, so the anchor edge sources it by ``id``
        and the inline target plus its whole nested subtree are flattened into one
        ``submit_batch``. Without this the edit path created only the immediate
        inline target and silently dropped its nested edges (TRAX-425-007).
        """
        target = cast(InlineCreate, action.target)
        client = client_factory()
        actor = resolve_actor(args.actor, client)
        _, anchor_id = client.resolve_id(ref)
        validate_writable_fields(target.kind, tuple(f.field for f in target.fields))

        items: list[tuple[Inquiry.InquiryKind, Mapping[str, object]]] = [
            (target.kind, _inline_create_body(target, actor, client))
        ]
        # The anchor edge: existing subject (by id) -> the inline target (item 0).
        edges: list[dict[str, object]] = [
            cls._batch_edge(action, from_id=anchor_id, to_index=0)
        ]
        deferred_costs: list[tuple[int, AddCost]] = [(0, cost) for cost in target.costs]
        # Nested edges of the inline target descend through the shared flatten
        # (F8), pulling existing-ref ids from one batch resolution (F14).
        flat_edges: list[tuple[EdgeAction, int, Ref | None]] = []
        resolved = iter(
            target_id
            for _kind, target_id in client.resolve_ids(
                cls._collect_existing_refs(target.edges)
            )
        )
        cls._flatten_inline_tree(
            target.edges,
            from_index=0,
            actor=actor,
            client=client,
            items=items,
            edges=edges,
            deferred_costs=deferred_costs,
            flat_edges=flat_edges,
            resolved=resolved,
        )
        ids = client.submit_batch(items, edges=edges)
        for node_index, cost in deferred_costs:
            client.add_cost(
                ids[node_index],
                cost_key(cost.field),
                cost.value,
                actor=actor,
                reason=args.reason,
            )
        bare_ids = args.format_ == "ids"
        # ``created:`` for every minted node, then ``added:`` for the anchor edge
        # and each nested edge -- the same echo contract ``run_create`` emits, so
        # the edit path no longer silently omits the relationship (F9).
        ref_by_index: dict[int, Ref] = {}
        for index, new_id in enumerate(ids):
            if bare_ids:
                echo(str(new_id))
                continue
            shown_ref = _submitted_ref(new_id, client)
            ref_by_index[index] = shown_ref
            echo(_created_line(shown_ref, new_id))
        if bare_ids:
            return
        # The anchor edge first (subject -> item 0), then nested edges in order.
        cls._echo_anchor_edge(ref, action, ref_by_index[0])
        next_inline_index = 1
        for nested, from_index, existing_ref in flat_edges:
            if existing_ref is None:
                shown_ref = ref_by_index[next_inline_index]
                next_inline_index += 1
            else:
                shown_ref = existing_ref
            anchor = ref_by_index[from_index]
            source, target_ref = (
                (shown_ref, anchor) if nested.edge.reverse else (anchor, shown_ref)
            )
            echo(f"added: {source} {nested.edge.name} {target_ref}")

    @classmethod
    def _echo_anchor_edge(
        cls, subject: Ref, action: EdgeAction, target_ref: Ref
    ) -> None:
        """Echo the ``added:`` line for the anchor edge subject -> inline target."""
        source, target = (
            (target_ref, subject) if action.edge.reverse else (subject, target_ref)
        )
        echo(f"added: {source} {action.edge.name} {target}")

    @classmethod
    def run_list_mutation(
        cls,
        ref: Ref,
        action: AddList | RemoveList,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        spec = FIELDS_BY_NAME.get(action.field)
        if spec is None or spec.shape != "list":
            raise ClientError(f"unknown list field {action.field!r}")
        include = isinstance(action, AddList)
        client = client_factory()
        actor = resolve_actor(args.actor, client)
        _, target_id = client.resolve_id(ref)
        verb_past = "added" if include else "removed"
        method = getattr(client, spec.list_add if include else spec.list_remove)
        if action.ref is not None:
            # Ref-list field: the parser attached the typed ref (trax #419). The
            # sole ref-list field (``codechanges``) is monomorphic, so the wire
            # method takes a bare id -- no kind, matching the client/server ABI.
            _value_kind, value_id = client.resolve_id(action.ref)
            method(target_id, value_id, actor=actor)
            echo(f"{verb_past}: {ref} {spec.cli_name} {action.ref}")
            return
        method(target_id, action.value, actor=actor)
        echo(f"{verb_past}: {ref} {spec.cli_name} {action.value}")

    @classmethod
    @override
    def help_text_for(cls, verb: str) -> str:
        """Help text for one kind verb."""
        return inquiry_help_text(verb)

    @classmethod
    @override
    def help_with_context(cls, verb: str, prefix: list[str]) -> str:
        """Per-kind help specialized to the tokens typed before ``help``."""
        return kind_help_for(KIND_LOWER[verb], prefix)


def _swap_edge_endpoints(
    src: Mapping[str, object], peer: Mapping[str, object]
) -> tuple[dict[str, object], dict[str, object]]:
    """Swap a ``from_*``/``to_*`` endpoint pair for a reverse-alias edge.

    A reverse alias stores ``peer -> source``, so the ``from_index``/``from_id``
    key on ``src`` becomes a ``to_*`` key and the ``to_*`` key on ``peer`` a
    ``from_*`` key, preserving whichever (index vs id) form each carried.
    """
    rename = {
        "from_index": "to_index",
        "from_id": "to_id",
        "to_index": "from_index",
        "to_id": "from_id",
    }
    new_src = {rename[k]: v for k, v in peer.items()}
    new_peer = {rename[k]: v for k, v in src.items()}
    return new_src, new_peer


def _resolve_stdin_actions(actions: Sequence[Action]) -> tuple[Action, ...]:
    """Resolve ``-`` (stdin) and ``@path`` value sentinels across all actions.

    Stdin may be consumed only once, so the flag threads through every action.
    """
    used_stdin = False
    resolved: list[Action] = []
    for action in actions:
        if isinstance(action, SetField):
            field, used = _resolve_field_value(action, used_stdin=used_stdin)
            used_stdin = used_stdin or used
            resolved.append(field)
        elif isinstance(action, EdgeAction) and isinstance(action.target, InlineCreate):
            target, used = _resolve_inline_create_values(
                action.target,
                used_stdin=used_stdin,
            )
            used_stdin = used_stdin or used
            resolved.append(
                EdgeAction(
                    edge=action.edge,
                    target=target,
                    metadata=action.metadata,
                    remove=action.remove,
                    annotate=action.annotate,
                )
            )
        else:
            resolved.append(action)
    return tuple(resolved)


def _resolve_inline_create_values(
    target: InlineCreate,
    *,
    used_stdin: bool,
) -> tuple[InlineCreate, bool]:
    """Resolve ``-`` and ``@path`` value sentinels inside one inline-create.

    Recurses into the node's nested ``edges`` (Issue#425 item 6) so a deep chain
    or a ``begin ... end`` group's field values are resolved too, and -- crucially
    -- the nested edge structure AND the node's ``costs`` are PRESERVED (rebuilding
    without them would silently drop the whole subtree or its cost deltas).
    """
    fields: list[SetField] = []
    used = False
    for field in target.fields:
        resolved, field_used = _resolve_field_value(
            field,
            used_stdin=used_stdin or used,
        )
        fields.append(resolved)
        used = used or field_used
    nested: list[EdgeAction] = []
    for action in target.edges:
        if isinstance(action.target, InlineCreate):
            inner, inner_used = _resolve_inline_create_values(
                action.target, used_stdin=used_stdin or used
            )
            used = used or inner_used
            nested.append(
                EdgeAction(
                    edge=action.edge,
                    target=inner,
                    metadata=action.metadata,
                    remove=action.remove,
                    annotate=action.annotate,
                )
            )
        else:
            nested.append(action)
    return (
        InlineCreate(
            kind=target.kind,
            fields=tuple(fields),
            edges=tuple(nested),
            costs=target.costs,
            inbound_meta=target.inbound_meta,
        ),
        used,
    )


def _resolve_field_value(field: SetField, *, used_stdin: bool) -> tuple[SetField, bool]:
    """Resolve one field value: ``-`` reads stdin, ``@path`` reads a file, else verbatim."""
    if not isinstance(field.value, str):
        return field, False
    if field.value == "-":
        if used_stdin:
            raise ClientError("stdin value can only be used once per command")
        return SetField(field=field.field, value=sys.stdin.read()), True
    if field.value.startswith("@"):
        path = field.value[1:]
        if not path:
            raise ClientError("@ value requires a path")
        try:
            return SetField(field=field.field, value=Path(path).read_text()), False
        except OSError as err:
            raise ClientError(f"cannot read @{path}: {err}") from err
    return field, False


def _inline_create_body(
    target: InlineCreate, actor: Inquiry.Actor, client: Client
) -> dict[str, object]:
    """Build an inline-create row body, resolving ref-list fields and defaults."""
    body: dict[str, object] = {"owner": actor}
    for field in target.fields:
        body[field.field] = _resolve_set_value(field, client)
    _apply_create_defaults(target.kind, body)
    return body


_DEFAULT_PRIORITY = 20


def _priority_or_default(priority: object) -> int:
    """Issue priority for sorting/display, defaulting only absent/None to 20.

    An explicit priority ``0`` (P0) is preserved -- the falsy-``or`` idiom would
    mis-map it to the medium default and sort/show a critical row as ordinary
    (F33).
    """
    return _DEFAULT_PRIORITY if priority is None else int(cast(int, priority))


def _apply_create_defaults(kind: Inquiry.InquiryKind, body: dict[str, object]) -> None:
    """Apply the CLI's per-kind ergonomic create defaults.

    These are convenience defaults so a bare ``trax issue title to X`` lands a
    usable row, not server requirements -- ``priority`` / ``judgement`` /
    ``confidence`` are all nullable columns.
    """
    if kind == "Issue":
        body.setdefault("priority", 20)
    elif kind == "Belief":
        body.setdefault("judgement", "unproven")
        body.setdefault("confidence", 0.5)


def _submitted_ref(target_id: uuid.UUID, client: Client) -> Ref:
    """Look up a just-created UUID's user-facing ``Kind#seq`` ref."""
    kind, _target_id, view = client.get_inquiry(UuidRef(uuid=target_id))
    self_view = cast(Mapping[str, object], view["self"])
    return SeqRef(kind=kind, seq=int(cast(int, self_view["seq"])))


def _created_line(ref: Ref, new_id: uuid.UUID) -> str:
    """Format the ``created:`` echo line, appending the UUID only under ``--show-ids``."""
    return f"created: {ref} {new_id}" if show_ids() else f"created: {ref}"


def resolve_actor(actor: str, client: Client) -> Inquiry.Actor:
    """Pick the audit actor: ``--as`` flag, profile, ``$USER``/``$USERNAME``, else ``user``."""
    return (
        actor
        or client.author
        or os.environ.get("USER")
        or os.environ.get("USERNAME")
        or "user"
    )


def kind_help_for(kind: Inquiry.InquiryKind, tokens: Sequence[str]) -> str:
    """Help for one kind, narrowing to a row or field as the tokens get longer."""
    prefix = kind.lower()
    if not tokens:
        return inquiry_help_text(prefix)
    seq = tokens[0] if tokens[0].isdigit() else "SEQ"
    if len(tokens) == 1 and starts_with_ref(tokens):
        return inquiry_help_text(prefix, seq=seq)
    if len(tokens) == 1:
        return inquiry_help_text(prefix)
    field_prefix = f"{prefix} {seq} {tokens[1]}"
    if len(tokens) == 2:
        return Kind.field_help.with_usage(f"trax {field_prefix} [to VALUE]").render()
    return Kind.field_set_help.with_usage(f"trax {field_prefix} to VALUE").render()


def _metrics_help(prefix: str, seq: str) -> str:
    """The Experiment-only ``metric`` grid section; empty for other kinds."""
    if prefix != "experiment":
        return ""
    return f"""
METRIC (experiment only) -- the (key, step) -> value grid, masked like numpy:
  trax {prefix} {seq} metric [at FIELD OP VALUE ...] [to VALUE] [sort ASC|DESC] [limit INT]

  at FIELD OP VALUE  masks a grid axis; clauses AND together
    FIELD: key | step | value       OP: is|ne|lt|le|gt|ge (and step max|min)
    at KEY  (bareword) is shorthand for  at key is KEY
  to VALUE           writes VALUE to every masked cell (step must be masked;
                     a multi-cell write needs --makeitso)
  no `to`            reads the masked cells, in (key, step) order

  trax {prefix} {seq} metric at key is loss at step is 3 to 0.5   one cell
  trax {prefix} {seq} metric at loss at step gt 3                 loss cells, step>3
  trax {prefix} {seq} metric at key is loss sort desc limit 5     loss's 5 largest
  trax {prefix} title to "run" metric at step is 3 at loss to 0.5 create + log
  trax {prefix} metric at loss at step is 100 sort desc limit 5   rank across experiments
"""


def inquiry_help_text(prefix: str, *, seq: str = "SEQ") -> str:
    """The full usage page for one inquiry kind."""
    return f"""\
Usage:
  trax {prefix} [--format FORMAT] [--limit INT]
  trax {prefix} FIELD to VALUE [FIELD to VALUE ...] [--as ACTOR] [--reason TEXT]
  trax {prefix} {seq} [ACTION]

Legend:
  SEQ     row number, e.g. 7
  KIND    issue|artifact|experiment|paper|belief|codechange|webresult|websearch|agentsession
  VALUE   text, number, URL, '-' for stdin, or '@path' to read a file
  FORMAT  table|json|ids
  SORT    priority|seq|recent|oldest|valence
  ACTOR   audit identity

ROW:
  trax {prefix}                                             list rows
  trax {prefix} {seq}                                           show row
  trax {prefix} {seq} del [--as ACTOR] [--reason TEXT]          delete row

FIELD:
  trax {prefix} FIELD to VALUE [FIELD to VALUE ...]             create row
  trax {prefix} {seq} FIELD [to VALUE]                          show / replace
  trax {prefix} {seq} agent-cost add VALUE                      signed USD delta

{_field_legend("scalar", "cost")}

LIST:
  trax {prefix} {seq} LIST [to|add|del VALUE]                   show / replace / append / remove

{_field_legend("list")}

  ``codechange`` takes a row SEQ as VALUE (e.g. ``codechange add 7``). The
  embedded list lives only on the row kind that declares it (``codechange`` on
  ``experiment``). To attach a CodeChange to a row of another kind, switch to
  the EDGE form ``produced codechange sha to <SHA>``.

RELATION:
  trax {prefix} {seq} RELATION [INDEX]                          list / select related rows

{_relation_legend()}
{_metrics_help(prefix, seq)}
EDGE:
  trax {prefix} {seq} EDGE KIND SEQ [FIELD to VALUE ...]        link rows; trailing fields annotate
  trax {prefix} {seq} EDGE KIND SEQ del                         unlink

{_edge_legend()}

Options:
  --format table|json|ids; --limit INT
  --sort priority|seq|recent|oldest|valence
  --as TEXT; --reason TEXT
"""


def _field_legend(*shapes: str) -> str:
    """Legend rows for the given field shapes: name, value shape, help."""
    fields = [
        spec for spec in FIELDS_BY_NAME.values() if spec.shape in shapes and spec.help
    ]
    name_width = max(len(spec.cli_name) for spec in fields)
    shape_label = {spec.cli_name: _value_shape(spec) for spec in fields}
    shape_width = max(len(label) for label in shape_label.values())
    return "\n".join(
        f"  {spec.cli_name.ljust(name_width)}  "
        f"{shape_label[spec.cli_name].ljust(shape_width)}  {spec.help}"
        for spec in fields
    )


def _value_shape(spec: Field) -> str:
    """The VALUE shape shown in the legend: ``<SEQ>`` for ref fields, else ``<VALUE>``."""
    if spec.ref_kind is not None:
        return "<SEQ>"
    return "<VALUE>"


def _relation_legend() -> str:
    """Relation keywords, each listed once per canonical relation."""
    seen: dict[str, str] = {}
    for key, (edge_kind, reverse) in RELATION_ALIASES.items():
        canonical = (edge_kind, reverse)
        seen.setdefault(f"{canonical[0]}{'.r' if canonical[1] else ''}", key)
    width = max(len(name) for name in seen.values())
    return "\n".join(f"  {name.ljust(width)}" for name in sorted(seen.values()))


def _edge_legend() -> str:
    """Edge keywords, one per line, sorted."""
    keys = sorted(EDGE_ALIASES)
    width = max(len(k) for k in keys)
    return "\n".join(f"  {k.ljust(width)}" for k in keys)


def run_list_query(
    query: ListQuery,
    args: argparse.Namespace,
    client_factory: Callable[[], Client],
) -> None:
    """List rows across the query's kinds, ranges, and filters.

    Filters go to the server, which applies them before ``LIMIT``, so the
    result is every match within ``limit`` regardless of DB size. Filtering
    locally after a limited fetch would silently drop matches past the
    recency window.
    """
    print_rows(
        _query_rows(query, client_factory(), limit=args.limit),
        args.format_,
        width=args.width,
    )


def _query_rows(
    query: ListQuery,
    client: Client,
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Fetch matching rows across the query's kinds, ranges, and filters.

    ``limit`` bounds the total returned set, not each kind: the budget is
    spent across kinds in order, so a multi-kind query never exceeds it and a
    caller can treat ``len(rows) == limit`` as a reliable truncation signal.
    """
    rows: list[dict[str, object]] = []
    for kind in query.kinds:
        if (remaining := limit - len(rows)) <= 0:
            break
        # The whole comma-separated union rides one ``list_kind`` call: the
        # server unions the intervals in a single indexed query and dedups
        # overlaps, so the CLI no longer fans out per interval.
        rows.extend(
            client.list_kind(
                kind,
                limit=remaining,
                seq_ranges=query.ranges.get(kind, ()),
                filters=query.filters,
            )
        )
    return rows


def run_bulk_apply(
    bulk: BulkApply,
    args: argparse.Namespace,
    client_factory: Callable[[], Client],
) -> None:
    """Apply field mutations to every row the query matches.

    A query matching more than one row requires ``--makeitso``; without it the
    matches are previewed and nothing is written. A zero- or single-row match
    applies directly, mirroring a seq-targeted edit.

    Selection uses ``MAX_LIST_LIMIT`` rather than the display ``--limit``: a
    write must cover the whole matched set, not a pagination window. A match
    that fills the ceiling is flagged, since rows past it would be missed.

    The matched set is queried once and reused for both the preview and the
    apply within this invocation. The documented preview-then-rerun workflow
    re-queries, so a concurrent edit between runs can shift the set.

    Args:
      bulk: The parsed query plus field mutations.
      args: Parsed CLI namespace, carrying ``makeitso`` and write flags.
      client_factory: Lazily builds the shared client.

    """
    client = client_factory()
    rows = _query_rows(bulk.query, client, limit=MAX_LIST_LIMIT)
    if len(rows) == MAX_LIST_LIMIT:
        echo(
            f"warning: matched the {MAX_LIST_LIMIT}-row ceiling; "
            "further matches are not included",
            err=True,
        )
    # The guard is keyed on match count, not command shape: a single-row match
    # is as safe as a seq-targeted edit, so only a genuinely multi-row write
    # demands explicit confirmation. This is intentional (reviewed).
    if len(rows) > 1 and not args.makeitso:
        echo(f"would apply to {len(rows)} rows; pass --makeitso to proceed:")
        print_rows(rows, args.format_, width=args.width)
        return
    actions = _resolve_stdin_actions(bulk.actions)
    for row in rows:
        row_kind = cast(Inquiry.InquiryKind, row["kind"])
        ref = SeqRef(kind=row_kind, seq=int(cast(int, row["seq"])))
        run_actions(ref, actions, args, client_factory, kind=row_kind)


def run_show(
    ref: Ref,
    args: argparse.Namespace,
    client_factory: Callable[[], Client],
) -> None:
    """Show one inquiry row."""
    client = client_factory()
    _kind, _target_id, payload = client.get_inquiry(ref)
    if args.format_ == "json":
        echo(fmt.format_json(payload), nl=False)
    else:
        echo(
            fmt.format_show(payload, changes=args.changes, include_id=show_ids()),
            nl=False,
        )


def run_field(
    ref: Ref,
    field: str,
    args: argparse.Namespace,
    client_factory: Callable[[], Client],
) -> None:
    """Print one field from one inquiry row."""
    del args
    client = client_factory()
    _kind, _target_id, payload = client.get_inquiry(ref)
    row = cast(Mapping[str, object], payload["self"])
    if field not in row:
        raise ClientError(f"field {field!r} not present on {ref}")
    echo(format_field_value(row[field]))


def run_cost_field(
    ref: Ref,
    field: str,
    args: argparse.Namespace,
    client_factory: Callable[[], Client],
) -> None:
    """Print one computed cost field for one inquiry row."""
    del args
    client = client_factory()
    _kind, target_id = client.resolve_id(ref)
    payload = client.cost_for(target_id)
    # ``cost_for`` keys by axis (``agent_usd`` / ``resource_usd``), not by
    # the ``marginal_cost_*`` column ``cost_key`` returns; strip the prefix.
    axis = cost_key(field).removeprefix("marginal_cost_")
    echo(f"{payload.get(axis, 0):.6f}")


def _split_metric_tail(
    rest: Sequence[str],
) -> tuple[Sequence[str], Sequence[str]] | None:
    """Split ``rest`` at the first ``metric`` keyword into ``(before, tail)``.

    ``metric`` is not a kind/field/edge/relation word, so its first appearance
    is unambiguously the grid-tail marker. Returns ``None`` when ``rest`` carries
    no ``metric`` word (an ordinary list/create/edit command).
    """
    for index, token_text in enumerate(rest):
        if token_text.lower() == "metric":
            return rest[:index], rest[index + 1 :]
    return None


def _mask_clauses(masks: Sequence[MetricMask]) -> list[MetricMaskClause]:
    """Translate parsed :class:`MetricMask`es into wire :class:`MetricMaskClause`es.

    A structural 1:1 map. The parser already narrows ``op`` to the metric op
    set, and the wire model's ``op`` type (:data:`MetricCompareOp` /
    :data:`MetricReduce`) re-validates it, so a bad op is rejected there -- the
    two share :data:`METRIC_COMPARE_OPS`, so they cannot disagree.
    """
    return [
        MetricMaskClause(
            axis=mask.field,
            op=cast("MetricCompareOp | MetricReduce", mask.op),
            value=mask.value,
        )
        for mask in masks
    ]


def _finite_float(raw: str) -> float:
    """Coerce a ``to`` value to a finite float, with a clean CLI error.

    ``float("nan")`` / ``float("inf")`` parse fine but are not valid JSON numbers
    and violate the DB CHECK; reject them here rather than leaking a deeper wire
    ValidationError (mirrors the old log-value guard).
    """
    try:
        value = float(raw)
    except ValueError as exc:
        raise ClientError(f"metric value must be a number, got {raw!r}") from exc
    if not math.isfinite(value):
        raise ClientError(f"metric value must be finite, got {raw!r}")
    return value


def run_add_cost(
    ref: Ref,
    field: str,
    value: float,
    args: argparse.Namespace,
    client_factory: Callable[[], Client],
) -> None:
    """Apply one signed cost delta to one inquiry row."""
    client = client_factory()
    _kind, target_id = client.resolve_id(ref)
    client.add_cost(
        target_id,
        cost_key(field),
        value,
        actor=resolve_actor(args.actor, client),
        reason=args.reason,
    )
    echo(f"added: {ref} {field} {value:.6f}")


def run_actions(
    ref: Ref,
    actions: Sequence[Action],
    args: argparse.Namespace,
    client_factory: Callable[[], Client],
    *,
    kind: Inquiry.InquiryKind,
) -> None:
    """Run the row-local actions in order against ``ref``.

    Validates every write field against ``kind`` before running any action:
    each field is its own request and transaction, so a kind-invalid field
    found mid-loop would otherwise 409 only after earlier fields had already
    committed. Up-front validation keeps the multi-field write atomic.
    """
    write_fields = tuple(
        action.field
        for action in actions
        if isinstance(action, SetField | AddList | RemoveList)
    )
    validate_writable_fields(kind, write_fields)
    for action in actions:
        run_action(ref, action, args, client_factory)


def run_action(
    ref: Ref,
    action: object,
    args: argparse.Namespace,
    client_factory: Callable[[], Client],
) -> None:
    """Dispatch one row-local action to its handler.

    The ladder is exhaustive over ``grammar.Action``; a new variant that
    skips this site hits the explicit ``else`` and raises, rather than
    silently falling through to something like a purge.
    """
    if isinstance(action, ReadField):
        if action.field in COST_FIELDS:
            run_cost_field(ref, action.field, args, client_factory)
        else:
            run_field(ref, action.field, args, client_factory)
    elif isinstance(action, SetField):
        run_set_field(ref, action, args, client_factory)
    elif isinstance(action, AddCost):
        run_add_cost(ref, action.field, action.value, args, client_factory)
    elif isinstance(action, AddList | RemoveList):
        Kind.run_list_mutation(ref, action, args, client_factory)
    elif isinstance(action, RelationAction):
        Kind.run_relation(
            ref,
            action.relation,
            (action.index,) if action.index else (),
            args,
            client_factory,
            against=action.against,
        )
    elif isinstance(action, EdgeAction):
        Kind.run_edge_action(ref, action, args, client_factory)
    elif isinstance(action, DeleteRow):
        Kind.run_purge(ref, args, client_factory)
    else:
        raise TypeError(f"unhandled Action variant: {type(action).__name__}")


def run_set_field(
    ref: Ref,
    action: SetField,
    args: argparse.Namespace,
    client_factory: Callable[[], Client],
) -> None:
    """Set one scalar field on one inquiry row."""
    client = client_factory()
    _, target_id = client.resolve_id(ref)
    value = _resolve_set_value(action, client)
    client.edit(
        target_id,
        action.field,
        value,
        actor=resolve_actor(args.actor, client),
        reason=args.reason,
    )
    echo(f"set: {ref} {action.field} = {_set_field_echo(action)}")


def _set_field_echo(action: SetField) -> object:
    """User-facing spelling of a set value: ref CLI form for ref-lists."""
    if action.field in REF_FIELD_BY_PAYLOAD and isinstance(action.value, tuple):
        return ", ".join(str(ref) for ref in cast("tuple[Ref, ...]", action.value))
    return action.value


def _resolve_set_value(action: SetField, client: Client) -> object:
    """Resolve a ref-list `... to KIND SEQ ...` value to its wire shape.

    For a ref-list field (``payload_key`` in :data:`REF_FIELD_BY_PAYLOAD`) the
    parser delivers ``action.value`` as a tuple of parsed :class:`Ref`s; each is
    resolved to a bare id (trax #419). The sole ref-list field (``codechanges``)
    is monomorphic, so the server stores bare ids. Plain ``SetField`` values pass
    through unchanged.
    """
    if action.field not in REF_FIELD_BY_PAYLOAD or not isinstance(action.value, tuple):
        return action.value
    return [
        str(client.resolve_id(ref)[1]) for ref in cast("tuple[Ref, ...]", action.value)
    ]


class Search(Command):
    """Search summaries and descriptions across kinds."""

    names = ("search",)
    help = """\
Usage: trax search QUERY... [OPTIONS]

Examples:
  trax search retry timeout                     search all subjects
  trax search retry --kind issue                restrict to issues
  trax search arxiv --kind paper --limit 20     limit results
  trax search bug --format json                 print JSON

Values:
  kinds: issue artifact experiment paper belief codechange webresult websearch agentsession

Options:
  --kind TEXT; --limit INT; --format table|json|ids
"""

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="trax search", description=cls.__doc__)
        parser.add_argument("query", nargs="+")
        parser.add_argument("--kind", default="")
        parser.add_argument("--limit", type=_positive_int, default=50)
        parser.add_argument(
            "--format",
            dest="format_",
            default="table",
            choices=("table", "json", "ids"),
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
        del verb
        client = client_factory()
        kind = parse_kind(args.kind) if args.kind else None
        rows = client.search(" ".join(args.query), kind=kind, limit=args.limit)
        print_rows(rows, args.format_)


class Recent(Command):
    """Recent audit-log entries."""

    names = ("recent",)
    help = HelpPage(
        usage="trax recent [OPTIONS]",
        summary="Show recent audit-log entries.",
        options=(
            ("--limit INT", "Maximum changes to return."),
            ("--format TEXT", "text|json."),
        ),
        examples=(
            "trax recent",
            "trax recent --limit 10",
            "trax recent --format json",
        ),
    )

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="trax recent", description=cls.__doc__)
        parser.add_argument("--limit", type=_positive_int, default=50)
        parser.add_argument(
            "--format",
            dest="format_",
            default="text",
            choices=("text", "json"),
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
        del verb
        rows = client_factory().recent_changes(limit=args.limit)
        if args.format_ == "json":
            echo(fmt.format_json(list(rows)), nl=False)
        else:
            echo(fmt.format_changes(list(rows)), nl=False)


class Id(Command):
    """Show one row by its global id, regardless of kind.

    ``id`` is already a filterable column; since a UUID is globally unique the
    leading kind is redundant, so ``trax id <uuid>`` resolves the row whatever
    its kind. Useful for scripts that capture a created row's id (``WS=$(trax
    ... --format ids)``) and chain on it without tracking which kind it was.
    """

    names = ("id",)
    help = """\
Usage: trax id <uuid> [OPTIONS]

Show one row by its global id, with no leading kind (the UUID is unique, so
the kind is redundant). Unlike ``trax <kind> <uuid>`` it applies no kind
typo-guard -- you asserted no kind to guard against.

Examples:
  trax id 29b5982f-2e1f-4749-9bb6-fe601444282c       show that row
  trax id "$WS"                                       show a captured row
  trax id "$WS" --format json                         print JSON

Options:
  --format table|json
  --changes        include the audit history
"""

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="trax id", description=cls.__doc__)
        parser.add_argument("uuid", metavar="UUID", help="the row's global id")
        parser.add_argument(
            "--format",
            dest="format_",
            default="table",
            choices=("table", "json"),
        )
        parser.add_argument("--changes", action="store_true")
        return parser

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        del verb
        try:
            target = uuid.UUID(args.uuid)
        except ValueError as exc:
            raise ClientError(f"trax id: {args.uuid!r} is not a valid uuid") from exc
        # ``expected_kind=None``: the caller named no kind, so no typo-guard --
        # the row's real kind is resolved server-side.
        run_show(UuidRef(uuid=target), args, client_factory)


class Next(Command):
    """Show the next unblocked active issue."""

    names = ("next",)
    help = """\
Usage: trax next [OPTIONS]

Examples:
  trax next                                     show next active issue
  trax next --format ids                       print selected row id
  trax next --format json                      print JSON

Options:
  --format text|json|ids
"""

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="trax next", description=cls.__doc__)
        parser.add_argument(
            "--format",
            dest="format_",
            default="text",
            choices=("text", "json", "ids"),
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
        del verb
        row = client_factory().next_issue()
        if row is None:
            echo("(no active issues)")
            return
        print_rows([row], args.format_)


class Blocked(Command):
    """List active issues that have at least one active prerequisite."""

    names = ("blocked",)
    help = """\
Usage: trax blocked

Examples:
  trax blocked                                  list active blocked issues
  trax issue 7 requires                        inspect prerequisites
  trax issue 7 required_by                     inspect issues it gates
"""

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        return argparse.ArgumentParser(prog="trax blocked", description=cls.__doc__)

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        del verb, args
        cls.render(client_factory().list_kind_all("Issue"))

    @classmethod
    def render(cls, rows: Sequence[Mapping[str, object]]) -> None:
        status_by_id = {
            str(row.get("id")): str(row.get("status") or "")
            for row in rows
            if row.get("id")
        }
        # Built once alongside status_by_id; the blocker-ref formatting below
        # reads it instead of rescanning ``rows`` per blocker, which was
        # O(rows x blockers) -- ~1e8 comparisons at limit=2000 (J1).
        seq_by_id = {
            str(row.get("id")): row.get("seq", "?") for row in rows if row.get("id")
        }
        blocked: list[tuple[Mapping[str, object], list[str]]] = []
        for row in rows:
            if str(row.get("status") or "") != "active":
                continue
            # ``requires`` projects the prerequisites this issue waits on, as
            # IssueEdge refs (dicts with an ``id``). An in-window prerequisite
            # that is no longer active is satisfied and dropped; an active OR
            # off-window (status unknown) prerequisite still blocks the row.
            prerequisites = [
                pid
                for ref in cast(
                    "Sequence[Mapping[str, object]]", row.get("requires") or ()
                )
                if (pid := str(ref.get("id")))
                and status_by_id.get(pid, "active") == "active"
            ]
            if prerequisites:
                blocked.append((row, prerequisites))
        if not blocked:
            echo("(no blocked issues)")
            return
        for row, prerequisites in blocked:
            prereq_refs = ", ".join(
                f"issue {seq_by_id[pid]}"
                if pid in seq_by_id
                else f"issue ?? ({pid} off-window)"
                for pid in prerequisites
            )
            priority = row.get("priority")
            echo(
                f"issue {row.get('seq', '?')!s:>4}  "
                f"[{('' if priority is None else priority)!s:>8}]  "
                f"{str(row.get('title', '') or '')[:50]:<50}  "
                f"requires: {prereq_refs}"
            )


def _ref_ids(refs: object) -> list[str]:
    """Peer ids from a relationship projection (a list of IssueEdge ref dicts)."""
    return [
        pid
        for ref in cast("Sequence[Mapping[str, object]]", refs or ())
        if (pid := str(ref.get("id")))
    ]


class Graph(Command):
    """Print the issue dependency tree along ``requires`` edges."""

    names = ("graph",)
    help = """\
Usage: trax graph [OPTIONS]

Examples:
  trax graph                                    show issue dependency tree
  trax graph --open-only                       hide closed issues

Options:
  --open-only
"""

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="trax graph", description=cls.__doc__)
        parser.add_argument("--open-only", dest="open_only", action="store_true")
        return parser

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        del verb
        # Page past the server's per-request cap so the dependency forest is
        # COMPLETE -- a single capped fetch would silently drop issues past the
        # ceiling, rendering a partial tree with no warning.
        rows = client_factory().list_kind_all("Issue")
        if args.open_only:
            rows = [r for r in rows if str(r.get("status") or "") == "active"]
        cls.render(rows)

    @classmethod
    def render(cls, rows: Sequence[Mapping[str, object]]) -> None:
        if not rows:
            echo("(no issues)")
            return
        rows_by_id = {str(row.get("id")): row for row in rows if row.get("id")}
        # A root depends on nothing in-window: no issue lists it as a
        # prerequisite (no edge points up to it via another row's ``requires``).
        depended_on = {
            pid
            for row in rows
            for pid in _ref_ids(row.get("requires"))
            if pid in rows_by_id
        }
        roots = sorted(
            (row for row in rows if str(row.get("id")) not in depended_on),
            key=lambda row: int(cast(int, row.get("seq", 0))),
        )
        # ``visited`` is per-root, so a node reachable from two roots
        # renders under each; it only breaks cycles within one traversal.
        for root in roots:
            cls._render_tree(root, rows_by_id, set(), depth=0)

    @classmethod
    def _render_tree(
        cls,
        row: Mapping[str, object],
        rows_by_id: Mapping[str, Mapping[str, object]],
        visited: set[str],
        *,
        depth: int,
    ) -> None:
        row_id = str(row.get("id"))
        if row_id in visited:
            echo(f"{'  ' * depth}cycle issue {row.get('seq', '?')}")
            return
        visited.add(row_id)
        echo(
            f"{'  ' * depth}issue {row.get('seq', '?')} "
            f"[{row.get('status') or '?'!s}] "
            f"{str(row.get('title', '') or '')[:50]}"
        )
        for child_id in _ref_ids(row.get("requires")):
            child = rows_by_id.get(child_id)
            if child is not None:
                cls._render_tree(child, rows_by_id, set(visited), depth=depth + 1)
            else:
                # An off-window prerequisite is not in the fetched set; render it
                # as a stub rather than silently dropping a real dependency (F31).
                echo(f"{'  ' * (depth + 1)}issue ?? ({child_id} off-window)")


class Board(Command):
    """List issues grouped by status."""

    names = ("board",)
    help = """\
Usage: trax board [OPTIONS]

Examples:
  trax board                                    group issues by status
  trax board --width 100                       constrain output width
  trax issue 7 status to complete              move issue to complete

Values:
  status: active complete abandoned invalid

Options:
  --width INT                                  truncate rows to this width
"""

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="trax board", description=cls.__doc__)
        parser.add_argument("--width", type=int, default=None)
        return parser

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        del verb
        # Page past the per-request cap so the board shows EVERY issue, not a
        # silently-truncated first window.
        cls.render(client_factory().list_kind_all("Issue"), width=args.width)

    @classmethod
    def render(
        cls, rows: Sequence[Mapping[str, object]], *, width: int | None = None
    ) -> None:
        if not rows:
            echo("(no issues)")
            return
        selected_width = table_width(width)
        groups: dict[str, list[Mapping[str, object]]] = {}
        for row in rows:
            groups.setdefault(str(row.get("status") or "?"), []).append(row)
        for status in get_args(Inquiry.Status.__value__):
            bucket = groups.get(status, [])
            if not bucket:
                continue
            echo(f"\n== {status} ({len(bucket)}) ==")
            for row in bucket:
                echo(cls._format_row(row, width=selected_width))

    @classmethod
    def _format_row(cls, row: Mapping[str, object], *, width: int) -> str:
        priority = row.get("priority")
        prefix = (
            f"  Issue#{row.get('seq', '?')} "
            f"P{('?' if priority is None else priority)!s} "
            f"{table_cell(str(row.get('owner') or '(unassigned)'), 16)} "
        )
        if width <= 0:
            return f"{prefix}{table_cell(str(row.get('title', '') or ''), 0)}"
        return f"{prefix}{table_cell(str(row.get('title', '') or ''), max(width - len(prefix), 1))}"


class Cost(Command):
    """Show the agent and resource cost of one row, optionally over its subtree."""

    names = ("cost",)
    help = """\
Usage: trax cost KIND SEQ [OPTIONS]

Examples:
  trax cost issue 7                            show direct agent-cost and resource-cost
  trax cost issue 7 --deep                     include subtree agent-cost and resource-cost
  trax cost belief 3 --format json             print JSON

Values:
  kinds: issue artifact experiment paper belief codechange webresult websearch agentsession

Options:
  --deep; --format text|json
"""

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="trax cost", description=cls.__doc__)
        parser.add_argument("kind", choices=list(KIND_LOWER), type=str.lower)
        parser.add_argument("seq", type=int)
        parser.add_argument("--deep", action="store_true")
        parser.add_argument(
            "--format",
            dest="format_",
            default="text",
            choices=("text", "json"),
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
        del verb
        client = client_factory()
        ref = SeqRef(kind=KIND_LOWER[args.kind], seq=args.seq)
        _, target_id = client.resolve_id(ref)
        payload = client.cost_for(target_id, deep=args.deep)
        if args.format_ == "json":
            echo(fmt.format_json(payload), nl=False)
            return
        scope = "subtree" if args.deep else "self"
        echo(f"scope:    {scope}")
        echo(f"agent:    ${payload.get('agent_usd', 0):.6f}")
        echo(f"resource: ${payload.get('resource_usd', 0):.6f}")


class Send(Command):
    """Send a message into a live agent session by routing name."""

    names = ("send",)
    help = """\
Usage: trax send @ACTOR[:ROOM] TEXT...

Examples:
  trax send @scientist "check the logs"         message a live session
  trax send @scientist:sear "status?"           scope to a room
  trax send scientist hello                      the @ is optional

Notes:
  The target is an AgentSession's routing name (its --as owner). The message
  is injected into the live CLI's input. Delivery is drop-if-absent: if no
  live session matches, nothing is sent and the receipt says so.
"""

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="trax send", description=cls.__doc__)
        parser.add_argument("target", help="@actor or @actor:room")
        parser.add_argument("text", nargs="+", help="message body")
        return parser

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        del verb
        actor, room = _parse_target(args.target)
        delivered = client_factory().send_message(actor, " ".join(args.text), room=room)
        if not delivered:
            scope = f"@{actor}:{room}" if room else f"@{actor}"
            echo(f"undelivered: no live session matches {scope}")
            return
        echo(f"sent to {len(delivered)} session(s)")


class Version(Command):
    """Show the running server's build SHA, for stale-deploy detection."""

    names = ("version",)
    help = """\
Usage: trax version

Examples:
  trax version                                  print the server's build SHA

Notes:
  Compare against your local 'git rev-parse HEAD' to tell whether the
  deployed server predates your latest push. A 404 means the server is
  too old to expose /api/version -- itself a staleness signal.
"""

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        return argparse.ArgumentParser(prog="trax version", description=cls.__doc__)

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        del verb, args
        echo(client_factory().version())


def _parse_target(target: str) -> tuple[str, str | None]:
    """Split a ``@actor[:room]`` target into ``(actor, room)``.

    The leading ``@`` is optional; a single ``:`` separates an optional room.
    """
    spec = target.removeprefix("@")
    actor, sep, room = spec.partition(":")
    if not actor:
        raise ClientError("send target must name an actor (e.g. @scientist)")
    if sep and not room:
        raise ClientError(
            f"empty room after ':' in target '{target}'; "
            "drop the ':' or name a room (e.g. @scientist:sear)"
        )
    return actor, (room or None)
