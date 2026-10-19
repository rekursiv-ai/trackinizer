# Visual modules

The server chooses which visuals exist. `server/visuals/catalog.py` composes
their trusted configgle `Fig` configurations into a `Workspace`. A deployment
supplies its own `Workspace`; the default is `default_workspace()`. Every
route reads that one catalog and returns safe descriptions through
`GET /api/visuals`. The default visual must not require a record. The
browser selects a renderer by visual type and version. It loads a renderer's
chunk only when its visual is shown. Chat is not a visual: it is a column of its
own beside the canvas.

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
   tile. The renderer draws rows: its record is the app's anchor row, it
   gathers the app's other rows by labels and edges, and it reads what only
   the app knows from each row's `app.<app>`, which the app's writers keep in
   the shape it draws. A visual serves no data of its own: to change what it
   shows, an agent writes the rows, and moves only attention on the canvas.
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
same operation the top bar's Workbench menu uses:

```http
POST /api/workspaces/<workspace-id>/operations
Authorization: Bearer <user-token>
Idempotency-Key: 4d5a0dc1-a916-4d4b-9c23-a58b654c4376
Content-Type: application/json

{
  "revision": 1,
  "operation": {
    "kind": "show",
    "visual_type": "trax.subgraph",
    "record_id": "<record-id>",
    "placement": "side"
  }
}
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

Chat is a column beside the canvas, on the left by default; the browser keeps
its side and width, and a key hides it. It is no visual, so a canvas has no Chat
instance and a line pins no record. To send, the browser calls `POST /api/chats`
with a fresh `Idempotency-Key`:

```json
{"kind":"science","workspace_id":"<workspace-id>","text":"What led to this experiment?"}
```

The server checks the signed-in writer and a live assistant. It adds the
workspace UUID and the visible visual identities to a typed inbound context.
The browser also sends
`page`, the `#/...` address it is on, and `trail`, the addresses before it,
oldest first and at most 8; a malformed address, or a longer trail, returns
422. The server resolves them: `#/lookup/<id>`, `#/inquiry/<id>`,
`#/ref/<Kind>/<seq>` and `#/graph?focus=<ref>` name a record, and the context
carries each as a page with its route and, when it exists, that record.
Any other address, or a record that does not exist, keeps its route and has
no record. Each visible visual carries the record it shows the same way, so
"this" means what the sender sees, and its `params` as the canvas holds them,
so an agent sees a narrowing the reader set in a pane. The agent receives the
context beside the message and reads the cited graph rows through trax.
The receipt names the conversation, and the session once the assistant has it
open; it does not claim the agent answered. Retry the same draft with the
same key.

A conversation is an AgentSession (label `science-chat`). Chat reads its
records and refreshes when the canvas event stream says the session changed;
a line by another poster shows its sender. Chat's header lists the chats of the
station's workstream, each a station holding one chat; the composer says who
reads the chat, by its audience and the server's org mode. A science
chat's session in the Console or on its detail page offers Continue in Chat.
Subgraph, timeline, Artifact and files visuals use the same catalog and
operation path.

## Lineage and timeline

`trax.timeline` is one view, off in a new canvas; the Workbench menu shows or
hides it and `trax workspace W show trax.timeline --record R` or the Chat
assistant opens it. Time runs across; lineage runs down: up to three lead issues pinned
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

## Campaign summary

`trax.summary` shows what a campaign did to readers who may not read its
transcripts. Open it on the campaign's brief Issue or on any of its AgentSessions
(`record_kinds` is those two). It reads the Artifacts the summary service files:
`GET /api/inquiries` for kind Artifact filtered by label, `summary:issue:<id>`
from an Issue, or `summary:session:<id>` then `summary:lead:<id>` from a session.
Each Artifact's description is a plain-JSON document that the service writes. The
page keeps only rows filed by `summary_accounts` in `GET /api/me/profile`, the staff
setting's service accounts, since any writer can put these labels on a row.
Sessions lists the campaign's sessions by start, with no spawn tree or cost, which
are how it was run. Session shows one with its steps, phases and beliefs, and
Corpus groups the efforts and claims they share.
A running campaign's summary is read again every minute.

