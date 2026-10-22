"""Routes for the exec channel: the assistant runs a command, and the host's two legs.

Only the configured assistant runs a command, for an attested writer or admin ``asker``
who owns the machine. The route queues the command in the hub (``server/execs.py``) and
holds the request until the machine's host reports. The host holds a ``GET`` on the wait
route, which returns the moment a command is queued, runs it as the machine's user, and
``POST``s the result. A person's own browser, an API key of anyone else, a service
account and a server with authentication off run nothing here.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from treekle import from_plain

from trackinizer.server.api._deps import (
    auth_is_disabled,
    get_exec_hub,
    get_replays,
)
from trackinizer.server.api._routes_shared import engine_of, idempotency_key
from trackinizer.server.api.machines_routes import MachineName
from trackinizer.server.api.session_access import resolve_asker
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.execs import (
    ExecBusyError,
    ExecHub,
    ExecLimitError,
    ExecLostError,
    ExecRevokedError,
    ExecStaleError,
    MayHaveRun,
    Submission,
)
from trackinizer.server.machine_auth import current_machine, machine_revoked
from trackinizer.types.errors import NotFoundError
from trackinizer.wire import wire_exec
from trackinizer.wire.wire_exec import (
    EXEC_PATH,
    MACHINE_EXEC_RESULT_PATH,
    MACHINE_EXEC_WAIT_PATH,
    ExecRequest,
    ExecResult,
    ExecWait,
)
from trackinizer.wire.wire_machine_host import OFFLINE_AFTER_SEC


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


__all__ = ["exec_result_route", "exec_route", "exec_wait_route", "router"]

router = APIRouter()


@router.post(EXEC_PATH)
async def exec_route(
    name: MachineName,
    body: ExecRequest,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    asker: str | None = None,
) -> ExecResult:
    """Run a command on the asker's machine and return what it produced.

    Only the configured assistant calls this, naming an attested writer or admin
    ``asker`` who owns the machine. The request is held until the machine's host
    reports, up to the command's ``timeout_sec`` and a short grace. A retry under the
    same ``Idempotency-Key`` is told what the first try was, its result or its 504 or
    410 once the host was handed the command, and runs nothing twice. A command that
    was refused, or that no host took, is run by the retry. The machine must be online,
    confirmed by its owner, and report ``exec: true``.

    Args:
      name: The machine's name.
      body: The command, its folder and timeout, and optionally stdin and variables.
      request: Request carrying the database engine and the exec hub.
      identity: The assistant's key.
      asker: The person the assistant runs the command for.

    Returns:
      result: The exit code, both streams, the folder the command ended in, whether
        output was cut, and how long it ran.

    Raises:
      HTTPException: 403 ``exec_forbidden`` for any caller but the assistant naming a
        writer or admin, and on a server with authentication off; 404 for no such
        machine of the asker; 409 ``exec_not_enabled`` for a machine that is offline,
        unconfirmed or not opted in, ``exec_busy`` past 4 commands on one machine; 410
        ``exec_machine_revoked`` when the machine was revoked meanwhile; 429
        ``exec_limit`` past 60 commands a minute for the asker; 504 ``exec_lost`` when
        the host did not take the command or did not report in time.

    """
    owner = await _exec_owner(request, identity, asker=asker)
    machine_id = await _exec_machine(request, owner=owner, name=name)
    key = idempotency_key(request)
    try:
        ended = await get_replays(request).exec_runs.run(
            None if key is None else (identity.user_id, identity.api_key_id, key),
            fingerprint=f"{owner}:{name}:{body.model_dump_json()}",
            operation=partial(
                _run_once,
                get_exec_hub(request),
                Submission(
                    user=owner,
                    machine_id=machine_id,
                    machine=name,
                    request=body,
                ),
            ),
        )
        if isinstance(ended, MayHaveRun):
            raise ended.error()
        return ended
    except ExecBusyError as error:
        raise _refused(
            409,
            code="exec_busy",
            message=f"{wire_exec.MAX_RUNNING_PER_MACHINE} commands are already waiting "
            f"on or running at {name}",
        ) from error
    except ExecLimitError as error:
        raise _refused(
            429,
            code="exec_limit",
            message="too many commands started just now; wait a minute",
        ) from error
    except ExecRevokedError as error:
        raise _refused(
            410,
            code="exec_machine_revoked",
            message=f"machine {name} was revoked",
        ) from error
    except ExecLostError as error:
        raise _refused(504, code="exec_lost", message=str(error)) from error


# A host on the exec wire holds this like the terminal wait, and the receipt is why a
# lost response does not lose the command: the hub gave it to the first try only.
@router.get(
    MACHINE_EXEC_WAIT_PATH,
    response_model_exclude_none=True,
    include_in_schema=False,
)
async def exec_wait_route(
    machine_id: UUID,
    request: Request,
    instance: UUID,
    wait_sec: Annotated[float, Query(ge=0)] = wire_exec.WAIT_MAX_SEC,
) -> ExecWait | None:
    """Hold until the machine has a command to run, then return it; else ``null``.

    The machine's own credential authenticates, as for a heartbeat. The wait is held
    at most 25 seconds.

    Args:
      machine_id: The machine.
      request: Request carrying the database engine and the exec hub.
      instance: The host instance, which must be the machine's current one when the
        wait starts and again when a command is handed to it.
      wait_sec: How long to hold, clamped to 25 seconds.

    Returns:
      command: The next command to run, or ``None`` when none came in time.

    Raises:
      HTTPException: 403 on a server with authentication off, or when another host
        took the machine over; 410 when the machine is revoked, including while the
        host waits.

    """
    if auth_is_disabled(request):
        raise _forbidden()
    await current_machine(request, machine_id)
    await _require_instance(request, machine_id, instance=instance)
    key = idempotency_key(request)
    try:
        return await get_replays(request).exec_waits.run(
            None if key is None else (machine_id, instance, key),
            fingerprint=str(machine_id),
            operation=partial(
                _next_command,
                get_exec_hub(request),
                machine_id=machine_id,
                wait_sec=min(wait_sec, wire_exec.WAIT_MAX_SEC),
                is_current=partial(_is_current, request, machine_id, instance=instance),
            ),
        )
    except ExecRevokedError as error:
        raise machine_revoked() from error
    except ExecStaleError as error:
        raise _stale_instance() from error


@router.post(
    MACHINE_EXEC_RESULT_PATH,
    status_code=204,
    include_in_schema=False,
)
async def exec_result_route(
    machine_id: UUID,
    exec_id: UUID,
    body: ExecResult,
    request: Request,
) -> None:
    """Take the result of a command the machine's host was handed.

    The machine's own credential authenticates. The result goes to the request that is
    still waiting for it.

    Args:
      machine_id: The machine.
      exec_id: The command, from the host's wait.
      body: What running it produced.
      request: Request carrying the database engine and the exec hub.

    A result sent again for a command already answered is accepted, so a host that
    lost the response may send it again.

    Raises:
      NotFoundError: The command is unknown, is another machine's, was never handed
        out, or ended without a result: it timed out, or its machine was revoked.
      HTTPException: 403 on a server with authentication off.

    """
    if auth_is_disabled(request):
        raise _forbidden()
    await current_machine(request, machine_id)
    if not get_exec_hub(request).deliver(machine_id, exec_id, body):
        raise NotFoundError("no such command waiting for a result")


# The assistant is trusted to say whom it acts for, as for a chat's settings, and the
# server reads that person's role itself. Nobody else may name an asker, and there is no
# route for a person's own session: a command runs as the machine's user, which only
# the assistant, acting for that user, may ask for. Only its key counts, as a browser
# session of the assistant's account would pass no origin check on this route.
async def _exec_owner(
    request: Request,
    identity: AuthIdentity,
    *,
    asker: str | None,
) -> str:
    """Return the person the assistant runs the command for, or refuse the caller."""
    if auth_is_disabled(request) or identity.api_key_id is None:
        raise _forbidden()
    found = await resolve_asker(request, identity, email=asker)
    if found is None or found.role not in {"writer", "admin"}:
        raise _forbidden()
    return found.email


async def _exec_machine(request: Request, *, owner: str, name: str) -> UUID:
    """Return the id of the owner's machine, once it is online, confirmed and opted in."""
    async with engine_of(request).acquire() as conn:
        machine = await conn.fetchrow(
            "SELECT m.id, m.facts, m.confirmed_at IS NOT NULL AS confirmed, "
            "(m.host_instance IS NOT NULL "
            "AND m.last_heartbeat > now() - make_interval(secs => $3) "
            "AND EXISTS (SELECT 1 FROM machine_credentials c "
            "WHERE c.machine_id = m.id AND c.revoked_at IS NULL)) AS online "
            "FROM machines m WHERE m.name = $1 AND m.owner = $2",
            name,
            owner,
            OFFLINE_AFTER_SEC,
        )
    if machine is None:
        raise NotFoundError(f"no machine {name} of yours")
    if machine["online"] is not True:
        raise _not_enabled(f"machine {name} is offline")
    if not machine["confirmed"]:
        raise _not_enabled(f"machine {name} is not confirmed; confirm it first")
    if from_plain(machine["facts"], dict[str, object]).get("exec") is not True:
        raise _not_enabled(
            f"machine {name} runs no commands; start rai-shell without --no-exec",
        )
    return from_plain(machine["id"], UUID)


