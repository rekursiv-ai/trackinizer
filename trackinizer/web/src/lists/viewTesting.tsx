// Test helpers for the list views that follow `narrows` (Streams, Outline,
// Columns); only tests import this file.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { expect, vi } from "vitest";
import type { Detail, Peer } from "../api/detail";
import type { Ancestor, InquiryRow } from "../api/inquiries";
import type { Profile } from "../api/me";
import { AGREED, type Sent, stubFetch } from "../api/testing";
import { type Meta, MetaContext, ProfileContext } from "../app/boot";
import { createQueryClient } from "../app/queryClient";
import { CommandRegistry, CommandRegistryContext, Shortcuts } from "../commands/registry";
import { uuid } from "../live/testing";
import { RouterProvider } from "../router/router";
import { ToastProvider } from "../ui/toast";
import { ListView } from ".";

const KINDS = ["Issue", "Paper"];
const META: Meta = {
  enums: { inquiry_kind_all: KINDS, status: ["active", "complete"] },
  fieldOwners: { priority: "issue", authors: "paper" },
  edges: {},
  kinds: KINDS,
};
const PROFILE: Profile = { user_id: "u1", email: "ada@example.com", name: "Ada", role: "writer", last_login: null, visual_workspace_enabled: false, ...AGREED };

/** An ancestor `seq` with the ids of what narrows it, as the list route sends one. */
export function up(seq: number, ...children: number[]): Ancestor {
  return { id: uuid(seq), kind: "Issue", seq, title: `Issue ${seq}`, status: "active", child_ids: children.map(uuid) };
}

/** Issue `seq` as a detail names a neighbour. */
export function peer(seq: number, kind = "Issue"): Peer {
  return { id: uuid(seq), kind, seq, title: `${kind} ${seq}`, status: "active" };
}

/** Issue `seq`'s detail, with the rows that `narrow` it and those it `produced`. */
export function detailOf(seq: number, { narrows = [], produced = [] }: { narrows?: Peer[]; produced?: Peer[] }): Detail {
  const self = { ...peer(seq), created: "2026-09-20T00:00:00+00:00", modified: "2026-09-20T00:00:00+00:00" };
  return { self, edges: {}, backlinks: { narrows, produced_by: produced }, changes: [] };
}

/**
 * Answer Issue list pages with `rows`, newest (highest seq) first, other kinds
 * with none; ancestry reads with `ancestry` for each row whose id the read's
 * filter names; and an Issue's id and detail from `details`, by seq.
 */
export function serveViews(
  rows: readonly InquiryRow[],
  ancestry: { [seq: number]: Ancestor[] },
  details: { [seq: number]: Detail } = {},
): Sent[] {
  return stubFetch((request) => {
    const url = new URL(request.url);
    const ref = /^\/api\/inquiries\/Issue\/(\d+)$/.exec(url.pathname);
    if (ref) return Response.json({ id: uuid(Number(ref[1])) });
    const opened = Object.values(details).find((detail) => url.pathname === `/api/web/get/${detail.self.id}`);
    if (opened) return Response.json(opened);
    const query = url.searchParams;
    if (query.get("ancestors") === "narrows") {
      const { value } = JSON.parse(query.get("filter")!) as { value: string };
      const named = rows.filter((row) => new RegExp(value).test(row.id));
      return Response.json(named.map((row) => ({ id: row.id, ancestors: ancestry[row.seq] ?? [] })));
    }
    if (query.get("kind") !== "Issue") return Response.json([]);
    return Response.json(rows.toSorted((a, b) => b.seq - a.seq));
  });
}

/** Render `kind`'s list at `hash`, with its keys bound. */
export function showList(kind = "Issue", hash = `#/list/${kind}`) {
  history.replaceState(null, "", hash);
  // jsdom lays nothing out: Radix measures its popover with ResizeObserver, and
  // the list scrolls the focused row into view.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Element.prototype.scrollIntoView = () => {};
  render(
    <QueryClientProvider client={createQueryClient(() => {})}>
      <CommandRegistryContext value={new CommandRegistry()}>
        <ToastProvider>
          <Shortcuts />
          <MetaContext value={META}>
            <ProfileContext value={PROFILE}>
              <RouterProvider kinds={KINDS}>
                <ListView kind={kind} />
              </RouterProvider>
            </ProfileContext>
          </MetaContext>
        </ToastProvider>
      </CommandRegistryContext>
    </QueryClientProvider>,
  );
}

/**
 * The view switch's button for `name`, once the list shows it. Selectors, not
 * role queries: computing every control's accessible name costs jsdom tens of ms.
 */
export async function viewButton(name: string): Promise<HTMLButtonElement> {
  const find = () => [...document.querySelectorAll<HTMLButtonElement>(".view-seg button")].find((button) => button.textContent === name);
  await waitFor(() => expect(find()).toBeTruthy());
  return find()!;
}
