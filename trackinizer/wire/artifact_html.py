"""How Trackinizer serves an HTML Artifact: the policy its page runs under.

Shared by the server, which sends it, and by anything that must load a page the
way a reader will, such as a check run before the page is published.
"""

from __future__ import annotations

from typing import Final


# The script and font allowlist matches Claude Sites, so a page built for Claude
# Sites works here. ``sandbox`` gives the page an opaque origin even when opened
# directly, so its scripts cannot read Trackinizer cookies or storage, nor call the
# API. ``connect-src 'none'`` keeps ``fetch`` from reaching anything, and
# ``frame-ancestors 'self'`` lets only Trackinizer embed it.
CONTENT_SECURITY_POLICY: Final = "; ".join(
    [
        "sandbox allow-scripts allow-popups",
        "default-src 'none'",
        "base-uri 'none'",
        "connect-src 'none'",
        "font-src data: https://fonts.gstatic.com",
        "form-action 'none'",
        "frame-ancestors 'self'",
        "frame-src 'none'",
        "img-src data: blob:",
        "manifest-src 'none'",
        "media-src data: blob:",
        "object-src 'none'",
        (
            "script-src 'unsafe-inline' 'unsafe-eval' "
            "https://cdnjs.cloudflare.com "
            "https://cdn.jsdelivr.net/npm/ https://unpkg.com "
            "https://cdn.tailwindcss.com https://code.jquery.com"
        ),
        "style-src 'unsafe-inline' https://fonts.googleapis.com",
        "worker-src 'none'",
    ],
)
"""The ``Content-Security-Policy`` of every HTML Artifact page."""
