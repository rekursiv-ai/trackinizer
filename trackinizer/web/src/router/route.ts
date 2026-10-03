/** Where the app is: one of the deep links the address bar can hold. */
export type Route =
  /**
   * One kind's list: `#/list/Issue`. With `columns`, its Columns view, the seqs
   * selected column by column: `#/list/Issue?view=columns&path=21933,21934`.
   */
  | { readonly name: "list"; readonly kind: string; readonly columns?: readonly number[] }
  /** An inquiry by kind and seq: `#/ref/Issue/412`. */
  | { readonly name: "ref"; readonly kind: string; readonly seq: number }
  /** An inquiry by id: `#/lookup/<uuid>`. */
  | { readonly name: "lookup"; readonly id: string }
  | { readonly name: "activity" }
  /** The live feed across sessions, and a line for messaging agents: `#/console`. */
  | { readonly name: "console" }
  /**
   * The newest inquiries and the edges between them, drawn as a graph: `#/graph`,
   * the home view, which gathers each root's subgraph into an island. With
   * `focus`, the graph lights what lies within `hops` of one inquiry,
   * `#/graph?focus=Issue/21919&hops=2`; with `grouped: false`, it draws one web,
   * `#/graph?group=none`.
   */
  | { readonly name: "graph"; readonly focus?: GraphFocus; readonly grouped?: false }
  /** Your settings: `#/settings`. */
  | { readonly name: "settings" }
  /** Users and the allowlist: `#/admin`. It parses for anyone; the view refuses non-admins. */
  | { readonly name: "admin" }
  /** The search results page for a query: `#/search/<q>`. */
  | { readonly name: "search"; readonly q: string }
  /** The create form for one kind, over the last view: `#/new/Issue`. */
  | { readonly name: "new"; readonly kind: string }
  /** A hash that names nothing; kept as typed. */
  | { readonly name: "notFound"; readonly hash: string };

/** An inquiry the graph centres a focus on, by kind and seq or by id. */
export type FocusRef = { readonly kind: string; readonly seq: number } | { readonly id: string };

/** How far a graph focus reaches: 1, 2 or 3 edges, or its whole connected part. */
export type Hops = 1 | 2 | 3 | "all";

/** The inquiry a graph focus centres on and how far it reaches. */
export type GraphFocus = { readonly ref: FocusRef; readonly hops: Hops };

/** The hash for `route`: what the address bar shows and what links point at. */
export function formatRoute(route: Route): string {
  switch (route.name) {
    case "list": {
      const list = `#/list/${encodeURIComponent(route.kind)}`;
      if (!route.columns) return list;
      return `${list}?view=columns${route.columns.length > 0 ? `&path=${route.columns.join(",")}` : ""}`;
    }
    case "ref":
      return `#/ref/${encodeURIComponent(route.kind)}/${route.seq}`;
    case "lookup":
      return `#/lookup/${route.id}`;
    case "activity":
      return "#/activity";
    case "console":
      return "#/console";
    case "graph": {
      const params = [];
      if (route.focus) {
        const { ref, hops } = route.focus;
        params.push(`focus=${"id" in ref ? ref.id : `${encodeURIComponent(ref.kind)}/${ref.seq}`}`, `hops=${hops}`);
      }
      if (route.grouped === false) params.push("group=none");
      return params.length > 0 ? `#/graph?${params.join("&")}` : "#/graph";
    }
    case "settings":
      return "#/settings";
    case "admin":
      return "#/admin";
    case "search":
      return `#/search/${encodeURIComponent(route.q)}`;
    case "new":
      return `#/new/${encodeURIComponent(route.kind)}`;
    case "notFound":
      return route.hash;
  }
}

