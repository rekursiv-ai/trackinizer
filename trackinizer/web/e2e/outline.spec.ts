import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { freshLabel, openView, post, seed } from "./listViews";

// The Issue list's Outline view (LV1) on the e2e server. The top parent carries
// no label, so the list does not hold it and the outline shows it as older.

/** The outline's lines as `<indent>title`, `(older)` after a parent the list does not hold; the No parent rows last. */
async function lines(page: Page, label: string): Promise<string[]> {
  return page.locator(".o-line").evaluateAll(
    (found, label) =>
      found.map((line) => {
        const title = line.querySelector(".row-title")!.textContent!.replace(` ${label}`, "");
        const older = line.querySelector(".o-older") ? " (older)" : "";
        return `${"  ".repeat(Number((line as HTMLElement).dataset.depth))}${title}${older}`;
      }),
    label,
  );
}

test("Outline nests rows under their parents; ← → fold, j moves, Enter opens, and Back returns to the outline", async ({
  page,
  request,
}) => {
  const tag = freshLabel("lv1");
  const ids = await seed(request, tag, ["Goal", "Parent", "Child one", "Child two", "Loose"], [
    [1, 0],
    [2, 1],
    [3, 1],
  ]);
  await openView(page, tag, "Outline");
  const shown = ["Goal (older)", "  Parent", "    Child two", "    Child one", "Loose"];
  await expect.poll(() => lines(page, tag)).toEqual(shown);
  await expect(page.locator(".o-group")).toHaveText("No parent1");

  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("ArrowLeft");
  await expect.poll(() => lines(page, tag)).toEqual(["Goal (older)", "Loose"]);
  await expect(page.locator(".o-line").first()).toContainText("3 issues");
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => lines(page, tag)).toEqual(shown);

  await page.keyboard.press("j");
  const focused = page.locator('.o-line a[aria-current="true"]');
  await expect(focused).toHaveAttribute("data-row", ids[1]!);
  const href = await focused.getAttribute("href");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`${href!.replace(/[/#]/g, "\\$&")}$`));
  await page.goBack();
  await expect.poll(() => lines(page, tag)).toEqual(shown);
  await expect(page.locator('.o-line a[aria-current="true"]')).toHaveAttribute("data-row", ids[1]!);
});

test("a narrows edge added elsewhere moves a row under its new parent, and an older parent's new title shows", async ({
  page,
  request,
}) => {
  const tag = freshLabel("lv1");
  const ids = await seed(request, tag, ["Goal", "Parent", "Mover"], [[1, 0]]);
  await openView(page, tag, "Outline");
  await expect.poll(() => lines(page, tag)).toEqual(["Goal (older)", "  Parent", "Mover"]);

  await post(request, `/api/edges/${ids[2]}/narrows/${ids[1]}`, {});
  await expect.poll(() => lines(page, tag)).toEqual(["Goal (older)", "  Parent", "    Mover"]);
  await expect(page.locator(".o-group")).toHaveCount(0);

  const renamed = await request.put(`/api/inquiries/${ids[0]}/title`, { data: { value: `Renamed goal ${tag}` } });
  expect(renamed.ok(), await renamed.text()).toBe(true);
  await expect.poll(() => lines(page, tag)).toEqual(["Renamed goal (older)", "  Parent", "    Mover"]);
});
