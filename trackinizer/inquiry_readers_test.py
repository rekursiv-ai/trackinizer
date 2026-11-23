"""Guard that every trackinizer module reading ``inquiries`` or ``change_log`` is classified.

Each production module of this package whose SQL reads either table is named below
with why it may (:data:`~trackinizer.inquiry_readers_test_support.Class`). A
new reader fails here until someone decides which, and a module named ``policy``
that stops calling the policy's filter fails too.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from trackinizer.inquiry_readers_test_support import misfiling, unclassified


if TYPE_CHECKING:
    from trackinizer.inquiry_readers_test_support import Class


_CWD: Final[Path] = Path(__file__).resolve().parent

_READERS: Final[dict[str, tuple[Class, str]]] = {
    "addons/embedding/search.py": (
        "audience",
        "the vector arm: session and inquiry units, under the test core hands it",
    ),
    "addons/embedding/sweep.py": (
        "trusted",
        "background embedding worker: the texts it embeds reach no caller",
    ),
    "server/access_policy.py": ("policy", "inquiry_read_filter: the filter itself"),
    "server/api/locks.py": ("trusted", "lock authorization lookups"),
    "server/api/host_runs_routes.py": (
        "policy",
        "the seq and chat of the owner's own sessions its host's runs drain",
    ),
    "server/api/session_access.py": ("audience", "session authorization"),
    "server/audience.py": ("audience", "the audience test itself"),
    "server/auth.py": (
        "trusted",
        "process-key authorization checks only the descendant session's account",
    ),
    "server/authority_sweep.py": ("trusted", "background authority worker"),
    "server/primitives.py": (
        "policy",
        "lookup_kinds filters; lookup_kind and the list check are write paths",
    ),
    "server/store/access_group.py": (
        "trusted",
        "the access policy's control writes, under a row lock",
    ),
    "server/sql_fragments.py": (
        "fragment",
        (
            "read.py and edit.py bind {readable} into NEXT_ISSUE, CLAIM_NEXT_ISSUE, "
            "PROVES_BELIEF, PROVING_EDGES, COST_SUBTREE and NARROWS_ANCESTORS"
        ),
    ),
    "server/store/cascade.py": ("trusted", "write-path audit and cascade"),
    "server/store/code_store.py": (
        "policy",
        "snapshot reads; the put path's existence check is a write",
    ),
    "server/store/core.py": ("trusted", "bootstrap and migrations"),
    "server/store/edit.py": (
        "policy",
        "claim_next_issue and its replay; the rest are write-path locks",
    ),
    "server/store/export.py": (
        "policy",
        "every exported table, change_log through change_read_filter",
    ),
    "server/store/graph_focus.py": ("policy", "the focus walk"),
    "server/store/metrics.py": ("policy", "the experiment checks and selection"),
    "server/store/named.py": ("policy", "records named by id or Kind/seq"),
    "server/store/read.py": (
        "policy",
        (
            "every request read, the by-seq lookup (inquiry_id) among them; "
            "what_changed_for_me and _for_anyone serve the subscriber worker, which "
            "has no request principal"
        ),
    ),
    "server/store/session.py": ("audience", "session lifecycle and listing"),
    "server/store/session_feed.py": ("audience", "the session record feed"),
    "server/store/session_ir.py": ("trusted", "write-path session locks and copies"),
    "server/store/search.py": (
        "audience",
        "transcript search, and inquiry search under inquiry_read_filter",
    ),
    "server/store/submit.py": ("trusted", "write path"),
    "server/visuals/reports.py": (
        "policy",
        "source Issue and citations; the created-audit lookup is a write",
    ),
    "server/visuals/session_list.py": (
        "policy",
        "producer Issues; the caller's sessions are audience-gated",
    ),
    "server/web.py": ("policy", "every web read"),
    # A canvas's own readers. Each is reached through a route that has already run
    # the session check, or reads only rows the caller's own account holds.
    "server/api/sessions_routes.py": (
        "audience",
        "every session route runs require_session_access before it reads",
    ),
    "server/asks.py": (
        "trusted",
        "the open asks of one session, read for the run that drains it",
    ),
    "server/store/deleted_chats.py": (
        "trusted",
        "whether a conversation was deleted: an audit row's existence, no content",
    ),
    "server/visuals/partners.py": (
        "trusted",
        "the chat's own session and the key that opened it: who answers, not content",
    ),
    "server/visuals/science_chats.py": (
        "audience",
        "visible_sql narrows a chat's sessions to the ones its reader reads",
    ),
    "server/visuals/screens.py": (
        "audience",
        "visible_sql narrows a screen's records to the ones its reader reads",
    ),
    "server/visuals/session_placement.py": (
        "trusted",
        "where a session sits, by the account that holds it; locks its own row",
    ),
    "server/visuals/timeline.py": (
        "audience",
        "one record by id, for a caller the timeline's route already tested",
    ),
    "server/visuals/workspace_store.py": (
        "audience",
        "a canvas's state and its chat, under the session check its operations run",
    ),
    "server/visuals/workstreams.py": (
        "trusted",
        "the caller's own workstream issues, by owner",
    ),
}
"""Each reading module, relative to this package, its class, and why."""


def test_every_reader_of_inquiries_or_change_log_is_classified() -> None:
    found = unclassified(_CWD, _READERS)
    assert not found, (
        "These modules read inquiries or change_log and are not classified in "
        "_READERS; filter them by the access policy or say why not:\n"
        + "\n".join(found)
    )


@pytest.mark.parametrize(("module", "kind"), [(m, k) for m, (k, _) in _READERS.items()])
def test_a_classified_module_reads_as_classified(module: str, kind: Class) -> None:
    found = misfiling(_CWD / module, kind)
    assert found != "stale", f"{module} is in _READERS and reads neither table"
    assert found != "unwired", f"{module} is a policy reader and calls no read filter"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
