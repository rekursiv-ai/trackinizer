# Trackinizer web app

The React app the trackinizer server serves at `/app/`. This file is its
contract with the server and its runbook: the routes it calls, how it renders
what they return, what it keeps in the browser, how it stays live, which links
it takes, and how to run, test and deploy it. Route details are in
[`docs/api.md`](../docs/api.md).

The server serves the build to whoever its API answers: a signed-in user, or
anyone when it runs with `--no-auth`. Any other browser gets the sign-in page.

## Layout

- `src/api/` is the only code that talks to the server: one module per area
  over one typed client, `client.ts`. Nothing else calls `fetch`.
- `src/api/openapi.json` is the server's schema, committed. `scripts/codegen.ts`
  generates `src/api/generated/` from it (not committed) before every `dev`,
  `build`, `test` and `typecheck`.
- `src/app/` boots and holds the shell, `src/router/` the hash router,
  `src/live/` the stream layer, `src/writes/` the write layer, `src/debug/` the
  logger and Copy details (see [debugging](#debugging)), and the other
  directories one view or control each.
- `test/testdata/` holds the server's real answer to the calls of the
  modules in `fixtures_test.py`'s table (see
  [response shapes](#response-shapes-and-fixtures)); the canvas's and Chat's
  calls (`workspaces`, `chats`, and the visuals, presets, timeline and Artifact
  modules) have none.
- `e2e/` is the Playwright suite, `e2e/live/` its live and responsiveness part.
- `scripts/` holds the schema dump, the local preview, the seed and writer
  scripts, the production timing script, the bundle-size check, and the
  script that takes the README's screenshots.
- `openapi_drift_test.py` and `fixtures_test.py` are the Python tests that keep
  the app in step with the server.

## Routes

### How every call is made

- Every call goes through `send` in `src/api/client.ts`, under a timeout: 15 s
  for reads, 6 s for search, 30 s for writes. A failure is an `ApiError` with the
  status and the server's `detail`. Status 0 means no answer: `code` is
  `timeout` or `network`. A 422's field errors become one line per field. A
  success whose body is not JSON keeps its status, with `code` `unreadable`.
  Every call carries its own `X-Request-ID`, and a failure keeps it, with the
  method, path and attempt, as `ApiError.sent` (see [debugging](#debugging)).
- Kind names are PascalCase in reads (`kind=Issue`, `/api/inquiries/Issue/4`) and
  lowercase in writes (`POST /api/inquiries/issue`). Fields every kind has are
  written under `/api/inquiries/{id}/<field>`, the others under the lowercase
  kind that owns them (`/api/issue/{id}/priority`).
- Every write request gets its own fresh idempotency key, frozen with its body
  (`keyed` in `src/api/idempotency.ts`). Edits, edge writes and purge send it as
  the `Idempotency-Key` header; creates send `idempotency_key` in the body, one
  per item in a batch. A retry resends the same key and body; a new edit is a new
  request. Account and admin writes (tokens, users, the allowlist) record no
  change, so the server keeps no key for them, and none is sent.
- No write sends `actor`: the server records the signed-in user.
- A `DELETE` always has a JSON body, `{}` or `{"reason": ...}`; without one the
  server answers 422. Clearing a field is a `DELETE`, never a `PUT` of `""`.
- A list field changes one element at a time, by `PATCH` with
  `{"op": "add" | "sub", "value": ...}`, never by a `PUT` of the whole list.
  An Issue's last type is the exception: the server answers 409 to a `PATCH`
  that would empty `issue_kind`, so removing it is a `DELETE` of the field,
  checked at save like any field without compare-and-set.
- Status, owner and judgement are set by compare-and-set:
  `{"value", "mode": "cas", "expected"}`. Another field is checked at save: the
  write layer reads the row and compares the field with the value editing began
  from.
- Inquiry and edge writes answer with ids only (`{id, change_id}`,
  `{change_id, created}`, `{id}`, `{ids}`). After a write, every cached read
  that shows a touched id refetches.
- Reads retry twice, after 1 s and 3 s, when no answer came or the server
  answered 5xx. Writes retry the same way three times, after 1, 3 and 9 s, when
  a resend is safe (`resendable` in `src/writes/requests.ts`). After an attempt
  that got no sure answer, automatic or before a Retry, an edit, edge write or
  purge first reads whether it landed: landed is done, unsent is sent again
  under the same key, someone else's change is a 409 conflict. A create or a
  batch is resent without that read, since the server replays its key and
  creates nothing twice. Account and admin writes are never resent on their
  own. A change landing between that read and the resend is overwritten.
- Errors show the server's message. A domain error is `{"detail": "<text>"}`,
  some with a `code` (`conflict` on a compare-and-set 409); a request-validation
  error (422) is `{"detail": [{"loc", "msg", "type", "input"}, ...]}`.

### The calls

Each call's fixture, where it has one, is
`test/testdata/<module>/<function>.json`. The `workspaces` and `chats` calls have
none: the schema types them (`tsc` against `openapi.json`, kept in step by
`openapi_drift_test.py`), and their API-module tests check what each sends and
how it reads a frame.

| Module | Function | Route |
|---|---|---|
| `meta` | `getEnums`, `getFieldOwners`, `getEdgeTopology` | `GET /api/meta/enums`, `/fields`, `/edges` |
| `me` | `getProfile` | `GET /api/me/profile` |
| | `acknowledgeRules` | `PUT /api/me/acknowledge` |
| | `listTokens`, `createToken` | `GET`, `POST /api/me/tokens` |
| | `setTokenRole`, `revokeToken` | `PUT /api/me/tokens/{id}/role`, `POST .../revoke` |
| | `signOut` | `POST /auth/logout` (302 to `/`) |
| `inquiries` | `listInquiries`, `listInquiriesBySeq` | `GET /api/inquiries?kind=&filter=&limit=&offset=&fields=&ancestors=` or `&seq_range=` |
| | `getInquiry` | `GET /api/inquiries/{id}` |
| | `createInquiry` | `POST /api/inquiries/{kind}` |
| | `createBatch` | `POST /api/inquiries/batch` |
| | `setField`, `clearField`, `patchField` | `PUT`, `DELETE`, `PATCH /api/{owner}/{id}/{field}` |
| | `purgeInquiry` | `DELETE /api/inquiries/{id}` |
| `detail` | `getDetail` | `GET /api/web/get/{id}` |
| | `findRef` | `GET /api/inquiries/{Kind}/{seq}` |
| | `getEvidenceConfidence` | `GET /api/inquiries/{id}/confidence` |
| `edges` | `addEdge`, `removeEdge` | `POST`, `DELETE /api/edges/{from}/{kind}/{to}` |
| | `setEdgeAnnotation` | `PUT .../{note,valence,priority}` |
| | `clearEdgeAnnotation` | `DELETE .../{note,valence,priority,labels}` |
| | `patchEdgeLabels` | `PATCH .../labels` |
| `changes` | `listChanges` | `GET /api/change_log?kind=&after_id=&since=&limit=&brief=` |
| `search` | `searchInquiries` | `GET /api/web/search?q=&kind=&limit=&fields=` |
| `metrics` | `readMetrics` | `GET /api/experiments/{id}/metrics?limit=1000` |
| `sessions` | `listSessionParts` | `GET /api/sessions/{id}/parts` |
| | `readSessionRecords` | `GET /api/sessions/{id}/records?part=&after_idx=&limit=&plaintext_only=true` |
| | `sendSessionMessage` | `POST /api/sessions/{id}/inbound` (header `Idempotency-Key`) |
| | `readFeed` | `GET /api/web/feed?tail=&conversation=&after_created=&after_session=&after_part=&after_seq=&since=&until=&limit=&actor=&room=&cli=&kind=` (each filter repeated per value) |
| | `readFeedFacets` | `GET /api/web/feed/facets?since=&until=&actor=&room=&cli=&kind=` |
| | `sendRoutedMessage` | `POST /api/messages` (header `Idempotency-Key`) |
| `histogram` | `readHistogram` | `GET /api/web/feed/histogram?since=&until=&buckets=&actor=&room=&cli=&kind=` |
| `admin` | `setInquiryLock` | `PUT /api/admin/inquiries/{id}/lock` |
| | `listUsers` | `GET /api/admin/users` |
| | `setUserRole` | `PUT /api/admin/users/{id}/role` |
| | `disableUser`, `enableUser` | `POST /api/admin/users/{id}/disable`, `/enable` |
| | `deleteUser` | `DELETE /api/admin/users/{id}` (204) |
| | `listAllowlist`, `addAllowlistEntry` | `GET`, `POST /api/admin/allowlist` |
| | `setAllowlistRole` | `PUT /api/admin/allowlist/{entry}/role` |
| | `removeAllowlistEntry` | `DELETE /api/admin/allowlist/{entry}` |
| `graph` | `getGraph` | `GET /api/web/graph?limit=` |
| `stream` | `openStream` | `GET /api/web/subscribe` (server-sent events) |
| `workspaces` | `openWorkspaceEvents` | `GET /api/workspaces/{id}/events` (server-sent events) |
| `chats` | `listChats` | `GET /api/chats` |
| | `getChatHead` | `GET /api/chats/{id}` (404 until the assistant opens the session: `null`) |
| | `sendChatLine` | `POST /api/chats` (header `Idempotency-Key`; `conversationId` null starts a conversation; `fork` starts one from a line of another) |

Edges are stored child to parent: `from` is the child, which `narrows` its
parent or `proves` its Belief. `/api/web/get` returns `edges` (this row is the
child) and `backlinks` (this row is the parent), grouped by edge kind.

### Response shapes and fixtures

The schema types most responses as free JSON, so the row types in the API
modules are written by hand from `types/inquiries.py` and the route code, and
`tsc` cannot check them. `fixtures_test.py` covers that gap. It starts the
server as the end-to-end suite does (`--ephemeral --no-auth`, PGlite in memory),
builds a small graph, makes every call in the table, and compares each exchange
with its fixture. A server change that alters a response fails it, in Python CI.

Before comparing, it normalises what differs from run to run. An inquiry keeps
one id across all fixtures, `00000000-0000-4000-8000-<n>` for the n-th the graph
made; other UUIDs (changes, users, tokens) are numbered per fixture,
`...-9000-<n>`. Every time becomes `2026-01-01T00:00:00.000000` with the
server's own zone suffix. Token secrets and prefixes become `<secret>` and
`<prefix>`. Lists of rows are put in an order by content, since the server's
order among rows rests on random ids. Seqs are as a fresh database assigns them.

Besides every call's success, the fixtures hold these errors:
`inquiries/setField.conflict` (409), `inquiries/setField.invalid` (422),
`inquiries/getInquiry.purged` (404), `search/searchInquiries.badQuery` (400) and
`admin/addAllowlistEntry.duplicate` (409). Field writes are one route per field
with one response shape, so the fixtures take one per verb.

### Keeping the app in step with the server

- A change to a route, a parameter or a body: regenerate the schema with
  `scripts/openapi_dump.py`, then typecheck. `openapi_drift_test.py` fails until
  the committed schema equals the in-repo app's.
- A change to what a route answers: rerun the fixture test with
  `TRACKINIZER_WEB_FIXTURES_UPDATE=1` and review the fixtures' diff. Then update
  the hand-written row types in `src/api/` to match.
## Rendering rules

- Nothing is hard-coded. Kinds, enums, field owners and edge topology come from
  `/api/meta/*` at boot. The sidebar lists kinds in the server's order
  (`inquiry_kind_all`); a kind the app has no name or icon for shows its own
  name.
- A viewer sees no write controls; while offline, write controls are disabled.
- A detail shows every field its kind has. A value that is absent, `null`, `""`
  or `[]` shows as unset. A field is editable when it has a `PUT` route; the
  editor's control comes from the route's body type (`src/api/fields.ts`, from
  the generated field map), or a menu when `/api/meta/enums` lists the field.
- Priority shows as a band, `priority // 10` capped at 3: P0 Critical, P1 High,
  P2 Medium, P3 Low, so backlog (40) shows as Low. `null` is "No priority". In a
  parent's list of children, the edge's priority overrides the child's own.
- Status and judgement show their stored value, `invalid` and `undecidable`
  included.
- Markdown renders with react-markdown and remark-gfm straight to elements.
  Raw HTML shows as text. Link and image targets pass only when they start with
  `http://`, `https://` or `#/`; a refused link shows as its text. `Kind#seq`
  refs and UUIDs in text become in-app links, never inside code or another link;
  a UUID shows its first eight characters. Headings step down two levels.
- A link written as a ref, its text a `Kind#seq` and its target that ref's
  route, alone or after the app's address on any host
  (`https://<host>/app/#/ref/Issue/4`, or the old UI's `/#/ref/…`), is the
  ref's own link, as agents copy one from trax. Any other link stays as written.
- An absolute path in text (`/` or `~/` opening a word, with two segments or
  more; not a URL's, one after a host as in `host:/opt/x`, a ratio as in
  `5/10`, nor the punctuation after it), and an inline code span that is one
  path, shows as code that copies the path on a click and says so: a browser
  opens neither a local file from the app nor a file picker at a path.
- A message, in a transcript or the console, keeps each line of its
  paragraphs, as chats and GitHub's comments do; a description's lines reflow,
  as Markdown's do.
- A field whose whole text is one JSON object or array, and a code block tagged
  `json` or `jsonc`, or untagged, whose text is one, shows as a JSON view: one
  entry a line, numbers as written, keys, strings, numbers, and true, false and
  null in the code colours. A nested object or array folds behind its key, and
  starts folded past 50 entries or three levels down. An object of numbers alone
  is a table of key and value, each key under the prefix before its first `/`,
  as W&B and TensorBoard group scalars. Refs and UUIDs in its strings link as in
  text. Only a text that starts with `{` or `[` is parsed, once per text; any
  other renders as Markdown.
- Every code block and JSON view has Copy, which copies its text as written.
  Code scrolls sideways, and the keyboard can focus a code block, named Code,
  to scroll it. A text with a fenced block loads the highlighter, a
  chunk of its own (lowlight, highlight.js's `common` languages, 50 KB gzip),
  which colours each code block that is not JSON: a tagged one as its tag says,
  or not at all when highlight.js has no such language; an untagged one as
  Python when it is a traceback, else as the likeliest of Python, a Python
  session, shell, TypeScript, JavaScript, YAML and diff when highlight.js's
  relevance is at least 3 and one for every five words. SQL, whose keywords are
  English words, is coloured only when tagged. The code colours are tokens in
  both themes, each over 4.5:1 on every surface.
- A detail's rail lists its parents and children by `narrows`, `requires`,
  `produced_by` and `supersedes`: each peer once, with every edge joining it,
  ten per section, then "Show all". Its other relations (citations) list under
  the text, 100 rows a group, then "Show all". A cost keeps every digit under a
  cent, and 0 reads "none recorded", since the server stores an unrecorded cost
  as 0.
- A detail's header has Show in graph, a link to the graph focused on the
  inquiry two hops out (`#/graph?focus=<Kind>/<seq>&hops=2`), also in the
  palette under its `Kind#seq`; the graph's Peek leaves it out. Narrow (a
  detail under 900 px wide, Peek included), the rail stacks under the title and
  text fields, before config, other relations and activity.
- A detail's rail opens with a graph preview: what lies within two hops of the
  inquiry, its nearest 60 (`/api/web/graph?focus=<id>&hops=2&limit=60`). The
  inquiry is in the middle, its neighbours on an inner ring, theirs on an outer
  one, each node drawn as the graph draws it and named on hover by its
  `Kind#seq` and title; the caption counts them, `60+` when the limit cut it
  short. It reads and draws after the rest of the page, in a deferred render,
  and is one link to the graph focused there, two hops out. An inquiry with no
  relations has none, and the graph's Peek leaves it out.
- A detail's activity is the last 50 changes `/api/web/get` sends, oldest first,
  with each run of `dependency_changed` alerts folded into "N upstream changes".
- Activity has the tabs All, Status, Judgements, Created, Relations and Edits,
  each a set of change kinds read in one `change_log` query (a repeated `kind`):
  Status `status`; Judgements `belief_judgement`; Created `created` and
  `purged`; Relations `edge_added` and `edge_removed`; Edits `description`,
  `title`, `issue_priority`, `labels` and `owner`. All reads every tab's kinds
  in one query, so `dependency_changed` alerts stay out. 50 brief rows a page,
  Load more by `after_id`. An edge change written on both ends shows once.
- Lists load 50 rows a page for one kind, 20 per kind for several. Rows come
  newest created first; grouping and sorting rearrange loaded rows only, so a
  group's count is of its loaded rows, with a `+` while Load more may find more.
  The Filter menu offers the labels and owners on rows of the list's kinds that
  any list has loaded. A kind that lacks a filtered field is not requested,
  since the server would refuse the whole request.
- A mouse press on a list's buttons (its tabs, the view switch, Load more, a
  group's heading) leaves the focus where it was, so Enter still opens the
  focused row; Tab focuses each button for the keyboard.
- An Issue list's Outline nests its loaded rows under their `narrows` parents.
  It reads their ancestry apart from the pages (`ancestors=narrows` by id), and
  refetches it when the stream names a shown row or one of its ancestors.
- An Issue list's Streams group its loaded rows by root goal, the top of their
  `narrows` ancestry, read as Outline reads it. Each header has its rows'
  counts, active and done, and the newest row's age. A stream shows its newest
  3 rows, then "N more in this stream". A row under several roots is listed
  under each.
- Columns lists the list's root Issues (its tab and filters plus `narrows
  isnull`), newest first. Each next column lists the selection's children,
  read from `/api/web/get`, and the last adds its outputs (what it produced,
  less its children). A click or right arrow goes deeper and left goes back,
  each a history entry; j and k move within the deepest column, and Enter opens
  its selection. A click leaves the focus where it was, as a list's buttons do,
  so Enter after a click still opens the selection.
- The canvas is on for everyone (`visual_workspace_enabled` defaults true;
  Settings' Agent workspace opts out). It wraps every view but Settings and
  Admin, so Chat stays while an agent moves the page. Its chunk loads only for a
  user who has it on, together with Chat's, the renderer it shows first, so the
  canvas's first render suspends on nothing (a suspended renderer holds its
  fallback for React's 300 ms throttle, which cost a cold first load of a page
  250 to 300 ms); until both are in, the shell holds its busy frame, and the view
  then mounts once, inside the canvas.
- The page is `trax.browse`: it has no record or parameters, it cannot be
  dismissed (no ×, and Configure's box is fixed), and a crash in it shows the
  app's crash screen (Copy details, Reload) inside the canvas, which clears when
  the page moves. The record the canvas's Chat button means is the one the
  address names (`#/lookup/<id>`, or `#/ref/<Kind>/<seq>` resolved), never a
  visual's.
- Navigation is an event. An agent's `navigate` frame moves the page when
  `parseHash` accepts its `#/...` route (any other is ignored), and writes
  nothing. The browser's own links, Chat's, the record context, the context
  graph's nodes and the timeline's only set the address, and write nothing to
  the canvas. Panes share the stage and shrink to a floor (a page or main visual
  300 px, a side visual 260 px, the side's share 30% up to 360 px), so three of
  them fit 1280 px beside the sidebar with no sideways scroll; at 900 px and
  narrower they stack, each 400 px or 48% of the screen high, and the stage
  scrolls up and down.
- Chat talks to the server's assistant (`src/visuals/Chat.tsx`). A conversation is
  a science chat: an AgentSession (label `science-chat`, `cli_session_id`
  `chat:<conversation id>`) that the assistant opens. Chat reads the session's
  records; there is no chat store. Its header names the partner
  (`workspace.partner`) and has History and Clear chat. The History menu has the
  `menu` role, its items `menuitemradio` with the current one checked, and Escape
  closes it. History lists `GET /api/chats` (the chats the user started or posted
  in: title, age, starter) on each open; picking one opens it. Any other science
  chat opens by link: a session's page, or the Console line, offers Continue in
  Chat (`useContinueInChat`, `src/visuals/continueChat.ts`), which leaves a
  request on the shell's `ChatFeed` and shows Chat; Chat resolves the session
  (`cli_session_id`, label) and opens its conversation, or says "That session is
  not a science chat." Clear chat starts an empty conversation and keeps the old
  one: the next send goes with `conversationId: null`, and the receipt's id
  becomes the open one, unless the user has picked another conversation since.
  The open conversation is held by the shell (`ChatFeed`), so Chat mounting again
  keeps it, and in `localStorage` under `trackinizer.v2.chat.<workspace id>`, so
  a reload reopens it; with storage refused it lasts until the page does.
- Chat sends the page it is on: each line carries `page`, the `#/...` hash as it
  is sent, and `trail`, the up to 8 hashes before it, oldest first (routes only;
  the server resolves their records). `src/router/trail.ts` records them from
  `main.tsx` on, so pages visited before Chat opened count. With no record
  pinned, Chat shows `On screen: Kind#seq title` for the record a
  `#/lookup/<id>` or `#/ref/<Kind>/<seq>` page shows, and follows the page.
- The partner is the shared assistant, or the owner's own `trax helper` when the
  canvas chooses `local`; with none live, Chat says how to start one
  (`ChatOff`, `HelperCommands`) and the box is off.
- A chat is joined by typing in it, or forked. Every stored line has a "Fork from
  here" button (not a line still sending, and not for a viewer): it picks the
  line, a banner says so with a Cancel, and the next message is sent with
  `fork: {sessionId, part, idx}` instead of a `conversationId`. The server makes the
  fork a new conversation, named by the idempotency key, and Chat opens it. A chat
  whose head says `forks_on_typing` (the server's call, from the verified email
  domains; see `chat_orgs` in `server/config.py`) forks the same way at its latest
  line whatever the user types, since the server refuses a post into it; the
  composer is off until the head is read, and a post the server still refuses
  (403) is sent again as a fork at the latest stored line. A fork
  opens with the original's lines up to the one picked, so mine among them are
  not waited for as a new line (`linesThrough`). The head also says how often a chat
  was forked and which chat a fork came from.
- The composer says chats are public to every user and cannot be deleted. Chat has
  no delete.
- An answer that arrives lights up the rows it cites at once, through the tab's
  highlight store, as the agent's Highlight does, with no server round trip beyond
  `GET /api/inquiries/{Kind}/{seq}` for a ref not seen before (`useAnswerHighlights`,
  `src/visuals/chatHighlights.ts`; the lookups run together and are kept, since a
  seq never moves). The newest answer that cites any replaces the marks; one that
  cites none leaves them. A cited row is a `Kind#seq` of a known kind or a UUID
  (`namedRows`, `chatRefs.ts`), read as the page links one: a ref in a code span
  or block is quoted, not cited, and a link's text cites its ref but its target
  does not. The answers there when a conversation opens are history and light
  nothing. Settings' "Highlight rows the assistant mentions", in this browser, per
  user, on by default (`settings/highlightMentions.ts`), turns it off at once,
  a lookup under way included.
- A conversation's lines are the session's records (`src/visuals/chatRecords.ts`):
  parts and records are read incrementally, part by part, and
  `readTranscript` (`chatLines.ts`) turns them into lines. A person's line
  (`AgentToAgentMessage`) shows its sender when it is not the signed-in user's;
  an `AssistantMessage` with content is an answer. `useLiveChat` registers on the
  live hub and reads what the session gained when the canvas stream's `changed`
  ids include the session (or on a gap), so every viewer of a chat sees a
  teammate's line. The reads use the app's read retry.
- A sent line shows at once, right-aligned and pending ("Sending…"). The send
  returns the conversation id at once; the session does not exist until the
  assistant opens it, so Chat polls `GET /api/chats/{id}` (every 1.5 s, up to 30
  s) and shows "Waiting for the assistant…". When the session appears, the line
  is its record and the pending one gives way (matched by counts of that text
  before the send). If none appears, Chat says the assistant has not opened the
  chat and the line can be sent again. "Working: <tool>…" shows while the last
  record is a tool call or a line with no answer after it. Should the partner go
  away (`workspace.partner.status` not live), the working line gives way to
  "<actor> has ended." or "is unavailable.", and the box is disabled. A viewer
  reads a chat but the box is disabled for them.
  The partner's lines are left-aligned Markdown, without images. The box stays
  open to typing while a line sends, and what is typed meanwhile stays. It
  refuses a blank message and one over 16,384 characters. A send the server
  refused (4xx) shows the server's reason and offers no Retry; one that got no
  answer, or a 5xx, keeps the draft with Retry under one idempotency key. The
  transcript follows the newest row, the working row too, unless the reader has
  scrolled up. Chat shows no write error of its own: the canvas shows it once. A
  Chat about a record links to it and has Clear context, which returns Chat to
  the side. The canvas toolbar's Chat button shows Chat at the side, or focuses
  it when it is shown.
- The graph is the home view. It draws the newest 1,000 inquiries by default,
  or 100, 5,000, all of them, or any count typed into the Nodes menu (a typed
  count picks exactly that count: 50 is 50, and 1,000 the 1k preset), each
  followed by the older ones it links to. Over 5,000 nodes it asks before it
  draws, Replay included: at 7,500 every frame of the layout was a long task
  (50 to 69 ms, 18 fps) for the 9 s it ran, while 5,000 drew at 33 fps with
  none.
- Its tools are a list's: search, Filter (kinds and statuses, shown as chips),
  Nodes, Replay (its speeds in its menu), Group by root (and, while grouped,
  Roots list) and Key. The key is a column in the canvas's top right, above the
  zoom buttons, listing under Kinds the kinds loaded (each shown one with its
  count), and only what is drawn under Links (each edge kind by its stroke,
  named as the detail names it) and Status (each by v1's neutral ring). A kind
  clicked there, as in a chart's legend, shows only that kind, and a hidden kind
  clicked adds it; the only kind shown clicked shows every kind. Only that kind
  hides every other, those not loaded yet too, so a kind a later read brings
  stays hidden. Each sets the kinds Filter hides, where one kind at a time is
  hidden or shown; a hidden kind stays listed, struck through, and the key shows
  while any node is loaded, drawn or not. The Key button hides or shows it;
  every fit leaves out what the key, Peek and the zoom buttons cover on the
  canvas's right.
- Hovering a node lights it and its neighbours; a click holds the light and
  opens the node in Peek, and the background or Esc clears it. A Peek link to a
  drawn node selects it in place; one to a node a filter or Only these hides
  navigates. A double-click, or `.` on the selection, focuses on a node: the
  focus row lights what lies within 1, 2 or 3 hops or the whole connected part,
  each with how many nodes it reaches, walks only the edge kinds Through names,
  and dims the rest or, with Only these, hides it. The focus frames what it
  lights, and so does a graph drawn anew under it (a new limit, or Draw them);
  grouping keeps that frame. Esc clears the selection, then the focus. Opened
  on a focus (Show in graph, the detail's preview, a link), the graph also
  selects it: Peek opens on it, and the focus's frame leaves Peek out.
- Search finds drawn nodes (none a filter hides) by title, kind or `#seq`. It
  rings its matches on the canvas and lists them newest first, within the
  focus's hops and elsewhere, 200 a group; a pick selects one and keeps the
  query. Shift+Enter searches every inquiry on the search page.
- The graph opens grouped by root: each root's subgraph (`src/graph/roots.ts`)
  gathers into an island, the roots are labelled, and they are listed in place
  of the search results. Group by root turns that off, one web at
  `#/graph?group=none`, and on again. A root picked in the list opens in Peek,
  and its island is framed beside it. While
  Peek is open and not collapsed, the list narrows to a strip of the roots'
  glyphs that keeps its marked root and its keys, and Peek takes at most 45% of
  the graph's width. A pick clears the list's filter and leaves the keyboard in
  the list; the keys it does not use (Esc, `.`, and `/` beside Peek) are the
  graph's. Roots list, or `[`, hides the list and keeps the islands, giving the
  canvas its width. Showing or hiding the key or the list frames again what was
  last framed.
- Replay grows the graph in `created` order, 70 ms a node at 1x. `f` fits, `/`
  searches, `.` focuses on the selection, `]` collapses or expands Peek, `[`
  the search results while searching and otherwise shows or hides the roots
  list while grouped, and `g g` opens the graph.
- Peek, the graph's search results, a detail's rail and the console's rail
  collapse (`src/ui/panel.tsx`), each by a button in the bar above it: Peek's
  own bar, the results' heading, the detail's and the console's top bars. `]`
  collapses or expands the one on the right (Peek, a detail's rail) and `[` the
  one on the left (the search results, the console's rail); the palette lists
  each under Panels. Collapsed, Peek and the search results leave a 40 px
  strip at their edge with the button that expands them and the ref or the
  match count; a rail leaves nothing, its button staying in the top bar, as
  the graph's Key does. Under its strip, Peek's detail stays mounted, hidden,
  so an edit begun in it is there on expanding. Whatever collapses or expands
  a panel, a focus in what hides or goes then moves to the button that takes
  its place. A collapsed Peek
  stays collapsed as the selection moves, as VS Code's and Linear's panels
  do: only its button, `]`, or Space in a list expands it. Peek's detail shows
  its rail whatever the detail page chose, with no button for it. Collapsing
  or expanding one frames the graph again. The canvas's Configure opens its
  panel, and Done collapses it.
- The app's sidebar collapses too, by the button beside New or ⌘B (Ctrl+B),
  as VS Code's primary side bar does: `[` and `]` stay each view's own. It
  narrows to a 48 px rail of its icons, as VS Code's activity bar is: the
  button that expands it on top, then search, New and every entry in its
  order, each a link named and titled by its label, the current one marked,
  and the user's avatar at the foot. Rules stand in for the section headings.
  The graph frames again in the width it gives. Narrow, the sidebar is the
  drawer, whole, whether collapsed or not, and has no collapse button.
- The sun or moon beside the user, in the sidebar or its rail, switches the
  theme to light or dark; Settings' Appearance also offers System, and shows
  a switch made here at once (`useTheme` in `src/theme.ts`).
- Search in the palette answers while you type from what the app has loaded:
  list rows and recent details. After a 300 ms pause, or on Enter, it asks the
  server one kind at a time, Issues first, 5 rows each. A `Kind#seq` or a UUID
  jumps straight to its row.
- The search page, `#/search/<q>`, asks the server once across every kind for
  the newest 50 matches, and shows them in a table of ref, status and title with
  their count. The server's message, a search over its time budget included,
  shows in the table's place.
- Metrics show the first 1,000 points, with a note when the run has more. The
  server keeps a step up to 2^63 - 1, but a JavaScript number holds one exactly
  only up to 2^53 - 1, so a page with a larger step fails with a message saying
  so rather than draw two steps as one.
  A transcript part shows its newest 1,000 records, read 200 a request, all
  at once; Load earlier reads the 1,000 before. The newest three draw first,
  then 100 at a time in renders React can interrupt.
- A transcript renders each record from its payload. Bookkeeping (token
  counts, context state, harness messages, codex's context on the user's turn,
  provider lines with no kind of their own, and messages with nothing in them)
  hides behind "Show N bookkeeping records"; a model switch shows. A record
  with nothing to read, reasoning sealed with no summary or a sealed codex peer
  message, is drawn and counted nowhere, the console included. Agent Markdown
  never loads images. A person's or another agent's message over 1,500
  characters or 20 lines shows its first lines until Show all; the agent's own
  replies show whole, as the Claude and Codex apps show them.
- A codex agent's message to another shows without the envelope codex writes
  before it (`Message Type`, `Task name`, `Sender`, `Payload:`), whose sender
  the header names. One codex sealed, keeping its payload only as ciphertext,
  has nothing to read. A Claude Code slash command shows as it was typed
  (`/goal …`), and what it printed under Command output, as its TUI shows them.
- A harness's block on the user's turn folds under its tag and its attributes'
  values, codex's context as `codex_internal_context · goal`; a turn that is
  wholly codex's context is the harness's. A background task's notice
  (`<task-notification>`) shows as Task notification and its status, with its
  summary and any result, not its ids and file.
- A tool call and the first result after it with its `call_id`, however far on,
  are one step: one line that opens in place, as the Claude and Codex CLIs show
  one. It says what the step did (Ran, Read, Edited, Wrote, Searched, Listed,
  Fetched, from the result's kind, else the call's name; any other tool by its
  name), to what (a command without the `bash -lc` around it; a file's name,
  then its directory; a pattern, URL or query), and how it went (an exit code
  other than 0, the lines it printed, an edit's `+N −M`, the results found).
  Closed, a command's, an agent's or another tool's output shows its first
  three lines, its errors' included, a row each, and `+N lines`, which opens
  it; an edit its first three diff lines; a read, search, listing or fetch
  nothing. A failed step (an exit other than 0, or its provider's error flag)
  starts open, or opens when its failure arrives, unless the reader opened or
  closed it. Open, it shows the call's arguments, its command, pattern or patch
  in full (an edit's or a write's only until a result that succeeded shows
  them), and the result: output in its ANSI colours, its first 20 and last 40
  lines with the rest a click away, or a JSON view when it is one JSON object
  or array; a file read's or write's text in its file's language (by
  extension), a read's line numbers in the gutter; an edit's diff on tinted
  rows, its old and new sides each coloured in its file's language, numbered
  when the provider said where; a failure's text, why. Code is coloured by the
  Markdown highlighter's chunk, and only once open. Reasoning is one line too,
  its first, and Markdown open: its text, or its summary when the text is
  sealed.
- Two or more steps in a row, with the reasoning between them, fold into one
  line that counts what they did (`Ran 3 commands, read 2 files`) and how many
  failed, as the Claude app folds a turn's tool calls; open, each is its line.
  A group drawn about a step the reader opened, or is on, starts open, so a
  later step that folds it in leaves it open and focused as it was. The console
  shows every record as a line of its own, so it folds none.
- A `trax run sh` run shows as one terminal, its colours kept; a record's model
  and time show when they change.
- Relative times ("2h ago") re-render every minute.
- The console shows one view at a time: the agents and rooms it picks, by name
  or by a pattern such as `atlas-*` that takes in new agents as they appear,
  its CLIs, its level and its place in time. The server filters the feed by
  them (`actor`, `room`, `cli`, `kind`, each repeated, in sorted order, so the
  same picks read the same feed). A pattern picks among every agent the facets
  have listed since the console opened, so a narrower activity window drops none
  of the view's agents, records or To chips. A pick that matches none of them
  reads nothing and says so.
- The console's levels are Messages, + Calls, + Output and All. Messages is the
  conversation: a person's message (not what a harness writes on the user's
  turn: Claude's `isMeta`, codex's context, a background task's notice), the
  agent's, another agent's, and a person's message queued while the agent
  worked, each with something in it. + Calls adds tool
  calls; All adds token usage, turn context, system messages, uncategorised
  records and the rest of the conversation kinds; + Output is everything else.
  Messages reads only the conversation (`conversation=true`, with no `kind`),
  so every record it reads shows. + Calls reads only its kinds; + Output and
  All read every kind; each drops what the level does not show. Live, while
  the pages read hold fewer than 50 records the level shows (+ Output's, for
  All), the first read of + Calls, + Output or All takes the page before, up
  to five pages; Messages' newest page is enough. Each level's count comes from
  a facets read under the view's picks: its agents' conversation and records,
  and its records by kind.
- The console draws each record as the transcript does, but every long
  message, the agent's own too, shows its first lines until Show all: among
  many agents' lines, one long reply would bury the rest. A line's header is
  two lines: its time and agent, then its rooms and what it is (User,
  Assistant, From …). A long agent or room name is cut with an ellipsis in a
  line's header and the To chips, as the rail's rows and chips are cut at its
  edge; each shows whole on hover and to a screen reader.
- A click on a line's agent, a button named Message and the agent, addresses
  the message box to it and focuses it, the caret at the end: `@agent ` in an
  empty box, added to the address the box opens with unless it is there, else
  before the text; as `agent:room`, in the line's room, when the agent is in
  several rooms, as the @ suggestions name it.
- The console's facets count over the view's window: a history range's own, but
  at most the 7 days before its end (now, for a range with no end), so a range
  with no start, or one further back, counts those 7 days; or, live, the last
  15 m, 1 h (the default), 24 h or 7 d. There is no "all": a facets read counts
  every record in its window, and the whole history took 8.8 s on 9 million
  records. The range's feed still reads its whole window, page by page. The
  window only finds agents; it never changes what a view picks. The agent facet
  lists each session, so a name an ended session and a live one share shows
  twice. It
  finds agents by a fragment of a name or room, or by a pattern, which a button
  picks in one step, and groups them by room, CLI, state (working: heard from in
  the last 5 minutes and not ended; quiet; ended, as of the last minute) or name
  family (the name before its last `-`). A group's tick picks it in one step: a
  family as its pattern, any other group as its agents' names. Unticking clears
  whatever picks those agents, a wider pattern too. Each pick shows as a chip
  that clears it, so an agent no longer counted can still be cleared.
- The console's minimap, above the feed, counts the view's filters over time
  (at Messages, its kinds: the histogram takes no `conversation`) and marks the
  time the feed's records cover. It spans the last hour, day or week, and the
  wheel and the track below it scroll it back to the 7-day mark and no
  further, as far back as the server's histogram counts; Week is the whole 7
  days. Each read starts at the first bar after the mark. A click on it sets
  the view to a window of history from that time on; a drag, to the window
  dragged.
- A console line with no `@` target goes to the To chips: the view's agents that
  have not ended, in each of their rooms the view shows, or with no room when
  they have none. A chip can be left out of its view; each view keeps its own
  while the console stays open. `@agent`, `@agent:room`, `@a,@b` and `@*`
  (every agent-and-room pair the feed shows) still work. An `@` at the start of
  the line, or after a comma there, lists what it may complete to: names that
  start with what follows it first, then the view's agents (the latest line
  first), those in its rooms, and the rest by latest activity, as GitHub and
  Zulip rank people; an agent in several rooms once per room, one the facets
  list as ended not at all, and `@*` last. ↑ and ↓ move, Enter or Tab
  completes, Esc closes.

## Client state

- Server data lives in one TanStack Query cache (`src/app/queryClient.ts`),
  keyed by request: `["meta", ...]`, `["me", "profile"]`, `["inquiries",
  "list", filters, kind, pageSize, offset, after]`, `["detail", id]`,
  `["confidence", id]`, `["ref", kind, seq]`, `["metrics", id]`,
  `["activity", ...]`, `["search", q, kind]` (kind `null` across every kind),
  `["ancestry", ids]` (an Issue list's `narrows` ancestry, by the ids it
  shows), `["graph", limit]`, `["graph", "focus", id, hops]` (a detail's graph
  preview), `["settings", ...]` and `["admin", ...]`. Nothing refetches on window focus;
  the stream keeps data current.
- The entry chunk (`src/main.tsx`) starts the four boot reads and the first
  view's data (the graph's 1,000 newest, or a list's first page, when the tab
  has kept no state for it, or a detail and its `Kind#seq` lookup) before React
  DOM and the app arrive, and
  `index.html` preloads every first-load chunk. Each query's first fetch takes
  the read started under its key (`src/app/prefetch.ts`); a read no query took
  within 10 s is dropped.
- A list row the pointer rests on for 150 ms, or that j or k moves to, has its
  detail read ahead into the detail's own query (`src/lists/prefetch.ts`): at
  most 10 a minute, and none read in the last 30 s. A `Kind#seq` link to a row
  the app holds opens by that row's id, with no by-seq lookup.
- A 401 from any read or write ends the session: open drafts are saved to
  `localStorage` (`trackinizer.v2.drafts.<email>`), the current hash to
  `sessionStorage` (`trackinizer.v2.return_hash`), and the browser goes to
  `/auth/login_page?next=%2Fapp%2F`. The next boot restores both.
- A write refused with 403 refetches the profile, since the role may have
  changed.
- This browser's own state is one versioned JSON value in `localStorage`, keyed
  `trackinizer.v2.<origin>.<email>` (`src/state/`): stars, saved views
  (`{id, name, request}`, the exact list request), aliases, people added by hand,
  notification read state (`{boundary, marks}`) and UI state
  (`{collapsed, lens, tiles}`). Settings exports it and imports another browser's: an
  import never deletes, views merge by id, and UI state stays this browser's.
  Tabs see each other's changes through the `storage` event. The value is
  version 2; a version 1 value reads as version 2 with no tiles.
- Every visual but the page has two dock buttons on its bar, ◧ and ◨: one press
  stands it in the column at that side of the page, floating or not (a `place`
  to `left`, or to `side`, the right).
- Chat is docked beside the page: in the column at either side, or in the main
  strip when placed there. A canvas that stored it floating shows it at the
  right. When an
  agent moves the page (`navigate`) or shows a visual (a `workspace` frame's
  `shown`), Chat stands aside: the same tile, never mounted again, floats over
  the page folded to a filled bar. It opens after the pointer has rested on it
  120 ms, stays open while the pointer is over it or the keyboard is in its
  contents, and folds 300 ms after both have left; each later navigation or
  show folds it at once. A dock button on its bar puts it back at that side,
  and the toolbar's Chat at the side it came from.
  Standing aside is the tab's own state (`ChatFeed.aside`), never the canvas's:
  a reload starts docked, and its fold is not remembered.
- A floating canvas tile (a visual placed Float, or Chat standing aside) moves
  by its whole top bar and by the
  ⠿ handle, which also moves by the arrow keys (16 px, 48 with Shift). A press
  on the bar that stays within 4 px is a click and folds the tile to its bar,
  and the next one unfolds it; a drag never folds. Controls on the bar are not
  part of the bar: a press on one starts nothing. Under 900 px the tile is a
  plain header in the page's flow and neither moves nor folds by the bar. A move
  is a transform on the tile alone, kept inside the stage, and the canvas takes
  the place once, on release. The place and the fold are kept per visual type in
  `ui.tiles` (`{collapsed, place: {left, top} | null}`), so they survive a
  reload; a browser that cannot store keeps them for the session. Opening a
  saved view forgets the places and shows the view's. A drag and an arrow key
  start from where the tile is drawn, which the stage's hold may have moved from
  the saved place. The stage holds each tile from the place it is meant to stand
  at, not from where an earlier hold left it, so a tile a narrow window pushed in
  returns when the window grows back; under 900 px it holds nothing. A drag whose
  bar loses pointer capture (the tile left the page) ends there.
- "Me" is the account email plus the aliases ticked in Settings. An owner or
  subscriber filter for me matches any of them; a write of me writes the email.
- Each list keeps its tab, filters, grouping, sort, view (List, or Streams or
  Outline on Issues; Columns lives in the hash), pages loaded and focused row
  in `sessionStorage` (`trackinizer.v2.list.<id>`), so Back returns to it as it
  was.
- The console's views are one JSON list in `localStorage` per signed-in email
  (`trackinizer.v2.console.<email>`), as drafts are: each view's id, name, pin,
  start time, agents, rooms, CLIs, level and range. A view saves itself on every
  change; a new "Untitled view" is stored on its first. Pinned views list first,
  then the rest, each newest first. The tab's open view is the stored one with
  the id in `sessionStorage` (`trackinizer.v2.console.open`), so a reload opens
  it again, another tab's edit shows, and another tab's delete opens the first
  view left; a tab with none opens the first view. A stored view of another
  shape, with a time that is not one, or with a range that ends before it
  starts, is left out. When the browser refuses to store, or denies storage
  altogether, the open view still changes, unsaved. The console's feed pages and
  facets are read through the query cache under `["console", "feed", read]` and
  `["console", "facets", window]`; live, the facets are read again on a stream
  batch at most every 10 s, and every minute. After a
  gap in the stream the feed reads its newest page again too, and each record it
  reads again replaces the one held under the same key.
- The graph keeps its node limit, the kinds and statuses it hides, the edge
  kinds a focus does not walk, Only these, and whether the key and the roots
  list show in `sessionStorage` (`trackinizer.v2.graph`). Its focus, hops and
  grouping live in the hash: they say what is looked at, and a link carries
  them; whether a panel shows is the tab's preference, as the key's is.
- So is whether each panel that collapses is collapsed, in `sessionStorage`
  under `trackinizer.v2.panel.<id>`: `app.sidebar`, `peek`, `graph.results`,
  `detail.rail`, `console.rail` and `visuals.configure`. A new tab starts with
  each expanded, and Configure's closed.
- Drafts live in their editor's state, never in a cached row, so a refetch
  leaves them alone.
- The canvas's state is `["workspace", id]` in the cache, filled by the shell's
  workspace events stream (`newerWorkspace` keeps the newest revision) and, once,
  by a read. A conversation's records are `["chat", "records", session]`, its head
  `["chat", "head", id]` and its history `["chats"]`. What is not records (how
  often the stream opened, each canvas's open conversation and a Continue in Chat
  request) is the shell's `ChatFeed`
  (`src/visuals/chatFeed.ts`), above Chat, which is a lazy chunk that may load
  after a frame arrived and mount again.

## The stream

`openStream` (`src/api/stream.ts`) opens one `EventSource` on
`/api/web/subscribe`. Each frame is `data: {"id": "<subject uuid>"}`, with no
event name or resume: one frame for every `change_log` row, and one with the
session's id for every transcript append. Metric writes, and user, token and
allowlist writes send none. The server
also sends an SSE comment when the stream opens and after every 25 s without a
frame, which `EventSource` ignores: a proxy in front of production holds the
headers until the first body byte, and the edge answers 524 after 125 s without
one and cuts a stream idle for 125 s. The
browser reconnects on its own after a drop. When the server refuses the stream
outright, the wrapper refetches the profile (a 401 then ends the session) and
tries again after 1, 3 and 10 s, then every 30 s. The server checks the stream's
credential only when it opens, so a revoked key or a disabled user keeps an open
stream until it drops, and its reconnect is refused.

To test what the proxy path does to a stream, without a redeploy, use
`scripts/probe_stream.py`. It asks `GET /api/web/subscribe/probe` for frames on
a schedule it sets and prints when the headers and each frame arrived against
when the server sent them, and whether the server or something else ended the
stream. `--via` sends the same request to a hop behind the edge (through an ssh
tunnel), which tells which hop holds headers or cuts an idle stream; see its
`--help`.

`src/live/` applies the frames:

- Ids are collected for one second and deduplicated, then every mounted query
  gets the batch. Each query runs one update at a time; ids that arrive during
  one wait for the next, and a failed update keeps its ids and tries again
  after 1, 3 and 10 s, then every 30 s.
- A list refetches the rows it holds that are on screen, with its own kinds and
  filters plus a `seq_range` of those rows; a row that does not come back no
  longer matches, and dims. Held rows off screen are marked stale and refetch
  when they scroll into view. Ids it does not hold wait for a membership check,
  at most one every 2 s: its kinds and filters plus an `id` filter naming up to
  63 ids by their last seven hex digits. Over 100 waiting ids, it reloads its
  first page instead. New rows wait behind an "N new" pill unless the list is at
  its top and idle.
- An open detail refetches when the batch has its id (everything keyed by the
  id: detail, evidence confidence, metrics, a transcript's parts listing, the
  graph preview, so an edge added or removed redraws it; each part then reads
  only what was appended) or a neighbour it shows (detail and evidence
  confidence).
- Activity asks for its tab's changes since the newest it shows, one request for
  every kind of the tab, at most every 2 s; an answer of 50 or more reloads the
  tab's first page.
- Search results are a snapshot of when the search ran and never refetch.
- The graph reads its whole answer again on any batch, at most every 2 ms for
  each node it holds and never sooner than 2 s (2 s up to 1,000 nodes, 10 s at
  5,000, 40 s at 20,000) after its last read, the view's own first read
  included, and at once on a gap: a frame's id may be a new inquiry, one past
  the limit, an edge change or a purge, and only the whole answer tells.
- Gap recovery: the stream has no resume. The entry chunk (`src/main.tsx`)
  opens it before the first reads, and the server listens before it says open,
  so a read that starts after the open misses nothing. On the first open only
  the queries whose reads started before it (main.tsx's first reads, or views
  mounted earlier) reload their first page and treat what they hold as changed.
  After every reconnect, and when a tab hidden for more than 30 s comes back,
  every query does. A hidden tab fetches nothing and only collects ids.
- After the stream has been down 10 s, a bar says live updates are paused.

### The workspace events stream

With the canvas on, the tab has one stream, and it is not `/api/web/subscribe`:
`CanvasStream` (`src/app/canvasStream.tsx`), above the routes, creates the user's
workspace and opens `openWorkspaceEvents` (`src/api/workspaces.ts`), one
`EventSource` on `/api/workspaces/{id}/events`. It carries the inquiry ids too,
so the live layer takes them from it, and the early `/api/web/subscribe` stream
that `main.tsx` opens before the profile is known is closed once the profile
says the canvas is on. A user who opted out keeps `/api/web/subscribe` as above.
The stream reconnects as `openStream` does (both share `openEvents` in
`src/api/stream.ts`): the browser reconnects by itself, and after a refusal the
wrapper tries again after 1, 3 and 10 s, then every 30 s. It outlives the canvas,
which unmounts on Settings and Admin. Each open, and each drop and refusal, goes
to the live layer, which recovers a gap as it does for `/api/web/subscribe`
(above). Every frame is JSON with `t`, the server's epoch milliseconds:

| Frame | Does |
|---|---|
| `{type: "workspace", state, t}` | The canvas as it stands: on every open, after every applied operation, and when the partner changes. Applied through `newerWorkspace`, so a frame never regresses a revision. |
| `{type: "navigate", route, t}` | An agent moved the page: the browser goes to `route` when `parseHash` accepts it. |
| `{type: "highlight", ids, t}` | An agent pointed at inquiries: the tab's highlight store (`app/highlights.ts`) takes `ids` as its marks, replacing the last; `[]` clears. The graph rings those nodes, list rows and the rail's peers take `is-highlighted`, and a record page marks its own header. Not state: a closed stream clears them. |
| `{type: "changed", id, t}` | An inquiry changed, as `/api/web/subscribe` says it. A record appended to a science chat's session changes it, so an open Chat reads what the session gained. |

A frame of another shape is logged and skipped. There is no polling beside the
stream, and no read of the canvas at boot: the stream's opening frame is the
canvas, and the canvas reads only after a refused write. A conversation's
records are read once at mount and then when the live layer reports the session
changed or a gap.
While the stream is down the live layer shows the paused bar, as it does for
`/api/web/subscribe`. The canvas loads every renderer's chunk and the record
view as soon as it is up (`preloadRenderers`, `src/visuals/registry.tsx`): a
first render that suspends holds its fallback for React's 300 ms throttle, which
was the 305 to 335 ms the first show of every visual type took before.

Metrics and transcripts are not on the stream: they refresh when their row's id
arrives, on open, and from their Refresh button.

## Deep links

The app lives at `https://<host>/app/`, and every view is a hash route
(`src/router/route.ts`):

| Hash | Opens |
|---|---|
| (empty) | the graph |
| `#/list/<Kind>` | one kind's list |
| `#/list/<Kind>?view=columns&path=<seq>,…` | an Issue list's Columns, the Issues selected column by column |
| `#/ref/<Kind>/<seq>` | an inquiry, found by `GET /api/inquiries/{Kind}/{seq}` |
| `#/lookup/<uuid>` | an inquiry, read with `GET /api/web/get/{id}` |
| `#/activity` | Activity |
| `#/console` | the live feed across sessions, narrowed by a saved view, and a line for messaging agents |
| `#/graph` | the graph of the newest 1,000 inquiries (or the count chosen), each followed by the older ones it links to, and the edges between them, gathered into one island per root |
| `#/graph?focus=<Kind>/<seq>&hops=<1\|2\|3\|all>` | the graph lit within that many hops of one inquiry; `focus` also takes a UUID, and `hops` is 1 when left out |
| `#/graph?group=none` | the graph as one web, not gathered by root, after any focus and hops |
| `#/search/<q>` | the search page for `q` |
| `#/new/<Kind>` | the create form, over the last view |
| `#/settings`, `#/admin` | Your settings; Admin (non-admins see "Admins only") |

The old UI's links still work: `#/inquiry/<uuid>` opens the lookup, `#/recent`
Activity, `#/search?q=` the search page, and a bare `#/list` or `#/new` the first
kind's list or create form. `#/graph?group=root`, from before grouping was the
default, opens the graph grouped. Kinds match in any case and are
rewritten as the server spells them; a hash that is not canonical is replaced in
place. The path is split into parts before any part is decoded, so an encoded
`?` or `/` stays in its part. A hash that names nothing shows a not-found view.

A signed-out request for `/app/` gets the server's 302 to the login page. The
browser keeps the hash across that redirect, and the login page puts it back on
`next`, so sign-in returns to the same view. A 401 that the running app sees
keeps the hash its own way, as described under [client state](#client-state).

## Running it

An installed trackinizer serves the build packaged with it: run
`trackinizer --no-auth`, then open http://127.0.0.1:8765/app/. The rest of this
section runs the app from source. Its commands run from this directory unless
they say otherwise.

Node comes from the `nodejs-wheel-binaries` wheel in the `test` dependency
group (`uv sync --frozen --group test`). Run npm through `./npm`, which puts
that Node first on `PATH`:

```sh
./npm ci                # install node_modules from package-lock.json
```

The local preview is a trackinizer server on a persistent PGlite database with
`--no-auth`, serving the latest build at `/app/`, with `vite build --watch`
keeping `dist/` current:

```sh
scripts/preview.sh      # then open http://127.0.0.1:8765/app/
scripts/seed_local.py   # seed a representative graph; safe to rerun
scripts/seed_local.py --writer --interval 1   # then keep editing, for live checks
```

`PORT` changes the port and `PREVIEW_DATA` the database directory, by default
`/opt/scratch/runs/trackinizer-web-local`, which a machine without
`/opt/scratch` must change; two servers cannot share one. `--no-auth` makes
every request an admin, so never expose it.

The Vite dev server serves the app with hot reload and proxies `/api` and
`/auth` to `TRACKINIZER_WEB_API_URL` (default the preview on port 8765), adding
`Authorization: Bearer $TRACKINIZER_WEB_API_TOKEN` when it is set. It does not
read the trax CLI's `TRACKINIZER_URL` or `TRACKINIZER_TOKEN`. Against a shared
server, use a token capped at the viewer role.

```sh
./npm run dev           # http://127.0.0.1:5173/app/
```

To serve a build you already have, on an in-memory database beside a running
preview, from the repository root:

```sh
uv --quiet run --frozen python -m trackinizer.server --ephemeral --no-auth \
  --app-dir trackinizer/web/dist --port 8766
```

`scripts/measure.py` times the reads the app makes, with GET requests only:
against the active trax profile's server by default, or against the preview
with `--url http://127.0.0.1:8765`. Reports go to
`/opt/scratch/artifacts/trackinizer-web/measure/`.

## Testing

The web checks, as CI runs them:

```sh
./npm run typecheck     # tsc --noEmit over src, e2e and scripts
./npm test              # Vitest: unit and screen tests, each under 100 ms
./npm run build         # vite build into dist/, with dist/version.json
./npm run size          # first-load JavaScript under 250 KB gzip
./npm exec -- playwright install chromium   # once per machine
./npm run e2e           # build into dist-e2e/<port>, then Playwright
```

Every test `npm test` runs takes under 100 ms warm on x86. A test that cannot
is tagged `manual` (`test(name, { tags: ["manual"] }, …)`), which `npm test`
skips; `./npm run test:manual` runs those and the build tier. Nothing runs them
automatically, so run them yourself, like the e2e suite.

`npm run e2e` starts its own server (`--ephemeral --no-auth`) on
`TRACKINIZER_E2E_PORT`, 8797 by default; give each concurrent run its own port.
It passes extra arguments to Playwright, so `./npm run e2e -- e2e/boot.spec.ts`
runs one spec. The live and responsiveness suite, `e2e/live/`, starts a server
of its own and drives it through `scripts/writer.py`, the Python client as a
second user. `src/writes/cas.live.test.tsx` runs only with
`TRACKINIZER_LIVE_URL` naming a disposable local server.

`e2e/live/canvas-push.spec.ts` is the canvas's push bar: it applies 20
operations per visual type through `POST /api/workspaces/{id}/operations`, and
20 navigations, waits for each paint (`trackinizer.timings()`), and writes p50,
p90 and max of `paint wall time - frame t` (and, for the context graph, timeline
and Artifact, of the data's paint, and for a navigation, of the target page drawn
with its data) to
`/opt/scratch/artifacts/trackinizer-web/canvas-chat/timings/canvas-push.json`.
It fails when any p50 or p90 reaches 100 ms. The canvas is on by default, so the
specs that watch the stream (`stream.spec.ts`, `session-chat.spec.ts`, `live/`)
take that path: they wait on whichever stream the
app uses (`streamOpened`, `abortStream` in `e2e/fixtures.ts`), the canvas's
events stream here, and `live/` runs on a 1640 x 790 screen that leaves its page
the size it had without the canvas. Every other spec measures the page's own
geometry and keys, which a Chat pane beside it changes, so `e2e/fixtures.ts`
turns the suite's one user's canvas off for it (`test.use({ canvas: true })` asks
for it on); `e2e/canvas-optout.spec.ts` covers a user who opted out, with
`/api/web/subscribe` and no canvas. `e2e/canvas-layout.spec.ts` shows
Chat with a side or a main visual at 1280 x 800, where nothing may scroll
sideways, and at 390 px, where nothing may either and the message box and Send
must be on screen.

`e2e/a11y.spec.ts` runs axe's WCAG 2.0 and 2.1 A and AA rules on each main view
in both themes and fails on any violation.

The Python tests, from the repository root:

```sh
uv --quiet run --frozen pytest -m compute_large_fixture \
  trackinizer/web/openapi_drift_test.py
uv --quiet run --frozen pytest -m db_pglite \
  trackinizer/web/fixtures_test.py
uv --quiet run --frozen pytest trackinizer/web
```

The first two run in the slow and integration tiers, which the default `-m`
leaves out. After a change to the server's routes, rewrite the committed schema
(`scripts/openapi_dump.py`) and run `./npm run typecheck`. After a change to what
a route answers, rewrite the fixtures and review their diff:

```sh
TRACKINIZER_WEB_FIXTURES_UPDATE=1 uv --quiet run --frozen pytest -m db_pglite \
  trackinizer/web/fixtures_test.py
```

The e2e specs import `test` and `expect` from `e2e/fixtures.ts`, not from
`@playwright/test`: a test fails when its page logs a `console.error` or throws
an error nothing caught. A test that provokes one on purpose allows it, with a
comment saying why: `allowErrors(failedResource("/api/me/profile", 401))` for
the browser's own line about a failed request.

## Updating the screenshots and the demo video

The package README opens with a demo, `../docs/screenshots/demo.webp`, a short
animated WebP that links to the full video, and shows five screenshots,
`../docs/screenshots/*.webp`, linked by file name. `scripts/screenshots.ts`
takes the screenshots again from synthetic records, and `scripts/demo_video.ts`
records the video:

```sh
./npm exec -- playwright install chromium   # once per machine
brew install webp ffmpeg                     # once: cwebp, img2webp, ffmpeg (apt install webp ffmpeg on Linux)
./npm run screenshots                        # rewrites ../docs/screenshots/*.webp
./npm run screenshots -- DIR                 # or writes them into DIR
./npm run demo-video -- DIR                  # demo.mp4, demo.webm, poster.*, teaser.webp into DIR
```

Each builds the app into `dist-screenshots/` and starts its own server, with
`--no-auth` and a fresh data directory: the screenshots on port 8812, the video
on 8829. `scripts/seed_screenshots.py` seeds it, first with a graph of some
5,000 nodes (`scripts/seed_graph.py`, from the shape alone in
`scripts/graph_structure.json`, with invented titles where text shows), then
three research efforts and three agents talking in two rooms; every person in it
is an `example.com` address. The screenshots are the graph grouped by root, the
console, and one Belief, Paper and Experiment, at 1280 by 800 and twice the
scale, in the dark theme, each lossless WebP, captured once its requests have
answered and its fonts have loaded, and the graph once its layout has stopped.
Two runs differ only in what depends on when they ran: a record's ID, Created
and Updated, and the console's times, as the seed's conversations end when it
runs. The video is under a minute: the graph replayed in five seconds, a root's
island and a Belief in it, Issues and Artifacts, and a message to an agent in
the console, which a stand-in answers. Look at every image and the whole video
before committing any of it.

The full video is too large for git, so it is published on its own, under a
key named for its contents that never changes, and the README links to it:
`https://media.rekursiv.ai/trackinizer-demo/<fingerprint>/demo.mp4`, the
fingerprint being the first 24 hex digits of the SHA-256 of the five files'
`sha256sum` list. A new video gets a new key; `demo.webp` is its `teaser.webp`.
## Debugging

- Every call sends a fresh `X-Request-ID` (a UUID), which the server echoes and
  logs on its request line (`event=trackinizer_request_completed ...
  request_id=<id>`).
- A failed request logs one console warning, in the server's logfmt shape:
  `trackinizer request.failed method=PUT path=/api/... status=503 ms=84
  request_id=<id> attempt=2`. `attempt` counts the same request failing in a
  row (method, URL and idempotency key), so retries count up. Uncaught errors,
  unhandled rejections and render crashes log console errors; a render crash
  shows a screen with Copy details and Reload in place of a blank page.
- Every failure the app shows has Copy details, the copy icon beside it. It
  copies the message, the time, the page's hash (search text left out), the
  build's commit, the browser, the failed request (method, path, status, request
  id, attempt, duration) and the last 200 events.
- Those events (`src/debug/log.ts`) are boot (commit and role), navigation,
  every request, a 401's end of session, a 403, each write's send, automatic
  retry (attempt, wait, key), replay, conflict and final failure, and the errors
  above. Warnings and errors always reach the console. The rest reach it with
  debug on: open `/app/?debug=1`, or run
  `localStorage.setItem("trackinizer.v2.debug", "1")` and reload
  (`removeItem` turns it off).
- In the devtools console, `trackinizer.events()` returns the events and
  `trackinizer.details()` the Copy details text. While the graph is open,
  `trackinizer.graph()` returns what it draws: each node, with its screen
  position, and edge, hidden or not, the selection and the focus, since its
  canvas cannot be read. When the browser refuses the
  clipboard (a page over plain http from another machine), the button says
  "Not copied: see the console", and `trackinizer.details()` gives the text it
  could not copy; the console only says where to find it.
- `trackinizer.timings()` returns the canvas's timing marks, the last 200 pushed
  frames, each with its `t` (server epoch milliseconds), when it arrived
  (`received`, `performance.now()`, and `receivedWall`, epoch milliseconds) and
  its marks. A `workspace` frame has its `revision` and the marks of the visuals
  it changed: `paint` is the pane's content committed and painted (a lazy
  renderer's mark waits for the renderer, not its fallback), and `data`, for the
  context graph, the timeline and the Artifact, is its data painted. A
  `navigate` frame has its `route`, and one `page` mark: the target's detail
  drawn with its data (other views have no mark). Each mark is taken in the first
  task after the frame that follows the commit, so `wall - t` is the time from
  the server accepting an operation to the pixels when browser and server share
  a clock. A change is to a visual's type, version, placement, record or
  parameters; moving a floating pane is none, and a revision no frame carried
  (the browser's own write) has no marks.
- Nothing logged holds a token, a cookie or `Authorization` value, a request
  body, a URL's query, or text a user wrote (titles, descriptions, search text):
  events carry ids, kinds, fields, routes, statuses and timings.
- To find the server's line for a request id, on the production host:
  `journalctl -u trackinizer | grep <id>`. Production logs at WARNING, which
  keeps only failures (5xx and exceptions); a 4xx's line is INFO and is not
  kept, so its Copy details is the record. A local server started with
  `--log-level INFO` keeps every line.
