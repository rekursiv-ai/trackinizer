"""Guard: design_idempotency.md names only Store methods that actually exist.

Docs drift when a method is renamed in code but the prose keeps the old name.
This greps the design doc for backtick-quoted ``Store`` method references
(``store.X`` / ``_submit_*`` / ``set_*``) and asserts each resolves to a real
attribute on :class:`Store`, so a future rename either updates the doc or trips
this test. Mirrors ``routes_drift_test`` for prose instead of routes.
"""

from __future__ import annotations

from pathlib import Path

import re

from trackinizer.server.store.core import Store


_DOC = Path(__file__).with_name("design_idempotency.md")

# Backtick-quoted method references the doc makes, e.g. ``store.set_description``,
# ``_submit_generic``, ``emit_change``. Captures the bare identifier.
_METHOD_RE = re.compile(r"`(?:store\.|Store\.)?(_?[a-z][a-z0-9_]+)\b")

# Identifiers that appear in backticks but are NOT Store methods (modules,
# fields, SQL, other classes). Excluded so the guard checks only method names.
_NOT_STORE_METHODS = {
    "change_log",
    "idempotency_key",
    "client",
    "submit",
    "server",
    "store",
    "types",
    "wire",
    "bodies",
    "id",
    "value",
}


def test_design_idempotency_names_real_store_methods() -> None:
    text = _DOC.read_text()
    cited = {
        name
        for name in _METHOD_RE.findall(text)
        if name not in _NOT_STORE_METHODS
        # Only check names that look like Store methods: a leading underscore
        # (private helper) or a known public-method prefix.
        and name.startswith(("_", "submit_", "set_", "emit_"))
    }
    missing = {name for name in cited if not hasattr(Store, name)}
    assert not missing, (
        f"design_idempotency.md cites Store methods that do not exist: "
        f"{sorted(missing)}. Update the doc to the current names."
    )


def test_design_idempotency_has_no_known_stale_names() -> None:
    """The specific dead names from the last drift must never reappear."""
    text = _DOC.read_text()
    stale = {"_submit_inquiry", "set_summary", "wire_inquiries"}
    present = {name for name in stale if name in text}
    assert not present, f"stale names back in design_idempotency.md: {sorted(present)}"
