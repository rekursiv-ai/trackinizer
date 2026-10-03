// Test helpers for the live stream; only tests import this file.
import { QueryClient, type QueryKey, QueryObserver } from "@tanstack/react-query";
import type { LoggedChange } from "../api/changes";
import type { InquiryRow } from "../api/inquiries";
import type { EventSourceLike } from "../api/stream";
import { type Sent, stubFetch } from "../api/testing";
import { serverText } from "../lists/pages";
import type { Filter } from "../query/query";
import { micros, serverOrder } from "./rows";

/**
 * A stand-in for the browser's `EventSource`: tests open, drop and refuse it,
 * and send it frames. Every instance made since `reset` is in `made`.
 */
export class FakeEventSource implements EventSourceLike {
  static made: FakeEventSource[] = [];

  readonly url: string;
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.made.push(this);
  }

  /** Forget every instance made so far. */
  static reset(): void {
    FakeEventSource.made = [];
  }

  /** The instance made last. */
  static get last(): FakeEventSource {
    const last = FakeEventSource.made.at(-1);
    if (!last) throw new Error("No EventSource was opened.");
    return last;
  }

  /** The connection is up, as on the first connect and every reconnect. */
  open(): void {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }

  /** One frame, as the server writes it: `{"id": …}` unless `data` says otherwise. */
  send(id: string, data = JSON.stringify({ id })): void {
    this.onmessage?.(new MessageEvent("message", { data }));
  }

  /** The connection dropped, and the browser will reconnect by itself. */
  drop(): void {
    this.readyState = 0;
    this.onerror?.(new Event("error"));
  }

  /** The server answered with something other than a stream; the browser gives up. */
  refuse(): void {
    this.readyState = 2;
    this.onerror?.(new Event("error"));
  }

  close(): void {
    this.readyState = 2;
  }
}

/** A cache with no retries, as tests want one. */
export function testClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

/**
 * Put `rows` in the cache under `key` as a mounted query, as a view's observer
 * would. Returns the function that unmounts it.
 */
export function mount(client: QueryClient, key: QueryKey, rows: unknown): () => void {
  client.setQueryData(key, rows);
  return new QueryObserver(client, { queryKey: key, enabled: false, staleTime: Infinity }).subscribe(() => {});
}

/** A stable, distinct UUID for test number `n`. */
export function uuid(n: number): string {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

/** An Issue row number `seq`, created `seq` minutes after a fixed time, so higher is newer. */
export function issue(seq: number, fields: Partial<InquiryRow> = {}): InquiryRow {
  const created = new Date(Date.UTC(2026, 8, 20, 0, seq)).toISOString().replace("Z", "+00:00");
  return {
    id: uuid(seq),
    kind: "Issue",
    seq,
    title: `Issue ${seq}`,
    status: "active",
    owner: null,
    labels: null,
    marginal_cost: { agent_usd: 0, resource_usd: 0 },
    created,
    modified: created,
    priority: null,
    ...fields,
  };
}

/**
 * A `change_log` row number `n` about Issue number `n`, written `n` seconds
 * after a fixed time, so higher is newer; `created` unless `kind` says otherwise.
 */
export function change(n: number, fields: Partial<LoggedChange> = {}): LoggedChange {
  return {
    id: uuid(100_000 + n),
    created: new Date(Date.UTC(2026, 8, 20, 0, 0, n)).toISOString().replace("Z", "+00:00"),
    actor: "ada@example.com",
    kind: "created",
    subject_id: uuid(n),
    subject_kind: "Issue",
    caused_by: null,
    reason: "",
    old: {},
    new: {},
    ...fields,
  };
}

/**
 * Answer `GET /api/change_log` from `changes()` as the server does: the kinds
 * the repeated `kind` names, newest first by `(created, id)`, at or after
 * `since` (both to the microsecond), up to `limit`. Any other request gets
 * `other(request)`, by default a 404.
 */
export function serveChanges(
  changes: () => readonly LoggedChange[],
  other: (request: Request) => Response = () => Response.json({ detail: "not found" }, { status: 404 }),
): Sent[] {
  return stubFetch((request) => {
    const url = new URL(request.url);
    if (url.pathname !== "/api/change_log") return other(request);
    const query = url.searchParams;
    const since = query.get("since");
    const body = changes()
      .filter((row) => query.getAll("kind").includes(row.kind) && (since === null || micros(row.created) >= micros(since)))
      .toSorted((a, b) => micros(b.created) - micros(a.created) || (a.id < b.id ? 1 : a.id > b.id ? -1 : 0))
      .slice(0, Number(query.get("limit")));
    return Response.json(body);
  });
}

/**
 * Answer `GET /api/inquiries` from `rows()` as the server does: each kind in
 * turn, rows matching every filter and any `seq_range`, newest created first,
 * `limit` and `offset` per kind. Any other request gets `other(request)`, by
 * default a 404.
 */
export function serveRows(
  rows: () => readonly InquiryRow[],
  other: (request: Request) => Response = () => Response.json({ detail: "not found" }, { status: 404 }),
): Sent[] {
  return stubFetch((request) => answerRows(rows(), request) ?? other(request));
}

/** `GET /api/inquiries` answered from `rows`, as `serveRows` does; null for any other request. */
export function answerRows(rows: readonly InquiryRow[], request: Request): Response | null {
  const url = new URL(request.url);
  if (url.pathname !== "/api/inquiries") return null;
  const query = url.searchParams;
  const filters = query.getAll("filter").map((raw) => JSON.parse(raw) as Filter);
  const ranges = query.getAll("seq_range").map((range) => range.split("..").map(Number));
  const limit = Number(query.get("limit") ?? 50);
  const offset = Number(query.get("offset") ?? 0);
  const body = query.getAll("kind").flatMap((kind) =>
    rows
      .filter(
        (row) =>
          row.kind === kind &&
          filters.every((filter) => matches(row, filter)) &&
          (ranges.length === 0 || ranges.some(([start, stop]) => row.seq >= start! && row.seq <= stop!)),
      )
      .toSorted(serverOrder)
      .slice(offset, offset + limit),
  );
  return Response.json(body);
}

/** The list request `sent` made, read back: its kinds, filters, seq ranges, limit and offset. */
export function listParams(sent: Sent) {
  const query = new URLSearchParams(sent.query);
  return {
    kinds: query.getAll("kind"),
    filters: query.getAll("filter").map((raw) => JSON.parse(raw) as Filter),
    seqRanges: query.getAll("seq_range"),
    limit: Number(query.get("limit")),
    offset: query.get("offset") === null ? null : Number(query.get("offset")),
  };
}

/** Whether `row` passes `filter`; a list field passes when any element does. */
function matches(row: InquiryRow, { field, op, value }: Filter): boolean {
  const raw = (row as { readonly [field: string]: unknown })[field];
  const values = Array.isArray(raw) ? raw.map(String) : raw == null ? [] : [String(raw)];
  switch (op) {
    case "is":
      return values.includes(value);
    case "ne":
      return !values.includes(value);
    case "re":
      return values.some((v) => new RegExp(value).test(v));
    case "isnull":
      return values.length === 0;
    case "notnull":
      return values.length > 0;
    case "ge":
      // The server compares a time as text (see `serverText`); only `created` is compared so here.
      if (field === "created") return values.some((v) => serverText(v) >= value);
      throw new Error(`The fake server does not compare ${field}.`);
    default:
      throw new Error(`The fake server does not filter with ${op}.`);
  }
}
