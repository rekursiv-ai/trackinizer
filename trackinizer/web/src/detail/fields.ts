import type { DetailRow } from "../api/detail";
import { editableFields, type FieldRoute } from "../api/fields";
import type { FieldOwners } from "../api/meta";

/** Where the detail shows a field. */
export type Place = "title" | "text" | "json" | "properties" | "details";

/** How the detail names and draws one field. */
export type FieldLook = {
  readonly label: string;
  readonly place: Place;
  /** Shown when the field is unset; `—` otherwise. */
  readonly unset?: string;
  readonly mono?: boolean;
  /**
   * `usd` for money, `fraction` for a probability drawn as a bar, `date` for a
   * time with no route to name its format, `day` for a calendar date.
   */
  readonly format?: "usd" | "fraction" | "date" | "day";
  /** An outside page for the value, if it has one; checked again before use. */
  readonly href?: (value: string) => string | null;
};

/** One field of an inquiry, as the detail shows it. */
export type Field = {
  readonly name: string;
  readonly look: FieldLook;
  /** Its `PUT` route; a field without one is read-only. */
  readonly route: FieldRoute | undefined;
  /** The row's value; `undefined` when unset. */
  readonly value: unknown;
};

/**
 * Every field `row`'s kind has, set or not, in the order the detail shows them.
 *
 * The list comes from the schema, the values from the row: the editable fields
 * of the field-type map, and the read-only ones `/api/meta/fields` assigns the
 * kind (`opened_by_api_key_id`). A field the row carries that neither names
 * (`ended`, which has no route) joins them, so nothing the server sends goes
 * unseen.
 */
export function kindFields(row: DetailRow, fieldOwners: FieldOwners): Field[] {
  const routes = editableFields(row.kind);
  const owner = row.kind.toLowerCase();
  const names = new Set([
    ...Object.keys(routes),
    ...Object.keys(fieldOwners).filter((name) => fieldOwners[name] === owner),
    ...Object.keys(row).filter((name) => !ROW_KEYS.has(name)),
  ]);
  const order = Object.keys(LOOKS);
  const rank = (name: string) => (order.includes(name) ? order.indexOf(name) : order.length);
  return [...names]
    .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
    .map((name) => {
      const route = routes[name];
      const value = valueOf(row, name);
      const json = route?.value.type === "object" || isRecord(value);
      const look = LOOKS[name] ?? { label: capitalizeWords(name), place: json ? "json" : "details" };
      return { name, look, route, value };
    });
}

/**
 * Whether a value counts as unset: absent, `null`, `""` or `[]`.
 *
 * The server sends `null` for an unset field (on `/api/web/get` since fix R17),
 * but an empty string or list means the same to a reader, so all four read alike.
 */
export function isUnset(value: unknown): boolean {
  return value === undefined || value === null || value === "" || (Array.isArray(value) && !value.length);
}

/**
 * A cost in dollars: cents, or under a cent every digit the server keeps (six
 * places), so a session that spent $0.0042 does not read as $0.00.
 */
export function usd(value: number): string {
  const size = Math.abs(value);
  const digits = size < 0.01 ? size.toFixed(6).replace(/\.?0+$/, "") : size.toFixed(2);
  return `${value < 0 ? "-" : ""}$${digits}`;
}

/**
 * A browsable page for a Paper's `source`, as the old UI resolves it: a known
 * scheme through its resolver, `http(s)` as is, any other scheme as a search.
 */
export function sourceUrl(source: string): string | null {
  const match = /^([A-Za-z][A-Za-z0-9+.-]*):\s*(\S.*)$/.exec(source.trim());
  if (!match) return null;
  const [, scheme, rest] = match;
  if (/^https?$/i.test(scheme)) return source.trim();
  const resolve = SOURCE_RESOLVERS[scheme.toLowerCase()];
  return resolve ? resolve(rest.trim()) : `https://www.google.com/search?q=${encodeURIComponent(source.trim())}`;
}

/** Row keys that are not fields: identity, and the cost axes' nesting. */
const ROW_KEYS = new Set(["id", "kind", "seq", "created", "modified", "marginal_cost"]);

function valueOf(row: DetailRow, name: string): unknown {
  // The cost axes are the fields `marginal_cost_agent_usd` and `..._resource_usd`,
  // but the row nests them as `marginal_cost.agent_usd` and `.resource_usd`.
  const cost = row.marginal_cost;
  const value =
    name in row
      ? row[name]
      : name.startsWith("marginal_cost_") && isRecord(cost)
        ? cost[name.slice("marginal_cost_".length)]
        : undefined;
  return isUnset(value) ? undefined : value;
}

