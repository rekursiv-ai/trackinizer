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
GET  /api/inquiries/<uuid>/confidence
GET  /api/inquiries/<uuid>/authority
POST /api/inquiries/lookup
```

### 1.2 Inquiry list query params

```
GET /api/inquiries?kind=<kind>
GET /api/inquiries?kind=<kind>&kind=<kind>
GET /api/inquiries?kind=<kind>&fields=<key>&fields=<key>
GET /api/inquiries?kind=Issue&ancestors=narrows
GET /api/inquiries?kind=<kind>&filter=<json>
GET /api/inquiries?kind=<kind>&filter=<json>&filter=<json>
GET /api/inquiries?kind=<kind>&limit=N
GET /api/inquiries?kind=<kind>&offset=N
GET /api/inquiries?kind=<kind>&seq_range=A..B
GET /api/inquiries?kind=<kind>&seq_range=A..B&seq_range=C..
GET /api/inquiries?kind=<kind>&status=<status>
```

`ancestors=narrows` adds each Issue row's `narrows` ancestors as
`ancestors: [{id, kind, seq, title, status, child_ids}]`, nearest first, each
once; `child_ids` names which of the row and its listed ancestors narrow that
one. One recursive read per response, capped at depth 8 and 200 ancestors.

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

A purge deletes the row and its edges, auditing each. One rule holds for every row:
you delete what you made, admins delete anything, so a row is purged by its `account`
or an admin, and any other writer is 403. For the same reason only a row's account
or an admin moves it to another account (`PUT .../account`), anyone else is 403, so
no writer takes a row and then purges it. A claimed row first releases its owner
(`DELETE .../owner`), a locked row or a
row linked to one needs an admin (section 1.24), and a session needs its audience
(section 1.21). A science chat's session is purged by the key that opened it or an
admin, and is never unowned while it holds its routing name; `DELETE /api/chats/<uuid>`
deletes a whole chat (section 1.25).

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
GET /api/change_log?kind=<change_kind>&kind=<change_kind>
GET /api/change_log?kind=<change_kind>&brief=true
GET /api/change_log?limit=N
```

`kind` repeats: a change of any named kind is kept, before `limit`.
`brief=true` drops each snapshot's null keys and cuts its free text (title,
description, ...) to 32 characters; ids, statuses and other values stay whole.

### 1.16 Auth

```
POST /auth/logout
GET  /auth/login_page
GET  /auth/login_page?next=<path>
```

A request is authenticated one of three ways:

- `--no-auth` (or `TRACKINIZER_NO_AUTH=1`, which `--auth` overrides) is
  single-user local mode. Every request is one local admin and no credential
  is read, so the web app at `/app/` needs no sign-in. The server binds
  `127.0.0.1` unless `--host` says otherwise; anyone who can reach the port
  can edit everything.
