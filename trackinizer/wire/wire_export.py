"""Wire contract for the logical export: the whole graph as JSON lines.

A datadir snapshot is bound to the storage internals that wrote it. The
export is the portable form: every inquiry, edge, and ``change_log`` row, plus
the per-kind detail tables, as one JSON object per line. It is read-only;
nothing here imports it back.

Line 1 is the header::

    {"format": "trackinizer-export", "version": 1, "migrations": [...]}

``migrations`` names the applied schema files, oldest first, so a reader knows
which column set the rows were written under. Every later line is one row::

    {"table": "<name>", "row": {"<column>": <value>, ...}}

A ``?filter=`` selector narrows the export to the subgraph whose inquiries
match, and the header then carries what was asked for::

    {"format": ..., "migrations": [...],
     "selector": [{"field": "labels", "op": "is", "value": "org:rekursiv"}]}

The key is ABSENT from a whole-graph export rather than empty, so those stay
byte-identical to the ones written before selectors existed. A reader must
not treat a subgraph as a graph: a row it does not find may be excluded
rather than missing, so ``selector`` is the difference between a backup and
one collaborator's slice.

Tables arrive in :data:`EXPORT_TABLES` order (a row's parent before the row)
and each table in a fixed order, so an unchanged database exports
byte-for-byte identically. Values are plain JSON: UUIDs and timestamps as
strings, ``TEXT[]`` as lists, and ``json`` / ``jsonb`` columns as the value
they hold.

This package is part of the publishable client distribution, so it must not
import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from typing import Final


__all__ = [
    "EXPORT_API_PATH",
    "EXPORT_API_PATHS",
    "EXPORT_FILTER_PARAM",
    "EXPORT_FORMAT",
    "EXPORT_MEDIA_TYPE",
    "EXPORT_SELECTOR_FIELDS",
    "EXPORT_TABLES",
    "EXPORT_VERSION",
]


EXPORT_API_PATH: Final = "/api/export"
"""``GET`` streams the export. A read, so the ``viewer`` role suffices."""

EXPORT_API_PATHS: tuple[str, ...] = (EXPORT_API_PATH,)
"""Registry the route drift test checks against the live app and the docs."""

EXPORT_FILTER_PARAM: Final = "filter"
"""Repeated ``?filter=<json>`` selector clauses, ANDed. Same spelling as the
list routes' own ``filter``, so one filter vocabulary serves both."""

EXPORT_SELECTOR_FIELDS: Final = frozenset({"labels"})
"""What a selector may filter on.

Labels alone, because a label is what the deployments in the request
partition by (``org:<x>``, ``machine:<host>``). Every other column selects
rows that share a value rather than a partition, and a subgraph carved on
``priority`` or ``title`` is a question nobody has asked. Widening this is
one line when someone does."""

EXPORT_MEDIA_TYPE: Final = "application/x-ndjson"

EXPORT_FORMAT: Final = "trackinizer-export"

EXPORT_VERSION: Final = 1
"""Bumped when the line shape changes, not when a column is added: a new
column is visible in ``migrations`` and in the rows themselves."""

EXPORT_TABLES: Final = (
    "inquiries",
    "edges",
    "change_log",
    "experiment_metrics",
    "session_manifests",
    "session_records",
    "session_slash_commands",
)
"""What the export carries, in the order it writes them.

Left out on purpose, and why:

* ``inquiry_embeddings``, ``session_embeddings`` -- derived from each row's
  text by the embedder, and specific to the model that produced them.
* ``session_index_state`` -- embedding-progress bookkeeping, derived from
  ``session_embeddings``.
* ``session_ciphertext``, ``session_bodies`` -- encrypted / cold replay-only
  blobs, which retention exists to drop.
* ``users``, ``api_keys``, ``allowlist`` -- credentials and access control,
  not the graph.
* ``applied_migrations`` -- carried in the header instead.
"""
