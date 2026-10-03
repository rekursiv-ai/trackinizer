// Test helpers for the detail view; only tests import this file.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { Change, Detail, DetailRow, Peer } from "../api/detail";
import type { Profile } from "../api/me";
import { type Sent, stubFetch } from "../api/testing";
import { type Meta, MetaContext, ProfileContext } from "../app/boot";
import { Session, SessionContext } from "../app/session";
import { CommandRegistry, CommandRegistryContext, Shortcuts } from "../commands/registry";
import { LiveContext } from "../live";
import type { LiveHub } from "../live/hub";
import { RouterProvider } from "../router/router";
import { ToastProvider } from "../ui/toast";
import { DetailView, type DetailTarget } from ".";

const ALL_KINDS = ["Issue", "Artifact", "Experiment", "Paper", "Belief", "CodeChange", "WebResult", "WebSearch", "AgentSession"];
const ARTIFACTS = ALL_KINDS.filter((kind) => kind !== "Issue");

/** The server's vocabulary, as the local preview's `/api/meta/*` returned it on 2026-09-26. */
export const META: Meta = {
  kinds: ALL_KINDS,
  enums: {
    status: ["active", "complete", "abandoned", "invalid"],
    judgement: ["proven", "disproven", "unproven", "undecidable"],
    issue_kind: ["feature", "bug", "task", "question"],
    publication_type: ["article", "inproceedings", "book", "thesis", "techreport", "misc"],
    edge_kind: ["narrows", "requires", "produced_by", "proves", "favors", "supersedes", "cites_paper"],
    inquiry_kind_all: ALL_KINDS,
  },
  fieldOwners: {
    issue_kind: "issue",
    validation: "issue",
    priority: "issue",
    codechanges: "experiment",
    outcome: "experiment",
    config: "experiment",
    abstract: "paper",
    authors: "paper",
    publication_type: "paper",
    venue: "paper",
    subvenue: "paper",
    publish_date: "paper",
    source: "paper",
    google_scholar_cluster_id: "paper",
    google_scholar_cites_id: "paper",
    judgement: "belief",
    confidence: "belief",
    sha: "codechange",
    url: "webresult",
    query: "websearch",
    provider: "websearch",
    cli: "agentsession",
    cli_session_id: "agentsession",
    started: "agentsession",
    rooms: "agentsession",
    opened_by_api_key_id: "agentsession",
  },
  edges: {
    narrows: { from_kinds: ["Issue"], to_kinds: ["Issue"], forward: "narrows", inverse: "narrowed_by" },
    requires: { from_kinds: ["Issue"], to_kinds: ["Issue"], forward: "requires", inverse: "required_by" },
    produced_by: { from_kinds: ALL_KINDS, to_kinds: ALL_KINDS, forward: "produced_by", inverse: "produces" },
    proves: { from_kinds: ARTIFACTS, to_kinds: ["Belief", "Experiment"], forward: "proves", inverse: "proved_by" },
    favors: { from_kinds: ARTIFACTS, to_kinds: ["Belief", "Experiment"], forward: "favors", inverse: "favored_by" },
    supersedes: { from_kinds: ALL_KINDS, to_kinds: ALL_KINDS, forward: "supersedes", inverse: "superseded_by" },
    cites_paper: { from_kinds: ["Paper"], to_kinds: ["Paper"], forward: "cites", inverse: "cited_by" },
  },
};

/** The signed-in user the detail renders for: a writer, so its editors show. */
export const PROFILE: Profile = { user_id: "u1", email: "ada@example.com", name: "Ada", role: "writer", last_login: null, visual_workspace_enabled: false };