function isRecord(value: unknown): value is { readonly [key: string]: unknown } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function capitalizeWords(name: string): string {
  const words = name.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const scholar = (param: string) => (id: string) =>
  id.trim() ? `https://scholar.google.com/scholar?${param}=${encodeURIComponent(id.trim())}` : null;

/**
 * The fields the detail knows how to name and place, in the order it shows them.
 * Which fields a kind has still comes from the server: a field missing here shows
 * under its own name in Details, and a named field the kind lacks never shows.
 */
const LOOKS: { readonly [field: string]: FieldLook } = {
  title: { label: "Title", place: "title" },
  description: { label: "Description", place: "text" },
  validation: { label: "Done when", place: "text" },
  outcome: { label: "Outcome", place: "text" },
  abstract: { label: "Abstract", place: "text" },
  config: { label: "Config", place: "json" },
  judgement: { label: "Judgement", place: "properties" },
  confidence: { label: "Author conf.", place: "properties", format: "fraction" },
  status: { label: "Status", place: "properties" },
  priority: { label: "Priority", place: "properties", unset: "No priority" },
  issue_kind: { label: "Type", place: "properties", unset: "No type" },
  owner: { label: "Owner", place: "properties" },
  subscribers: { label: "Subscribers", place: "properties" },
  labels: { label: "Labels", place: "properties" },
  authors: { label: "Authors", place: "details" },
  publication_type: { label: "Type", place: "details" },
  venue: { label: "Venue", place: "details" },
  subvenue: { label: "Subvenue", place: "details" },
  publish_date: { label: "Published", place: "details", format: "day" },
  source: { label: "Source", place: "details", href: sourceUrl },
  google_scholar_cluster_id: { label: "Scholar cluster", place: "details", mono: true, href: scholar("cluster") },
  google_scholar_cites_id: { label: "Scholar cites", place: "details", mono: true, href: scholar("cites") },
  sha: { label: "SHA", place: "details", mono: true },
  url: { label: "URL", place: "details", href: (url) => (/^https?:\/\//i.test(url.trim()) ? url.trim() : null) },
  query: { label: "Query", place: "details" },
  provider: { label: "Provider", place: "details" },
  cli: { label: "CLI", place: "details" },
  cli_session_id: { label: "Session id", place: "details", mono: true },
  started: { label: "Started", place: "details" },
  // A session with no end is still running, as the old UI's "live" said.
  ended: { label: "Ended", place: "details", format: "date", unset: "Live" },
  recorded: { label: "Recorded", place: "details" },
  rooms: { label: "Rooms", place: "details" },
  codechanges: { label: "Code changes", place: "details" },
  opened_by_api_key_id: { label: "Opened by key", place: "details", mono: true },
  account: { label: "Account", place: "details" },
  marginal_cost_agent_usd: { label: "Agent cost", place: "details", format: "usd" },
  marginal_cost_resource_usd: { label: "Resource cost", place: "details", format: "usd" },
};

/**
 * Landing pages for scheme-tagged sources, copied from the old UI
 * (`SOURCE_RESOLVERS` in `server/assets/index.html`, after idutils).
 */
const SOURCE_RESOLVERS: { readonly [scheme: string]: (id: string) => string } = {
  doi: (id) => `https://doi.org/${id}`,
  arxiv: (id) => `https://arxiv.org/abs/${id}`,
  pmid: (id) => `https://pubmed.ncbi.nlm.nih.gov/${id}/`,
  pmcid: (id) => `https://pmc.ncbi.nlm.nih.gov/articles/${id}/`,
  handle: (id) => `https://hdl.handle.net/${id}`,
  hdl: (id) => `https://hdl.handle.net/${id}`,
  ark: (id) => `https://n2t.net/ark:/${id.replace(/^\/?ark:\/?/i, "")}`,
  urn: (id) => `https://nbn-resolving.org/${id}`,
  ascl: (id) => `https://ascl.net/${id}`,
  ads: (id) => `https://ui.adsabs.harvard.edu/abs/${id}`,
  orcid: (id) => `https://orcid.org/${id}`,
  ror: (id) => `https://ror.org/${id}`,
  hal: (id) => `https://hal.science/${id}`,
  swh: (id) => `https://archive.softwareheritage.org/${id}`,
  isbn: (id) => `https://openlibrary.org/isbn/${id.replace(/[ -]/g, "")}`,
  issn: (id) => `https://portal.issn.org/resource/ISSN/${id}`,
};
