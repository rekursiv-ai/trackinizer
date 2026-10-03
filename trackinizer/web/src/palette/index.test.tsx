import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Detail, DetailRow } from "../api/detail";
import type { InquiryRow } from "../api/inquiries";
import { stubFetch } from "../api/testing";
import { type Meta, MetaContext } from "../app/boot";
import { PaletteContext, usePaletteState } from "../commands/palette";
import { type Command, CommandRegistry, CommandRegistryContext, useCommands } from "../commands/registry";
import { RouterProvider } from "../router/router";
import { markOpened } from "../detail/queries";
import { PaletteView } from ".";
import { SEARCH_DEBOUNCE_MS } from "./sources";

// The server's order puts Issue last here, so a test can see the palette ask for it first.
const KINDS = ["Belief", "Paper", "Issue"];
const META: Meta = { kinds: KINDS, enums: { inquiry_kind_all: KINDS }, fieldOwners: {}, edges: {} };
const BUDGET = "query exceeded the time budget; narrow the filters or add more specific terms";

function uuid(n: number): string {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

function row(kind: string, seq: number, title: string, fields: Partial<InquiryRow> = {}): InquiryRow & DetailRow {
  return {
    id: uuid(seq),
    kind,
    seq,
    title,
    status: "active",
    owner: null,
    labels: null,
    description: null,
    marginal_cost: { agent_usd: 0, resource_usd: 0 },
    created: "2026-09-20T00:00:00+00:00",
    modified: `2026-09-2${seq % 10}T00:00:00+00:00`,
    ...fields,
  };
}

/** A cache holding one list page of `rows`, and a detail for each of `details`, opened in that order. */
function cacheOf(rows: readonly InquiryRow[], details: readonly Detail[] = []): QueryClient {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["inquiries", "list", [], "Issue", 50, 0], rows);
  for (const detail of details) {
    queryClient.setQueryData(["detail", detail.self.id], detail);
    vi.advanceTimersByTime(1);
    markOpened(queryClient, detail.self.id);
  }
  return queryClient;
}

/** The app around the palette: a route, the meta, `commands` mounted, and an Open button. */
function renderPalette({
  queryClient = cacheOf([]),
  commands = [],
  searchLink = null,
}: { queryClient?: QueryClient; commands?: readonly Command[]; searchLink?: string | null } = {}) {
  function Host() {
    const palette = usePaletteState(searchLink, () => {});
    useCommands(commands);
    return (
      <PaletteContext value={palette}>
        <button type="button" onClick={() => palette.show()}>
          Open
        </button>
        <PaletteView />
      </PaletteContext>
    );
  }
  render(
    <QueryClientProvider client={queryClient}>
      <CommandRegistryContext value={new CommandRegistry()}>
        <MetaContext value={META}>
          <RouterProvider kinds={KINDS}>
            <Host />
          </RouterProvider>
        </MetaContext>
      </CommandRegistryContext>
    </QueryClientProvider>,
  );
  if (!searchLink) fireEvent.click(screen.getByRole("button", { name: "Open" }));
  return screen.getByRole("combobox", { name: "Command" });
}

/**
 * Let `ms` pass, then let what it set off settle: a fetch answers in promises,
 * and the cache tells the palette about it on a timer of its own.
 */
async function pass(ms = 0) {
  await act(() => vi.advanceTimersByTimeAsync(ms));
  await act(() => vi.advanceTimersByTimeAsync(0));
}

function type(input: HTMLElement, text: string) {
  fireEvent.change(input, { target: { value: text } });
}

/** Each row's text, by its section's heading. */
function listed(): { [section: string]: string[] } {
  const groups = within(screen.getByRole("listbox")).queryAllByRole("group");
  return Object.fromEntries(
    groups.map((group) => [
      group.querySelector(".pal-sec")!.textContent!.replace(/ · as of .*/, ""),
      within(group)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ]),
  );
}

function active(input: HTMLElement): string | null {
  return document.getElementById(input.getAttribute("aria-activedescendant")!)?.textContent ?? null;
}

