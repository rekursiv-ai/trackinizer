"""Experiment-metric routes: ``log`` / ``read`` / mask ``query`` / ``write`` / ``rank``.

The server side of experiment-metric capture (the wandb ``log()`` analogue).
A run streams batches of step-grained points into ``experiment_metrics``;
``GET .../metrics`` reads them back, paginated. The mask query, masked write,
and cross-experiment rank are the ``trax metric`` grammar's surface. The two
mutating routes require the ``writer`` role; the reads require ``viewer``.
There is no per-tenant scoping: any principal holding the role reaches any
experiment.

Thin by construction: ``Store.log_metrics`` / ``read_metrics`` /
``write_metrics_masked`` own the Experiment check (raising ``NotFoundError`` /
``ConflictError``, which the app maps to 404 / 409), and ``query_metrics``
owns mask validation (409). The mask query and rank select by id, so an id
that is not an Experiment matches no cells. These handlers only reject the
other operation's controls, validate pagination, and serialize.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from trackinizer.server.api._deps import get_store
from trackinizer.server.auth import require_role
from trackinizer.wire.routes import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
)
from trackinizer.wire.wire_metrics import (
    EXPERIMENT_METRICS_PATH,
    LogMetricsRequest,
    LogMetricsResponse,
    ReadMetricsResponse,
)
from trackinizer.wire.wire_metrics_query import (
    EXPERIMENT_METRIC_QUERY_PATH,
    EXPERIMENT_METRIC_WRITE_PATH,
    METRIC_RANK_PATH,
    MetricQueryRequest,
    MetricQueryResponse,
    MetricRankRequest,
    MetricRankResponse,
    MetricRankRow,
    MetricWriteResponse,
)


router = APIRouter()


@router.post(
    EXPERIMENT_METRICS_PATH,
    dependencies=[Depends(require_role("writer"))],
)
async def log_metrics_route(
    experiment_id: UUID,
    body: LogMetricsRequest,
    request: Request,
) -> LogMetricsResponse:
    """Batch-append metric points to an experiment.

    Idempotent on ``(experiment_id, key, step)``: a retried batch reports
    ``logged=0``. ``Store.log_metrics`` rejects a non-Experiment id (409) or a
    missing one (404).

    Args:
      experiment_id: Target Experiment inquiry id.
      body: Request body with points array.
      request: FastAPI Request context (used to get the store).

    Returns:
      result: LogMetricsResponse with logged and skipped counts.

    """
    store = get_store(request)
    logged, skipped = await store.log_metrics(experiment_id, body.points)
    return LogMetricsResponse(logged=logged, skipped=skipped)


@router.get(
    EXPERIMENT_METRICS_PATH,
    dependencies=[Depends(require_role("viewer"))],
)
async def read_metrics_route(
    experiment_id: UUID,
    request: Request,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    key: str | None = None,
) -> ReadMetricsResponse:
    """Read one page of an experiment's metric points in ``(key, step)`` order.

    Paginated so a caller never pulls a whole large run at once; ``key``
    narrows to one metric. ``Store.read_metrics`` rejects a non-Experiment id
    (409) or a missing one (404), so an empty page means a run with no points.

    Args:
      experiment_id: Target Experiment inquiry id.
      request: FastAPI Request context (used to get the store).
      limit: Max points per page; must be in [1, MAX_LIST_LIMIT].
      offset: Number of points to skip from the start; must be >= 0.
      key: Optional metric name to filter by.

    Returns:
      result: ReadMetricsResponse with points array.

    Raises:
      HTTPException: 400 if limit or offset invalid.

    """
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be in [1, {MAX_LIST_LIMIT}]",
        )
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    store = get_store(request)
    points = await store.read_metrics(
        experiment_id,
        key=key,
        limit=limit,
        offset=offset,
    )
    return ReadMetricsResponse(points=points)


@router.post(
    EXPERIMENT_METRIC_QUERY_PATH,
    dependencies=[Depends(require_role("viewer"))],
)
async def query_metrics_route(
    experiment_id: UUID,
    body: MetricQueryRequest,
    request: Request,
) -> MetricQueryResponse:
    """Read one experiment's masked metric cells (the mask-query surface).

    Args:
      experiment_id: Experiment id.
      body: Request body with masks, sort, and limit.
      request: FastAPI Request context (used to get the store).

    Returns:
      result: The MetricQueryResponse.

    Raises:
      HTTPException: 400 if the body carries a write value.

    """
    _reject_write(body)
    store = get_store(request)
    rows = await store.query_metrics(
        [experiment_id],
        masks=body.masks,
        sort=body.sort,
        limit=body.limit,
    )
    return MetricQueryResponse(points=[point for _eid, point in rows])


@router.post(
    EXPERIMENT_METRIC_WRITE_PATH,
    dependencies=[Depends(require_role("writer"))],
)
async def write_metrics_route(
    experiment_id: UUID,
    body: MetricQueryRequest,
    request: Request,
) -> MetricWriteResponse:
    """Assign ``body.write`` to every cell the mask selects (bulk upsert).

    Args:
      experiment_id: Target Experiment inquiry id.
      body: Request body with masks and write value.
      request: FastAPI Request context (used to get the store).

    Returns:
      result: MetricWriteResponse with count of cells written.

    Raises:
      HTTPException: 400 if write value is missing.

    """
    if body.write is None:
        raise HTTPException(status_code=400, detail="write requires a 'to' value")
    store = get_store(request)
    written = await store.write_metrics_masked(
        experiment_id,
        masks=body.masks,
        value=body.write,
    )
    return MetricWriteResponse(written=written)


@router.post(
    METRIC_RANK_PATH,
    dependencies=[Depends(require_role("viewer"))],
)
async def rank_metrics_route(
    body: MetricRankRequest,
    request: Request,
) -> MetricRankResponse:
    """Cross-experiment masked read/rank over the given experiments.

    Args:
      body: Request body with experiment_ids and query (masks, sort, limit).
      request: FastAPI Request context (used to get the store).

    Returns:
      result: MetricRankResponse with rows array (experiment_id, point pairs).

    Raises:
      HTTPException: 400 if the query carries a write value.

    """
    _reject_write(body.query)
    store = get_store(request)
    rows = await store.query_metrics(
        body.experiment_ids,
        masks=body.query.masks,
        sort=body.query.sort,
        limit=body.query.limit,
    )
    return MetricRankResponse(
        rows=[MetricRankRow(experiment_id=eid, point=point) for eid, point in rows],
    )


# The read routes share the write route's body model, so without this a ``write``
# sent to a read path would be dropped and the caller would believe it had written.
def _reject_write(query: MetricQueryRequest) -> None:
    """Reject a write value sent to a read route."""
    if query.write is not None:
        raise HTTPException(
            status_code=400,
            detail="a metric read takes no 'to' value; POST it to the write route",
        )
