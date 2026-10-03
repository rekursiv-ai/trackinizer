import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { freshLabel, openView, post, seed } from "./listViews";

// The Issue list's Streams view (LV3) on the e2e server. The root goal carries no
// label, so the list does not hold it; its stream groups the rows under it.

/** Each stream as `<root>: <row>, …`, without this test's label, a row's parent as `(in …)`. */
async function streams(page: Page, label: string): Promise<string[]> {
  return page.locator(".s-stream").evaluateAll(
    (found, label) =>
      found.map((stream) => {
        const strip = (text: string) => text.replaceAll(` ${label}`, "");
        const rows = [...stream.querySelectorAll("a.row")].map((row) => {
          const context = row.querySelector(".row-ctx")?.textContent ?? "";
          const title = row.querySelector(".row-title")!.textContent!.slice(0, -context.length || undefined);
          return context ? `${strip(title)} (${strip(context).replace(" · in ", "in ")})` : strip(title);
        });
        return `${strip(stream.querySelector(".s-title")!.textContent!)}: ${rows.join(", ")}`;
      }),
    label,
  );
}

test("Streams groups rows under their root goal, newest few first, and N more in this stream shows the rest", async ({
  page,
  request,
}) => {
  const tag = freshLabel("lv3");
  await seed(request, tag, ["Goal", "One", "Two", "Three", "Four", "Five", "Loose"], [
    [1, 0],
    [2, 0],
    [3, 0],
    [4, 0],
    [5, 0],
  ]);
  await openView(page, tag, "Streams");
  await expect.poll(() => streams(page, tag)).toEqual(["Goal: Five, Four, Three", "No parent: Loose"]);
  await expect(page.locator(".s-head").first()).toContainText("5 issues · 5 active · 0 done · newest now");
  await page.getByRole("button", { name: "2 more in this stream" }).click();
  await expect.poll(() => streams(page, tag)).toEqual(["Goal: Five, Four, Three, Two, One", "No parent: Loose"]);
  await expect(page.getByRole("button", { name: /more in this stream/ })).toHaveCount(0);
});

test("a narrows edge added elsewhere moves a row from No parent into its root's stream, naming its parent", async ({
  page,
  request,
}) => {
  const tag = freshLabel("lv3");
  const ids = await seed(request, tag, ["Goal", "Child", "Mover"], [[1, 0]]);
  await openView(page, tag, "Streams");
  await expect.poll(() => streams(page, tag)).toEqual(["Goal: Child", "No parent: Mover"]);

  await post(request, `/api/edges/${ids[2]}/narrows/${ids[1]}`, {});
  await expect.poll(() => streams(page, tag)).toEqual(["Goal: Mover (in Child), Child"]);
  await expect(page.locator(".s-head")).toContainText("2 issues · 2 active · 0 done");
});
