# Trackinizer JSON+REST API

`types/{inquiries,edges,change_log,cost}.py` is the ultimate source of truth for names, DB schema, structure, etc.

This doc is solely responsible for specifying the JSON/REST API. In the event of discrepancy, `types/{inquiries,edges,change_log}.py` wins.

Canonical `Inquiry`, `Edge`, `Change`, and `Cost` JSON is the matching dataclass JSON from `types/*`, using those field names. This doc does not restate those schemas.

## 1. Route index

### 1.1 Inquiry read

```
GET  /api/inquiries/<uuid>
GET  /api/inquiries/<kind>/<seq>
GET  /api/inquiries/<uuid>/cost
GET  /api/inquiries/<uuid>/cost?deep=true
GET  /api/inquiries
GET  /api/inquiries/next_issue
GET  /api/inquiries/<uuid>/proves_belief
POST /api/inquiries/lookup
```

### 1.2 Inquiry list query params

```
GET /api/inquiries?kind=<kind>
GET /api/inquiries?kind=<kind>&kind=<kind>
GET /api/inquiries?kind=<kind>&filter=<json>
GET /api/inquiries?kind=<kind>&filter=<json>&filter=<json>
GET /api/inquiries?kind=<kind>&limit=N
GET /api/inquiries?kind=<kind>&offset=N
GET /api/inquiries?kind=<kind>&seq_range=A..B
GET /api/inquiries?kind=<kind>&seq_range=A..B&seq_range=C..
GET /api/inquiries?kind=<kind>&status=<status>
```

### 1.3 Inquiry create

```
POST /api/inquiries/issue
POST /api/inquiries/artifact
POST /api/inquiries/experiment
POST /api/inquiries/paper
POST /api/inquiries/belief
POST /api/inquiries/codechange
POST /api/inquiries/webresult
POST /api/inquiries/websearch
POST /api/inquiries/batch
```

### 1.4 Inquiry delete

```
DELETE /api/inquiries/<uuid>
```

### 1.5 Inquiry set fields

```
PUT /api/inquiries/<uuid>/owner
PUT /api/inquiries/<uuid>/status
PUT /api/inquiries/<uuid>/title
PUT /api/inquiries/<uuid>/description
PUT /api/inquiries/<uuid>/labels
PUT /api/inquiries/<uuid>/marginal_cost_agent_usd
PUT /api/inquiries/<uuid>/marginal_cost_resource_usd
PUT /api/inquiries/<uuid>/subscribers
PUT /api/issue/<uuid>/issue_kind
PUT /api/issue/<uuid>/validation
PUT /api/issue/<uuid>/priority
PUT /api/experiment/<uuid>/outcome
PUT /api/experiment/<uuid>/codechanges
PUT /api/paper/<uuid>/source
PUT /api/belief/<uuid>/judgement
PUT /api/belief/<uuid>/confidence
PUT /api/codechange/<uuid>/sha
PUT /api/webresult/<uuid>/url
PUT /api/websearch/<uuid>/query
PUT /api/websearch/<uuid>/provider
```

Base fields and cost axes route under `/api/inquiries`; kind-specific
fields route under their owning kind (`/api/<kind>/<uuid>/<field>`),
mirroring the Python `paper.source` and CLI `trax paper` structure.

### 1.6 Inquiry add/sub fields

```
PATCH /api/inquiries/<uuid>/labels
PATCH /api/inquiries/<uuid>/marginal_cost_agent_usd
PATCH /api/inquiries/<uuid>/marginal_cost_resource_usd
PATCH /api/inquiries/<uuid>/subscribers
PATCH /api/issue/<uuid>/issue_kind
PATCH /api/experiment/<uuid>/codechanges
```

### 1.7 Inquiry unset fields

