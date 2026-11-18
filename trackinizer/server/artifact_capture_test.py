"""The capture script added to an HTML Artifact page."""

from __future__ import annotations

import pytest

from trackinizer.server.artifact_capture import with_capture_script


def test_the_script_is_added_once_before_the_last_closing_body_tag() -> None:
    page = "<!doctype html><body><pre>&lt;/body&gt;</pre></body></html>"

    served = with_capture_script(page)

    assert served.count("<script>") == 1
    assert served.index("<script>") > served.index("</pre>")
    assert served.endswith("</script></body></html>")
    assert served.startswith("<!doctype html><body>")


@pytest.mark.parametrize("closing", ["</BODY>", "</body >"])
def test_the_closing_tag_is_found_however_it_is_spelled(closing: str) -> None:
    served = with_capture_script(f"<p>x</p>{closing}")

    assert served.endswith(f"</script>{closing}")


def test_a_page_with_no_closing_body_tag_gets_the_script_at_the_end() -> None:
    served = with_capture_script("<!doctype html><h1>Hi</h1>")

    assert served.startswith("<!doctype html><h1>Hi</h1><script>")
    assert served.endswith("</script>")


def test_the_script_carries_no_nonce_so_the_pages_own_scripts_still_run() -> None:
    assert "nonce" not in with_capture_script("<body></body>").lower()


def test_the_script_cannot_end_the_script_element_it_sits_in() -> None:
    served = with_capture_script("")

    assert served.count("</script") == 1


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
