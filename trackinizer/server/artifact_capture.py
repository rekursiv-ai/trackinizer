"""The script that lets a canvas take a picture of a sandboxed HTML Artifact."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import re


_CWD: Final = Path(__file__).resolve().parent

__all__ = ["with_capture_script"]


_SCRIPT: Final = _CWD / "assets" / "artifact_capture.js"
_BODY_END: Final = re.compile(r"</body\s*>(?![\s\S]*</body\s*>)", re.IGNORECASE)


def with_capture_script(html: str) -> str:
    """Return ``html`` with the capture script added, as the last thing in the body.

    The script is inline and has no nonce: the Artifact policy allows inline
    scripts, and a nonce would make browsers ignore that allowance for the page's
    own scripts. The document type is never touched, so the page keeps its mode.

    Args:
      html: The page as its author published it.

    Returns:
      page: The page that is served.

    """
    script = f"<script>{_SCRIPT.read_text()}</script>"
    found = _BODY_END.search(html)
    if found is None:
        return html + script
    return html[: found.start()] + script + html[found.start() :]
