---
name: trax-agentsession
description: Use when authoring a trackinizer AgentSession -- the captured session envelope, cli/session-id, started/ended lifecycle, the events-table asymmetry. Not trax grammar (trax).
---

# AgentSession — authoring expectations

An `AgentSession` is a captured agent-CLI session envelope, citeable like any
other artifact. Produced by `trax run <cli>`.

Per-kind expectations doc (SoT). Fields owned by `types/inquiries.py`; grammar by
`../trax-skill.md`.

## Completeness bar

- **title** = what the session was for.
- **cli** = the wrapped CLI (`claude`, `gemini`, `codex`, `cursor`).
- **cli_session_id** = the CLI's own session id (for vendor correlation / dedup).
- **started** / **ended** = session bounds (ISO). `ended` is written only by the
  atomic lifecycle moves (`end_session` / `_resume_session`), never a blind field
  edit — a standalone PUT desyncs the lifecycle CHECK.
- `rooms` = namespaces the session can be addressed within (`@actor:room`).

## The events asymmetry (do not collapse it)

This row is only the ENVELOPE. The per-turn transcript (user/assistant/thinking/
tool turns) lives in the separate append-only `agent_session_events` table,
scoped by `session_id`, **outside** `inquiries`, edges, and `change_log`.
Per-turn model/cwd live on the events, not this row (both can change mid-session,
so a single row value would be lossy). Preserve this boundary — simplifying trax
must not fold events into the inquiry graph.

## Expectations

- Edge the session to the Issues/CodeChanges it bore on; supersede it when a
  session is resumed.
- Do not set `ended` by hand; use the lifecycle moves.

## Common mistakes

- A blind `ended`/`status` edit that breaks the lifecycle CHECK.
- Treating the row as the transcript (it is the envelope; turns are in
  `agent_session_events`).
- Putting per-turn model/cwd on the row instead of the events.
