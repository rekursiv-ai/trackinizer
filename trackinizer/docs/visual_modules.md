# Visual modules

The server chooses which visuals exist. `server/visuals/catalog.py` composes
their trusted configgle `Fig` configurations and returns safe descriptions
through `GET /api/visuals`. The browser selects a renderer by visual type and
version. It loads the Chat chunk only when Chat is visible.

## Add a visual

1. Give the visual a namespaced type such as `trax.timeline` and version 1.
   Keep the type stable. Raise the version when old saved parameters cannot
   render correctly.
2. Add a provider with a nested `Config(Fig[...])` and a cheap `describe()`.
   Put its config in `Workspace.Config.visuals`. Catalog reads must not query
   the graph or start a session.
3. Describe accepted parameters in `parameter_schema`. Each parameter needs
   a valid default. Integers need minimum and maximum; strings need
   `max_length` at most 512. Graph data endpoints must page or cap results.
   Browser input is validated against the descriptor. Never pass browser
   JSON to configgle `deserialize()`.
4. Add a renderer to `private/web/src/visuals/registry.tsx` using a dynamic
   import. Add the type and version to `renderer-versions.json`. An unavailable
   renderer produces an error inside its tile.
5. Regenerate `catalog.preview.json` from the backend catalog and dump the
   OpenAPI schema. The snapshot tests compare both JSON files with the Python
   catalog, and TypeScript checks the renderer registration.

## Change a canvas

First enable the canvas in Settings, then `POST /api/workspaces` with the
user's browser session. It returns the default workspace id and revision.
The browser connects a live `trax run` AgentSession through
`PUT /api/workspaces/<workspace-id>/connection` with the current revision and
the session UUID. That session must have been opened by an unrevoked API key
owned by the user. API keys cannot create or change the connection. Once
paired, only that session's key can show a visual through the same operation
the Configure panel uses:

```http
POST /api/workspaces/<workspace-id>/operations
Authorization: Bearer <user-token>
Idempotency-Key: 4d5a0dc1-a916-4d4b-9c23-a58b654c4376
Content-Type: application/json

{"revision":1,"operation":{"kind":"show","visual_type":"trax.chat","placement":"side"}}
```

The response is the full workspace at revision 2. The caller may retry that
exact request with the same key while its receipt is retained. If another tab
or agent changed the canvas,
the server returns 409 and the current state; read it before retrying with a
new revision and key. Reusing a key with different content also returns 409
and the current state. An ended session or revoked key loses control,
including idempotent replay. `hide`, `focus`, and `place` take an `instance_id` from
the workspace response. A `show` can also include `record_id` and bounded
`params`. Showing an existing visual type focuses and updates that instance.

## Chat with a paired session

The browser reads `GET /api/workspaces/<workspace-id>/connection` to check
the stored pairing. Use this direct status for the composer: the session picker
lists only the 100 most recent sessions and cannot prove that an older pairing
ended. A disconnected, ended, or unavailable pairing disables the composer.

`Chat about this` shows `trax.chat` with the record UUID in `record_id`. To
send, the browser calls `POST /api/workspaces/<workspace-id>/messages` with
the persisted Chat instance UUID and a fresh `Idempotency-Key`:

```json
{"text":"What led to this experiment?","chat_instance_id":"<chat-instance-id>","expected_record_id":"<record-id>"}
```

The server checks the signed-in workspace owner, live pairing, and Chat
instance, and that its record still matches `expected_record_id`. A changed
record returns 409 before queueing. The server adds the persisted record UUID,
workspace UUID, and visible visual identities to a typed inbound context.
When the record exists in this server's graph, the context also includes its
kind, sequence, and bounded title. A record resolved through a separate read
profile carries its UUID without invented metadata. The agent receives the
context beside the message and reads the cited graph rows through trax.
The `queued` count is a queue receipt; it does not claim the agent answered.
Retry the same draft with the same key. When the canvas has no persisted
visuals, send null for both `chat_instance_id` and `expected_record_id`; Chat
remains the fallback view.

Chat previews captured turns from the first browser message in its recent
window. It hides transport context and collapses long turns. The full
transcript remains available on the session record page. Subgraph, timeline,
and Artifact visuals use the same catalog and operation path.

## Save and reopen a workflow

The signed-in browser can save the current canvas with
`POST /api/workspace-presets`. Supply the workspace UUID and revision, a name,
optional agent instructions and continuation record UUID, and rectangles for
floating panes. The server snapshots the persisted visual instances only if
the revision still matches. Supply an `Idempotency-Key` UUID and reuse it when
retrying the same save. A stale save returns 409 with the current canvas.

`GET /api/workspace-presets` lists up to 100 saved views for the account. To
continue on another device, open the default canvas and call
`POST /api/workspace-presets/<preset-id>/open` with that canvas UUID, revision,
and an `Idempotency-Key` UUID. Reuse the key when retrying the same open.
Opening restores visuals, placement, floating rectangles, and the
workflow context at a new revision. It clears the old session connection; the
user pairs a live session before sending another message. The next message
carries saved guidance and the continuation record in its typed context.
Presets never store session credentials. Existing browser-only saved inquiry
queries remain a separate feature.

## Publish shared Artifact content

An authenticated writer publishes with `POST /api/artifacts/content`. The
request names an existing Issue, a title, a summary, and either `format:
"structured"` with sections or `format: "html"` with HTML content. Supply an
`Idempotency-Key` UUID. The server creates an Artifact linked to the Issue by
`produced_by` in the same transaction. A retry with the same request and key
returns the same Artifact; a different request with that key returns 409. To
publish another revision, pass `previous_artifact_id` with an existing Artifact
ID from the series. The new revision receives its own Artifact ID.

Each HTML file or structured content body is limited to 30 MB. A publisher may
store up to 500 MB in total; every revision counts. The server checks the
account quota in the publication transaction and rejects an excess with 422.

Structured findings require a claim, measured result, denominator, split,
uncertainty, and citations. A citation can name a record alone, or name a
`proves` or `favors` edge from that record to a `claim_id`. Publication copies
the record title and signed edge value into the Artifact. Later graph edits
cannot change what it cited. For example:

```json
{
  "issue_id": "<issue-uuid>",
  "title": "ARC3 directions",
  "summary": "Measured outcomes and open questions.",
  "format": "structured",
  "sections": [{
    "title": "Representation",
    "summary": "The wider representation improved the held-out score.",
    "details": "Matched runs on the frozen split.",
    "findings": [{
      "claim": "Wider features improved the score",
      "outcome": {"result": "12 wins", "denominator": 16, "split": "held-out"},
      "uncertainty": "The sample is small.",
      "citations": [{
        "record_id": "<source-uuid>",
        "claim_id": "<belief-uuid>",
        "edge_kind": "favors"
      }]
    }]
  }]
}
```

Any signed-in teammate can read the exact revision at
`/app/#/lookup/<artifact-id>` or through
`GET /api/artifacts/<artifact-id>/content`. An agent can show it on a canvas
with `visual_type: "trax.artifact"` and `record_id` set to the Artifact ID.
The renderer loads only when shown. `Chat about this` uses the same `record_id`;
the agent receives frozen citations and a link to the full content. Custom HTML
runs in an opaque-origin iframe with a restrictive content security policy;
it cannot read the app's session.