```
DELETE /api/inquiries/<uuid>/owner
DELETE /api/inquiries/<uuid>/description
DELETE /api/inquiries/<uuid>/labels
DELETE /api/inquiries/<uuid>/marginal_cost_agent_usd
DELETE /api/inquiries/<uuid>/marginal_cost_resource_usd
DELETE /api/inquiries/<uuid>/subscribers
DELETE /api/issue/<uuid>/issue_kind
DELETE /api/issue/<uuid>/validation
DELETE /api/issue/<uuid>/priority
DELETE /api/experiment/<uuid>/outcome
DELETE /api/experiment/<uuid>/codechanges
DELETE /api/paper/<uuid>/source
DELETE /api/belief/<uuid>/judgement
DELETE /api/belief/<uuid>/confidence
DELETE /api/codechange/<uuid>/sha
DELETE /api/webresult/<uuid>/url
DELETE /api/websearch/<uuid>/query
DELETE /api/websearch/<uuid>/provider
```

### 1.8 Edge read

```
GET /api/edges/<from_uuid>/<edge_kind>/<to_uuid>
```

### 1.9 Edge create

```
POST /api/edges/<from_uuid>/<edge_kind>/<to_uuid>
POST /api/edges/batch
```

Edge create is an upsert: a new edge is inserted, and a re-create on an
existing edge applies any supplied annotations (`priority` / `note` /
`valence` / `labels`) to it -- never a duplicate error. A bare re-create is
a no-op. The single-edge response carries a `created` flag.

### 1.10 Edge delete

```
DELETE /api/edges/<from_uuid>/<edge_kind>/<to_uuid>
```

### 1.11 Edge set fields

```
PUT /api/edges/<from_uuid>/<edge_kind>/<to_uuid>/priority
PUT /api/edges/<from_uuid>/<edge_kind>/<to_uuid>/note
PUT /api/edges/<from_uuid>/<edge_kind>/<to_uuid>/valence
PUT /api/edges/<from_uuid>/<edge_kind>/<to_uuid>/labels
```

### 1.12 Edge add/sub fields

```
PATCH /api/edges/<from_uuid>/<edge_kind>/<to_uuid>/labels
```

### 1.13 Edge unset fields

```
DELETE /api/edges/<from_uuid>/<edge_kind>/<to_uuid>/priority
DELETE /api/edges/<from_uuid>/<edge_kind>/<to_uuid>/note
DELETE /api/edges/<from_uuid>/<edge_kind>/<to_uuid>/valence
DELETE /api/edges/<from_uuid>/<edge_kind>/<to_uuid>/labels
```

### 1.14 Change log

```
GET /api/change_log
GET /api/change_log/<uuid>
GET /api/change_log/stream
```

### 1.15 Change log query params

```
GET /api/change_log?since=<ts>
GET /api/change_log?after_id=<uuid>
GET /api/change_log?actor=<actor>
GET /api/change_log?subject_id=<uuid>
GET /api/change_log?subject_kind=<kind>
GET /api/change_log?kind=<change_kind>
GET /api/change_log?limit=N
```

### 1.16 Auth

```
GET  /auth/login
GET  /auth/login?next=<path>
GET  /auth/callback
POST /auth/logout
```

### 1.17 Me

```
GET  /api/me/profile
GET  /api/me/tokens
POST /api/me/tokens
POST /api/me/tokens/<uuid>/revoke
PUT  /api/me/tokens/<uuid>/role
```

### 1.18 Admin users

```
GET    /api/admin/users
PUT    /api/admin/users/<uuid>/role
POST   /api/admin/users/<uuid>/disable
POST   /api/admin/users/<uuid>/enable
DELETE /api/admin/users/<uuid>
```

### 1.19 Admin allowlist

```
GET    /api/admin/allowlist
POST   /api/admin/allowlist
PUT    /api/admin/allowlist/<email_or_pattern>/role
DELETE /api/admin/allowlist/<email_or_pattern>
```

### 1.20 Web UI

```
GET /api/web/search
GET /api/web/search?q=<query>
GET /api/web/search?q=<query>&kind=<kind>
GET /api/web/search?q=<query>&kind=<kind>&limit=N
GET /api/web/recent_changes
GET /api/web/recent_changes?limit=N
GET /api/web/lookup/<uuid>
GET /api/web/get/<uuid>
GET /api/web/subscribe
GET /api/web/feed
GET /api/web/feed?after_created=<iso>&after_session=<uuid>&after_seq=N
GET /api/web/feed?since=<iso>&until=<iso>&room=<room>&actor=<actor>&limit=N&tail=<bool>
```

