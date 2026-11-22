from __future__ import annotations

from trackinizer.wire.artifact_html import CONTENT_SECURITY_POLICY


def _directives() -> dict[str, str]:
    """Split the policy into ``directive -> sources``."""
    parts = (d.partition(" ") for d in CONTENT_SECURITY_POLICY.split("; "))
    return {name: sources for name, _, sources in parts}


def test_page_runs_sandboxed_without_same_origin() -> None:
    sandbox = _directives()["sandbox"].split()
    assert sandbox == ["allow-scripts", "allow-popups"]
    assert "allow-same-origin" not in CONTENT_SECURITY_POLICY


def test_page_reaches_nothing_and_embeds_only_in_trackinizer() -> None:
    directives = _directives()
    for name in ("default-src", "connect-src", "form-action", "worker-src"):
        assert directives[name] == "'none'"
    assert directives["frame-ancestors"] == "'self'"
    assert "https://cdnjs.cloudflare.com" in directives["script-src"]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
