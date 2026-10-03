// The first reads, which the entry chunk (src/main.tsx) starts before React DOM
// and the app's code arrive, so that data and code travel together: React's
// "render-as-you-fetch" (https://react.dev/reference/react/Suspense). Each waits
// under the TanStack Query key of the query that would make it, whose first fetch
// takes it (`prefetched`).
//
// The keys are written out here, not imported from the query options that own
// them (src/app/boot.ts, src/lists/pages.ts, src/detail/queries.ts,
// src/graph/live.ts): those import
// React and TanStack Query, which would then load with the entry, and the reads
// would wait for them. src/app/prefetch.test.tsx fails when a key drifts.
import { findRef, getDetail } from "../api/detail";
import { getGraph } from "../api/graph";
import { INQUIRY_ROW_FIELDS, inquiryKinds, isInquiryKind, listInquiries } from "../api/inquiries";
import { getProfile } from "../api/me";
import { getEdgeTopology, getEnums, getFieldOwners } from "../api/meta";
import { parseHash, type Route } from "../router/route";

/**
 * Start the boot reads and the data of the view `hash` opens: the graph (the
 * empty hash's), a list's first page, or a detail (after its `Kind#seq` lookup).
 *
 * The kinds in the hash name its view, as they will once the server's kinds
 * arrive. A hash that names none that way (a kind spelled in another case, or a
 * bare `#/list`, which opens the first of the server's kinds) starts its view's
 * data once the server's kinds have come.
 *
 * What no query has taken 10 s after the call is dropped. Its view went away
 * before it showed, and a query of the same key that mounts later would show an
 * answer that old until the stream layer had caught it up.
 */
export function prefetch(hash: string): void {
  const enums = start(["meta", "enums"], () => getEnums());
  start(["meta", "fields"], () => getFieldOwners());
  start(["meta", "edges"], () => getEdgeTopology());
  start(["me", "profile"], () => getProfile());
  // A graph's focus names its kind in the query.
  const route = parseHash(hash, hash.split(/[/?&=]/).filter(isInquiryKind));
  if (route.name !== "notFound") startView(route);
  // Caught after the handler, not beside it: kinds the router cannot read (server
  // drift) throw in it. The app's boot takes the same answer and shows that on
  // its crash screen; uncaught here, it would also be an unhandled rejection.
  else enums.then(({ inquiry_kind_all }) => startView(parseHash(hash, inquiry_kind_all ?? []))).catch(() => {});
  setTimeout(() => STARTED.clear(), 10_000);
}

/**
 * The read `prefetch` started for the query `key`, once; otherwise `read()`.
 *
 * A query's `queryFn` calls it, so its first fetch takes the read already on its
 * way, and every later fetch (a refetch, a retry) reads afresh. A started read
 * that failed fails the fetch as its own read would: a 401 still ends the session.
 */
export function prefetched<T>(key: readonly unknown[], read: () => Promise<T>): Promise<T> {
  const name = JSON.stringify(key);
  const started = STARTED.get(name);
  STARTED.delete(name);
  if (!started) return read();
  TAKEN.set(name, { key, at: started.at });
  // The key names the read, as it names the query's data in TanStack's cache.
  return started.promise as Promise<T>;
}

/**
 * When the earliest read `prefetched` handed a query whose key `matches` started
 * (`Date.now()` time), once. A live view asks as it mounts: a read that started
 * before the stream opened may have missed a change, and catches up on the gap.
 */
export function takeReadAt(matches: (key: readonly unknown[]) => boolean): number | undefined {
  let earliest: number | undefined;
  for (const [name, { key, at }] of TAKEN) {
    if (!matches(key)) continue;
    earliest = Math.min(earliest ?? at, at);
    TAKEN.delete(name);
  }
  return earliest;
}

/** Start the data of `route`'s view. */
function startView(route: Route): void {
  switch (route.name) {
    case "list":
      startList(route.kind);
      return;
    case "ref":
      start(["ref", route.kind, route.seq], () => findRef(route.kind, route.seq)).then(
        (id) => void start(["detail", id], () => getDetail(id)),
        () => {},
      );
      return;
    case "lookup":
      start(["detail", route.id], () => getDetail(route.id));
      return;
    case "graph":
      startGraph();
      return;
  }
}

/**
 * Start the graph's read as the view asks for it on a tab that has not kept its
 * state (`initialState` in src/graph/index.tsx): the newest 1,000 inquiries. A
 * kept state may ask for any limit.
 */
function startGraph(): void {
  try {
    if (sessionStorage.getItem("trackinizer.v2.graph") !== null) return;
  } catch {
    return;
  }
  start(["graph", 1000], () => getGraph(1000));
}

/**
 * Start `kind`'s first page as its list asks for it on a tab that has not kept
 * the list's state (`initialState` in src/lists/index.tsx): Papers open on All,
 * every other kind on Active, 50 rows. A kept state may ask for anything.
 */
function startList(kind: string): void {
  try {
    if (sessionStorage.getItem(`trackinizer.v2.list.${kind}`) !== null) return;
  } catch {
    return;
  }
  const filters = kind === "Paper" ? [] : [{ field: "status", op: "is", value: "active" }];
  // Async, so a kind this build does not know rejects the read rather than
  // throwing out of `prefetch`.
  start(["inquiries", "list", filters, kind, 50, 0, null], async () =>
    listInquiries({ kinds: inquiryKinds([kind]), filters, limit: 50, offset: 0, fields: INQUIRY_ROW_FIELDS }),
  );
}

/** Start `read` for the query `key`; until a query takes it, its failure is nobody's. */
function start<T>(key: readonly unknown[], read: () => Promise<T>): Promise<T> {
  const at = Date.now();
  const promise = read();
  promise.catch(() => {});
  STARTED.set(JSON.stringify(key), { promise, at });
  return promise;
}

/** The started reads no query has taken yet, by their key as JSON, with when each started. */
const STARTED = new Map<string, { readonly promise: Promise<unknown>; readonly at: number }>();

/** The started reads queries took, until a live view asks when they started (`takeReadAt`). */
const TAKEN = new Map<string, { readonly key: readonly unknown[]; readonly at: number }>();
