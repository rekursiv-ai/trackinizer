// Test helpers for the editors; only tests import this file.
import { screen, within } from "@testing-library/react";
import { vi } from "vitest";
import type { Change, DetailRow } from "../api/detail";
import { type Sent, stubFetch } from "../api/testing";
import { change, detail } from "../detail/testing";

/**
 * A server holding one inquiry, `self`: it serves its detail, its row and its
 * `Kind#seq` lookup, and applies field writes as trackinizer does, one change
 * each: `PUT` sets (refusing a compare-and-set whose `expected` is stale with
 * 409, and changing nothing when the value is already so), `DELETE` clears,
 * `PATCH` adds or removes one element (a byline appends, and drops only the
 * first match). `answers` take the next writes over; `change` edits the row as
 * another user would; `hold` keeps a request waiting.
 */
export function serveRow(self: DetailRow) {
  const changes: Change[] = [];
  const held: { method: string; path: string; until: Promise<void> }[] = [];
  const server = {
    self,
    sent: [] as Sent[],
    answers: [] as (() => Response)[],
    /** Keep the next `method` request to `path` waiting until the returned function lets it through. */
    hold(method: string, path: string): () => void {
      let release = () => {};
      held.push({ method, path, until: new Promise((resolve) => (release = resolve)) });
      return () => release();
    },
    change(field: string, value: unknown, actor: string) {
      const old = server.self[field];
      server.self = { ...server.self, [field]: value };
      changes.unshift(change(server.self, changes.length + 1, { kind: field, actor, old: { [field]: old }, new: { [field]: value } }));
    },
    /** The writes sent, as `METHOD field` and body. */
    writes: () =>
      server.sent
        .filter((request) => request.method !== "GET")
        .map((request) => ({ call: `${request.method} ${request.path.split("/").at(-1)}`, body: request.body })),
  };
  server.sent = stubFetch(async (request) => {
    const { pathname } = new URL(request.url);
    const at = held.findIndex((hold) => hold.method === request.method && hold.path === pathname);
    if (at >= 0) await held.splice(at, 1)[0]!.until;
    const row = server.self;
    if (request.method === "GET") {
      if (pathname === `/api/web/get/${row.id}`) return Response.json(detail(row, { changes }));
      if (pathname === `/api/inquiries/${row.id}`) return Response.json(row);
      if (pathname === `/api/inquiries/${row.kind}/${row.seq}`) return Response.json({ id: row.id });
      if (pathname === `/api/inquiries/${row.id}/confidence`) return Response.json({ confidence: 0.5 });
      return Response.json({ detail: "not found" }, { status: 404 });
    }
    const scripted = server.answers.shift();
    if (scripted) return scripted();
    const field = pathname.split("/").at(-1)!;
    const body = (await request.clone().json()) as { value?: unknown; op?: string; mode?: string; expected?: unknown };
    if (body.mode === "cas" && body.expected !== (row[field] ?? null)) {
      return Response.json({ detail: `${field} transition rejected`, code: "conflict" }, { status: 409 });
    }
    const list = Array.isArray(row[field]) ? (row[field] as unknown[]) : [];
    const byline = field === "authors";
    const value =
      request.method === "DELETE"
        ? null
        : request.method === "PATCH"
          ? body.op === "add"
            ? byline || !list.includes(body.value)
              ? [...list, body.value]
              : list
            : byline
              ? list.toSpliced(list.indexOf(body.value), list.includes(body.value) ? 1 : 0)
              : list.filter((item) => item !== body.value)
          : (body.value ?? null);
    const stored = (list: unknown) => (Array.isArray(list) && list.length === 0 ? null : list);
    if (JSON.stringify(stored(value)) === JSON.stringify(row[field] ?? null)) return Response.json({ id: row.id, change_id: null });
    server.change(field, stored(value), "ada@example.com");
    return Response.json({ id: row.id, change_id: `c${changes.length}` });
  });
  return server;
}

/**
 * Stub what jsdom lacks for Radix: it lays nothing out, and measures popovers
 * with ResizeObserver; the menu scrolls its active option into view.
 */
export function stubLayout(): void {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Element.prototype.scrollIntoView = () => {};
}

/** The properties panel's value cell for the field labelled `label`. */
export function prop(label: string): HTMLElement {
  // By its label: a query by role works out the style of the whole detail first,
  // some 10 ms a call, and the editors' tests call this one at every step.
  const panel = screen.getByLabelText("Properties", { selector: "aside" });
  return within(panel).getByText(label, { selector: "dt" }).nextElementSibling as HTMLElement;
}

/** The button that edits the property labelled `label`. */
export function propButton(label: string): HTMLElement {
  return prop(label).querySelector("button.prop-btn")!;
}