- Otherwise an `Authorization: Bearer <token>` header carries an API token,
  minted with `POST /api/me/tokens` (or the bootstrap admin's). The CLI and
  agents sign in this way (`trax profile`).
- A browser carries the `trackinizer_session` cookie, signed with
  `TRACKINIZER_SESSION_SECRET`. The package mounts no route that issues one: a
  deployment that adds a sign-in provider mounts its routes itself, outside
  the OpenAPI schema, so the schema is the same with or without them.

`POST /auth/logout` clears the session cookie and answers 302 to `/`. A
request whose `Origin`, or else `Referer`, names another host gets 403.

`/auth/login_page` (with the web routes) is where a signed-out browser is sent.
It offers a sign-in button only when `GET /auth/login/ready` answers 2xx;
without a provider that route does not exist, and the page says sign-in is not
configured. The browser keeps a link's hash across the redirect to the page,
and the page puts it back on `next`, so signing in returns to the same view.

### 1.17 Me

```
GET  /api/me/profile
PUT  /api/me/visual-workspace
PUT  /api/me/acknowledge
GET  /api/me/tokens
POST /api/me/tokens
POST /api/me/tokens/<uuid>/revoke
PUT  /api/me/tokens/<uuid>/role
```

`GET /api/me/profile` also reports the audiences the credential holds:
`read_audiences` (whose session transcripts it may read), `open_audiences` (what
it may open a session into) and `pinned_audience` (the one a key is pinned to, or
null). They are `open` for everyone; `staff` is added for a person the server
names as staff. The server's `--staff PATTERNS` (or `$TRACKINIZER_STAFF`) is a
comma-separated list of `user@host` and `*@domain` patterns. A literal address
matches the verified email alone. A domain pattern also needs the person's Google
Workspace domain (Google's `hd` claim at their last login) to be that domain, so a
consumer account with an address on the domain is not staff. Empty, the default,
means nobody is staff. It is not stored per user and no route sets it.

A domain pattern takes effect at each person's next browser login: the claim is
stored only by the Google callback, so until then a person's `users` row has no
Workspace domain and neither they nor their keys are staff (fail closed). Before
setting a `*@domain` pattern on a deployment, sign in once with a Workspace account
and once with a consumer account on the same domain, and check that only the first
gets `staff` in `GET /api/me/profile`.

The callback also binds the `users` row to the Google account (`sub`) that first
signed in as that email. A login with another `sub` on the same address gets 403
and changes nothing, so a second Google account holding the address cannot take over
the row's sessions and keys or rewrite its Workspace domain. A row from before the
binding takes the next login's `sub`. To rebind an address to a different Google
account, an administrator deletes the user (`DELETE /api/admin/users/<uuid>`); the
next login creates the row again.

`POST /api/me/tokens` takes an optional `open_into` beside `name` and `role`: it pins
the one audience the key opens sessions into. It is at most what the caller opens
into (403 otherwise), a name that is no audience is 422, and a key minting a key hands
its own pin on when the request names none; an unpinned key mints an unpinned key. The
pin is recomputed against the owner at every request, so a key never holds more than
its owner does. A key reads what its owner reads. The response and
`GET /api/me/tokens` carry `open_into`, null for no pin.

A pinned key can revoke (`POST .../revoke`) or re-tier (`PUT .../role`) only its own
key and keys pinned to an audience it opens into. Any other key answers 404, as an
absent one does. A browser session or an unpinned key manages every key of its owner.

A service account's key (section 1.18) answers 403 on `POST /api/me/tokens`,
`PUT .../role` and `POST .../revoke`, before the body is read; an admin manages
its keys.

### 1.18 Admin users

```
PUT    /api/admin/inquiries/<uuid>/lock
GET    /api/admin/users
PUT    /api/admin/users/<uuid>/role
POST   /api/admin/users/<uuid>/disable
POST   /api/admin/users/<uuid>/enable
DELETE /api/admin/users/<uuid>
POST   /api/admin/service-accounts
POST   /api/admin/service-accounts/<uuid>/tokens
GET    /api/admin/service-accounts/<uuid>/tokens
POST   /api/admin/service-accounts/<uuid>/tokens/<uuid>/revoke
```

`GET /api/admin/users` marks each user with `staff`, true when the server's staff
setting makes them staff. It is derived and read-only.

#### Service accounts

A service account is a user whose address is `NAME@service.invalid`; no column
marks it. An admin creates one for a program, without a sign-in:
a chat assistant, a job runner, the shell an agent runs in. It has its own
identity and its own keys, and nobody has to create a fake person for it. All four
routes are admin role. Bodies: section 3.27.

`NAME` matches `^[a-z][a-z0-9-]{1,30}$`. `.invalid` is reserved by RFC 2606, so no identity
provider issues it. `POST /api/admin/service-accounts` takes a `name` and a `role`,
`viewer` or `writer`, and answers 201 with the user, as `GET /api/admin/users`
lists one. A name is taken while any row holds it, a disabled one included, and
answers 409: creating a name again never revives a disabled account. Deleting the
account (`DELETE /api/admin/users/<uuid>`) frees the name and removes its keys, so a
new account of the same name starts with none and no old key opens it.

`POST /api/admin/service-accounts/<uuid>/tokens` mints one API key for the account
and answers 201 with the secret, once, marked `Cache-Control: no-store`. The body
is a `name` and, optionally, `role` and `open_into`, which mean what they mean in
section 1.17. The key's role is at most the account's and
defaults to it; a stronger role answers 403. Its pin is at most what the
account holds under the server's staff setting: `open`, and `staff` only for an
exact address entry such as `campaign@service.invalid`. A `*@domain` pattern never
matches a service account, which has no Workspace domain. A pin beyond that
answers 403, and a name that is no audience answers 422. The key is also held to what
the minting admin's own credential reads and opens into, because a key never
grants more than it holds: an admin who does not hold `staff` cannot hand it on
(403), and a pinned key hands its pin to the keys it mints.
Creating an account whose address the staff setting names is held to the same
rule. An id that is not a service account, a person's included, answers 404, and a
disabled account answers 409.

`GET /api/admin/service-accounts/<uuid>/tokens` lists the account's keys, live
and revoked, newest first, with no secret or hash.
`POST /api/admin/service-accounts/<uuid>/tokens/<uuid>/revoke` revokes one and
answers 404 for a key that is not the account's or is already revoked. Disabling
the account revokes all its keys at once and enabling it restores none; mint
fresh ones.

A service account can never:

- sign in. The allowlist refuses an entry at the service domain (422), whether an
  address or `*@service.invalid`, and no sign-in admits such an address. No
  session cookie opens one.
- be an admin. Creating one as `admin` and `PUT /api/admin/users/<uuid>/role` to
  `admin` answer 422, and a key's role is at most its account's, so no key of
  the account ever holds it.
- hold a Google account or a Workspace domain: no sign-in writes either to its
  row.
- manage keys. Its own key answers 403 on `POST /api/me/tokens`,
  `PUT /api/me/tokens/<uuid>/role` and `POST /api/me/tokens/<uuid>/revoke`, its
  own key included, so a leaked key cannot make more. `GET /api/me/profile` and
  `GET /api/me/tokens` still answer.

Creating an account and minting or revoking a key are logged at WARNING with the admin
who acted (user id, address and key id) and the account acted on (user id and
address), and a key's id and label, never a secret. Admin actions on users keep no
other record.

### 1.19 Admin allowlist

```
GET    /api/admin/allowlist
POST   /api/admin/allowlist
PUT    /api/admin/allowlist/<email_or_pattern>/role
DELETE /api/admin/allowlist/<email_or_pattern>
```

### 1.20 Web app

```
GET /api/web/search
GET /api/web/search?q=<query>
GET /api/web/search?q=<query>&kind=<kind>
GET /api/web/search?q=<query>&kind=<kind>&limit=N
GET /api/web/search?q=<query>&kind=<kind>&fields=<key>
GET /api/web/recent_changes
GET /api/web/recent_changes?limit=N
GET /api/web/lookup/<uuid>
GET /api/web/get/<uuid>
GET /api/web/graph
GET /api/web/graph?limit=N
GET /api/web/graph?focus=<uuid>&hops=N&limit=N
GET /api/web/subscribe
GET /api/web/feed
GET /api/web/feed?after_created=<iso>&after_session=<uuid>&after_seq=N
GET /api/web/feed?since=<iso>&until=<iso>&room=<room>&actor=<actor>&limit=N&tail=<bool>
GET /api/web/feed?actor=<actor>&actor=<actor>&room=<room>&cli=<cli>&kind=<kind>
GET /api/web/feed?conversation=true&tail=true&limit=N
GET /api/web/feed?station=<workspace uuid>
GET /api/web/feed/facets?since=<iso>&until=<iso>
GET /api/web/feed/facets?since=<iso>&until=<iso>&actor=<actor>&kind=<kind>
GET /api/web/feed/histogram?since=<iso>&until=<iso>&buckets=N
GET /api/web/feed/histogram?buckets=N&room=<room>&kind=<kind>
GET /app/
GET /app/<file>
```

`/api/web/search` splits `q` on whitespace and returns rows matching every
term. A bare term is a case-insensitive substring of `title` or
`description`; `title:RE` and `description:RE` are case-insensitive regexes.
Only `"` groups words into one term. `'` and `\` are ordinary characters, so
`don't` and `title:\d+` mean what they say.

`/api/web/graph` returns at most `limit` inquiries (default 1000) and the
edges between them. The newest come first, each followed by the older
inquiries it links to, until `limit` is reached. A `limit` below 1 answers
400. There is no upper bound, so a `limit` at or above the number of
inquiries answers the whole graph: for a benchmark of about 100,000 inquiries
and 180,000 edges that was 42 MB (10 MB gzipped) in 0.7 s, against 8.6 MB in
0.16 s for 20,000 and 2.1 MB in 0.06 s for 5,000.

With `focus`, it returns that inquiry's neighbourhood instead, in the same
shape: the focus, then every inquiry one edge away in either direction, then
two away, up to `hops` (1 to 3, default 2). Within a hop the newest come
first, and selection stops at `limit` (default 60), the focus included. The
edges are those between returned nodes, and each node adds `hops`, its
distance from the focus (0 for the focus). An unknown `focus` answers 404, and
`hops` outside 1 to 3 answers 400. A hop reads every edge of the hop before it
and looks up each neighbour, so its cost grows with those nodes' edges, not
with the graph: on the same copy, 300 random foci took 1.3 ms at the median
and 10 ms at worst, and its most-linked Issue 3 ms. A focus whose 30
neighbours had 1,000 edges each took 35 ms.

`/api/web/feed` interleaves every agent session's captured records into one
stream, oldest first, ordered by `(created, session_id, part, seq)`. A poll
resumes past `next_after`, a cursor of all four; `tail=true` reads the newest
page; `since` and `until` bound `created`, both inclusive. `limit` is 1 to
1000 (default 200).

The feed and its two counts below take the same filters, each repeatable:
`actor` (a session's routing name), `room` (a room it joined), `cli` (the CLI
it wraps) and `kind` (the record's kind, such as `AssistantMessage` or
`ToolCall`). A record passes a filter when it matches any of the filter's
values, and must pass every filter given, so
`actor=a&actor=b&kind=ToolCall` is the tool calls of `a` and `b`.

`station=<workspace uuid>` (also on the facets and the histogram) keeps the agents of
one of the caller's stations: the sessions reachable by `produced_by` edges
from the session whose `cli_session_id` is `chat:<the station's conversation>`
(a campaign launched from the chat makes its lead's session `produced_by` the
chat's), to depth 4 and at most 500 sessions, never the chat's own. It is one
query. A station that is not the caller's, or has no chat, keeps nothing.

The feed alone also takes `conversation=true`, which keeps only the records
the facets count as `conversation` (below), so a page of `limit` records holds
that many messages however much else the sessions wrote. On a benchmark of 9
million records the newest 300 took 23 ms, alone or beside `actor` or the four
conversation kinds; beside one message kind alone, which the page must then
dig further for, 0.8 s.

`/api/web/feed/facets` counts the records the feed returns for a window and
the same filters, by session, room and kind. Either end of the window may be
open, and `until` before `since` answers 400. It reads every record in the
window, so a bounded one answers fastest: on a benchmark of 9 million
records, an hour took 4 ms, a day 31 ms, a week 0.5 s, and the whole history
8 s and 6.6 MB.

```json
{
  "actors": [
    {"actor": "worker", "session_id": "…", "cli": "codex",
     "rooms": ["lab", "ops"], "count": 2, "conversation": 1,
     "last": "2026-09-01T00:11:00Z", "ended": "2026-09-01T03:00:00Z"}
  ],
  "rooms": [{"room": "lab", "count": 2, "actors": ["worker"]}],
  "kinds": [{"kind": "AssistantMessage", "count": 1},
            {"kind": "ToolCall", "count": 1}]
}
```

`actors` holds one entry per session, newest `last` (its newest record's
`created`) first; `ended` is `null` while the session is live. `conversation`
counts what a person or agent said: every `AssistantMessage` and
`AgentToAgentMessage`, a `UserMessage` unless Claude marked it `isMeta` (its
harness wrote it), and a `ContextState` of kind `queued_command` whose origin is
human (a message sent while the agent worked). An `AgentToAgentMessage` that is
only the envelope codex writes before a message to another of its agents
(`Message Type: …` to `Payload:`), its payload sealed, says nothing, nor does a
`UserMessage` that is wholly codex's context (`<codex_internal_context>`) or a
background task's notice (`<task-notification>`). The facets count no record
with nothing to read: neither that sealed message, with no attachment, nor a
`Thinking` with no `content` and no `summary`. `rooms` and `kinds` are largest
`count` first.

`/api/web/feed/histogram` counts the feed's records per time bucket over a
span of at most the last 7 days. An unset `since` starts at the first record
of those 7 days, and an earlier one starts 7 days ago; an unset `until` ends
now. `buckets` is 2 to 1000 (default 120); `until` before `since`, or more
than 7 days ago, answers 400. Bucket widths are round -- 1, 2, 5, 10, 15 and
30 seconds, the same in minutes, 1, 2, 3, 6 and 12 hours, then whole days --
and the answer uses the finest that holds the span in at most `buckets`.
Buckets start on multiples of their width from the Unix epoch, and run from
the one holding `since` through the one holding `until`, so a span already
aligned to a width, `buckets` long, gets that width. Every bucket is listed,
empty ones as 0. A first bucket that starts more than 7 days ago counts only
its records from then on.

```json
{
  "start": "2026-09-01T00:00:00Z",
  "end": "2026-09-04T00:00:00Z",
  "bucket_seconds": 86400,
  "counts": [{"start": "2026-09-01T00:00:00Z", "count": 3},
             {"start": "2026-09-02T00:00:00Z", "count": 1},
             {"start": "2026-09-03T00:00:00Z", "count": 1}]
}
```

Each read counts the span's records, which is why it reaches back only 7
days. On a benchmark of 9 million records, a span of 480,000 records answered
in 55 ms or less at the median and 70 ms at worst, with any filter, at 120 or
300 buckets; spans of 1.9 million records took 160 ms or less at the median
and 210 ms at worst.
Unlike the feed and its facets, the
histogram counts every stored record: when a compaction rewrites a session
file shorter, the feed reads only the part's new length, and the histogram
still counts the records written before it, at the times they were written.

`/app/` exists when the server has a built web app: the one in
`--app-dir DIR`, else the build packaged with trackinizer. It serves the app,
with `DIR/index.html` at `/app/`, to every caller the API answers: any
signed-in role, and anyone under `--no-auth`. It refuses every other caller,
as the API does, whether or not the server has session login: `/app/` and
`/app/index.html` answer 302 to `/auth/login_page?next=<path>`, and every
other file answers 401. `DIR` is resolved on
every request: it may be missing (404) or be a symlink swapped to a new
build without a restart. A path that leaves `DIR`, through `..` or a
symlink, answers 404. Every `/app/` response, errors included, is
`private`, so shared caches never store it. A file found under
`/app/assets/` (200, or 304 on revalidation) carries `Cache-Control:
private, max-age=31536000, immutable`: its name holds its content's hash, so
browsers keep it without asking again. Every other response carries
`Cache-Control: private, no-cache`, so browsers revalidate the entry page
after a new build lands.

With an app, the old UI's paths lead into it: `/` answers 302 to
`/app/`, and `/me`, `/admin`, `/graph` and `/console` to `/app/#/settings`,
`/app/#/admin`, `/app/#/graph` and `/app/#/console`, each with
`Cache-Control: private, no-cache`. A browser keeps a link's hash across a
redirect whose `Location` has none, and the app reads the old UI's hashes.
Without one, nothing is served at those paths.

`/auth/login_page` is the sign-in page, served to anyone.

### 1.21 Agent-session ingest

The capture-and-messaging surface for `trax run <cli>`. A run opens a
session, streams turn-grained events, and closes it; messages route back
into a live session by routing name.

```
POST   /api/sessions/start
POST   /api/sessions/<uuid>/records
GET    /api/sessions/<uuid>/records?part=N&after_idx=N&limit=N&plaintext_only=B
GET    /api/sessions/<uuid>/parts
POST   /api/sessions/<uuid>/end
POST   /api/sessions/<uuid>/inbound
GET    /api/sessions/<uuid>/inbound
POST   /api/messages
```

Each drained message carries the sender the server attested as `source` and,
beside it, `source_role`, that sender's effective role (`viewer`, `writer` or
`admin`) as the server saw it when the message was sent. Neither is taken from
the request, and a message the server generates has no `source_role`.

Inbound messages whose attested `source` is `trackinizer` are subscriber
push envelopes -- JSON metadata for a committed change, generated
server-side (no HTTP surface produces them). Shape and client-side
handling: `design_subscriber.md`.

An AgentSession carries an `audience` (`open` or `staff`) saying who may read its
transcript. A session opens into the audience its key is pinned to (`open_into`, see
section 1.17), else `open`. Only the transcript is gated: the session's title, actor,
rooms, times, cost, edges and `audience`, and every row it files, stay readable by
anyone who can read the graph. A caller reads a session when its `read_audiences` (see
`GET /api/me/profile`) hold the session's audience, and the key that opened a
session always does, so a key whose owner no longer holds `staff` can still run the
`staff` session it opened. Otherwise the session answers 403 with
`{"detail": {"code": "session_audience", "audience": "<value>"}}` on
`GET .../records`, `.../parts`, `.../turns`, `POST .../records`, `POST .../end`,
`GET .../inbound` (a drain), `DELETE /api/inquiries/<uuid>` and every field edit of
the row. Sending input (`POST .../inbound`, `POST /api/messages`) needs the audience
too. The configured assistant (`--assistant`) relays a user's line to a session it
cannot read only for an `asker`, an optional field of both bodies naming the user's
account email. It passes on that user's rights, not its own, and the server reads them
from its own tables: the asker must be an active user and a writer or admin, and be the
session's account or hold its audience as a person, whatever key they use. A send that
names no asker, or one who does not qualify, is 403 `session_audience`. A session the
assistant reads itself needs no asker, and another sender's `asker` is ignored.
`POST /api/messages` is refused whole when any session its actor names is hidden from
the sender: nothing is queued to the others. No field route sets `audience`.

The feed, its facets and histogram, session search, record filters on
`GET /api/inquiries`, and the export leave out a session the caller does not read,
and the export's header names the audiences it was read for (section 3.24). A
record written to a session pings the live streams
(`GET /api/change_log/stream`, `GET /api/web/subscribe`, the `changed` frames of
`GET /api/workspaces/<uuid>/events`) with the session's id, and a stream relays
that ping only to a caller whose `read_audiences` hold the session's audience.
Every other change reaches every viewer.

A subscriber push into a session that is not `open` carries no `actor` and no
`reason`, and its `agent_message` omits "(by ...)": those are free text a writer sets,
and an inbound line becomes the session's next user turn. The row is still named by
`subject_ref` and `row`, so the session reads the change from the graph.

Existing sessions migrate to `open`; the migration cannot tell a campaign lead or other
staff session a running deployment already captured from any other. The audience is
written once, when the session opens, and no route or key changes it afterwards.

A session whose run dies without calling `end` (killed, host crashed) is
closed by the server: once a session that polls its inbound queue has gone
15 minutes without a poll or a records upload, it is ended at the time it
was last seen, audited as `trackinizer`. Its next poll or upload reopens it,
so a run that was only cut off carries on as the same session. A session
that never polls is never closed this way. Liveness is kept in the database,
so a server restart neither hides a dead run nor strands a closed one.

### 1.22 Service meta

```
GET /api/version
GET /api/meta/edges
```

`GET /api/version` is unauthenticated and store-free, returning
`{"sha": "<hex>"}` (the running build, from `$TRACKINIZER_SHA` or
`git HEAD`, else `"unknown"`). A 404 means the live binary predates the
endpoint -- itself a staleness signal.

`GET /api/meta/edges`, also unauthenticated, maps each edge kind to
`{from_kinds, to_kinds, forward, inverse, annotations}`: the inquiry kinds
each stored end admits, the relation read from the child (`narrows`) and
from the parent (`narrowed_by`), and the annotations an edge of that kind
takes, in the order `priority`, `note`, `valence`, `labels`. Setting any
other annotation on it answers 422.

### 1.23 Export

```
GET /api/export
```

The whole graph as JSON lines (`application/x-ndjson`), for backup or a
public mirror: every inquiry, edge, and change-log row, plus experiment
metrics and agent-session records, read in one snapshot. Viewer role, like
any read. A session's records are exported only to a caller that reads its
audience (section 1.21), so a backup that must hold every transcript runs as a
caller that reads every audience. Read-only; nothing imports it yet. Line shape:
section 3.24.

### 1.24 Visual catalog and workspace

```
GET  /api/visuals
POST /api/workspaces
GET  /api/workspaces
POST /api/workspaces/new
POST /api/workspaces/for-chat/<conversation uuid>
GET  /api/workspaces/<uuid>
PATCH /api/workspaces/<uuid>
PUT  /api/workspaces/<uuid>/page
POST /api/workspaces/<uuid>/operations
POST /api/workspaces/<uuid>/relay
```

`GET /api/visuals` returns safe descriptors and the default visual type. It
does not fetch graph data or start a session. Each descriptor has a stable
`type`, `version`, title, description, requirements, default size, bounded
parameter schema, and `record_kinds`: the kinds of record a show may name
(`null` for any, an empty list for none). `trax.browse` is the page itself: it
takes no record and no parameter, and it cannot be hidden. `trax.timeline`
shows an Issue or an Experiment, `trax.artifact` an Artifact, `trax.subgraph`
any record.

`GET /api/visuals/timeline/<record-uuid>` is the one bounded read behind the
`trax.timeline` visual (Lineage and timeline). The record may be of any kind;
an Experiment is shown on the Issue that produced it, and any other kind
stays the record and takes the nearest `produced_by` Issue as its anchor
(none: the record is returned alone). The response carries `target`, the
anchor `issue`, `leads` (the anchor's `narrows` ancestors, at most three,
farthest first; for a non-Issue record the anchor is itself the nearest lead),
the record's `root_results` with signed evidence, and up to `direction_limit`
directions with `results_per_direction` results each. The two limits come from
the catalog descriptor; an out-of-range value returns 422, a missing record
404.

`PUT /api/me/visual-workspace` sets the signed-in user's canvas opt-in from
an interactive browser session. API keys cannot change that choice.
`GET /api/me/profile` includes `visual_workspace_enabled`; it defaults to true. It
also names `api_key_id`, the key the request used, or null for a browser session.

`PUT /api/me/acknowledge` with `{"rules_version": "<version shown>"}` records that
the signed-in user agreed to the alpha rules, from an interactive browser session;
API keys are refused with 403. The rules are Issue#1 and their version is when its
title or description last changed (its creation until then), so editing the words
makes every recorded agreement stale and a cascade or cost roll-up under it does not.
A version that is no longer the current one answers 409: the user read other rules.
`GET /api/me/profile` returns
`acknowledged_at` and `acknowledged_rules_version` (null before the user agrees), the
current `rules_version` and `rules_issue_id`, both null unless Issue#1 exists and is
locked: rules are in force only then, so a deployment turns the welcome flow on by
locking its rules Issue. With no rules in force the acknowledge call answers 409.

`PUT /api/admin/inquiries/<uuid>/lock` with `{"locked": true | false}` is admin
only. While an inquiry is locked, only an admin may set, patch or clear its
fields, add or remove its edges (an edge write is refused when either end is
locked), name it in a create (a parent, a prerequisite, a citation, a batch edge),
or purge it or a row linked to it, or publish an Artifact for it or as a revision of
a locked Artifact (`POST /api/artifacts/content`); any other writer gets 403 naming
the row. The lock covers fields, edges and delete only: a session's lifecycle
(`/api/sessions/<id>/end`), its metrics and its records are not edits of the row and
stay open to the session's owner. `POST /api/inquiries/next_issue` skips a locked
Issue. Locking records no change and leaves `modified` alone; `inquiry_lock_log`
holds who set or cleared each lock and when. Migration `schema.038.sql` locks nothing;
an admin locks Issue#1 so that no writer rewrites the rules, and unlocks it to
edit them.
`GET /api/web/get/<uuid>` carries `locked` beside
`self`.

**Stations.** A canvas is a station: a user has many, each with a `name`
(1 to 120 characters, unique among the user's; `unnamed-workstream#N`, the lowest
free N of 1 or more, until renamed), at most one science chat
(`conversation_id`), and the `#/...` `page` it was last on. The database keeps
these three in one optional JSON column, `station`, beside the canvas's own. The
routes here other than `GET /api/workspaces/<uuid>` are the signed-in browser's
own; an API key is 403.
`POST /api/workspaces` returns the station the user changed last (an operation, a
rename, a page or a chat bound), and makes the first, once however many requests
race, when there is none. It starts
with `trax.browse` in the main pane. Chat is not a visual: the browser draws it as a
column beside the canvas, and `show trax.chat` is refused as an unknown type. A
stored canvas that still holds a `trax.chat` visual reads without it. The response
has `id`, `revision`, `name`, `conversation_id`, `page`, `visuals`,
`focused_instance`, `assistant`, and `partner`.
`GET /api/workspaces` lists the stations, the one changed last first, as
`{id, name, conversation_id, page, created_at}`.
`POST /api/workspaces/new` makes a station.
`PATCH /api/workspaces/<uuid>` with `{"name"}` renames it: the name is trimmed
and must then be 1 to 120 characters (else 422); a name the user already has is
409.
`PUT /api/workspaces/<uuid>/page` with `{"route"}`, a `#/...` address valid as a
navigation's, remembers the page; it is 204, changes no revision and publishes
no frame, and the browser sends it at most every 500 ms.
`POST /api/workspaces/for-chat/<conversation uuid>` returns the user's station that
holds that chat, or makes one bound to it. A station that is not the user's is 404;
a deleted chat is 410 `chat_deleted`.
`GET /api/workspaces/<uuid>` returns that state to its owner, and to an
assistant's key (below).

`POST /api/workspaces/<uuid>/relay` with `{"targets": [{"actor", "room"}], "text"}`
and a required `Idempotency-Key` tells the station's chat what its owner sent
agents directly from the Console, after the per-agent `POST /api/messages`
sends; it is the browser's own and a station that is not the owner's is 404. One
request is one Console line, however many targets. The partner it queues for is
the one a Chat line of the station would go to: the chat's own session when the
assistant has it open, else the assistant's service session. The message carries
`context.relay` (`targets` and `text`) beside the usual `workspace_id` and
`conversation_id`, and its `text` is the relay's text. The assistant records it in
the chat as a line of the poster's that starts with `→ ` (an arrow and a
space), then `actor` or `actor:room` for each target joined by `, `, then `: `
and the text; it runs no turn, so there is no answer and no cost, and it reads
the line with its next prompt. A `trax helper` reads it as a note. The response is
`{"delivered": bool}`: false when the station has no chat, its chat was deleted,
its owner may not post into its chat (a station may be bound to a chat to read it,
section 1.25 says who posts), a Chat line of the station would be refused there
(the 409s of a local helper in the assistant's chat and of the assistant in a
helper's), its partner is not running or its queue is full, which is never an
error.
A retry under the same key queues nothing again.

**The assistant and the partner.** The server's `--assistant ACTOR=EMAIL`, or
`$TRACKINIZER_ASSISTANT`, names one assistant. A live AgentSession is the
assistant's when its granted actor is `ACTOR` (or `ACTOR#N`, the suffix the
server adds once a name has been used) and the API key that opened it belongs to
the account `EMAIL`, compared lowercase. Every canvas talks to the assistant's
newest live session. `assistant` in the state is its actor, or null when none is
configured. `partner` is computed on every read and never stored: `session_id`,
`actor`, the configured name, `cli`, `status`, `live` or `unavailable`, and
`kind`, `shared` or `local`.

**The local choice.** A canvas starts `shared`. Its owner's browser can send the
operation `{"kind": "partner", "choice": "local"}` (or `"shared"`), which is
stored as `partner_choice` in the canvas state and bumps the revision as any
operation does; an API key sending it is 422. With `local`, the partner is the
owner's newest live `trax helper` session (CLI `trax-helper`) that an unrevoked
key of the owner's own account opened and that is polling its inbound queue, and
`actor` is that session's granted actor. Another user's helper never qualifies,
and the shared assistant never stands in. With none running the partner is `kind`
`local`, `unavailable`, with a null `session_id`, `actor` and `cli`, and a Chat
send is 409. The owner needs no `--assistant`: a server with none can still serve
`local` canvases.

Any session can be the assistant. `trax helper claude` (or `codex`) `--as
ACTOR`, run with a key of the account `EMAIL`, opens one and answers each science
chat line by a turn of that CLI, resuming the chat's own CLI conversation. Its
service session takes messages only from Chat: a direct or routed send to it is
403.

The partner's key may read and operate a station while that station's partner
is the session the key opened, and the live science chat the station holds
(`conversation_id`) is one that key opened with the station's owner as its
account or a poster (section 1.25). The check is per station: a station without
a chat takes no key. Any other station is 404, so a user who never talked to
the partner in that station gives it nothing; the owner's own key is 403 unless the partner is
the owner's local helper, which that key opened. Only the key that opened a
partner or science-chat session may drain its inbound queue, whatever the role.

An operation body has `revision` and one `operation`: `show`, `hide`, `focus`,
`place`, `partner`, `navigate`, `highlight`, or `chat`. The caller supplies a UUID `Idempotency-Key`
header. The
server locks the workspace, checks the revision, validates the visual type,
parameters and record kind, and returns the new state. Retrying a recent body
with the same key returns the original state and publishes nothing. The latest 64
receipts are retained; an older retry receives a stale-revision 409 after its
receipt expires. Reusing a retained key for another body returns 409. Both that
conflict and a stale revision include `current` with the live workspace. An API
key can operate only as the partner above; that check also runs before
replaying an idempotency receipt.

`navigate` is `{"kind": "navigate", "route": "#/..."}` with a route of at most
512 characters and no space or control character. Only an agent key may send it.
It changes no visual and no revision: the server pushes a `navigate` frame, and
the browser goes there when it accepts the route. A navigation made while no tab
listens is not replayed.

`highlight` is `{"kind": "highlight", "ids": [uuid, ...]}` with at most 50 ids;
an empty list clears. It is an event as `navigate` is: only an agent key may
send it, it changes no visual and no revision, and the server pushes a
`highlight` frame, which the browser marks the inquiries from. It is never
stored and not replayed.

`chat` is `{"kind": "chat", "hidden": true | false}`: it hides or shows the browser
tab's Chat window. An event, as `navigate` is: only an agent key may send it (a
browser is refused with 422), it changes no visual and no revision, and the server
pushes a `chat` frame. It is the tab's own state: never stored, not replayed, and a
reload shows Chat.

Viewer access is enough to change one's own canvas. Workspace operations do
not edit trax records or add entries to `change_log`.

### 1.25 Science chat and canvas events

```
POST   /api/chats
GET    /api/chats/<uuid>
DELETE /api/chats/<uuid>
PUT    /api/chats/<uuid>/shares/<email>
DELETE /api/chats/<uuid>/shares/<email>
GET    /api/workspaces/<uuid>/events
```

A canvas Chat conversation is one AgentSession the assistant opens: label
`science-chat`, `cli_session_id` `chat:<conversation id>`, actor
`chat-<12 hex>`, account the person who started it. Its records are the
conversation (a person's line is an agent message from its poster, then the
assistant's tool calls, results and answers), read with the ordinary session
routes. There is no separate chat store: chats are public to every user to read.
Its starter posts into a chat, as do the people they share it with and whoever
`TRACKINIZER_CHAT_ORGS` lets in (Forking below); anyone else forks it. Its starter
deletes it while every person's line in it is theirs (Deleting below).

`POST /api/chats` (browser only, writer role, with an `Idempotency-Key`) posts a
line. The body is `{kind: "science", workspace_id, text, conversation_id, page,
trail}`; `text` holds a non-space
character and at most 16,384 characters, and `workspace_id` is the poster's own
station. A station holds one chat: with none, the line's conversation becomes its
chat in the transaction that queues the line; with one, a line that names another
conversation is 409, as is one for a conversation another station of the poster
holds, and a line that names none continues the station's chat. `page` is the `#/...` address the sender is on and `trail` the addresses
they came through before it, oldest first, at most 8; a malformed address or a
longer trail is 422. The server resolves them to records in the context. A
`conversation_id` continues a chat, for a writer who may post into it (see Forking
below). None starts one, and the key
names it, so a retry names the same conversation. `fork: {session_id, part, idx}`
instead starts a new conversation from that line of another chat, in a new station
copied from the one the line was sent from (the receipt's `workspace_id`); it and
`conversation_id` together are 422. The line's poster is the attested account,
never the body. The server builds the typed canvas context (the canvas, its
visible visuals, the page and the trail; it pins no record) and queues the line
for the conversation's own session when the canvas's partner has it open
and some poller drains it, and for the partner's service session otherwise (a
`trax helper` does not poll its chats, so it hears every line there). The
receipt is `{conversation_id, workspace_id, session_id}` at once;
`workspace_id` is the station the line went to; `session_id` is null until
the assistant has the session open. The same key again returns the original receipt and queues nothing; a key
reused for another line is 409. The de-duplication is in memory, so a retry after
a server restart queues the line again. No live assistant is 409; a full queue is
409 `partner busy`, and nothing is queued.

`GET /api/chats/<uuid>` returns `{conversation_id, session_id, title, account, live,
forks, forked_from, forks_on_typing, shared_with, may_share, may_delete}` and is 404
until the assistant has opened the session, and 410 `chat_deleted` once it was
deleted (Deleting below); any signed-in user may
read it, which is how a chat opens by link. It is browser only. A chat that
the caller's own `trax helper` opened (the local choice) is found for its starter
only. Its `session_id` is the assistant's session under the id whenever one exists,
and `account` is the chat's starter (Forking below). `shared_with` lists the emails
the chat is shared with, oldest share first,
and is empty for anyone but its starter and an admin; `may_share` and `may_delete`
say whether the caller may share it and delete it (Sharing and Deleting below).

Forking. A fork is a new science chat that starts from a line of another: its
`account` is the forker, it has its own conversation id (the key), and the
assistant that serves it (or `trax helper`) opens its session with
copies of the original's lines up to and including that line, read from the
original's session, and adds a `produced_by` edge from the fork's session to the
original's, labelled `chat-fork` and `fork-at:<part>:<idx>`. The original is not
written, and a fork point is dropped for a conversation that already has lines. Spend,
campaigns and the new station belong to the forker. `forks` on the original's head counts
the conversations with such an edge to it, counting only an edge that the key that
opened the fork's session added with the fork label; `forked_from` on a fork's
head is the original's conversation (the earliest such edge). A helper's forks
count for the forker alone, since the head counts what the viewer may find. A
person always joins their own chat, and so does each person it is shared with.
Who else may join a chat or must fork it is the server's `TRACKINIZER_CHAT_ORGS`:
`domain` (the default, also when unset) makes each verified email domain one
organisation, except the consumer domains (`gmail.com`, `googlemail.com`, `outlook.com`, `hotmail.com`,
`live.com`, `yahoo.com`, `icloud.com`, `me.com`, `proton.me`, `protonmail.com`),
which are none, and the starter's organisation joins; `single` makes every user
one organisation, and only a server whose users are all one organisation sets it;
`individual` lets nobody else join. Anyone else's post is 403
`{"detail": {"code": "chat_not_shared", "message": ...}}` and queues
nothing, whether the conversation is named by `conversation_id` or by the
`Idempotency-Key` of a post that names none. The chat's starter is the account of
its oldest session that the assistant opened or that its own account's key opened
(as a person's `trax helper` does), or, before any session carries the id, the
sender of the post that began it. That one rule names the starter for a post, the
head's `account`, sharing and deleting: any writer's key may open a session under a
`chat:` id, and the assistant opens one for a poster a helper's chat is shared
with, and neither makes anyone else the starter. Only a session start names a
session's `chat:` id: an edit of `cli_session_id` (by its opening key or an admin
alone) never moves a session into or out of one, and is 403 if it would. A post the
assistant would answer
in a chat a person's own helper began is 409, since only that helper reads it. A
`fork` whose key names a conversation someone else began is 409. The head's
`forks_on_typing` says a post
is refused, and the browser then forks at the latest line instead (or on the 403,
if it posted before the head was read). A `fork` is allowed to anyone, in a chat they
may join too. A fork of a chat that was since deleted keeps its lines, and its
`forked_from` is null.

Sharing. `PUT /api/chats/<uuid>/shares/<email>` (writer role, browser only) shares a
chat with one person, who then posts into it as its starter does; `DELETE` of the
same path stops sharing it, so their next line, by Chat or by a Console relay, is
refused while their earlier lines stay. The email is stored lowercase; a malformed
one is 422. A share follows the conversation, whatever sessions it holds. Only the
chat's starter (Forking above says who that is) or an admin shares and unshares, an
API key is 403, and anyone else is 403 `{"detail": {"code": "chat_not_starter"}}`;
a chat the caller cannot find is 404. Either again changes nothing. The answer is
the chat's shares, a JSON array of emails, oldest share first.

Deleting. `DELETE /api/chats/<uuid>` (writer role) deletes a whole chat: every
session under its id, whoever's key opened it, with its records (each purged as
`DELETE /api/inquiries/<uuid>` purges a row, audited), its shares, and its binding
to any station, whose next line then starts a new chat; each such station moves to
its next `revision`, and its open tabs get a `workspace` frame without the chat at
that revision. A tombstone keeps the id, and nothing starts the chat again: its
head, a later post naming it, a `fork` keyed by it or from one of its lines,
`POST /api/workspaces/for-chat/<uuid>` and `POST /api/sessions/start` with its
`chat:<uuid>` id are each 410 `{"detail": {"code": "chat_deleted", "message":
...}}`, and an append to one of its sessions is 404, as the session is gone. Lines
still queued for it go with it. It answers 204, or 404 for a chat the
caller cannot find. An admin deletes any chat, and so does the key that opened
every session under its id; any other API key is 403, so a person's browser deletes
it otherwise. Its starter deletes it while every person's line in it is theirs,
counting only the sessions the assistant or the starter's own key opened (a line a
fork copied from its original counts as its original poster's, a line on its way
in as posted, whether still queued or taken by the assistant in the last 10 minutes
and not yet recorded in one of those sessions, and a session whose audience they do
not read as someone else's); once someone else has posted, the starter is 403
`{"detail": {"code": "chat_has_other_posters"}}`. Anyone else is 403
`{"detail": {"code": "chat_not_starter"}}`. A session that is locked, or linked to a
locked row, needs an admin, as a purge does. The chat's forks stay. Its sessions'
`change_log` rows stay, but each title and description they held is blanked (a
title to `""`, a description to null): the log is append-only by convention, not
by any constraint, and a deleted chat's words do not outlive it there.

When a session ends with Chat lines it never drained, the unread lines of a
science chat are queued again for the assistant's service session, which
reopens the conversation's session. The unread lines of an assistant or
`trax-helper` service session that names itself (a `cli_session_id`) stay queued
for the session its next start resumes; one that does not is released, and the
lines it held are logged with their senders.

Only the key that opened a science chat writes or drains it, whoever that is and
whether or not an assistant is configured (a session under a `chat:` id is one, a
user's own `trax helper` chats included; a `trax-helper` session is drained by its
key alone): appending records, ending it,
editing any of its fields (its labels and account decide who may use it from a station
and which stations the assistant may use) and purging it by id are 403 from any
other key, a writer's included (an admin may purge it), and
`POST /api/sessions/<uuid>/inbound` and
`/api/messages` refuse it with 409 because a line sent that way names no
conversation. A browser's `/api/messages` send to a science chat, live or idle, is
refused with the same 409 and the detail "Open its station to talk to this
chat". A conversation has one session per assistant key that has spoken in it,
since a session resumes only for the key that opened it; `GET /api/chats/<uuid>`
names the live one, else the newest.

`GET /api/workspaces/<uuid>/events` streams the owner's canvas as server-sent
events (browser only), and is the tab's one stream. Each frame is `data: <json>`
with `t`, the server's epoch milliseconds: `{type: "workspace", state}`
on open, after every applied operation, and when the partner changes (a
session starts or ends, or its poller lease lapses);
`{type: "navigate", route}`; `{type: "highlight", ids}`; `{type: "chat", hidden}`; and
`{type: "changed", id}` for each inquiry
id `/api/web/subscribe` relays. A record appended to a science chat's session
changes that session, so every viewer who reads its audience gets a `changed`
frame and reads what the session gained. A comment goes out on open and after 25 s without a
frame, and on each the server checks the user is still active and ends
the stream if not. A subscriber more than 256 frames behind is dropped and
reconnects from the `workspace` frame.

### 1.26 Variables

```
GET    /api/variables
PUT    /api/variables/<name>
DELETE /api/variables/<name>
```

The environment variables an agent launch exports, plain and secret. Only the
org layer has routes; the store also keys machine and user layers. `GET` is
writer role and `PUT` / `DELETE` are admin role. Bodies: section 3.25.

A plain value is stored and listed. A secret's value goes to the server's
secret backend, which `TRACKINIZER_SECRETS` selects (`file`, `file:/abs/path`,
or `none`); no route returns it, so a listed secret has `value: null`. A name
stored as a secret stays secret: a plain `PUT` on it answers 409 until the
variable is deleted. Without a secret backend, a secret `PUT` and the `DELETE`
of a secret answer 503. A name must match `^[A-Za-z_][A-Za-z0-9_]{0,127}$`
and a value is 1 to 65536 bytes of UTF-8 with no NUL; anything else answers
422, and a 422 under `/api/variables` never echoes the rejected input, not
even as a body key. The same holds behind a path prefix.

### 1.26 Machines

```
GET    /api/machines
GET    /api/machines/<name>
PUT    /api/machines/<name>
PATCH  /api/machines/<name>/labels
DELETE /api/machines/<name>
POST   /api/machines/enroll
POST   /api/machines/join
POST   /api/machines/<uuid>/heartbeat
POST   /api/machines/<name>/revoke
```