async def _is_current(request: Request, machine_id: UUID, *, instance: UUID) -> bool:
    """Say whether ``instance`` is the machine's current host instance."""
    async with engine_of(request).acquire() as conn:
        current = await conn.fetchval(
            "SELECT host_instance = $2 FROM machines WHERE id = $1",
            machine_id,
            instance,
        )
    return current is True


async def _require_instance(
    request: Request,
    machine_id: UUID,
    *,
    instance: UUID,
) -> None:
    """Refuse a host that is not the machine's current instance."""
    if not await _is_current(request, machine_id, instance=instance):
        raise _stale_instance()


def _stale_instance() -> HTTPException:
    """Return the refusal of a host that another has taken the machine over from."""
    return HTTPException(status_code=403, detail="stale host instance")


# A receipt drops an operation that raised, and a retry would then run the command a
# second time, so what may have run is returned rather than raised.
async def _run_once(hub: ExecHub, submission: Submission) -> ExecResult | MayHaveRun:
    """Run a command; a command lost after a host took it is a result to keep."""
    try:
        return await hub.run(submission)
    except (ExecLostError, ExecRevokedError) as error:
        if error.taken:
            return MayHaveRun(revoked=isinstance(error, ExecRevokedError))
        raise


async def _next_command(
    hub: ExecHub,
    *,
    machine_id: UUID,
    wait_sec: float,
    is_current: Callable[[], Awaitable[bool]],
) -> ExecWait | None:
    """Take one command and freeze the offer before its response can be lost."""
    found = await hub.wait(machine_id, wait_sec=wait_sec, is_current=is_current)
    if found is None:
        return None
    return ExecWait(
        exec_id=found.id,
        asker=found.user,
        command=found.request.command,
        cwd=found.request.cwd,
        timeout_sec=found.request.timeout_sec,
        stdin_b64=found.request.stdin_b64,
        env=found.request.env,
    )


def _not_enabled(message: str) -> HTTPException:
    """Return the refusal of a machine that cannot run commands now."""
    return _refused(409, code="exec_not_enabled", message=message)


def _forbidden() -> HTTPException:
    """Return the one refusal of a caller that may not run commands here."""
    return _refused(
        403,
        code="exec_forbidden",
        message="commands are not available here",
    )


def _refused(status_code: int, *, code: str, message: str) -> HTTPException:
    """Return a refusal whose ``detail`` is ``{code, message}``."""
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )
