import type { CanvasState } from "../src/api/canvases";
import { expect, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

test.use({ canvas: true });

test.afterEach(async ({ request }) => {
  await resetCanvas(request);
});

test("floating Browse's stable content host folds to its bar and opens again", async ({ page, request }) => {
  const id = await resetCanvas(request);
  const state = await (await request.get(`/api/canvases/${id}`)).json() as CanvasState;
  const browse = state.visuals.find((visual) => visual.type === "trax.browse")!;
  const placed = await request.post(`/api/canvases/${id}/operations`, {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: { revision: state.revision, operation: { kind: "place", instance_id: browse.id, placement: "floating" } },
  });
  expect(placed.ok(), await placed.text()).toBe(true);
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/trax/#/list/Issue");
  const tile = page.locator(".visual-tile-browse.visual-tile-floating");
  const content = tile.locator(".visual-browse-content > .view");
  await expect(content).toBeVisible();
  await tile.getByRole("button", { name: "Collapse Browse" }).click();
  await expect(content).toBeHidden();
  await expect(tile.locator(".visual-tile-toolbar")).toBeVisible();
  await tile.getByRole("button", { name: "Expand Browse" }).click();
  await expect(content).toBeVisible();
});

test("an HTML Artifact in Browse keeps its document, edit and focus through rerenders and placement changes", async ({ page, request }) => {
  const id = await resetCanvas(request);
  const made = await request.post("/api/inquiries/batch", {
    data: { items: [{ kind: "Issue", title: "Browse lifecycle", idempotency_key: crypto.randomUUID() }], edges: [] },
  });
  expect(made.ok(), await made.text()).toBe(true);
  const { ids } = await made.json() as { ids: string[] };
  const published = await request.post("/api/artifacts/content", {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: {
      issue_id: ids[0], title: "Browse browser state", summary: "Lifecycle regression", format: "html",
      html: '<input aria-label="Artifact draft" value="initial"><script>window.documentToken = crypto.randomUUID();</script>',
    },
  });
  expect(published.ok(), await published.text()).toBe(true);
  const { artifact_id: artifact } = await published.json() as { artifact_id: string };
  let reads = 0;
  page.on("request", (read) => { if (new URL(read.url()).pathname === `/api/artifacts/${artifact}/html`) reads += 1; });
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto(`/trax/#/lookup/${artifact}`);
  const draft = page.frameLocator(".artifact-html-frame").getByRole("textbox", { name: "Artifact draft" });
  await draft.fill("unsaved iframe edit");
  const token = await draft.evaluate(() => Reflect.get(window, "documentToken") as string);
  expect(token).toBeTruthy();
  expect(reads).toBe(1);
  await page.setViewportSize({ width: 1616, height: 900 });
  await expect(page.locator(".visual-stage")).toBeVisible();
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  expect.soft(await draft.inputValue(), "resize keeps the iframe edit").toBe("unsaved iframe edit");
  expect.soft(await draft.evaluate((element) => document.activeElement === element), "resize keeps iframe focus").toBe(true);
  expect.soft(await page.locator(".artifact-html-frame").evaluate((element) => document.activeElement === element), "resize keeps outer focus").toBe(true);
  expect.soft(await draft.evaluate(() => Reflect.get(window, "documentToken") as string), "resize keeps the iframe document").toBe(token);
  expect.soft(reads, "resize does not reload the iframe").toBe(1);
  for (const placement of ["floating", "main", "floating"] as const) {
    await draft.fill("unsaved iframe edit");
    const state = await (await request.get(`/api/canvases/${id}`)).json() as CanvasState;
    const browse = state.visuals.find((visual) => visual.type === "trax.browse")!;
    const changed = await request.post(`/api/canvases/${id}/operations`, {
      headers: { "Idempotency-Key": crypto.randomUUID() },
      data: { revision: state.revision, operation: { kind: "place", instance_id: browse.id, placement } },
    });
    expect(changed.ok(), await changed.text()).toBe(true);
    await expect(page.locator(`.visual-tile-browse.visual-tile-${placement}`)).toBeVisible();
    if (placement === "main") {
      const tile = (await page.locator(".visual-tile-browse").boundingBox())!;
      const slot = (await page.locator(".visual-browse-slot").boundingBox())!;
      for (const dimension of ["x", "y", "width", "height"] as const) expect(tile[dimension], `docked Browse ${dimension}`).toBeCloseTo(slot[dimension], 0);
    }
    expect.soft(await draft.inputValue(), `${placement} keeps the iframe edit`).toBe("unsaved iframe edit");
    expect.soft(await draft.evaluate((element) => document.activeElement === element), `${placement} keeps iframe focus`).toBe(true);
    expect.soft(await page.locator(".artifact-html-frame").evaluate((element) => document.activeElement === element), `${placement} keeps outer focus`).toBe(true);
    expect.soft(await draft.evaluate(() => Reflect.get(window, "documentToken") as string), `${placement} keeps the iframe document`).toBe(token);
    expect.soft(reads, `${placement} does not reload the iframe`).toBe(1);
  }
  const others: string[] = [];
  for (const type of ["trax.timeline", "trax.subgraph"]) {
    const state = await (await request.get(`/api/canvases/${id}`)).json() as CanvasState;
    const changed = await request.post(`/api/canvases/${id}/operations`, {
      headers: { "Idempotency-Key": crypto.randomUUID() },
      data: { revision: state.revision, operation: { kind: "show", visual_type: type, placement: "floating", record_id: ids[0] } },
    });
    expect(changed.ok(), await changed.text()).toBe(true);
    const next = await changed.json() as CanvasState;
    others.push(next.visuals.find((visual) => visual.type === type)!.id);
    await expect(page.locator(`[data-visual-instance="${others.at(-1)}"]`)).toBeVisible();
    expect(await draft.inputValue(), "adding a floating visual keeps the iframe edit").toBe("unsaved iframe edit");
    expect(await draft.evaluate(() => Reflect.get(window, "documentToken") as string)).toBe(token);
  }
  const state = await (await request.get(`/api/canvases/${id}`)).json() as CanvasState;
  const focused = await request.post(`/api/canvases/${id}/operations`, {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: { revision: state.revision, operation: { kind: "focus", instance_id: others[0] } },
  });
  expect(focused.ok(), await focused.text()).toBe(true);
  await expect(page.locator(`[data-visual-instance="${others[0]}"].visual-tile-focused`)).toBeVisible();
  expect(await draft.inputValue(), "reordering floating visuals keeps the iframe edit").toBe("unsaved iframe edit");
  expect(await draft.evaluate(() => Reflect.get(window, "documentToken") as string)).toBe(token);
  expect(await draft.evaluate((element) => document.activeElement === element)).toBe(true);
  expect(await page.locator(".artifact-html-frame").evaluate((element) => document.activeElement === element)).toBe(true);
  expect(reads).toBe(1);
});

test("Browse in the last main slot keeps overflow inside its strip", async ({ page, request }) => {
  const id = await resetCanvas(request);
  const made = await request.post("/api/inquiries/batch", {
    data: { items: [{ kind: "Issue", title: "Browse slot clipping", idempotency_key: crypto.randomUUID() }], edges: [] },
  });
  expect(made.ok(), await made.text()).toBe(true);
  const { ids } = await made.json() as { ids: string[] };
  let state = await (await request.get(`/api/canvases/${id}`)).json() as CanvasState;
  const browse = state.visuals.find((visual) => visual.type === "trax.browse")!;
  for (const operation of [
    { kind: "show", visual_type: "trax.timeline", placement: "main", record_id: ids[0] },
    { kind: "show", visual_type: "trax.subgraph", placement: "main", record_id: ids[0] },
    { kind: "hide", instance_id: browse.id },
    { kind: "show", visual_type: "trax.browse", placement: "main" },
  ]) {
    const changed = await request.post(`/api/canvases/${id}/operations`, {
      headers: { "Idempotency-Key": crypto.randomUUID() }, data: { revision: state.revision, operation },
    });
    expect(changed.ok(), await changed.text()).toBe(true);
    state = await changed.json() as CanvasState;
  }
  await page.setViewportSize({ width: 1000, height: 900 });
  await page.goto("/trax/#/list/Issue");
  const strip = page.locator(".visual-main-strip");
  await expect(page.locator(".visual-browse-slot")).toBeAttached();
  await expect.poll(() => strip.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
  expect.soft(await page.locator(".visual-stage").evaluate((element) => element.scrollWidth - element.clientWidth), "the stage does not inherit the strip's overflow").toBeLessThanOrEqual(1);
  await strip.evaluate((element) => { element.scrollLeft = element.scrollWidth - element.clientWidth; });
  await expect(page.locator(".visual-tile-browse .view")).toBeVisible();
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  const tile = (await page.locator(".visual-tile-browse").boundingBox())!;
  const slot = (await page.locator(".visual-browse-slot").boundingBox())!;
  expect(tile.x).toBeCloseTo(slot.x + 1, 0);
  expect(tile.width).toBeCloseTo(slot.width - 1, 0);
});
