import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { MetricPoint } from "../../api/metrics";
import { type Sent, stubFetch } from "../../api/testing";
import { detail, renderDetail, row } from "../testing";

const EXPERIMENT = row("Experiment", 7);

/**
 * Serve the Experiment's detail, and `metrics()` as its metrics, a page by
 * `offset` and `limit` as the server cuts one; `fail(n)` says whether the nth
 * metrics read fails with a 500.
 */
function serve(metrics: () => MetricPoint[], fail: (n: number) => boolean = () => false): Sent[] {
  let reads = 0;
  return stubFetch((request) => {
    const url = new URL(request.url);
    if (url.pathname === `/api/web/get/${EXPERIMENT.id}`) return Response.json(detail(EXPERIMENT));
    if (url.pathname === `/api/inquiries/${EXPERIMENT.id}/confidence`) return Response.json({ confidence: 0.5 });
    if (url.pathname === `/api/experiments/${EXPERIMENT.id}/metrics`) {
      if (fail(reads++)) return Response.json({ detail: "database is restarting" }, { status: 500 });
      const offset = Number(url.searchParams.get("offset") ?? 0);
      return Response.json({ points: metrics().slice(offset, offset + Number(url.searchParams.get("limit"))) });
    }
    return Response.json({ detail: "not found" }, { status: 404 });
  });
}

const run = (key: string, count: number, value: (step: number) => number): MetricPoint[] =>
  Array.from({ length: count }, (_, step) => ({ key, step, value: value(step), kind: "scalar", timestamp: null }));

function section() {
  return within(screen.getByRole("region", { name: /^Metrics/ }));
}

beforeEach(() => {
  history.replaceState(null, "", "#/ref/Experiment/7");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("each metric is a card: its key, last value, sparkline and points", async () => {
  serve(() => [...run("loss", 40, (s) => 2 - s / 40), ...run("ece", 5, () => 0.03)]);
  renderDetail({ id: EXPERIMENT.id });
  await waitFor(() => expect(document.querySelectorAll(".metric")).toHaveLength(2));
  expect(screen.getByRole("heading", { name: "Metrics 2" })).toBeTruthy();
  const loss = document.querySelector<HTMLElement>('[data-metric="loss"]')!;
  expect(within(loss).getByText("loss").tagName).toBe("FIGCAPTION");
  expect(loss.querySelector(".v")!.textContent).toBe("1.02");
  expect(loss.querySelector(".r")!.textContent).toBe("40 points · steps 0–39 · range 1.02–2.00");
  expect(loss.querySelector("svg path")!.getAttribute("d")).toMatch(/^M2\.0 4\.0L/);
  expect(document.querySelector('[data-metric="ece"] .r')!.textContent).toBe("5 points · steps 0–4");
  expect(section().queryByRole("note")).toBeNull();
});

test("a run past one page says it is truncated, and which metric is partial", async () => {
  serve(() => [...run("loss", 600, (s) => s), ...run("zz_last", 401, (s) => s)]);
  renderDetail({ id: EXPERIMENT.id });
  const note = await screen.findByRole("note");
  expect(note.textContent).toBe(
    "Truncated: the server returns at most 1,000 points, in key order. Later points of zz_last and any metric after it are not shown.",
  );
  expect(document.querySelector('[data-metric="zz_last"] .r')!.textContent).toMatch(/ · partial$/);
  expect(document.querySelector('[data-metric="loss"] .r')!.textContent).not.toMatch(/partial/);
});

test("a page that ends at a metric's last point names the metrics left out, and no card is partial", async () => {
  serve(() => [...run("loss", 600, (s) => s), ...run("mid", 400, (s) => s), ...run("zz_after", 3, (s) => s)]);
  renderDetail({ id: EXPERIMENT.id });
  const note = await screen.findByRole("note");
  expect(note.textContent).toBe(
    "Truncated: the server returns at most 1,000 points, in key order. zz_after and any metric after it are not shown.",
  );
  expect(document.querySelector(".metric .r")!.textContent).not.toMatch(/partial/);
  expect(document.querySelector('[data-metric="mid"] .r')!.textContent).not.toMatch(/partial/);
});

test("exactly one full page is the whole run: nothing claims it is truncated (DRV-04)", async () => {
  const sent = serve(() => [...run("loss", 600, (s) => s), ...run("zz_last", 400, (s) => s)]);
  renderDetail({ id: EXPERIMENT.id });
  await waitFor(() => expect(document.querySelectorAll(".metric")).toHaveLength(2));
  await waitFor(() => expect(sent.filter((request) => request.path.endsWith("/metrics"))).toHaveLength(2));
  expect(section().queryByRole("note")).toBeNull();
  expect(document.querySelector('[data-metric="zz_last"] .r')!.textContent).not.toMatch(/partial/);
});

test("a failed refresh keeps the cards, and says so in the section's header with Retry (READ-01)", async () => {
  let points = run("loss", 2, (s) => s);
  const sent = serve(() => points, (n) => n === 1);
  renderDetail({ id: EXPERIMENT.id });
  await waitFor(() => expect(document.querySelectorAll(".metric")).toHaveLength(1));
  fireEvent.click(section().getByRole("button", { name: "Refresh" }));
  expect((await section().findByRole("alert")).textContent).toBe("Could not refresh: database is restartingRetry");
  expect(document.querySelectorAll(".metric")).toHaveLength(1);
  points = [...points, ...run("acc", 3, () => 0.9)];
  fireEvent.click(section().getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(document.querySelectorAll(".metric")).toHaveLength(2));
  expect(section().queryByRole("alert")).toBeNull();
  expect(sent.filter((request) => request.path.endsWith("/metrics"))).toHaveLength(3);
});

test("Refresh reads the metrics again; a failure shows the server's message and Retry", async () => {
  let points = run("loss", 2, (s) => s);
  const sent = serve(() => points, (n) => n === 0);
  renderDetail({ id: EXPERIMENT.id });
  expect((await screen.findByRole("alert")).textContent).toBe("database is restartingRetry");
  fireEvent.click(section().getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(document.querySelectorAll(".metric")).toHaveLength(1));
  points = [...points, ...run("acc", 3, () => 0.9)];
  fireEvent.click(section().getByRole("button", { name: "Refresh" }));
  await waitFor(() => expect(document.querySelectorAll(".metric")).toHaveLength(2));
  expect(sent.filter((request) => request.path.endsWith("/metrics"))).toHaveLength(3);
});

test("an Experiment with no metrics says so; other kinds have no metrics section", async () => {
  serve(() => []);
  renderDetail({ id: EXPERIMENT.id });
  expect(await screen.findByText("No metrics logged.")).toBeTruthy();
  cleanup();
  const issue = row("Issue", 8);
  const sent = stubFetch((request) =>
    new URL(request.url).pathname === `/api/web/get/${issue.id}` ? Response.json(detail(issue)) : Response.json({}, { status: 404 }),
  );
  renderDetail({ id: issue.id });
  await screen.findByRole("heading", { level: 1 });
  expect(screen.queryByRole("region", { name: /^Metrics/ })).toBeNull();
  expect(sent.map((request) => request.path)).toEqual([`/api/web/get/${issue.id}`]);
});
