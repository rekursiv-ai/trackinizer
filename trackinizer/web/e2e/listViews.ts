import type { APIRequestContext, Page } from "@playwright/test";
import { expect, streamOpened } from "./fixtures";

// Helpers for the specs of the Issue list's views (Outline, Streams). Each test
// seeds its own Issues under a label of its own and filters the list to it,
// since specs share one server. The first Issue seeded carries no label, so the
// list does not hold it: a parent outside the page.

export async function post(request: APIRequestContext, path: string, data: object): Promise<{ [key: string]: unknown }> {
  const response = await request.post(path, { data });
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

/** Create Issues titled `titles`, labelled `label` but the first, with `narrows` edges as `[child, parent]` indexes. */
export async function seed(request: APIRequestContext, label: string, titles: string[], narrows: [number, number][]): Promise<string[]> {
  const items = titles.map((title, n) => ({
    kind: "Issue",
    title: `${title} ${label}`,
    labels: n === 0 ? [] : [label],
    idempotency_key: crypto.randomUUID(),
  }));
  const edges = narrows.map(([from_index, to_index]) => ({ from_index, to_index, edge_kind: "narrows" }));
  return (await post(request, "/api/inquiries/batch", { items, edges })).ids as string[];
}

/** Open the Issue list filtered to `label`, once the live stream is connected, and switch it to `view`. */
export async function openView(page: Page, label: string, view: "List" | "Streams" | "Outline" | "Columns") {
  const state = { tab: "active", choices: [{ field: "labels", values: [label] }], grouping: "none", ordering: "created", pages: {}, collapsed: [], focus: null };
  await page.addInitScript((saved) => sessionStorage.setItem("trackinizer.v2.list.Issue", saved), JSON.stringify(state));
  const subscribed = streamOpened(page);
  await page.goto("/app/#/list/Issue");
  await subscribed;
  await page.getByRole("group", { name: "View" }).getByRole("button", { name: view }).click();
}

/** A label no other test uses. */
export const freshLabel = (view: string) => `${view}-${crypto.randomUUID().slice(0, 8)}`;
