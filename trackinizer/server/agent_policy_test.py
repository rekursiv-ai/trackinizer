"""The agent policy seam: what counts as a policy, and what a policy answers with."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from trackinizer.server.agent_policy import AgentPolicy, Halt, Refusal


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.server.agent_policy import AgentWork
    from trackinizer.wire.wire_session_ir import RecordBody


class _Whole:
    """A policy: it answers each of the three questions."""

    def serves(self, asker: str) -> bool:
        del asker
        return False

    async def refusal(
        self,
        engine: DatabaseEngine,
        *,
        asker: str,
        session_id: UUID,
        work: AgentWork,
    ) -> Refusal | None:
        del engine, asker, session_id, work
        return None

    async def stored(
        self,
        engine: DatabaseEngine,
        *,
        session_id: UUID,
        part: int,
        records: Sequence[RecordBody],
        live: Sequence[UUID],
    ) -> Halt | None:
        del engine, session_id, part, records, live
        return None


class _NoStored:
    """Not a policy: it is never shown what a remote agent stored."""

    def serves(self, asker: str) -> bool:
        del asker
        return False

    async def refusal(self, engine: object, **asked: object) -> None:
        del engine, asked


def test_an_object_that_answers_all_three_is_a_policy_and_one_that_does_not_is_not() -> (
    None
):
    """The server keeps what a deployment set only when it is one."""
    assert isinstance(_Whole(), AgentPolicy)
    assert not isinstance(_NoStored(), AgentPolicy)
    assert not isinstance(object(), AgentPolicy)


def test_a_refusal_and_a_halt_compare_by_what_they_say() -> None:
    refused = Refusal(status=402, code="no_way", message="Not now.")
    halt = Halt(sessions=frozenset({UUID(int=1)}), notice="Stopped.")

    assert refused == Refusal(status=402, code="no_way", message="Not now.")
    assert refused != Refusal(status=402, code="no_way", message="Later.")
    assert halt == Halt(sessions=frozenset({UUID(int=1)}), notice="Stopped.")
    assert halt != Halt(sessions=frozenset(), notice="Stopped.")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