The registry of machines a campaign may run on: a name, a role, a `how`
line telling an agent how to use the machine, and labels. It never reaches a
machine, so `DELETE` only unregisters. Both `GET`s are writer role; `PUT`,
`PATCH`, `DELETE`, `enroll` and `revoke` are admin role. `join` and
`heartbeat` take no role: the secret they carry is the credential. Bodies:
section 3.26.

`PUT` creates the machine when the name is new. A field the body leaves out
keeps its value (a new machine starts with it empty) and `""` clears it.
`PATCH` adds the `add` labels, then removes the `remove` labels; adding a
label already present and removing one that is absent are no-ops. Labels are
stripped and deduplicated, as an Issue's are, and a blank label answers 422.
`PATCH` and `DELETE` answer 404 for an unregistered name, as does `GET`.

A name must match `^[a-z0-9][a-z0-9-]{0,62}$` and must not be `enroll`, `join`,
`init`, `import`, `check`, `connect`, `leave` or `top`, which are, or will be,
route segments and CLI words. A role is empty or matches
`^[a-z][a-z0-9-]{0,31}$`; `how` is at most 2000 characters with no NUL.
Anything else, on any of the five registry routes, answers 422.

A machine that runs a host connects in three steps. An admin calls `enroll`
with the machine's name, which registers the machine if it is new and returns a
one-use enrollment token (`enr_...`) valid for 15 minutes; a new `enroll`
supersedes any open token. The host calls `join` with the token, once, and
receives the machine's id and its machine credential (`trax_machine_...`).
A token that is malformed, unknown, used, expired, issued for another name, or
issued by an account that is no longer an active admin answers 401 with one
body. Joining a machine that already has a live credential revokes that
credential first. Both responses are `Cache-Control: no-store`, and a 422 on
`join` never echoes the token, not even as a body key.