/** A stable, distinct UUID for test number `n`. */
export function uuid(n: number): string {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

/**
 * A row as `/api/web/get` sends one since fix R17 (8ef72ccfeb): every field of
 * the kind present, `null` when unset, then `fields` set.
 */
export function row(kind: string, seq: number, fields: { [field: string]: unknown } = {}): DetailRow {
  return {
    id: uuid(seq),
    kind,
    seq,
    title: `${kind} number ${seq}`,
    status: "active",
    owner: null,
    account: "ada@example.com",
    description: null,
    labels: null,
    subscribers: null,
    marginal_cost: { agent_usd: 0, resource_usd: 0 },
    created: "2026-09-20T10:00:00+00:00",
    modified: "2026-09-24T10:00:00+00:00",
    ...Object.fromEntries((KIND_KEYS[kind] ?? []).map((key) => [key, null])),
    ...fields,
  };
}

/**
 * Each kind's own keys on `/api/web/get`'s `self`, read from a PGlite server at
 * 8ef72ccfeb (one row of each kind, 2026-09-26).
 */
const KIND_KEYS: { readonly [kind: string]: readonly string[] } = {
  Issue: ["issue_kind", "priority", "validation"],
  Artifact: [],
  Experiment: ["codechanges", "config", "outcome"],
  Paper: [
    "abstract",
    "authors",
    "google_scholar_cites_id",
    "google_scholar_cluster_id",
    "publication_type",
    "publish_date",
    "source",
    "subvenue",
    "venue",
  ],
  Belief: ["confidence", "judgement"],
  CodeChange: ["sha"],
  WebResult: ["url"],
  WebSearch: ["provider", "query"],
  AgentSession: ["cli", "cli_session_id", "ended", "opened_by_api_key_id", "rooms", "started"],
};

/** A neighbour across one edge. */
export function peer(kind: string, seq: number, annotations: Partial<Peer> = {}): Peer {
  return { id: uuid(seq), kind, seq, title: `${kind} number ${seq}`, status: "active", ...annotations };
}

/**
 * Change number `n` about `subject`, made `n` minutes past 10:00 UTC on
 * 2026-09-24. A test that shows its time pins the clock, or the time reads as a
 * date once that day is a week past.
 */
export function change(subject: DetailRow, n: number, fields: Partial<Change>): Change {
  return {
    id: uuid(10_000 + n),
    created: `2026-09-24T10:${String(n).padStart(2, "0")}:00+00:00`,
    actor: "ada@example.com",
    kind: "created",
    subject_kind: subject.kind,
    caused_by: null,
    reason: "",
    old: {},
    new: {},
    ...fields,
  };
}

/** A whole `/api/web/get` answer around `self`. */
export function detail(self: DetailRow, parts: Partial<Omit<Detail, "self">> = {}): Detail {
  return { self, edges: {}, backlinks: {}, changes: [], ...parts };
}

/**
 * Serve `details` (and each row's `Kind#seq` lookup) through a stubbed `fetch`,
 * with `confidence` for the evidence route; anything else answers 404.
 */
export function serveDetails(details: readonly Detail[], confidence = 0.5): Sent[] {
  return stubFetch((request) => {
    const path = new URL(request.url).pathname;
    for (const { self, ...rest } of details) {
      if (path === `/api/web/get/${self.id}`) return Response.json({ self, ...rest });
      if (path === `/api/inquiries/${self.kind}/${self.seq}`) return Response.json({ id: self.id, kind: self.kind });
      if (path === `/api/inquiries/${self.id}/confidence`) return Response.json({ confidence });
    }
    return Response.json({ detail: "not found" }, { status: 404 });
  });
}

/**
 * Render the detail for `target` with the app's providers, and its shortcuts
 * bound; `queryClient` is its cache, `profile` the signed-in user, `session`
 * the one a 401 would end, `commands` the registry the palette would list, and
 * `hub` the live stream's, if it is to keep the detail current.
 */
export function renderDetail(
  target: DetailTarget,
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
  {
    profile = PROFILE,
    session = new Session(() => {}),
    commands = new CommandRegistry(),
    hub = null,
  }: { profile?: Profile; session?: Session; commands?: CommandRegistry; hub?: LiveHub | null } = {},
) {
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionContext value={session}>
        <CommandRegistryContext value={commands}>
          <ToastProvider>
            <MetaContext value={META}>
              <ProfileContext value={profile}>
                <RouterProvider kinds={META.kinds}>
                  <LiveContext value={hub}>
                    <Shortcuts />
                    <DetailView target={target} />
                  </LiveContext>
                </RouterProvider>
              </ProfileContext>
            </MetaContext>
          </ToastProvider>
        </CommandRegistryContext>
      </SessionContext>
    </QueryClientProvider>,
  );
}
