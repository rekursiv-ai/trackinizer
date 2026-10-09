# Visual modules

The server chooses which visuals exist. `server/visuals/catalog.py` composes
their trusted configgle `Fig` configurations into a `Workspace`. A deployment
supplies its own `Workspace`; the default is `default_workspace()`. Every
route reads that one catalog and returns safe descriptions through
`GET /api/visuals`. The default visual must not require a record. The
browser selects a renderer by visual type and version. It loads the Chat chunk
only when Chat is visible.

## Add a visual

1. Give the visual a namespaced type such as `trax.timeline` and version 1.
   Keep the type stable. Raise the version when old saved parameters cannot
   render correctly.
2. Add a `StaticVisual.Config` entry for a visual without parameters, or a
   provider with a nested `Config(Fig[...])` and a cheap `describe()`. Put its
   config in `Workspace.Config.visuals`. Catalog reads must not query
   the graph or start a session.
3. Describe accepted parameters in `parameter_schema`. Each parameter needs
   a valid default. Integers need minimum and maximum; strings need
   `max_length` at most 512. Graph data endpoints must page or cap results.
   A parameter accepts only the bounds of its type. The descriptor is the
   single source of a parameter's default and maximum; routes read those
   limits from it. Browser input is validated against the descriptor. Never
   pass browser JSON to configgle `deserialize()`.
4. Register a renderer for the type and version in the web client, loaded with
   a dynamic import. An unavailable renderer produces an error inside its
   tile.
5. Regenerate the client's catalog preview and the OpenAPI schema from the
   backend catalog so the snapshot tests and the client type check agree with
   it.

The renderer lives in `trackinizer/web/src/visuals/registry.tsx`, and
its type and version go in `renderer-versions.json` beside it. The preview
snapshot is `catalog.preview.json` in the same directory.

The snapshot command from the repository root is:

```sh
uv --quiet run --frozen python -c 'from trackinizer.server.visuals.catalog import default_catalog; print(default_catalog().model_dump_json(indent=2))' > trackinizer/web/src/visuals/catalog.preview.json
uv --quiet run --frozen trackinizer/web/scripts/openapi_dump.py
```

The preview snapshot is used only when local Vite points at an older backend
that answers 404 for `/api/visuals`. A deployed server reads the route.

## Change a canvas

The canvas is on by default (Settings turns it off). `POST /api/workspaces` with the
user's browser session. It returns the default workspace id and revision.
Besides the browser, only the server's Chat assistant may change a canvas, and
only one whose owner has talked to it there. Its key shows a visual through the
same operation the Configure panel uses:

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

## Chat with the assistant

Chat talks to the canvas's partner, which `WorkspaceState.partner` names on
every read and the events stream pushes when it changes. By default it is the
server's shared assistant; `WorkspaceState.partner_choice` can instead be
`local`, the owner's own `trax helper` session, which the `partner`
operation sets. A partner that is not `live` disables the composer. A server with
no assistant of its own can run `trax helper claude --as ACTOR` as one.

The canvas's one `Chat` button shows `trax.chat`; on a record's page it puts
the record UUID in `record_id`, and elsewhere it clears it. To
send, the browser calls `POST /api/chats` with
the persisted Chat instance UUID and a fresh `Idempotency-Key`:

```json
{"kind":"science","workspace_id":"<workspace-id>","text":"What led to this experiment?","chat_instance_id":"<chat-instance-id>","expected_record_id":"<record-id>"}
```

The server checks the signed-in writer, a live assistant, and the Chat
instance, and that its record still matches `expected_record_id`. A changed
record returns 409 before queueing. The server adds the persisted record UUID,
workspace UUID, and visible visual identities to a typed inbound context.
When the record exists in this server's graph, the context also includes its
kind, sequence, and bounded title. A record resolved through a separate read
profile carries its UUID without invented metadata. The browser also sends
`page`, the `#/...` address it is on, and `trail`, the addresses before it,
oldest first and at most 8; a malformed address, or a longer trail, returns
422. The server resolves them: `#/lookup/<id>`, `#/inquiry/<id>`,
`#/ref/<Kind>/<seq>` and `#/graph?focus=<ref>` name a record, and the context
carries each as a page with its route and, when it exists, that record.
Any other address, or a record that does not exist, keeps its route and has
no record. Each visible visual carries the record it shows the same way, so
"this" means what the sender sees. The agent receives the
context beside the message and reads the cited graph rows through trax.
The receipt names the conversation, and the session once the assistant has it
open; it does not claim the agent answered. Retry the same draft with the
same key. When the canvas has no persisted visuals, send null for both
`chat_instance_id` and `expected_record_id`; Chat remains the fallback view.

A conversation is an AgentSession (label `science-chat`). Chat reads its
records and refreshes when the canvas event stream says the session changed;
a line by another poster shows its sender. History lists the chats the user
started or posted in; Clear chat starts a new one and deletes nothing, and the
composer says chats are public to every user and cannot be deleted. A science
chat's session in the Console or on its detail page offers Continue in Chat.
Subgraph, timeline, and Artifact visuals use the same catalog and operation
path.

## Lineage and timeline

`trax.timeline` is one view, off in a new canvas; Configure shows or hides it
and `trax workspace W show trax.timeline --record R` or the Chat assistant
opens it. Time runs across; lineage runs down: up to three lead issues pinned
at the axis's left edge with their own dates, the record, then its directions
as rows. Each row's results sit on it by date, and their signed evidence is
marked for or against by the sign of its valence. A card or square moves the
page to that record and re-centres the window on it (`show` on the same
instance with the clicked record).

It makes one read, `GET /api/visuals/timeline/<id>`, for a record of any kind.
An Issue is the record and its own anchor. An Experiment is shown on the Issue
that produced it, itself marked among that Issue's results. Any other kind is the
record row, with leads and directions taken from the nearest Issue it was
`produced_by` (the oldest if several; `narrows` runs between Issues only), and
that Issue becomes its nearest lead; with no such Issue it is shown alone.
Leads are the anchor's `narrows` ancestors, at most three (`_LEAD_LEVELS` in
`server/visuals/timeline.py`), one indexed lookup per level.

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
workflow context at a new revision. The next message carries saved guidance
and the continuation record in its typed context.
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
The renderer loads only when shown. `Chat` on its page uses the same `record_id`;
the agent receives frozen citations and a link to the full content. Custom HTML
runs in an opaque-origin iframe with a restrictive content security policy;
it cannot read the app's session.