The host then calls `heartbeat` with the credential as its bearer token, about
every 15 seconds, naming its `instance` (its own id, kept across restarts). One
instance holds a machine at a time: a different instance answers 409 until the
first has been silent for 180 seconds, and then takes over. A heartbeat whose
credential was revoked while it was in flight answers 410 and writes nothing.
A machine's `status` is derived, never stored: `online` within 180 seconds of
its last heartbeat, `offline` after, `never` before the first, and `revoked`
once every credential it had was revoked. `GET /api/machines/<name>` adds
`last_heartbeat`, `host_version` and `facts`.

A machine credential is checked by its own dependency, not by the user roles. It
never authenticates any other route (those answer 401), and an API key never
authenticates `heartbeat` (401). A credential for machine A on machine B's path
answers 404. `revoke` keeps the credential's row and closes any unused token; the
host's next request then answers 410 `machine_revoked`, while an unknown
credential answers 401, so only a holder of a once-valid secret learns of the
revoke. Revoking a revoked machine, or one that never joined, is a no-op.
`DELETE` answers 409 `machine_in_service` while the machine holds a live
credential; revoke it first.

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
cli_session_id              yes  no     yes     opening key or admin; never chat:
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
is this sign, not a separate edge kind. `cites_paper` carries no `valence`: it
records a historical, bibliographic fact, not our judgement.

Endpoint pairs

Every edge is stored child -> parent (`from` younger/dependent, `to` older
parent). Exactly seven kinds:

```
narrows         Issue      -> Issue          (narrower  -> broader)
requires        Issue      -> Issue          (requirer  -> prerequisite)
produced_by     Inquiry    -> Inquiry        (produced  -> producer)
supersedes      Inquiry    -> Inquiry        (successor -> predecessor)
proves          Artifact   -> {Belief, Experiment}   (citing -> cited)
favors          Artifact   -> {Belief, Experiment}   (citing -> cited)
cites_paper     Paper      -> Paper          (citing    -> cited)
```

Projected pairs

