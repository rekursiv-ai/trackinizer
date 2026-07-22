"""Service-meta routes: build version plus the SPA's reflected type metadata.

Deliberately unauthenticated and store-free: a deploy probe must answer
before auth or the database is reachable, so an operator can confirm the
running build SHA without credentials. See ``server/version.py`` for how
the SHA is resolved. The metadata routes (``/api/meta/enums``,
``/api/meta/fields``, ``/api/meta/edges``) are likewise store-free -- each
reflects the type ``Literal``s / route table so the SPA needs no
hand-maintained copy of the enums, field-owner map, or edge topology.
"""

from __future__ import annotations

from typing import get_args

from fastapi import APIRouter

from trackinizer.server.version import build_sha
from trackinizer.types.edges import (
    Edge,
    edge_labels,
    edge_topology,
)
from trackinizer.types.inquiries import (
    Belief,
    Inquiry,
    Issue,
    Paper,
)
from trackinizer.wire.routes import field_owner_kind


router = APIRouter()


@router.get("/api/version")
async def version_route() -> dict[str, str]:
    """Return the running server's build SHA.

    ``{"sha": "<hex>"}`` when known (from ``$TRACKINIZER_SHA`` or the
    checkout's ``git HEAD``), else ``{"sha": "unknown"}``. A 404 instead
    means the live binary predates this endpoint -- itself a staleness
    signal.
    """
    return {"sha": build_sha()}


def enum_values() -> dict[str, list[str]]:
    """The closed-set vocabularies the SPA renders as ``<select>`` options.

    Each list is read straight off its type ``Literal`` via ``get_args`` so
    there is exactly one source of truth (the type), not a copy in the page.
    Adding a publication-type / issue-kind / edge-kind here is automatic.
    """
    return {
        "status": list(get_args(Issue.Status.__value__)),
        "judgement": list(get_args(Belief.Judgement.__value__)),
        "issue_kind": list(get_args(Issue.Kind.__value__)),
        "publication_type": list(get_args(Paper.PublicationType.__value__)),
        "edge_kind": list(get_args(Edge.Kind.__value__)),
        # The inquiry-kind taxonomy the SPA needs for its kind dropdowns,
        # short-ref (Kind#seq) parser, and edge/supersede pickers. Mirrored
        # from the type Literals so the SPA never hand-types them (the
        # favors-flip drift class -- a hand-typed JS copy that lags Python).
        "inquiry_kind_all": list(get_args(Inquiry.InquiryKind.__value__)),
    }


@router.get("/api/meta/enums")
async def enums_route() -> dict[str, list[str]]:
    """Return every closed-set enum the SPA needs, reflected from the types.

    The single-page app fetches this on load to populate its ``<select>``
    controls (status, judgement, issue kind, paper publication type, edge
    kind) instead of hard-coding the lists, so a new Literal member can never
    desync the UI.
    """
    return enum_values()


@router.get("/api/meta/fields")
async def fields_route() -> dict[str, str]:
    """Return the ``field -> owning kind`` map for kind-scoped edit routes.

    The SPA builds its per-field edit URL (``/api/<owner>/<id>/<field>``) from
    this instead of a hand-typed copy, so a new kind-specific field can never
    desync the UI from the server's route table.
    """
    return field_owner_kind()


@router.get("/api/meta/edges")
async def edges_route() -> dict[str, dict[str, list[str] | str]]:
    """Return the edge topology + labels (``edge_kind -> {from_kinds, to_kinds,
    forward, inverse}``).

    The SPA derives BOTH its edge picker (which kinds each edge admits on each
    stored endpoint) and its ``edgeDisplayName`` relation labels from this
    instead of hard-coding either. The topology backs the schema CHECK and is
    pinned to it by ``server/edge_topology_test``, so a citation-direction or
    label change updates one server place and the SPA follows -- it can no
    longer hold a stale hand-typed copy.
    """
    topology = edge_topology()
    labels = edge_labels()
    return {kind: {**topology[kind], **labels[kind]} for kind in topology}
