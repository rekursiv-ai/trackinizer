"""HTTP transport for the trackinizer server.

A thin wrapper over a persistent ``httpx.Client`` that round-trips JSON
against the API. Pure transport: no profile loading, no argparse, no
connection-resolution chain. The CLI layer composes a profile and flags
into a constructor call; ``Client`` itself knows only ``base_url`` /
``author`` / ``api_key`` / ``timeout_sec``.

Every mutating (non-GET) request carries an ``Idempotency-Key`` header.
The server uses that UUID as the ``change_log.id`` of the change, so a
retry after a lost response collides on the primary key and the server
replays the original outcome instead of applying it twice. ``Client``
retries 5xx (500/502/503/504) and read-timeout failures with the *same*
UUID so the retry stays dedup-eligible; a stale pooled socket is handled
transparently by ``httpx.HTTPTransport(retries=1)``. ``500`` is included
because the single-writer PGlite substrate can return a transient 500
under concurrent load, and the idempotency key makes the replay safe (a
write that already landed dedups on the primary key rather than applying
twice).

The key dedups the FIRST ``change_log`` row of a request. Single-change
mutations (the overwhelming majority) are therefore fully replay-safe. A
multi-change endpoint (e.g. ``/api/edges/batch``) emits one row per item but
carries a single key, so only its first item is dedup-eligible on a blind
replay; this is not a live hazard because that route catches per-item errors
and returns partial-success (HTTP 200) rather than 500-ing mid-batch, so a
retry is only triggered when nothing -- or everything -- has been applied.

Connect and write failures (``ConnectError`` / ``ConnectTimeout`` /
``WriteError`` / ``WriteTimeout``) are wrapped in ``ClientError`` but *not*
retried: a write may already have reached the server, so a blind retry
could duplicate the operation. Either way the raw httpx exception never
escapes the ``ClientError`` contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Self, cast
from urllib.parse import urlparse

import json
import time
import uuid

import httpx
import pydantic

from trackinizer.client.errors import ClientError
from trackinizer.lib.custom_types import ABSENT, Absent
from trackinizer.types.inquiries import Inquiry, Issue
from trackinizer.wire.filters import Filter
from trackinizer.wire.refs import Ref, SeqRef, UuidRef
from trackinizer.wire.routes import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    inquiry_field_path,
)
from trackinizer.wire.seq_ranges import SeqRange, format_interval


if TYPE_CHECKING:
    from trackinizer.wire import (
        wire_metrics,
        wire_metrics_query,
        wire_sessions,
    )
    from trackinizer.wire.wire_metrics import (
        LogMetricsResponse,
        MetricPoint,
    )
    from trackinizer.wire.wire_metrics_query import (
        MetricMaskClause,
        MetricRankRow,
    )
    from trackinizer.wire.wire_sessions import (
        AppendEventsResponse,
        EventBody,
        SessionEnd,
        SessionEndResponse,
        SessionStart,
        SessionStartResponse,
    )
else:
    from wrapt import lazy_import

    # ``wire_sessions`` builds its pydantic models at import (~200ms), paid on
    # every ``trax`` cold start even though only ``trax run --sync`` opens a
    # session. Bind it as a lazy module proxy so the import fires on first
    # attribute access -- inside the session methods below, never on cold start.
    wire_sessions = lazy_import("trackinizer.wire.wire_sessions")
    # Same lazy-bind for ``wire_metrics``: only ``log_metrics`` / ``read_metrics``
    # touch it, so its pydantic-model build stays off the cold-start path.
    wire_metrics = lazy_import("trackinizer.wire.wire_metrics")
    wire_metrics_query = lazy_import("trackinizer.wire.wire_metrics_query")


def _field_path(target_id: uuid.UUID, field: str) -> str:
    """Fill in the inquiry field-route path for one inquiry.

    The template lives in :func:`wire.routes.inquiry_field_path`, so the
    client never spells the path itself; it only supplies the id.
    """
    return inquiry_field_path(field).format(target_id=target_id)


# Hard cap on a single HTTP round-trip, so a hung or firewalled server
# can't stall every ``trax`` invocation indefinitely.
DEFAULT_TIMEOUT_SEC: float = 30.0

# Idle-socket lifetime in the client pool. Kept below the smallest
# upstream idle timeout in the path (Cloudflare's ~100s, Caddy's 2m) so
# the client is the one that decides when to evict.
_KEEPALIVE_EXPIRY_SEC: float = 90.0

# Attempts a mutating request gets after a 5xx or read timeout. Three
# bounds the worst-case wait (~1s) while covering the common cases: one
# bad pool socket, one transient 502.
_RETRY_ATTEMPTS: int = 3

# Cap on how much server error text rides into a ``ClientError`` message.
# An unbounded body would balloon logs/memory and could echo a secret
# verbatim; a 2KB prefix keeps the diagnostic useful while bounding both.
_MAX_ERROR_TEXT_CHARS: int = 2_048


def _truncate(text: str, limit: int = _MAX_ERROR_TEXT_CHARS) -> str:
    """Return ``text`` capped at ``limit`` chars, with an ellipsis marker."""
    return text if len(text) <= limit else f"{text[:limit]}... (truncated)"


def _require_mapping(payload: object, where: str) -> Mapping[str, object]:
    """Return ``payload`` as a mapping, or raise a wrapped ``ClientError``.

    A server response of the wrong JSON type would otherwise leak a raw
    ``TypeError`` when a caller subscripts it, past the ClientError contract.
    """
    if not isinstance(payload, Mapping):
        raise ClientError(f"{where} returned a malformed payload: {payload!r}")
    return cast(Mapping[str, object], payload)


def _require_list(payload: object, where: str) -> list[Any]:
    """Return ``payload`` as a list, or raise a wrapped ``ClientError``."""
    if not isinstance(payload, list):
        raise ClientError(f"{where} returned a malformed payload: {payload!r}")
    return cast(list[Any], payload)


def _require_field(payload: object, field: str, where: str) -> object:
    """Return ``payload[field]``, wrapping a missing key or wrong type."""
    mapping = _require_mapping(payload, where)
    if field not in mapping:
        raise ClientError(f"{where} returned a malformed payload: missing {field!r}")
    return mapping[field]


def _require_uuid(value: object, where: str) -> uuid.UUID:
    """Parse ``value`` into a ``UUID``, wrapping a bad value as ``ClientError``."""
    try:
        return uuid.UUID(cast(str, value))
    except (ValueError, TypeError, AttributeError) as err:
        raise ClientError(f"{where} returned a malformed id {value!r}: {err}") from err


def _validate_model[M: pydantic.BaseModel](
    model: type[M], response: object, where: str
) -> M:
    """Validate ``response`` into ``model``, wrapping pydantic errors.

    A malformed session-route response would otherwise leak a raw
    ``pydantic.ValidationError`` past the ClientError contract.
    """
    try:
        return model.model_validate(response)
    except pydantic.ValidationError as err:
        raise ClientError(f"{where} returned a malformed payload: {err}") from err


def server_url(raw: str, source: str) -> str:
    """Return a normalized HTTP(S) URL, or raise ``ClientError``.

    Only scheme, host, port, and an optional path are allowed; a URL with
    embedded credentials, a query, or a fragment is rejected, as is a
    missing host or a malformed / out-of-range port. Bearer credentials
    belong in the ``api_key`` field, not the URL.
    """
    url = raw.rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ClientError(f"{source} has invalid URL {raw!r}")
    if parsed.username or parsed.password:
        raise ClientError(
            f"{source} URL must not embed credentials; use api_key instead"
        )
    if parsed.query or parsed.fragment:
        raise ClientError(f"{source} URL must not contain query or fragment")
    if not parsed.hostname:
        raise ClientError(f"{source} has invalid URL {raw!r}: missing host")
    # ``parsed.port`` raises ``ValueError`` for a non-numeric or out-of-range
    # port (urllib only validates it on access), so a malformed port would
    # otherwise escape the ClientError contract here rather than failing later.
    try:
        _ = parsed.port
    except ValueError as err:
        raise ClientError(f"{source} has invalid URL {raw!r}: {err}") from err
    return url


def _clean_params(
    params: Mapping[str, object] | None,
) -> tuple[tuple[str, str], ...] | None:
    """Drop ``None`` and empty values, stringifying the rest for httpx.

    A sequence-valued entry emits one repeated query param per element
    (``filter`` is the current consumer), with empty elements dropped just
    like empty scalars. Returns a tuple of ``(key, value)`` pairs, which
    matches httpx's ``QueryParams`` signature without an unsafe cast.
    """
    if not params:
        return None
    out: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple)):
            for item in cast(Sequence[object], value):
                if item is None or item == "":
                    continue
                out.append((key, str(item)))
        else:
            out.append((key, str(value)))
    return tuple(out) or None


class EdgeWrite(NamedTuple):
    """Outcome of an edge upsert.

    ``created`` is ``True`` for a brand-new edge; ``changed`` is ``True`` when
    the server emitted a change (a create, or an annotation applied to an
    existing edge). A no-op (existing edge, nothing to set) is
    ``EdgeWrite(created=False, changed=False)``.
    """

    created: bool
    changed: bool


class Client:
    """Thin HTTP client over the trackinizer server."""

    def __init__(
        self,
        base_url: str,
        *,
        author: str = "",
        api_key: str = "",
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self.base_url = server_url(base_url, "base_url")
        self.author = author
        self.api_key = api_key
        self.timeout_sec = timeout_sec
        headers: dict[str, str] = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # ``timeout_sec`` is the per-read budget, not total wall-clock;
        # with retries a single call can wait up to ~3x that. The other
        # phases are split out: a short connect window covers DNS+TLS+TCP,
        # write is short because bodies are tiny, and pool guards against
        # connection-pool starvation.
        self._http: httpx.Client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=5.0,
                read=timeout_sec,
                write=10.0,
                pool=5.0,
            ),
            limits=httpx.Limits(keepalive_expiry=_KEEPALIVE_EXPIRY_SEC),
            transport=httpx.HTTPTransport(retries=1),
            headers=headers,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def get(self, path: str, *, params: Mapping[str, object] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, *, body: object = None) -> Any:
        return self._request("POST", path, body=body)

    def put(self, path: str, *, body: object = None) -> Any:
        return self._request("PUT", path, body=body)

    def patch(self, path: str, *, body: object = None) -> Any:
        return self._request("PATCH", path, body=body)

    def delete(self, path: str, *, body: object = None) -> Any:
        return self._request("DELETE", path, body=body)

    # -- Reference resolution ------------------------------------------------

    def resolve_id(self, ref: Ref) -> tuple[Inquiry.InquiryKind, uuid.UUID]:
        """Resolve a ref to ``(kind, uuid)``."""
        if isinstance(ref, SeqRef):
            where = f"/api/inquiries/{ref.kind}/{ref.seq}"
            view = self.get(where)
            if view is None:
                raise ClientError(f"{ref} not found")
            return ref.kind, _require_uuid(_require_field(view, "id", where), where)
        # UuidRef: one lookup for the kind. If the user gave an
        # ``expected_kind``, guard against a typo where it doesn't match.
        where = f"/api/web/lookup/{ref.uuid}"
        kind = cast(Inquiry.InquiryKind, _require_field(self.get(where), "kind", where))
        if ref.expected_kind is not None and ref.expected_kind != kind:
            raise ClientError(
                f"ref {ref.expected_kind} {ref.uuid} resolves to a {kind} row"
            )
        return kind, ref.uuid

    def resolve_ids(
        self, refs: Sequence[Ref]
    ) -> list[tuple[Inquiry.InquiryKind, uuid.UUID]]:
        """Resolve many refs in one go.

        UUID refs resolve in a single lookup round-trip; SeqRefs still go
        one-by-one (rare in bulk). Output order matches input order, so a
        caller can zip with the original list.
        """
        uuid_indices = [i for i, r in enumerate(refs) if isinstance(r, UuidRef)]
        if uuid_indices:
            uuid_refs = [cast(UuidRef, refs[i]) for i in uuid_indices]
            response = self.post(
                "/api/inquiries/lookup",
                body=[str(r.uuid) for r in uuid_refs],
            )
            # The route returns ``{"found": {id: kind}, "missing": [id]}``;
            # ``resolve_ids`` raises on any missing ref below, so it reads
            # only the found mapping here.
            where = "/api/inquiries/lookup"
            uuid_kinds = cast(
                dict[str, str],
                _require_mapping(_require_field(response, "found", where), where),
            )
        else:
            uuid_kinds = {}
        out: list[tuple[Inquiry.InquiryKind, uuid.UUID]] = []
        for ref in refs:
            if isinstance(ref, SeqRef):
                out.append(self.resolve_id(ref))
                continue
            kind_str = uuid_kinds.get(str(ref.uuid))
            if kind_str is None:
                raise ClientError(f"{ref} not found")
            kind = cast(Inquiry.InquiryKind, kind_str)
            if ref.expected_kind is not None and ref.expected_kind != kind:
                raise ClientError(
                    f"ref {ref.expected_kind} {ref.uuid} resolves to a {kind} row"
                )
            out.append((kind, ref.uuid))
        return out

    # -- Reads --------------------------------------------------------------

    def list_kind(
        self,
        kind: Inquiry.InquiryKind,
        *,
        status: Inquiry.Status | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        seq_ranges: Sequence[SeqRange] = (),
        filters: Sequence[Filter] = (),
    ) -> list[dict[str, Any]]:
        # Each filter rides as its own ``filter=<json>`` query param, so
        # values containing separators round-trip without escaping. The
        # server applies them before LIMIT to keep the result set honest.
        # ``seq_range`` repeats the same way: one ``a..b`` interval per
        # param, their union selecting rows across disjoint seq windows.
        return _require_list(
            self.get(
                "/api/inquiries",
                params={
                    "kind": kind,
                    "status": status,
                    "limit": limit,
                    "offset": offset,
                    "seq_range": [format_interval(r) for r in seq_ranges],
                    "filter": [
                        json.dumps(
                            {
                                "field": filt.field,
                                "op": filt.op,
                                "value": filt.value,
                            },
                            separators=(",", ":"),
                        )
                        for filt in filters
                    ],
                },
            ),
            "/api/inquiries",
        )

    def list_kind_all(
        self,
        kind: Inquiry.InquiryKind,
        *,
        status: Inquiry.Status | None = None,
        seq_ranges: Sequence[SeqRange] = (),
        filters: Sequence[Filter] = (),
    ) -> list[dict[str, Any]]:
        """Fetch EVERY matching row, paging past the server's per-request cap.

        ``list_kind`` is bounded by ``MAX_LIST_LIMIT`` (the route rejects a
        larger ``limit`` with 400). Whole-collection views (``graph``,
        ``board``, ``blocked``) want the complete set, so this pages by
        ``offset`` in ``MAX_LIST_LIMIT`` chunks until a short (or empty) page,
        concatenating results. A page exactly filling the cap is not assumed to
        be the last -- only a page *under* the cap proves the end -- so a
        collection that is a clean multiple of the cap still terminates (the
        next page comes back empty).

        Pagination is OFFSET-based, which is stable for this project's usage
        (a single operator's snapshot read of an O(hundreds)-row collection).
        It is NOT robust to a concurrent insert landing between two page
        fetches: the server orders by ``created DESC, id DESC``, so a row
        inserted mid-walk shifts the window and ``OFFSET`` can skip or
        re-read the boundary row. A seq cursor cannot fix this without the
        list route also sorting by ``seq`` (it sorts by ``created``); that
        route change is not worth it for a dormant, self-healing-on-refresh
        edge in whole-collection display views. Revisit if collections ever
        exceed the cap under concurrent writes.
        """
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.list_kind(
                kind,
                status=status,
                limit=MAX_LIST_LIMIT,
                offset=offset,
                seq_ranges=seq_ranges,
                filters=filters,
            )
            rows.extend(page)
            if len(page) < MAX_LIST_LIMIT:
                return rows
            offset += MAX_LIST_LIMIT

    def get_inquiry(
        self,
        ref: Ref,
    ) -> tuple[Inquiry.InquiryKind, uuid.UUID, dict[str, Any]]:
        """Resolve and fetch the SPA detail view (self + edges + changes)."""
        kind, target_id = self.resolve_id(ref)
        where = f"/api/web/get/{target_id}"
        return kind, target_id, dict(_require_mapping(self.get(where), where))

    def next_issue(self) -> dict[str, Any] | None:
        where = "/api/inquiries/next_issue"
        payload = self.get(where)
        if payload is None:
            return None
        return dict(_require_mapping(payload, where))

    def version(self) -> str:
        """Return the server's build SHA, for stale-deploy detection.

        The literal ``"unknown"`` only when the *server* reports it (it could
        not resolve its own build); a transport error (including 404 against a
        server too old to expose the route) propagates, which is itself the
        staleness signal. A response missing the ``sha`` key is a contract
        violation, not a build the server declined to name, so it raises rather
        than masquerading as the server's own ``"unknown"``.
        """
        payload = self.get("/api/version")
        if not isinstance(payload, dict) or "sha" not in payload:
            raise ClientError(f"/api/version returned a malformed payload: {payload!r}")
        return str(cast(dict[str, Any], payload)["sha"])

    def search(
        self,
        query: str,
        *,
        kind: Inquiry.InquiryKind | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return _require_list(
            self.get(
                "/api/web/search",
                params={"q": query, "kind": kind, "limit": limit},
            ),
            "/api/web/search",
        )

    def recent_changes(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return _require_list(
            self.get("/api/web/recent_changes", params={"limit": limit}),
            "/api/web/recent_changes",
        )

    def cost_for(self, target_id: uuid.UUID, *, deep: bool = False) -> dict[str, float]:
        where = f"/api/inquiries/{target_id}/cost"
        return cast(
            dict[str, float],
            _require_mapping(
                self.get(where, params={"deep": "true"} if deep else None),
                where,
            ),
        )

    # -- Writes -------------------------------------------------------------

    def submit(
        self,
        kind: Inquiry.InquiryKind,
        body: Mapping[str, object],
    ) -> uuid.UUID:
        """Submit a new inquiry of ``kind`` and return its server id.

        Mints one ``idempotency_key`` per call so a network-timed-out
        retry replays safely; a caller-supplied key wins. The new
        inquiry's ``id`` is server-minted and read from the response.
        """
        payload = dict(body)
        existing = payload.get("idempotency_key")
        if existing is None:
            payload["idempotency_key"] = str(uuid.uuid4())
        where = f"/api/inquiries/{kind.lower()}"
        response = self._request("POST", where, body=payload)
        return _require_uuid(_require_field(response, "id", where), where)

    def submit_batch(
        self,
        items: Sequence[tuple[Inquiry.InquiryKind, Mapping[str, object]]],
        *,
        edges: Sequence[Mapping[str, object]] = (),
    ) -> list[uuid.UUID]:
        """Create many inquiries and their edges in one atomic request.

        All items and edges commit together or not at all. Each item is
        ``(kind, body)``; a missing ``idempotency_key`` is minted per item
        so a timed-out retry replays without duplicating rows. Edges name
        endpoints by item index (see ``BatchEdge``). Returns server-minted
        ids in item order.
        """
        item_bodies: list[Mapping[str, object]] = []
        for index, (kind, body) in enumerate(items):
            payload = dict(body)
            # The tuple kind is authoritative. A body that also carries a
            # *conflicting* kind is a caller mistake -- silently overwriting it
            # would create the wrong inquiry kind -- so reject it rather than
            # paper over the disagreement.
            body_kind = payload.get("kind")
            if body_kind is not None and body_kind != kind:
                raise ClientError(
                    f"submit_batch item {index}: body kind {body_kind!r} "
                    f"conflicts with tuple kind {kind!r}"
                )
            payload["kind"] = kind
            if payload.get("idempotency_key") is None:
                payload["idempotency_key"] = str(uuid.uuid4())
            item_bodies.append(payload)
        where = "/api/inquiries/batch"
        response = self._request(
            "POST",
            where,
            body={"items": item_bodies, "edges": list(edges)},
        )
        return [
            _require_uuid(rid, where)
            for rid in _require_list(_require_field(response, "ids", where), where)
        ]

    def edit(
        self,
        target_id: uuid.UUID,
        field: str,
        value: object,
        *,
        actor: Inquiry.Actor,
        reason: str = "",
    ) -> None:
        """Overwrite ``field`` with ``value`` (a blind PUT)."""
        body: dict[str, object] = {"value": value, "actor": actor}
        if reason:
            body["reason"] = reason
        self.put(_field_path(target_id, field), body=body)

    def add_cost(
        self,
        target_id: uuid.UUID,
        field: str,
        value: float,
        *,
        actor: Inquiry.Actor,
        reason: str = "",
    ) -> None:
        """Add a signed cost delta to a ``marginal_cost`` axis.

        ``field`` is ``marginal_cost_agent_usd`` or
        ``marginal_cost_resource_usd``. A negative ``value`` is sent as
        ``op=sub`` so the wire keeps its non-negative ``value`` convention.
        """
        op = "add" if value >= 0 else "sub"
        body: dict[str, object] = {"op": op, "value": abs(value), "actor": actor}
        if reason:
            body["reason"] = reason
        self.patch(_field_path(target_id, field), body=body)

    def add_edge(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        *,
        actor: Inquiry.Actor,
        priority: int | None = None,
        note: str = "",
        valence: float | None = None,
        labels: Sequence[str] | None = (),
        reason: str = "",
    ) -> EdgeWrite:
        """Add an edge, upserting its annotations -- never an error on repeat.

        Edge creation is an upsert (symmetric with ``label add`` and every
        other set): a new edge is created, and a re-create on an existing edge
        applies any supplied annotations (``priority`` / ``note`` /
        ``valence`` / ``labels``) to it. A bare re-create is a pure no-op.
        ``reason`` is the audit note recorded on ``change_log.reason``.

        ``labels`` distinguishes three intents: ``()`` (the default) leaves the
        stored labels untouched; a non-empty list replaces them; ``None`` CLEARS
        them to empty. The upsert route collapses an empty list to "unset" (the
        store cannot clear through it), so the clear is threaded through the
        per-field labels route after the upsert, the one path that writes NULL
        (TRAX-CLI-004).

        Returns an :class:`EdgeWrite` distinguishing a fresh create
        (``created=True``) from an annotation applied to an existing edge
        (``created=False, changed=True``) from a no-op (both ``False``).
        """
        body: dict[str, object] = {"actor": actor}
        if priority is not None:
            body["priority"] = priority
        if note:
            body["note"] = note
        if valence is not None:
            body["valence"] = valence
        if labels:
            body["labels"] = list(labels)
        if reason:
            body["reason"] = reason
        response = cast(
            Mapping[str, object],
            self.post(f"/api/edges/{from_id}/{edge_kind}/{to_id}", body=body),
        )
        # The route returns ``{change_id, created}``: ``change_id`` is a UUID
        # when a change was emitted (``None`` for a no-op); ``created`` is the
        # brand-new-edge flag.
        write = EdgeWrite(
            created=bool(response.get("created")),
            changed=response.get("change_id") is not None,
        )
        if labels is None:
            # An explicit clear-to-empty: the upsert above could not carry it,
            # so issue the labels write that the store maps to NULL. The edge
            # now exists (just upserted), so this annotates rather than 409s.
            self.annotate_edge(from_id, to_id, edge_kind, actor=actor, labels=[])
            return EdgeWrite(created=write.created, changed=True)
        return write

    def annotate_edge(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        *,
        actor: Inquiry.Actor,
        priority: int | None | Absent = ABSENT,
        note: str | None | Absent = ABSENT,
        valence: float | None | Absent = ABSENT,
        labels: Sequence[str] | None | Absent = ABSENT,
    ) -> None:
        """Set edge annotation fields, one PUT per field actually sent.

        The API has one route per annotation field. This sends a PUT for
        each field the caller passed and leaves omitted (:data:`ABSENT`)
        fields untouched.

        Best-effort, not atomic (I3): the PUTs fan out in the fixed order
        ``priority, note, valence, labels``, so a mid-sequence failure
        leaves the fields before it applied and the rest unset, then raises.
        This path is reached only when re-annotating an edge that already
        exists (a brand-new edge carries its metadata atomically through
        ``add_edge``'s create). The server exposes no composite edge-update
        route to make the multi-field case transactional; callers needing
        all-or-nothing must retry. The fixed field order keeps the partial
        state deterministic.

        Retries are NOT audit-idempotent: each PUT mints a fresh
        ``Idempotency-Key`` (one per field, per call), so re-invoking this
        method after a partial failure re-applies the fields that already
        landed and writes a *duplicate* ``change_log`` audit row for each. The
        end state is correct (a PUT is a blind overwrite), but the audit log
        gains redundant entries. Stable per-(edge, field) keys would dedup
        them, but the value-overwrite semantics make the duplicates harmless,
        so this is documented rather than engineered around.
        """
        base = f"/api/edges/{from_id}/{edge_kind}/{to_id}"
        for field, value in (
            ("priority", priority),
            ("note", note),
            ("valence", valence),
            ("labels", labels),
        ):
            if isinstance(value, Absent):
                continue
            sent = list(value) if isinstance(value, (list, tuple)) else value
            self.put(f"{base}/{field}", body={"value": sent, "actor": actor})

    def remove_edge(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        self.delete(
            f"/api/edges/{from_id}/{edge_kind}/{to_id}",
            body={"actor": actor},
        )

    def add_subscriber(
        self,
        target_id: uuid.UUID,
        subscriber: Inquiry.Actor,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        """Add one subscriber, atomically and idempotently.

        ``subscriber`` may differ from ``actor``, which is the provenance
        recorded for the change.
        """
        self._patch_field(target_id, "subscribers", "add", subscriber, actor=actor)

    def remove_subscriber(
        self,
        target_id: uuid.UUID,
        subscriber: Inquiry.Actor,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        """Remove one subscriber, atomically and idempotently."""
        self._patch_field(target_id, "subscribers", "sub", subscriber, actor=actor)

    def add_label(
        self,
        target_id: uuid.UUID,
        label: str,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        """Add one label, race-free."""
        self._patch_field(target_id, "labels", "add", label, actor=actor)

    def remove_label(
        self,
        target_id: uuid.UUID,
        label: str,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        """Remove one label, race-free."""
        self._patch_field(target_id, "labels", "sub", label, actor=actor)

    def add_issue_kind(
        self,
        target_id: uuid.UUID,
        issue_kind: Issue.Kind,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        self._patch_field(target_id, "issue_kind", "add", issue_kind, actor=actor)

    def remove_issue_kind(
        self,
        target_id: uuid.UUID,
        issue_kind: Issue.Kind,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        self._patch_field(target_id, "issue_kind", "sub", issue_kind, actor=actor)

    def add_codechange(
        self,
        target_id: uuid.UUID,
        codechange_id: uuid.UUID,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        self._patch_field(
            target_id, "codechanges", "add", str(codechange_id), actor=actor
        )

    def remove_codechange(
        self,
        target_id: uuid.UUID,
        codechange_id: uuid.UUID,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        self._patch_field(
            target_id, "codechanges", "sub", str(codechange_id), actor=actor
        )

    def add_author(
        self,
        target_id: uuid.UUID,
        author: str,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        """Atomically append one author to a Paper's byline, race-free."""
        self._patch_field(target_id, "authors", "add", author, actor=actor)

    def remove_author(
        self,
        target_id: uuid.UUID,
        author: str,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        """Atomically remove one author from a Paper's byline, race-free."""
        self._patch_field(target_id, "authors", "sub", author, actor=actor)

    def transition_status(
        self,
        target_id: uuid.UUID,
        *,
        expected_from: str,
        to: str,
        actor: Inquiry.Actor,
        reason: str = "",
    ) -> None:
        """Compare-and-set the status; 409s if it isn't ``expected_from``.

        Sent as a PUT on the ``status`` field with ``mode='cas'`` and the
        ``expected`` guard.
        """
        self.put(
            _field_path(target_id, "status"),
            body={
                "actor": actor,
                "value": to,
                "mode": "cas",
                "expected": expected_from,
                "reason": reason,
            },
        )

    def purge(
        self,
        target_id: uuid.UUID,
        *,
        actor: Inquiry.Actor,
        reason: str = "",
    ) -> None:
        """Delete an inquiry."""
        self.delete(
            f"/api/inquiries/{target_id}",
            body={"actor": actor, "reason": reason},
        )

    def session_start(self, body: SessionStart) -> SessionStartResponse:
        """Open a capture session; return the server-minted identity.

        Mints an ``idempotency_key`` when the caller omits one, so a
        timed-out retry replays rather than minting a second session.
        """
        if body.idempotency_key is None:
            body = body.model_copy(update={"idempotency_key": uuid.uuid4()})
        response = self._request(
            "POST", wire_sessions.SESSION_START_PATH, body=body.model_dump(mode="json")
        )
        return _validate_model(
            wire_sessions.SessionStartResponse,
            response,
            wire_sessions.SESSION_START_PATH,
        )

    def append_events(
        self,
        session_id: uuid.UUID,
        events: Sequence[EventBody],
    ) -> AppendEventsResponse:
        """Batch-append captured events; idempotent on ``(session_id, seq)``."""
        req = wire_sessions.AppendEventsRequest(events=list(events))
        where = wire_sessions.session_events_path(session_id)
        response = self._request("POST", where, body=req.model_dump(mode="json"))
        return _validate_model(wire_sessions.AppendEventsResponse, response, where)

    def read_events(
        self,
        session_id: uuid.UUID,
        *,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        seq_ranges: Sequence[SeqRange] = (),
        kind: str | None = None,
    ) -> list[EventBody]:
        """Read one page of a session's events in ``seq`` order.

        Paginated (``limit`` / ``offset`` / ``seq_range`` / ``kind``) so a
        caller never pulls a whole large session at once. ``seq_ranges`` is
        a union of inclusive intervals, each riding the wire as one
        repeated ``seq_range=a..b`` param, exactly as ``list_kind`` does.
        """
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if seq_ranges:
            params["seq_range"] = [format_interval(r) for r in seq_ranges]
        if kind is not None:
            params["kind"] = kind
        where = wire_sessions.session_events_path(session_id)
        response = self._request("GET", where, params=params)
        return _validate_model(wire_sessions.ReadEventsResponse, response, where).events

    def log_metrics(
        self,
        experiment_id: uuid.UUID,
        points: Sequence[MetricPoint],
    ) -> LogMetricsResponse:
        """Batch-log experiment metric points; idempotent on ``(key, step)``.

        A retried batch (same ``(key, step)`` pairs) reports ``logged=0``. The
        server rejects a non-Experiment id (409) or a missing one (404).
        """
        req = wire_metrics.LogMetricsRequest(points=list(points))
        where = wire_metrics.experiment_metrics_path(experiment_id)
        response = self._request("POST", where, body=req.model_dump(mode="json"))
        return _validate_model(wire_metrics.LogMetricsResponse, response, where)

    def read_metrics(
        self,
        experiment_id: uuid.UUID,
        *,
        key: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[MetricPoint]:
        """Read one page of an experiment's metric points in ``(key, step)`` order.

        Paginated (``limit`` / ``offset`` / ``key``) so a caller never pulls a
        whole large run at once; ``key`` narrows to one metric.
        """
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if key is not None:
            params["key"] = key
        where = wire_metrics.experiment_metrics_path(experiment_id)
        response = self._request("GET", where, params=params)
        return _validate_model(wire_metrics.ReadMetricsResponse, response, where).points

    def query_metrics(
        self,
        experiment_id: uuid.UUID,
        *,
        masks: Sequence[MetricMaskClause],
        sort: Literal["asc", "desc"] | None = None,
        limit: int | None = None,
    ) -> list[MetricPoint]:
        """Read one experiment's masked metric cells (the mask-query surface)."""
        req = wire_metrics_query.MetricQueryRequest(
            masks=list(masks), sort=sort, limit=limit
        )
        where = wire_metrics_query.experiment_metric_query_path(experiment_id)
        response = self._request("POST", where, body=req.model_dump(mode="json"))
        return _validate_model(
            wire_metrics_query.MetricQueryResponse, response, where
        ).points

    def write_metrics_masked(
        self,
        experiment_id: uuid.UUID,
        *,
        masks: Sequence[MetricMaskClause],
        value: float,
    ) -> int:
        """Assign ``value`` to every cell the mask selects; return the count."""
        req = wire_metrics_query.MetricQueryRequest(masks=list(masks), write=value)
        where = wire_metrics_query.experiment_metric_write_path(experiment_id)
        response = self._request("POST", where, body=req.model_dump(mode="json"))
        return _validate_model(
            wire_metrics_query.MetricWriteResponse, response, where
        ).written

    def rank_metrics(
        self,
        experiment_ids: Sequence[uuid.UUID],
        *,
        masks: Sequence[MetricMaskClause],
        sort: Literal["asc", "desc"] | None = None,
        limit: int | None = None,
    ) -> list[MetricRankRow]:
        """Cross-experiment masked read/rank over the given experiments."""
        query = wire_metrics_query.MetricQueryRequest(
            masks=list(masks), sort=sort, limit=limit
        )
        req = wire_metrics_query.MetricRankRequest(
            experiment_ids=list(experiment_ids), query=query
        )
        where = wire_metrics_query.METRIC_RANK_PATH
        response = self._request("POST", where, body=req.model_dump(mode="json"))
        return _validate_model(
            wire_metrics_query.MetricRankResponse, response, where
        ).rows

    def session_end(
        self,
        session_id: uuid.UUID,
        body: SessionEnd | None = None,
    ) -> SessionEndResponse:
        """Close a capture session, optionally backfilling late-known fields."""
        payload = (body or wire_sessions.SessionEnd()).model_dump(mode="json")
        where = wire_sessions.session_end_path(session_id)
        response = self._request("POST", where, body=payload)
        return _validate_model(wire_sessions.SessionEndResponse, response, where)

    def enqueue_inbound(
        self,
        session_id: uuid.UUID,
        text: str,
    ) -> int:
        """Enqueue a message to inject into a live session; return queued count.

        The sender is attested server-side from the authenticated principal,
        so the request carries no ``source`` -- the enqueue body forbids it.
        """
        body = wire_sessions.InboundEnqueueRequest(text=text).model_dump(mode="json")
        where = wire_sessions.session_inbound_path(session_id)
        response = self._request("POST", where, body=body)
        return _validate_model(
            wire_sessions.InboundEnqueueResponse, response, where
        ).queued

    def drain_inbound(
        self, session_id: uuid.UUID
    ) -> list[tuple[str, str | None, str | None]]:
        """Drain pending inbound messages for a session, oldest first.

        Returns ``(text, source, room)`` triples so the caller (the ``trax
        run`` poller) can render the ``[room] sender:`` injection context
        without a wire-type import.
        """
        where = wire_sessions.session_inbound_path(session_id)
        response = self._request("GET", where)
        drained = _validate_model(wire_sessions.DrainInboundResponse, response, where)
        return [(m.text, m.source, m.room) for m in drained.messages]

    def send_message(
        self,
        actor: str,
        text: str,
        *,
        room: str | None = None,
    ) -> list[uuid.UUID]:
        """Send ``text`` to live sessions named by ``@actor[:room]``.

        Returns the session ids the server enqueued to (empty = no live
        session matched: an honest undelivered signal).
        """
        body = wire_sessions.SendMessage(actor=actor, room=room, text=text).model_dump(
            mode="json"
        )
        response = self._request("POST", wire_sessions.SEND_MESSAGE_PATH, body=body)
        return _validate_model(
            wire_sessions.SendMessageResponse, response, wire_sessions.SEND_MESSAGE_PATH
        ).delivered

    def _patch_field(
        self,
        target_id: uuid.UUID,
        field: str,
        op: str,
        value: object,
        *,
        actor: Inquiry.Actor,
    ) -> None:
        """Augment one list field with a PATCH ``add`` / ``sub`` op."""
        self.patch(
            _field_path(target_id, field),
            body={"op": op, "value": value, "actor": actor},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        params: Mapping[str, object] | None = None,
        change_id: uuid.UUID | None = None,
    ) -> Any:
        # Only mutations need an Idempotency-Key (GETs are stateless).
        # Mint one if the caller didn't, so every retry in this loop
        # reuses the *same* UUID and the server sees a replay rather than
        # a duplicate operation.
        headers: dict[str, str] = {}
        if method != "GET":
            if change_id is None:
                change_id = uuid.uuid4()
            headers["Idempotency-Key"] = str(change_id)
        clean = _clean_params(params)
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = self._http.request(
                    method,
                    path,
                    json=body,
                    params=clean,
                    headers=headers,
                )
            # Connect and write failures are wrapped but not retried: a write
            # may already have reached the server, so a blind retry could
            # duplicate the operation, and a connect failure won't recover
            # within this loop. Either way the raw httpx error must not escape
            # the ClientError contract.
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.WriteError,
                httpx.WriteTimeout,
            ) as err:
                raise ClientError(f"{method} {path} failed: {err}") from err
            except (
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.ReadTimeout,
                httpx.PoolTimeout,
            ) as err:
                if attempt == _RETRY_ATTEMPTS - 1:
                    raise ClientError(f"{method} {path} failed: {err}") from err
                time.sleep(0.1 * (3**attempt))
                continue
            if (
                response.status_code in (500, 502, 503, 504)
                and attempt < _RETRY_ATTEMPTS - 1
            ):
                time.sleep(0.1 * (3**attempt))
                continue
            break
        else:
            raise AssertionError("unreachable: _RETRY_ATTEMPTS >= 1")
        if response.status_code >= 400:
            error_code = ""
            try:
                payload = cast(object, response.json())
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                payload_map = cast(Mapping[object, object], payload)
                code = payload_map.get("code")
                if isinstance(code, str):
                    error_code = code
            raise ClientError(
                f"{method} {path} -> {response.status_code}: "
                f"{_truncate(response.text)}",
                status_code=response.status_code,
                code=error_code,
            )
        if not response.content:
            return None
        # A 2xx with a non-empty but malformed body would otherwise leak a raw
        # ``json.JSONDecodeError`` (a ``ValueError``) past the ClientError
        # contract; wrap it so callers see one error type.
        try:
            return response.json()
        except ValueError as err:
            raise ClientError(
                f"{method} {path}: malformed JSON in server response"
            ) from err