```
narrows         -> Issue.narrows / Issue.narrowed_by
requires        -> Issue.requires / Issue.required_by
produced_by     -> Inquiry.produced_by / Inquiry.produces
supersedes      -> Inquiry.supersedes / Inquiry.superseded_by
proves          -> Artifact.proves / {Belief,Experiment}.proved_by
favors          -> Artifact.favors / {Belief,Experiment}.favored_by
cites_paper     -> Paper.cites / Paper.cited_by
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
      "last_login": "<ts>" | null,
      "staff": <bool>
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

`self` is the inquiry as `GET /api/inquiries/<uuid>` returns it, with every
field of its kind and `null` for an unset one, less the relation fields
(`produces`, `narrows`, `proves` and the rest). `edges` and `backlinks` carry
those relations instead, grouped by edge kind. Each peer has its `id`, `kind`,
`seq`, `title`, `status` and `peer_created` (when the peer was created); a
Belief adds its `judgement`, and an Issue with a priority adds it as
`peer_priority`. The edge's own `priority`, `note`, `valence` and `labels`
ride on the same peer when set, so in a parent's list an edge `priority`
overrides the child's `peer_priority`. `changes` holds the latest 50
changes, newest first, ties in `created` broken by descending `id`. Each
`/api/web/search` result has the shape of `self`.

### 3.23 SSE event

```
event: change
data: {"id": "<change_uuid>"}
```

### 3.24 Export lines

```
{"format": "trackinizer-export", "version": 1, "migrations": ["schema.sql", ...], "audiences": ["open", "staff"]}
{"table": "inquiries", "row": {"id": "<uuid>", "kind": "Issue", ...}}
{"table": "edges", "row": {"from_id": "<uuid>", "edge_kind": "narrows", ...}}
...
```

The first line is the header: `migrations` names the applied schema files,
so a reader knows which columns the rows carry, and `audiences` names, sorted,
the audiences the caller reads. A session outside them has no row in
`session_manifests`, `session_records` or `session_slash_commands`, though its
`inquiries` row is there, so a file whose `audiences` lacks one the deployment
holds is not a whole backup. Every later line is one row
of one table, keyed by column name. Tables come in the order `inquiries`,
`edges`, `change_log`, `experiment_metrics`, `session_manifests`,
`session_records`, `session_slash_commands`, and rows in a fixed order within
each, so an unchanged graph exports byte-for-byte the same and new rows land
at the end. UUIDs and timestamps are strings; `json` columns are their
decoded value.

Not exported: `inquiry_embeddings` (derived), `session_ciphertext`
(encrypted, retention-managed), and `users` / `api_keys` / `allowlist`
(credentials and access control).

### 3.25 Variables

```
PUT /api/variables/<name>   {"value": "<text>", "secret": false}
GET /api/variables          {"variables": [{"layer": "org", "owner": "", "name": "<name>", "secret": false, "value": "<text>", "updated_by": "<email>", "updated": "<timestamp>"}, ...]}
```

`secret` defaults to false. The list is sorted by name. A secret's `value` is
`null`. `PUT` and `DELETE` answer `204` with no body, so a secret is never
echoed. `updated_by` is the email of the principal who last set the variable.

### 3.26 Machines

```
PUT   /api/machines/<name>          {"role": "<role>", "how": "<text>"}
PATCH /api/machines/<name>/labels   {"add": ["<label>"], "remove": ["<label>"]}
GET   /api/machines                 {"machines": [{"name": "<name>", "role": "<role>", "how": "<text>", "labels": ["<label>"], "updated_by": "<email>", "updated": "<timestamp>"}, ...]}
GET   /api/machines/<name>          {"name": "<name>", "role": "<role>", "how": "<text>", "labels": ["<label>"], "updated_by": "<email>", "updated": "<timestamp>"}
```

Every `PUT` field is optional; `add` and `remove` default to empty. The list
is sorted by name in byte order and labels keep the order they were added in.
`PUT`, `PATCH` and `DELETE` answer `204` with no body. `updated_by` is the
email of the principal who last changed the machine. Each listed machine also
carries `"status": "never|online|offline|revoked"` and `"last_heartbeat":
"<timestamp>"` (`null` before the first); `GET /api/machines/<name>` adds
`"host_version": "<text>"` and `"facts": {"<key>": <value>, ...}`.

```
POST /api/machines/enroll             {"name": "<name>"}
                                      -> 201 {"token": "enr_...", "expires_at": "<timestamp>"}
POST /api/machines/join               {"name": "<name>", "token": "enr_...", "instance": "<uuid>",
                                       "host_version": "<text>", "facts": {"<key>": <value>}}
                                      -> 201 {"machine_id": "<uuid>", "credential": "trax_machine_..."}
POST /api/machines/<uuid>/heartbeat   {"instance": "<uuid>", "host_version": "<text>",
                                       "facts": {"<key>": <value>}}
                                      -> 200 {"server_time": "<timestamp>"}
POST /api/machines/<name>/revoke      -> 204 with no body
```

`facts` is optional on `join` (default empty) and on `heartbeat`, where leaving
it out keeps the stored facts and sending it replaces them. A key matches
`^[a-z][a-z0-9_]{0,31}$`; a value is a string of at most 256 characters with no
NUL, an integer within 64 bits, a boolean, or a list of such strings; at most 64
keys and 4096 bytes as compact JSON. `host_version` is at most 64 characters
with no NUL. Anything else answers 422.

### 3.27 Service accounts

```
POST /api/admin/service-accounts
     {"name": "<name>", "role": "viewer" | "writer"}
     -> 201 {"id": "<uuid>", "email": "<name>@service.invalid", "name": "<name>",
             "role": "viewer" | "writer", "status": "active",
             "created_at": "<ts>", "last_login": null, "staff": <bool>}
POST /api/admin/service-accounts/<uuid>/tokens
     {"name": "<label>", "role": "viewer" | "writer" | "admin" | null,
      "open_into": "<audience>" | null}
     -> 201 {"id": "<uuid>", "name": "<label>", "prefix": "<prefix>",
             "role": "<role>", "open_into": "<audience>" | null,
             "secret": "trax_..."}
GET  /api/admin/service-accounts/<uuid>/tokens
     -> 200 {"tokens": [{"id": "<uuid>", "name": "<label>", "prefix": "<prefix>",
                         "role": "<role>", "open_into": "<audience>" | null,
                         "created_at": "<ts>",
                         "last_used_at": "<ts>" | null, "revoked_at": "<ts>" | null}]}
POST /api/admin/service-accounts/<uuid>/tokens/<uuid>/revoke
     -> 200 {"ok": true}
```

`name` matches `^[a-z][a-z0-9-]{1,30}$`; any other field, `email` among them,
answers 422, as does `role: "admin"` on the account. A key's `role` and
`open_into` are optional and default to the account's role and to no pin. `secret` is in the one answer that mints the key and in no
later one.

## 4. Other details

### 4.1 HTTP status codes

```
200  read / mutation / delete success with body
201  create success
302  auth redirect
400  invalid body, invalid field for kind, invalid projected edge mutation
401  missing or invalid auth; an unusable enrollment token or machine credential
403  role too low; a service account's key on a route that manages keys; a grant beyond the caller's role or audiences; a chat's share or delete the caller may not make
404  row not found
409  idempotency conflict, expected mismatch, immutable field, edge cycle, citation kind mismatch, plain value for a secret variable, machine in service, another host connected, service account name taken or account disabled
410  machine credential revoked; a deleted science chat (`chat_deleted`)
422  well-formed body rejected by domain validation (e.g. self-loop edge, priority on a non-priority edge kind)
500  server fault
503  secret variable requested and no secret backend is configured
```

Variable and machine `PUT`, `PATCH` and `DELETE`, and machine `revoke`, return `204`
with no body.

Inquiry and edge mutations -- including `DELETE` (field unset, inquiry
purge, edge remove) -- return `200` with a body carrying the `change_id`
they produced (section 3.13). Admin user-DELETE and chat `DELETE` return `204` with
no body.
Workspace mutations return canvas state; they do not produce a graph change.

### 4.2 Roles

```
viewer  GET /api/** except GET /api/variables and GET /api/machines/**, GET /api/web/**, GET /api/me/**, GET /app/**,
        PUT /api/me/visual-workspace,
        POST /api/workspaces, POST /api/workspaces/<uuid>/operations
writer  viewer + inquiry/edge create/mutate/delete, GET /api/variables,
        GET /api/machines/**
admin   writer + /api/admin/**, PUT/DELETE /api/variables/<name>,
        PUT/PATCH/DELETE /api/machines/**, POST /api/machines/enroll,
        POST /api/machines/<name>/revoke
none    POST /api/machines/join, POST /api/machines/<uuid>/heartbeat (the
        enrollment token or machine credential in the request is the credential)
```

### 4.3 Filters and pagination

```
filter.field narrows   Issue only; is <id> | ne <id> | isnull (roots) | notnull; answered from edges
fields                 repeated row-key param; rows keep only those keys; unknown key 400; another kind's key absent; edges read only when a relation is named
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