### 1.21 Agent-session ingest

The capture-and-messaging surface for `trax run <cli>`. A run opens a
session, streams turn-grained events, and closes it; messages route back
into a live session by routing name.

```
POST   /api/sessions/start
POST   /api/sessions/<uuid>/events
GET    /api/sessions/<uuid>/events
GET    /api/sessions/<uuid>/events?limit=N&offset=N&seq_range=<a..b>&kind=<kind>
POST   /api/sessions/<uuid>/end
POST   /api/sessions/<uuid>/inbound
GET    /api/sessions/<uuid>/inbound
POST   /api/messages
```

### 1.22 Service meta

```
GET /api/version
```

`GET /api/version` is unauthenticated and store-free, returning
`{"sha": "<hex>"}` (the running build, from `$TRACKINIZER_SHA` or
`git HEAD`, else `"unknown"`). A 404 means the live binary predates the
endpoint -- itself a staleness signal.

## 2. Glossary

### 2.1 Route tokens

```
<uuid>              canonical UUID string
<kind>              URL inquiry kind
<seq>               per-kind integer sequence number
<field>             mutable SQL column field
<edge_kind>         Edge.Kind value
<from_uuid>         Edge.from_id
<to_uuid>           Edge.to_id
<change_kind>       Change.Kind value
<actor>             Inquiry.Actor string
<email_or_pattern>  exact email or allowlist glob
<path>              relative redirect path
<ts>                ISO-8601 timestamp
```

### 2.2 URL kind tokens

```
issue        -> Issue
artifact     -> Artifact
experiment   -> Experiment
paper        -> Paper
belief       -> Belief
codechange   -> CodeChange
webresult    -> WebResult
websearch    -> WebSearch
```

### 2.3 Inquiry field operations

```
field                       PUT  PATCH  DELETE  API note
account                     yes  no     no      required
owner                       yes  no     yes
status                      yes  no     no      required
title                       yes  no     no      required
description                 yes  no     yes
labels                      yes  yes    yes
marginal_cost_agent_usd     yes  yes    yes     alias
marginal_cost_resource_usd  yes  yes    yes     alias
subscribers                 yes  yes    yes
issue_kind                  yes  yes    yes
validation                  yes  no     yes
priority                    yes  no     yes
outcome                     yes  no     yes
abstract                    yes  no     yes
authors                     yes  yes    yes
publication_type            yes  no     yes
venue                       yes  no     yes
subvenue                    yes  no     yes
publish_date                yes  no     yes
source                      yes  no     yes
google_scholar_cluster_id   yes  no     yes
google_scholar_cites_id     yes  no     yes
judgement                   yes  no     yes
confidence                  yes  no     yes
sha                         yes  no     yes
url                         yes  no     yes
query                       yes  no     yes
provider                    yes  no     yes
cli                         yes  no     yes
cli_session_id              yes  no     yes
started                     yes  no     yes
rooms                       yes  yes    yes
codechanges                 yes  yes    yes
id                          no   no     no      immutable
kind                        no   no     no      immutable
seq                         no   no     no      immutable
created                     no   no     no      immutable
modified                    no   no     no      server-managed
projected edge fields       no   no     no      mutate via /api/edges
```


### 2.4 Edge fields

Annotation fields

```
priority
note
valence
labels
```

`valence` is a signed `[-1, 1]` weight on `proves` / `favors` citations: the
sign is the polarity (positive supports the claim, negative argues against),
the magnitude the evidential weight (`0` neutral, default `0.5`). For-vs-against
is this sign, not a separate edge kind.

Endpoint pairs

Every edge is stored child -> parent (`from` younger/dependent, `to` older
parent). Exactly six kinds:

```
narrows         Issue      -> Issue          (narrower  -> broader)
requires        Issue      -> Issue          (requirer  -> prerequisite)
produced_by     Inquiry    -> Inquiry        (produced  -> producer)
supersedes      Inquiry    -> Inquiry        (successor -> predecessor)
proves          Artifact   -> {Belief, Experiment}   (citing -> cited)
favors          Artifact   -> {Belief, Experiment}   (citing -> cited)
```

