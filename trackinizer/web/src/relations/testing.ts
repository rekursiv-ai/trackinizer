// Test helpers for relation editing; only tests import this file.
import { QueryClient } from "@tanstack/react-query";
import type { DetailRow, Peer } from "../api/detail";
import { type Sent, stubFetch } from "../api/testing";
import { detail, row } from "../detail/testing";

/** One stored edge, child to parent, with the annotations set on it. */
export type StoredEdge = { from: string; kind: string; to: string } & Pick<Peer, "note" | "valence" | "priority" | "labels">;

/**
 * A server holding `rows` and the `edges` between them, as trackinizer answers:
 * each row's detail (its edges both ways, with their annotations), its row and
 * `Kind#seq` lookup, and a title search per kind. It applies edge writes (an
 * add that exists is a no-op; `PUT` sets and `DELETE` clears an annotation;
 * `PATCH` adds or removes one label) and batch creates. `hold` keeps a request
 * waiting.
 */
export function serveGraph(rows: readonly DetailRow[], edges: readonly StoredEdge[] = []) {
  const held: { method: string; path: string; until: Promise<void> }[] = [];
  const server = {
    /** Keep the next `method` request to `path` waiting until the returned function lets it through. */
    hold(method: string, path: string): () => void {
      let release = () => {};
      held.push({ method, path, until: new Promise((resolve) => (release = resolve)) });
      return () => release();
    },
    rows: [...rows],
    edges: edges.map((edge) => ({ ...edge })),
    sent: [] as Sent[],
    /** Answers for the next writes, in order; one that returns null lets its write through. */
    answers: [] as (() => Response | null)[],
    /** The writes sent, as `METHOD path` and body. */
    writes: () =>
      server.sent.filter((request) => request.method !== "GET").map(({ method, path, body }) => ({ call: `${method} ${path}`, body })),
    /** The edge from `from` to `to`, of `kind`. */
    edge: (from: DetailRow, kind: string, to: DetailRow) =>
      server.edges.find((edge) => edge.from === from.id && edge.kind === kind && edge.to === to.id),
  };
  const byId = (id: string) => server.rows.find((candidate) => candidate.id === id);
  server.sent = stubFetch(async (request) => {
    const { pathname, searchParams } = new URL(request.url);
    const waiting = held.findIndex((hold) => hold.method === request.method && hold.path === pathname);
    if (waiting >= 0) await held.splice(waiting, 1)[0]!.until;
    if (request.method === "GET") return read(pathname, searchParams);
    const scripted = server.answers.shift()?.();
    if (scripted) return scripted;
    const body = (await request.clone().json()) as { [key: string]: unknown };
    if (pathname === "/api/inquiries/batch") return Response.json(createBatch(body));
    const [, from, kind, to, annotation] = /^\/api\/edges\/([^/]+)\/([^/]+)\/([^/]+)(?:\/(\w+))?$/.exec(pathname) ?? [];
    const at = server.edges.findIndex((edge) => edge.from === from && edge.kind === kind && edge.to === to);
    if (!annotation && request.method === "POST") {
      if (at >= 0) return Response.json({ change_id: null, created: false });
      server.edges.push({ from: from!, kind: kind!, to: to! });
      return Response.json({ change_id: `c${server.sent.length}`, created: true });
    }
    if (at < 0) return Response.json({ detail: "edge not found" }, { status: 404 });
    const edge = server.edges[at]!;
    if (!annotation) server.edges.splice(at, 1);
    else if (annotation === "labels") {
      const labels = (edge.labels ?? []).filter((label) => label !== body.value);
      edge.labels = body.op === "add" ? [...labels, String(body.value)] : labels;
    } else if (request.method === "PUT") Object.assign(edge, { [annotation]: body.value });
    else delete edge[annotation as "note" | "valence" | "priority"];
    return Response.json({ change_id: `c${server.sent.length}`, created: false });
  });

  function read(path: string, query: URLSearchParams): Response {
    for (const self of server.rows) {
      if (path === `/api/web/get/${self.id}`) return Response.json(detailOf(self));
      if (path === `/api/inquiries/${self.kind}/${self.seq}`) return Response.json({ id: self.id });
      if (path === `/api/inquiries/${self.id}/confidence`) return Response.json({ confidence: 0.5 });
      if (path === `/api/inquiries/${self.id}`) return Response.json(self);
    }
    if (path === "/api/web/search") {
      const q = query.get("q")!.toLowerCase();
      const hits = server.rows.filter((row) => row.kind === query.get("kind") && row.title.toLowerCase().includes(q));
      return Response.json(hits.slice(0, Number(query.get("limit"))));
    }
    return Response.json({ detail: "not found" }, { status: 404 });
  }

  function detailOf(self: DetailRow) {
    const out: { [kind: string]: Peer[] } = {};
    const back: { [kind: string]: Peer[] } = {};
    for (const { from, kind, to, ...annotations } of server.edges) {
      if (from === self.id) (out[kind] ??= []).push(peerOf(byId(to)!, annotations));
      if (to === self.id) (back[kind] ??= []).push(peerOf(byId(from)!, annotations));
    }
    return detail(self, { edges: out, backlinks: back });
  }

  function createBatch(body: { [key: string]: unknown }): { ids: string[] } {
    const items = body.items as { kind: string; title: string }[];
    const created = items.map(({ kind, title, ...fields }) => {
      const next = row(kind, Math.max(...server.rows.map((candidate) => candidate.seq)) + 1, { title, ...fields });
      server.rows.push(next);
      return next;
    });
    for (const edge of body.edges as { edge_kind: string; from_index?: number; to_id?: string }[]) {
      server.edges.push({ from: created[edge.from_index!]!.id, kind: edge.edge_kind, to: edge.to_id! });
    }
    return { ids: created.map((made) => made.id) };
  }

  return server;
}

/** The far end of an edge as `/api/web/get` sends it: annotations only when set. */
function peerOf(self: DetailRow, annotations: Omit<StoredEdge, "from" | "kind" | "to">): Peer {
  const set = Object.entries(annotations).filter(([, value]) => value !== undefined && !(Array.isArray(value) && !value.length));
  return { id: self.id, kind: self.kind, seq: self.seq, title: self.title, status: self.status, ...Object.fromEntries(set) };
}

/** A cache that holds `rows` as one loaded list page, as the link picker's first rows. */
export function cacheWith(rows: readonly DetailRow[]): QueryClient {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["inquiries", "list", "loaded"], rows);
  return queryClient;
}
