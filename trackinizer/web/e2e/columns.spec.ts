import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { freshLabel, openView, post } from "./listViews";

// The Issue list's Columns view (LV2) on the e2e server: a root goal, labelled so
// the filtered list holds it, and four levels of Issues narrowing it, which the
// columns read from each selection's detail.

/** Create Root ← One ← Two ← Three ← Four under `label`; returns their ids and seqs. */
async function seedChain(request: APIRequestContext, label: string): Promise<{ id: string; seq: number }[]> {
  const titles = ["Root", "One", "Two", "Three", "Four"];
  const items = titles.map((title, n) => ({
    kind: "Issue",
    title: `${title} ${label}`,
    labels: n === 0 ? [label] : [],
    idempotency_key: crypto.randomUUID(),
  }));
  const edges = titles.slice(1).map((_, n) => ({ from_index: n + 1, to_index: n, edge_kind: "narrows" }));
  const ids = (await post(request, "/api/inquiries/batch", { items, edges })).ids as string[];
  return Promise.all(ids.map(async (id) => ({ id, seq: (await (await request.get(`/api/inquiries/${id}`)).json()).seq as number })));
}

/** Each column as `<heading>: <titles>`, without this test's label, the selected one starred. */
async function columns(page: Page, label: string): Promise<string[]> {
  return page.locator(".c-column").evaluateAll(
    (found, label) =>
      found.map((column) => {
        const strip = (text: string) => text.replaceAll(` ${label}`, "");
        const items = [...column.querySelectorAll(".c-item")].map(
          (item) => `${strip(item.querySelector(".c-title")!.textContent!)}${item.getAttribute("aria-current") ? "*" : ""}`,
        );
        return `${strip(column.querySelector(".c-heading")!.textContent!)}: ${items.join(", ")}`;
      }),
    label,
  );
}

const pathOf = (chain: { seq: number }[]) => chain.map(({ seq }) => seq).join(",");

test("Columns drill four levels, Back steps back, and a deep link opens the same columns", async ({ page, request }) => {
  const tag = freshLabel("lv2");
  const chain = await seedChain(request, tag);
  await openView(page, tag, "Columns");
  await expect.poll(() => columns(page, tag)).toEqual(["Root issues: Root"]);

  await page.locator(".c-item", { hasText: `Root ${tag}` }).click();
  await page.locator(".c-item", { hasText: `One ${tag}` }).click();
  await page.locator(".c-item", { hasText: `Two ${tag}` }).click();
  // → selects the first child shown, so it waits for Two's children.
  await expect.poll(async () => (await columns(page, tag)).at(-1)).toBe("Under Two: Three");
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("ArrowRight");
  const drilled = ["Root issues: Root*", "Under Root: One*", "Under One: Two*", "Under Two: Three*", "Under Three: Four"];
  await expect.poll(() => columns(page, tag)).toEqual(drilled);
  await expect(page).toHaveURL(new RegExp(`#/list/Issue\\?view=columns&path=${pathOf(chain.slice(0, 4))}$`));

  await page.goBack();
  await expect.poll(() => columns(page, tag)).toEqual(drilled.slice(0, 3).concat("Under Two: Three"));
  await page.goBack();
  await expect.poll(() => columns(page, tag)).toEqual(drilled.slice(0, 2).concat("Under One: Two"));

  await page.goto(`/app/?deep=1#/list/Issue?view=columns&path=${pathOf(chain.slice(0, 4))}`);
  await expect.poll(() => columns(page, tag)).toEqual(drilled);
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`#/ref/Issue/${chain[3]!.seq}$`));
});

test("a child added elsewhere to the Issue selected last shows in its column", async ({ page, request }) => {
  const tag = freshLabel("lv2");
  const chain = await seedChain(request, tag);
  await openView(page, tag, "Columns");
  await page.goto(`/app/#/list/Issue?view=columns&path=${pathOf(chain.slice(0, 4))}`);
  await expect.poll(async () => (await columns(page, tag)).at(-1)).toBe("Under Three: Four");

  await post(request, "/api/inquiries/batch", {
    items: [{ kind: "Issue", title: `Five ${tag}`, idempotency_key: crypto.randomUUID() }],
    edges: [{ from_index: 0, to_id: chain[3]!.id, edge_kind: "narrows" }],
  });
  await expect.poll(async () => (await columns(page, tag)).at(-1)).toMatch(/^Under Three: (Five, Four|Four, Five)$/);
});