Projected pairs

```
narrows         -> Issue.narrows / Issue.narrowed_by
requires        -> Issue.requires / Issue.required_by
produced_by     -> Inquiry.produced_by / Inquiry.produces
supersedes      -> Inquiry.supersedes / Inquiry.superseded_by
proves          -> Artifact.proves / {Belief,Experiment}.proved_by
favors          -> Artifact.favors / {Belief,Experiment}.favored_by
```

### 2.5 Auth roles

```
viewer
writer
admin
```

### 2.6 API-only body aliases

```
marginal_cost_agent_usd     -> marginal_cost.agent_usd
marginal_cost_resource_usd  -> marginal_cost.resource_usd
```

## 3. Wire format

### 3.1 Required request headers

```
Authorization: Bearer <token>
Content-Type: application/json
```

### 3.2 Mutating request headers

```
Idempotency-Key: <uuid>
```

Optional on every field/edge mutating route. When sent, the server uses
it as the `change_log.id` of the change the request produces, so a
retried request collides on that id and replays the original outcome
instead of double-applying. When omitted, the server mints a fresh id
and the request is not retry-safe.

Inquiry create carries its idempotency key in the body instead
(`idempotency_key`, sections 3.6-3.7): the new row's `id` is
server-minted, so the create dedups on the body key rather than the
header. Batch create requires a per-item `idempotency_key` because one
request commits many items.

### 3.3 Field set body

```
{
  "value": <field_value>,
  "actor": "<actor>",
  "reason": "<text>",
  "expected": <expected_value>
}
```

```
actor   optional Agent label;
reason  optional; default is empty string
```

```
expected only valid for PUT owner, status, and judgement.
expected omitted means blind overwrite.
expected mismatch returns 409.
expected on any other field returns 400.
```

### 3.4 Field add/sub body

```
{
  "op": "add" | "sub",
  "value": <field_value>,
  "actor": "<actor>",
  "reason": "<text>"
}
```

```
actor   optional audit label; omitted means server uses principal email
reason  optional; default is empty string
```

### 3.5 Delete body

```
{
  "actor": "<actor>",
  "reason": "<text>"
}
```

```
actor   optional audit label; omitted means server uses principal email
reason  optional; default is empty string
```

### 3.6 Inquiry create body

```
{
  <SubmitKind JSON from wire/bodies.py>,
  "actor": "<actor>",
  "reason": "<text>"
}
```

```
actor   optional audit label; omitted means server uses principal email
reason  optional; default is empty string
```

### 3.7 Inquiry batch create body

```
{
  "items": [
    {
      "kind": "<kind>",
      "idempotency_key": "<uuid>",
      "body": <inquiry_create_body_without_idempotency_key>
    }
  ]
}
```

```
Batch route has no top-level Idempotency-Key.
Each batch item has idempotency_key.
The batch is all-or-nothing: every item commits in one transaction, or
  any failure rolls the whole batch back and no row is persisted.
Each committed item writes one change_log row.
Retrying the same item idempotency_key returns the original id.
Reusing one item idempotency_key for different content returns 409.
```

### 3.8 Edge create body

```
{
  "priority": <int|null>,
  "note": "<text>" | null,
  "valence": <float in [-1, 1]|null>,
  "labels": ["<label>"] | null,
  "actor": "<actor>",
  "reason": "<text>"
}
```

### 3.9 Edge batch create body

```
{
  "items": [
    {
      "from_id": "<uuid>",
      "to_id": "<uuid>",
      "edge_kind": "<edge_kind>",
      "priority": <int|null>,
      "note": "<text>" | null,
      "valence": <float in [-1, 1]|null>,
      "labels": ["<label>"] | null
    }
  ]
}
```

Edge creation is an upsert: a re-created edge applies any supplied
annotations to the existing edge (and a bare re-create is a no-op), so a
retried edge batch is naturally idempotent on the edge's identity and never
errors on a duplicate. The response's `created` flag distinguishes a
brand-new edge from an annotated existing one.

### 3.10 Inquiry lookup body

```
[
  "<uuid>",
  "<uuid>"
]
```

### 3.11 Inquiry lookup response

