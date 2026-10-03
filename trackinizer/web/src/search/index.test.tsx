import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { stubFetch } from "../api/testing";
import { MetaContext } from "../app/boot";
import { META, row } from "../detail/testing";
import { RouterProvider } from "../router/router";
import { SearchView } from ".";

const BUDGET = "query exceeded the time budget; narrow the filters or add more specific terms";

beforeEach(() => {
  history.replaceState(null, "", "#/search/retry");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function show(q: string) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MetaContext value={META}>
        <RouterProvider kinds={META.kinds}>
          <SearchView q={q} />
        </RouterProvider>
      </MetaContext>
    </QueryClientProvider>,
  );
}

/** The results table's rows, each as its cells' text. */
function rows(): string[][] {
  return within(screen.getByRole("table"))
    .getAllByRole("row")
    .map((tableRow) => [...tableRow.querySelectorAll("th, td")].map((cell) => cell.textContent ?? ""));
}

test("an empty query asks nothing and says to enter one above", () => {
  const sent = stubFetch(() => Response.json([]));
  show("");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Search");
  expect(screen.getByText("Enter a query above")).toBeTruthy();
  expect(screen.getByRole("searchbox", { name: "Search query" })).toBeTruthy();
  expect(sent).toEqual([]);
});

test("a query is one request across every kind, 50 at most, asking only the keys the table shows", async () => {
  const sent = stubFetch(() => Response.json([]));
  show("retry jitter");
  await screen.findByText("No matches");
  expect(sent.map((request) => request.path)).toEqual(["/api/web/search"]);
  const query = new URLSearchParams(sent[0]!.query);
  expect([query.get("q"), query.get("kind"), query.get("limit")]).toEqual(["retry jitter", null, "50"]);
  expect(query.getAll("fields")).toEqual(["id", "kind", "seq", "title", "status", "judgement"]);
});

test("the matches show in a Ref, Status and Title table, with their count", async () => {
  stubFetch(() =>
    Response.json([
      row("Issue", 7, { title: "Retry jitter" }),
      row("Belief", 9, { title: "Retries help", status: "complete", judgement: "proven" }),
    ]),
  );
  show("retry");
  expect((await screen.findByRole("heading", { level: 1, name: "Search: retry (2)" })).textContent).toBe("Search: retry (2)");
  expect(rows()).toEqual([
    ["Ref", "Status", "Title"],
    ["Issue#7", "Active", "Retry jitter"],
    ["Belief#9", "Complete", "Retries help"],
  ]);
  const ref = within(screen.getByRole("table")).getByRole("link", { name: "Issue#7" });
  expect(ref.getAttribute("href")).toBe("#/ref/Issue/7");
  expect(within(screen.getAllByRole("row")[2]!).getAllByRole("img").map((img) => img.getAttribute("aria-label"))).toEqual([
    "Proven",
    "Complete",
  ]);
});

test("no matches says so", async () => {
  stubFetch(() => Response.json([]));
  show("zebra");
  expect(await screen.findByText("No matches")).toBeTruthy();
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Search: zebra (0)");
  expect(screen.queryByRole("table")).toBeNull();
});

test("the server's message shows, the time budget's included, and Retry searches again", async () => {
  let refuse = true;
  const sent = stubFetch(() =>
    refuse ? Response.json({ detail: BUDGET }, { status: 400 }) : Response.json([row("Issue", 7, { title: "Retry jitter" })]),
  );
  show("retry");
  expect((await screen.findByRole("alert")).textContent).toMatch(new RegExp(`^${BUDGET}Retry`));
  refuse = false;
  await userEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByRole("heading", { level: 1, name: "Search: retry (1)" })).toBeTruthy();
  expect(sent).toHaveLength(2);
});

test("a query entered in the box opens its own search link", async () => {
  stubFetch(() => Response.json([]));
  show("retry");
  const box = screen.getByRole("searchbox", { name: "Search query" });
  expect((box as HTMLInputElement).value).toBe("retry");
  await userEvent.clear(box);
  await userEvent.type(box, "why? not{Enter}");
  expect(location.hash).toBe("#/search/why%3F%20not");
});