/** Answer each search with `answer(kind, q)`, or hold it until it is aborted. */
function serveSearch(answer: (kind: string, q: string) => Response | null) {
  const signals: AbortSignal[] = [];
  const sent = stubFetch((request) => {
    signals.push(request.signal);
    const query = new URL(request.url).searchParams;
    const response = answer(query.get("kind")!, query.get("q")!);
    return (
      response ??
      new Promise((_, reject) => request.signal.addEventListener("abort", () => reject(request.signal.reason)))
    );
  });
  return { sent, signals, kinds: () => sent.map((request) => new URLSearchParams(request.query).get("kind")) };
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(new Date("2026-09-26T12:34:56Z"));
  history.replaceState(null, "", location.pathname);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("loaded rows and commands match while typing; the server is asked after a pause, Issues first", async () => {
  const server = serveSearch((kind) =>
    Response.json(
      kind === "Issue" ? [row("Issue", 1, "Retry the upload"), row("Issue", 9, "Retry forever")] : [],
    ),
  );
  const loaded = [row("Issue", 1, "Retry the upload"), row("Issue", 2, "Unrelated"), row("Issue", 3, "Why retry")];
  const hub = row("Issue", 4, "Hub");
  const input = renderPalette({
    queryClient: cacheOf(loaded, [
      {
        self: hub,
        edges: { narrows: [{ id: uuid(5), kind: "Belief", seq: 5, title: "Retry is idempotent", status: "active" }] },
        backlinks: {},
        changes: [],
      },
    ]),
    commands: [
      { id: "go.retry", title: "Go to retry queue", section: "Navigate", run: () => {} },
      { id: "retry.key", title: "Retry shortcut", keys: ["r"], run: () => {} },
    ],
  });

  type(input, "retry");
  await pass(SEARCH_DEBOUNCE_MS - 1);
  expect(listed()).toEqual({
    Inquiries: ["Issue#3Why retry", "Issue#1Retry the upload", "Belief#5Retry is idempotent"],
    Navigate: ["Go to retry queue"],
  });
  expect(server.sent).toEqual([]);

  expect(active(input)).toBe("Issue#3Why retry");
  ["ArrowUp", "ArrowDown", "ArrowDown", "ArrowDown", "ArrowDown"].forEach((key) => fireEvent.keyDown(input, { key }));
  expect(active(input)).toBe("Go to retry queue");
  await pass(1);
  expect(server.kinds()).toEqual(["Issue", "Belief", "Paper"]);
  expect(listed()["From the server"]).toEqual(["Issue#9Retry forever"]);
  expect(screen.getByText(`From the server · as of ${new Date().toLocaleTimeString()}`)).toBeTruthy();
  // The row under the keyboard stays put when the server's rows arrive.
  expect(active(input)).toBe("Go to retry queue");
});

test("a keystroke cancels the search in flight at once, and the next waits for its own pause", async () => {
  const server = serveSearch(() => null);
  const input = renderPalette();
  type(input, "slow");
  await pass(SEARCH_DEBOUNCE_MS);
  expect(server.sent).toHaveLength(3);
  expect(screen.getByRole("status").textContent).toBe("Searching the server…");

  type(input, "slower");
  await pass();
  expect(server.signals.map((signal) => signal.aborted)).toEqual([true, true, true]);
  await pass(SEARCH_DEBOUNCE_MS - 1);
  expect(server.sent).toHaveLength(3);
  await pass(1);
  expect(server.sent.slice(3).map((request) => new URLSearchParams(request.query).get("q"))).toEqual([
    "slower",
    "slower",
    "slower",
  ]);
});

test("a search over the time budget shows the server's message once, with Retry", async () => {
  let budget = true;
  const server = serveSearch((kind) =>
    kind !== "Belief" && budget ? Response.json({ detail: BUDGET }, { status: 400 }) : Response.json([]),
  );
  const input = renderPalette();
  type(input, "title:(a+)+$");
  await pass(SEARCH_DEBOUNCE_MS);
  expect(listed()).toEqual({ "From the server": [`Issues, Papers: ${BUDGET}Retry`] });

  budget = false;
  fireEvent.keyDown(input, { key: "Enter" });
  await pass();
  expect(server.kinds().slice(3)).toEqual(["Issue", "Paper"]);
  expect(screen.getByText("No results for “title:(a+)+$”")).toBeTruthy();
});

test("picking an inquiry opens it and closes the palette; picking a command runs it", async () => {
  serveSearch(() => Response.json([]));
  const run = vi.fn();
  const commands = [{ id: "go.activity", title: "Go to Activity", keys: ["g a"], section: "Navigate", run }];
  const input = renderPalette({ queryClient: cacheOf([row("Issue", 7, "Ship it")]), commands });
  type(input, "ship");
  fireEvent.keyDown(input, { key: "Enter" });
  await pass();
  expect(location.hash).toBe("#/ref/Issue/7");
  expect(screen.queryByRole("dialog")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Open" }));
  const again = screen.getByRole("combobox", { name: "Command" });
  expect(listed()).toEqual({ Navigate: ["Go to ActivityGA"] });
  fireEvent.click(screen.getByRole("option", { name: /Go to Activity/ }));
  expect(run).toHaveBeenCalledTimes(1);
  expect(again.isConnected).toBe(false);
});

test("a Kind#seq or a UUID jumps without a search; Enter with nothing listed searches at once", async () => {
  const server = serveSearch(() => Response.json([]));
  const input = renderPalette({ queryClient: cacheOf([row("Issue", 7, "Ship it")]) });
  type(input, "iss#7");
  expect(listed()).toEqual({ "Jump to": ["Issue#7Ship it"] });
  type(input, uuid(42).toUpperCase());
  expect(listed()).toEqual({ "Jump to": [`${uuid(42)}Open`] });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(location.hash).toBe(`#/lookup/${uuid(42)}`);

  fireEvent.click(screen.getByRole("button", { name: "Open" }));
  const next = screen.getByRole("combobox", { name: "Command" });
  type(next, "nothing like it");
  fireEvent.keyDown(next, { key: "Enter" });
  await pass();
  expect(server.kinds()).toEqual(["Issue", "Belief", "Paper"]);
});

test("an empty palette lists recently opened inquiries, newest first, then the commands", () => {
  const details = [1, 2, 3, 4, 5].map((seq) => ({
    self: row("Issue", seq, `Opened ${seq}`),
    edges: {},
    backlinks: {},
    changes: [],
  }));
  renderPalette({
    queryClient: cacheOf([], details),
    commands: [{ id: "go.activity", title: "Go to Activity", section: "Navigate", run: () => {} }],
  });
  expect(listed()).toEqual({
    "Recently opened": [5, 4, 3, 2].map((seq) => `Issue#${seq}Opened ${seq}`),
    Navigate: ["Go to Activity"],
  });
});

test("recently opened keeps the order they were opened in when one is refetched (WEB-09)", () => {
  const details = [1, 2, 3, 4, 5].map((seq) => ({ self: row("Issue", seq, `Opened ${seq}`), edges: {}, backlinks: {}, changes: [] }));
  const queryClient = cacheOf([], details);
  // A live update refetches the detail opened first: it was not opened again.
  vi.advanceTimersByTime(1000);
  queryClient.setQueryData(["detail", details[0]!.self.id], { ...details[0]!, self: { ...details[0]!.self, title: "Opened 1, edited" } });
  renderPalette({ queryClient });
  expect(listed()["Recently opened"]).toEqual([5, 4, 3, 2].map((seq) => `Issue#${seq}Opened ${seq}`));
});

test("rows that load while the palette is open are listed without a keystroke (WEB-08)", async () => {
  serveSearch(() => null);
  const queryClient = cacheOf([row("Issue", 1, "Retry the upload")]);
  const input = renderPalette({ queryClient });
  type(input, "retry");
  expect(listed().Inquiries).toEqual(["Issue#1Retry the upload"]);
  act(() => {
    queryClient.setQueryData(["inquiries", "list", [], "Issue", 50, 50], [row("Issue", 2, "Retry later")]);
  });
  await pass();
  expect(listed().Inquiries).toEqual(["Issue#2Retry later", "Issue#1Retry the upload"]);
});

test("nothing says No results while the search waits for its pause (WEB-21)", async () => {
  const server = serveSearch(() => Response.json([]));
  const input = renderPalette();
  type(input, "nothing like it");
  await pass(SEARCH_DEBOUNCE_MS - 1);
  expect(server.sent).toEqual([]);
  expect(screen.queryByText(/No results/)).toBeNull();
  expect(screen.getByRole("status").textContent).toBe("Searching the server…");
  await pass(1);
  expect(screen.getByText("No results for “nothing like it”")).toBeTruthy();
});

test("a search link searches at once, without waiting for a pause", async () => {
  const server = serveSearch(() => Response.json([row("Issue", 3, "What now?")]));
  const input = renderPalette({ searchLink: "what?" });
  expect((input as HTMLInputElement).value).toBe("what?");
  await pass();
  expect(server.sent.map((request) => new URLSearchParams(request.query).get("q"))).toEqual(["what?", "what?", "what?"]);
  expect(listed()["From the server"]).toEqual(["Issue#3What now?"]);
});