```
{
  "<uuid>": "<kind>"
}
```

### 3.12 Batch response

All-or-nothing: on success every item committed, with ids in input
order. Any item failure rolls the whole batch back and surfaces the same
HTTP error a single submit of that item would raise (e.g. 409 on a
conflict, 422 on invalid input).

```
{
  "ids": ["<uuid>"]
}
```

### 3.13 Success responses

```
create one      -> {"id": "<uuid>"}
read one        -> <Inquiry JSON from types>
read list       -> [<Inquiry JSON from types>]
mutate inquiry  -> {"id": "<uuid>", "change_id": "<uuid>"}
delete inquiry  -> {"id": "<uuid>", "change_id": "<uuid>"}
read edge       -> <Edge JSON from types>
create edge     -> {"change_id": "<uuid>" | null, "created": <bool>}
mutate edge     -> {"change_id": "<uuid>"}
delete edge     -> {"change_id": "<uuid>"}
read changes    -> [<Change JSON from types>]
action          -> {"ok": true}
```

### 3.14 Error response

```
{
  "error": "<code>",
  "detail": "<text>"
}
```

### 3.15 Filter query value

```
{"field":"title","op":"re","value":"parser"}
{"field":"status","op":"is","value":"active"}
{"field":"priority","op":"le","value":"10"}
{"field":"labels","op":"is","value":"sagent"}
```

### 3.16 Profile response

```
{
  "user_id": "<uuid>",
  "email": "<email>",
  "name": "<name>",
  "role": "viewer" | "writer" | "admin",
  "last_login": "<ts>" | null
}
```

### 3.17 Token create body

```
{
  "name": "<label>",
  "role": "viewer" | "writer" | "admin" | null
}
```

### 3.18 Token create response

```
{
  "id": "<uuid>",
  "name": "<label>",
  "prefix": "<prefix>",
  "role": "<role>",
  "secret": "trax_..."
}
```

### 3.19 Token list response

```
{
  "tokens": [
    {
      "id": "<uuid>",
      "name": "<label>",
      "prefix": "<prefix>",
      "role": "<role>",
      "created_at": "<ts>",
      "last_used_at": "<ts>" | null,
      "revoked_at": "<ts>" | null
    }
  ]
}
```

### 3.20 Admin users response

```
{
  "users": [
    {
      "id": "<uuid>",
      "email": "<email>",
      "name": "<name>",
      "role": "<role>",
      "status": "active" | "disabled",
      "created_at": "<ts>",
      "last_login": "<ts>" | null
    }
  ]
}
```

### 3.21 Admin allowlist response

```
{
  "entries": [
    {
      "email_or_pattern": "<email_or_pattern>",
      "role": "<role>",
      "added_by": "<uuid>" | null,
      "added_at": "<ts>"
    }
  ]
}
```

### 3.22 Web get response

```
{
  "self": <web_inquiry>,
  "edges": <web_edges>,
  "backlinks": <web_edges>,
  "changes": [<web_change>]
}
```

### 3.23 SSE event

```
event: change
data: {"id": "<change_uuid>"}
```

## 4. Other details

### 4.1 HTTP status codes

```
200  read / mutation / delete success with body
201  create success
302  auth redirect
400  invalid body, invalid field for kind, invalid projected edge mutation
401  missing or invalid auth
403  role too low
404  row not found
409  idempotency conflict, expected mismatch, immutable field, edge cycle, citation kind mismatch
422  well-formed body rejected by domain validation (e.g. self-loop edge, priority on a non-priority edge kind)
500  server fault
```

Every mutating route -- including `DELETE` (field unset, inquiry purge,
edge remove) -- returns `200` with a body carrying the `change_id` it
produced (section 3.13). The sole exception is the admin user-DELETE
(`DELETE /api/admin/users/<uuid>`), which returns `204` with no body.

### 4.2 Roles

```
viewer  GET /api/**, GET /api/web/**, GET /api/me/**
writer  viewer + inquiry/edge create/mutate/delete
admin   writer + /api/admin/**
```

### 4.3 Filters and pagination