The documents carry no transcript text, only item numbers and where each item is
stored. A reader whose profile lists the `staff` audience also gets the
transcript beside the steps (`/api/sessions/<id>/records`) and the numbers open
it; the page only chooses what to show, and the server refuses the transcript to
anyone else (`session_audience`).

## Files

`trax.files` shows a record's files and what changed in them, each change with
its diff. It takes a record of any kind and reads the newest files document filed
for it. No route reads a work tree or a file: the document is an Artifact, and the
page opens and downloads nothing.

The document is the Artifact's description, one compact JSON object
(`wire/files_doc.py`, `FilesDoc`, `dump_files`, `load_files`):

| Key | Meaning |
|---|---|
| `kind`, `version` | `"files"` and `1`; a reader drops any other version. |
| `base` | What `changed` is measured against (`origin/main`, a sha); `""` when unknown. |
| `files`, `more_files` | The tree's paths, sorted and possibly capped; whether more exist. |
| `changed`, `more_changes` | Each file that differs from `base`, sorted by path; whether more exist. |
| `changed[].change` | `added`, `modified` or `deleted`; a deleted file is never in `files`. |
| `changed[].diff`, `note` | Git's diff of the one path, or `""` when `note` is `withheld` or `omitted`; `clipped` cuts it at a line end. |
| `hidden` | Paths the source withheld from both lists by its own rule. |

It holds no file content beyond diffs and no time, so an unchanged tree writes the
same text. A producer labels the Artifact `files` and `files:of:<record id>` for
each record it describes, may add labels of its own, and files a `produced_by`
edge to each record so the document shows among its relations. A `files:of:` label
is a link, never a row key: every source of a record's files writes it, so a
producer that refreshes its row in place finds it by a label only it writes (the
campaign summary service keys `files:campaign:<lead id>`), or it would overwrite
another source's document. The shape names no source, so a campaign's work tree, a
branch or a pull request is filed alike.

The page reads `GET /api/inquiries` for kind Artifact with the labels `files` and
`files:of:<record id>`, and keeps a row only when it was filed by one of the
`summary_accounts` in `GET /api/me/profile` (the operator's service accounts) or
by the account the record itself is filed under, read with
`GET /api/inquiries/<record id>` only when some row is filed by another account.
Any writer can put these labels on a row, so a third party's document on someone
else's record is dropped, while a person's own source (a work tree they file on
their own session or Issue) shows with no change to the page. Of those kept, the
newest by `modified` shows, with a count when there are several. The list route
applies the reader's audiences to every row, so a reader sees a document only
where they may read that Artifact.

Three bounded params narrow it, applied in order:

- `path` (string, at most 512): `""` shows everything; with `*` or `?` it is a
  glob over the whole path (`**/` zero or more directories, `*` any run without
  `/`, `?` one character but `/`); otherwise a prefix (`src/foo/` is that
  directory). Case is ignored. It narrows the tree and the changes.
- `changed` (boolean): `true` hides the tree; only the changed files show.
- `file` (string, at most 512): opens the diff of the changed file at that path,
  else of every changed file ending in `/<file>`, among only those `path`
  leaves; it never narrows the list.

As with `trax.subgraph`, a show naming only a new record keeps the params, and a
show with any param sets all three. The server refuses a param outside its bound
with the sentence `refuse_params` (`server/visuals/workspaces.py`) writes, which the
Chat assistant's `ShowFiles` also checks before it asks. `trax.campaign` embeds
the same panel under the campaign's lineage, reading the brief's document with
the extra label `campaign:<slug>`.

## Stations

A canvas is a station: a named canvas that holds at most one science chat. The
signed-in browser lists its stations with `GET /api/workspaces`, makes one with
`POST /api/workspaces/new`, renames it with `PATCH /api/workspaces/<id>`, and
returns to it with `POST /api/workspaces/<id>/open`; `POST /api/workspaces` opens
the one used last. A station keeps its visual instances, agent instructions and
continuation record as its canvas state, and the page it was last on
(`PUT /api/workspaces/<id>/page`). The next chat message carries the station's
guidance and continuation record in its typed context. A station never stores
session credentials. Existing browser-only saved inquiry queries remain a separate
feature.

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
The renderer loads only when shown. Custom HTML
runs in an opaque-origin iframe with a restrictive content security policy;
it cannot read the app's session.