/**
 * The route a location hash names, given the server's inquiry kinds.
 *
 * The path is split from its query at the first literal `?`, and into parts at
 * each `/`, before any part is decoded, so an encoded `?` or `/` stays inside
 * its part (COLD-11). Kinds match case-insensitively and come back as the server
 * spells them; ids, in any case, come back in lower case, as the server's are.
 * An empty hash is the graph. The old UI's links still work: `#/inquiry/<uuid>`,
 * `#/recent`, `#/search?q=`, `#/new/<Kind>`, which v2 spells the same, and a
 * bare `#/list` or `#/new`, the first kind's. A list's Columns view keeps its
 * path in the query, `?view=columns&path=<seq>,<seq>`, and the graph its focus,
 * `?focus=<Kind>/<seq>` or `?focus=<uuid>`, its `hops`, 1 when left out, and
 * `group=none`; `group=root`, from before grouping was the default, still
 * parses, as the default. A hash whose `formatRoute` differs is not canonical;
 * the router replaces it in place.
 */
export function parseHash(hash: string, kinds: readonly string[]): Route {
  const notFound: Route = { name: "notFound", hash };
  const raw = hash.replace(/^#\/?/, "");
  const cut = raw.indexOf("?");
  const path = (cut < 0 ? raw : raw.slice(0, cut)).replace(/\/$/, "");
  const query = new URLSearchParams(cut < 0 ? "" : raw.slice(cut + 1));
  let parts: string[];
  try {
    parts = path.split("/").map(decodeURIComponent);
  } catch {
    return notFound;
  }
  const [head, ...rest] = parts;
  const kind = kinds.find((known) => known.toLowerCase() === rest[0]?.toLowerCase());
  switch (head) {
    case "":
      return { name: "graph" };
    case "list":
    case "new": {
      const named = rest.length === 0 ? kinds[0] : kind;
      if (!named || rest.length > 1) return notFound;
      if (head === "new" || query.get("view") !== "columns") return { name: head, kind: named };
      const columns = (query.get("path") ?? "").split(",").filter(Boolean).map(parseSeq);
      return columns.every((seq) => seq !== null) ? { name: "list", kind: named, columns } : notFound;
    }
    case "ref": {
      const seq = parseSeq(rest[1] ?? "");
      return kind && rest.length === 2 && seq !== null ? { name: "ref", kind, seq } : notFound;
    }
    case "lookup":
    case "inquiry":
      return rest.length === 1 && UUID.test(rest[0]!) ? { name: "lookup", id: rest[0]!.toLowerCase() } : notFound;
    case "activity":
    case "recent":
      return rest.length === 0 ? { name: "activity" } : notFound;
    case "console":
      return rest.length === 0 ? { name: "console" } : notFound;
    case "graph": {
      const group = query.get("group");
      if (rest.length > 0 || (group !== null && group !== "root" && group !== "none")) return notFound;
      const grouped = group === "none" ? { grouped: false as const } : {};
      const focus = query.get("focus");
      if (focus === null) return { name: "graph", ...grouped };
      const ref = parseFocusRef(focus, kinds);
      const hops = HOPS.find((option) => String(option) === (query.get("hops") ?? "1"));
      return ref && hops ? { name: "graph", focus: { ref, hops }, ...grouped } : notFound;
    }
    case "settings":
    case "admin":
      return rest.length === 0 ? { name: head } : notFound;
    case "search":
      return { name: "search", q: rest.length ? rest.join("/") : (query.get("q") ?? "") };
    default:
      return notFound;
  }
}

/** The inquiry a graph's `focus` names, `<Kind>/<seq>` or a UUID, or null. */
function parseFocusRef(text: string, kinds: readonly string[]): FocusRef | null {
  if (UUID.test(text)) return { id: text.toLowerCase() };
  const [named, digits, ...more] = text.split("/");
  const kind = kinds.find((known) => known.toLowerCase() === named?.toLowerCase());
  const seq = parseSeq(digits ?? "");
  return kind && seq !== null && more.length === 0 ? { kind, seq } : null;
}

/** Every reach a graph focus can have, as the hash spells them. */
export const HOPS: readonly Hops[] = [1, 2, 3, "all"];

/**
 * The seq `digits` names, or null. Past 2^53 `Number` rounds to a neighbour, so
 * such a seq would open another inquiry; it names none.
 */
export function parseSeq(digits: string): number | null {
  const seq = Number(digits);
  return /^\d+$/.test(digits) && Number.isSafeInteger(seq) ? seq : null;
}

/** A record id: the five-group UUID the server assigns. */
export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
