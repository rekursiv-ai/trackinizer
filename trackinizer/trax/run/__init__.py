"""``trax run``: spawn an agent CLI, tail its session log, emit events.

The wrapped CLI inherits the real terminal and runs as if launched
directly. Alongside it, a per-CLI ``Adapter`` watches the CLI's session-log
directory and turns new JSONL lines into ``Event`` records, which flow to a
sink.

By default events sync to the Trackinizer server resolved from the active
trax profile (URL plus auth), reaching the same server as every other
``trax`` verb. ``--no-sync`` (or ``--out PATH``) captures to a local JSONL
file with no network instead.
"""

from __future__ import annotations
