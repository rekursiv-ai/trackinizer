"""The ``sh`` adapter: a locator plus a normalizer factory.

The framing of a pty's bytes into lines is :class:`LineCapture`, tested in
``types/line_capture_test.py``.
"""

from __future__ import annotations

from pathlib import Path

from trackinizer.runs.adapters.iostream import IOStreamAdapter


class TestAdapter:
    def test_has_no_session_files_to_tail(self) -> None:
        """A wrapped command writes no log; the PTY stream is the source."""
        adapter = IOStreamAdapter()

        assert tuple(adapter.session_dirs()) == ()
        assert adapter.session_scope() is None

    def test_claims_no_file_even_one_that_looks_like_a_log(self) -> None:
        assert not IOStreamAdapter().matches_session_file(Path("session.jsonl"))

    def test_names_no_cli_binary(self) -> None:
        """The command comes from the ``--`` args, not the adapter.

        The empty binary is what the runner dispatches on to take the
        command line from the user instead of exec'ing a known CLI.
        """
        assert IOStreamAdapter().cli_binary == ""

    def test_is_not_resumable(self) -> None:
        """No CLI wrote these bytes, so none can be handed them back."""
        assert IOStreamAdapter().session_id_from_path(Path("x")) is None
        assert IOStreamAdapter().resumed_session(("--resume", "x")) is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
