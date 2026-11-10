"""Tests for the access-policy seam: its value types, the default, and the read gate."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast, override

import uuid

import pytest

from trackinizer.conftest import executed_sql, make_conn
from trackinizer.server.access_policy import (
    AccessDeniedError,
    DefaultPolicy,
    Principal,
    Resource,
    inquiry_read_filter,
)


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn
    from trackinizer.server.access_policy import ResourceKind


_READER = Principal(email="ada@example.com", role="viewer")
_SESSION = Resource(kind="session", id=str(uuid.UUID(int=7)), account="ada@example.com")


class _GroupPolicy(DefaultPolicy):
    """Reads only the rows a group grant names, binding the reader's groups."""

    @override
    def read_filter(
        self,
        kind: ResourceKind,
        alias: str,
        *,
        params: list[object],
        principal: Principal,
    ) -> str:
        params.append(sorted(principal.groups))
        return f"{alias}.kind = '{kind}' AND {alias}.grp = ANY(${len(params)})"


def test_a_principal_defaults_no_groups_and_no_key() -> None:
    assert (_READER.groups, _READER.api_key_id) == (frozenset(), None)


def test_a_resource_defaults_no_account() -> None:
    assert Resource(kind="host", id="box").account is None


def test_access_denied_is_an_exception_carrying_its_message() -> None:
    with pytest.raises(AccessDeniedError, match=r"^no grant$"):
        raise AccessDeniedError("no grant")


@pytest.mark.parametrize(
    ("policy", "principal"),
    [(None, _READER), (DefaultPolicy(), None), (None, None)],
    ids=["no policy", "no principal", "neither"],
)
def test_a_trusted_internal_read_is_unrestricted(
    policy: DefaultPolicy | None,
    principal: Principal | None,
) -> None:
    params: list[object] = []

    predicate = inquiry_read_filter(
        "s.inquiry_id",
        policy=policy,
        principal=principal,
        params=params,
    )

    assert (predicate, params) == ("TRUE", [])


def test_the_default_policy_reads_every_row_without_a_subquery() -> None:
    params: list[object] = []

    predicate = inquiry_read_filter(
        "s.inquiry_id",
        policy=DefaultPolicy(),
        principal=_READER,
        params=params,
    )

    assert (predicate, params) == ("TRUE", [])


def test_a_restricting_policy_gates_the_reference_by_its_inquiry_row() -> None:
    params: list[object] = ["earlier"]
    reader = Principal(email="ada@example.com", role="viewer", groups=frozenset({"g"}))

    predicate = inquiry_read_filter(
        "s.inquiry_id",
        policy=_GroupPolicy(),
        principal=reader,
        params=params,
    )

    assert predicate == (
        "EXISTS (SELECT 1 FROM inquiries readable_inquiry "
        "WHERE readable_inquiry.id = s.inquiry_id AND ("
        "readable_inquiry.kind = 'inquiry' AND readable_inquiry.grp = ANY($2)))"
    )
    assert params == ["earlier", ["g"]]


@pytest.mark.asyncio
async def test_the_default_policy_allows_everything_and_sends_nothing() -> None:
    mock = make_conn()
    conn = cast("Conn", mock)
    policy = DefaultPolicy()

    assert await policy.groups(conn, _READER) == frozenset()
    await policy.check(conn, principal=_READER, resource=_SESSION, action="write")
    await policy.on_create(conn, principal=None, resource=_SESSION)
    await policy.prepare_account_change(conn, resource=_SESSION)
    await policy.validate(conn, resource=_SESSION)
    await policy.on_user(conn, email="ada@example.com", role="writer")
    await policy.prepare_audience_change(conn)
    await policy.on_audience(conn, principal=_READER, resource=_SESSION)

    assert executed_sql(mock) == []
    assert mock.fetchrow.call_count == mock.fetchval.call_count == 0


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