```
filter                 repeated JSON query param
filter.field           SQL column name
filter.op              is|ne|re|nre|lt|le|gt|ge
filter.value           string
filter timing          before limit and offset
limit                  default 50; min 1; max 1000
offset                 default 0; min 0
seq_range              repeated `A..B` interval param; union; min 1
sort                   fixed created DESC, id DESC
```

### 4.4 Cost

```
marginal_cost_agent_usd     maps to marginal_cost.agent_usd
marginal_cost_resource_usd  maps to marginal_cost.resource_usd
PUT                          overwrite axis
PATCH add                    add to axis
PATCH sub                    subtract from axis; 422 if negative
DELETE                       set axis to 0
GET /cost                    row cost
GET /cost?deep=true          Issue subtree rollup
```

### 4.5 SSE

```
/api/change_log/stream  all changes
/api/web/subscribe      web-visible changes
event                   change
data                    {"id":"<change_uuid>"}
lookup                  GET /api/change_log/<uuid>
```

### 4.6 Naming

```
route path segments  lowercase
JSON keys            snake_case
URL kind tokens      kind.lower()  (codechange, webresult, websearch)
Python kind values   PascalCase
SQL column names     snake_case (kind-specific columns prefixed; see below)
server API aliases   none from trax CLI
```

#### The naming rule

One principle governs every surface: **keep the structure where the kind is
known; flatten with a kind prefix only where it is lost.** A field's name is
bare wherever its owning kind is already in scope, and carries the kind as a
prefix wherever it would otherwise be orphaned in a shared namespace.

Worked example, `Paper.source`:

| surface | kind in scope? | name |
|---|---|---|
| Python dataclass | yes (the class) | `paper.source` |
| trax CLI | yes (the verb) | `trax paper 7 source to ...` |
| HTTP body | yes (kind-scoped route) | `{"value": "10.1234/foo"}` (bare) |
| HTTP field route | kind in the path | `PUT /api/paper/<id>/source` |
| `inquiries` SQL column | no (all kinds share one table) | `paper_source` |
| `change_log` mirror | no (cross-kind) | `old_paper_source` / `new_paper_source` |
| `Change.Kind` value | no (cross-kind discriminator) | `paper_source` |
| `change_log` audit JSON | no (cross-kind feed) | `{"kind":"paper_source","new":{"paper_source":...}}` |

So a Paper-`source` edit is `PUT /api/paper/<id>/source` with body
`{"value": ...}`, and it lands in the audit log as `kind: "paper_source"`.

Base `Inquiry` fields (`status`, `owner`, `title`, `description`,
`labels`, `subscribers`, `marginal_cost`) apply to every kind, so they have
no owning kind and stay bare everywhere -- including their routes, which
remain under `/api/inquiries/<id>/<field>`. `issue_kind` already names its
owner and is left as-is. `marginal_cost_agent_usd` /
`marginal_cost_resource_usd` are the `Cost` parent flattened into a prefix --
the same rule applied to a composite value.

#### Why the Snapshot is flat, not nested

A `change_log` Snapshot (`old` / `new`) is *one changed field*, not a kind
object: every field defaults to None and only the touched one is set. A
nested `change.new.paper.source` would require a real `Paper`, which cannot
express "untouched" (its fields have real defaults). So the Snapshot stays
flat and prefixed (`change.new.paper_source`), 1:1 with the SQL mirror
column and the `Change.Kind` value.

#### Derivation, not declaration

The owning kind already lives in `ColumnSpec.applies_to_inquiry_kinds`. The
flat name is computed at the flatten boundary (`types/columns.py`
`storage_name`, `server/schema_gen.py`), never hand-declared:

- base field: bare;
- kind-specific (one owner): `f"{owner.lower()}_{field}"`;
- `flatten=` composite: `f"{flatten_prefix}{axis}"` (the `Cost` path).

There is no separate `change_kind` metadata: a field edit's `Change.Kind`
*is* its flat storage name. The Store's wire-facing setter stays bare
(`set_source`) and resolves the prefixed name from the subject kind;
everything inward (SQL, `COLUMN_SPECS`, `Change.Kind`) speaks the prefixed
name. `TestChangeKindAlignment` asserts each field's change kind equals the
derived flat name and is a member of the `Change.Kind` literal.
