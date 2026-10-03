"""The committed ``src/api/openapi.json`` must equal the in-repo app's schema.

The web app is typed against the committed dump, so a server change that alters
the contract (a route, a parameter, a body) fails here, in Python CI, instead of
shipping a client typed against routes that no longer exist.
"""

from __future__ import annotations

import difflib

import pytest

from trackinizer.web.scripts.openapi_dump import (
    OPENAPI_JSON,
    render,
    web_app_schema,
)


# Generating the whole app's schema takes 0.14 s on the dev Mac, over the 100 ms
# unit budget, so this runs in the slow tier, which CI runs on every pull request.
@pytest.mark.compute_large_fixture
def test_committed_schema_matches_the_app() -> None:
    committed = OPENAPI_JSON.read_text()
    current = render(web_app_schema())
    diff = difflib.unified_diff(
        committed.splitlines(keepends=True),
        current.splitlines(keepends=True),
        fromfile="committed",
        tofile="app",
    )
    assert committed == current, (
        f"{OPENAPI_JSON} differs from the server's schema. Regenerate it with\n"
        "  trackinizer/web/scripts/openapi_dump.py\n"
        "then run `./npm run typecheck` in trackinizer/web.\n" + "".join(diff)[:4000]
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
