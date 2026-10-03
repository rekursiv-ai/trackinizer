import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";
import type { InquiryRow } from "../api/inquiries";
import type { Profile } from "../api/me";
import { type Sent, stubFetch } from "../api/testing";
import { type Meta, MetaContext, ProfileContext } from "../app/boot";
import { createQueryClient } from "../app/queryClient";
import { CommandRegistry, CommandRegistryContext, Shortcuts } from "../commands/registry";
import { RouterProvider } from "../router/router";
import { DetailView } from "../router/views";
import { meFilter, meNames } from "../state/me";
import { storageKey } from "../state/store";
import { EMPTY_STATE } from "../state/value";
import { KindIcon } from "../ui/kinds";
import { ToastProvider } from "../ui/toast";
import { InquiryList, ListView } from ".";

// The detail is its own task with its own reads; the peek only has to show it for the
// focused row. Like the real one, it binds Escape to go back to the list.
vi.mock("../detail", async () => {
  const { useCommands } = await import("../commands/registry");
  return {
    DetailView: ({ target }: { target: { kind: string; seq: number } }) => {
      useCommands([{ id: "detail.back", title: "Back", keys: ["Escape"], run: () => void (location.hash = "#/activity") }]);
      return (
        <p>
          Detail of {target.kind}#{target.seq}
        </p>
      );
    },
  };
});

// The peek's detail is a chunk of its own; loaded first, it shows at once, as it
// does once the app has loaded it.
beforeAll(() => DetailView.preload());

const KINDS = ["Issue", "Paper", "Belief"];
const META: Meta = {
  enums: {
    inquiry_kind_all: KINDS,
    status: ["active", "complete", "abandoned", "invalid"],
    judgement: ["proven", "disproven", "unproven", "undecidable"],
  },
  fieldOwners: { priority: "issue", judgement: "belief", confidence: "belief", authors: "paper" },
  edges: {},
  kinds: KINDS,
};
const PROFILE: Profile = { user_id: "u1", email: "ada@example.com", name: "Ada", role: "writer", last_login: null, visual_workspace_enabled: false };

let nextSeq = 1;
function row(fields: Partial<InquiryRow> = {}): InquiryRow {
  const seq = nextSeq++;
  return {
    id: `00000000-0000-4000-8000-${String(seq).padStart(12, "0")}`,
    kind: "Issue",
    seq,
    title: `Row ${seq}`,
    status: "active",
    owner: null,
    labels: null,
    marginal_cost: { agent_usd: 0, resource_usd: 0 },
    created: "2026-09-20T00:00:00+00:00",
    modified: "2026-09-20T00:00:00+00:00",
    ...((fields.kind ?? "Issue") === "Issue" ? { priority: null } : {}),
    ...fields,
  };
}

/**
 * Answer `GET /api/inquiries` from `rows` as the server does: per kind, newest
 * first, `limit` and `offset` per kind, and 400 when a requested kind lacks a
 * filtered field. Filters are not applied; tests read them off the requests.
 */
function serve(rows: readonly InquiryRow[], fail: () => Response | null = () => null): Sent[] {
  return stubFetch((request) => {
    const failure = fail();
    if (failure) return failure;
    const query = new URL(request.url).searchParams;
    const limit = Number(query.get("limit"));
    const offset = Number(query.get("offset"));
    const fields = query.getAll("filter").map((raw) => JSON.parse(raw).field as string);
    const body: InquiryRow[] = [];
    for (const kind of query.getAll("kind")) {
      const missing = fields.find((field) => ![undefined, kind.toLowerCase()].includes(META.fieldOwners[field]));
      if (missing) return Response.json({ detail: `unknown filter field '${missing}' for ${kind}` }, { status: 400 });
      const ofKind = rows.filter((r) => r.kind === kind).toSorted((a, b) => b.seq - a.seq);
      body.push(...ofKind.slice(offset, offset + limit));
    }
    return Response.json(body);
  });
}

function show(ui: ReactNode, client = createQueryClient(() => {})) {
  const registry = new CommandRegistry();
  render(
    <QueryClientProvider client={client}>
      <CommandRegistryContext value={registry}>
        <ToastProvider>
          <Shortcuts />
          <MetaContext value={META}>
            <ProfileContext value={PROFILE}>
              <RouterProvider kinds={KINDS}>{ui}</RouterProvider>
            </ProfileContext>
          </MetaContext>
        </ToastProvider>
      </CommandRegistryContext>
    </QueryClientProvider>,
  );
}

/** The rows on screen, by title, in order, once there are any. */
async function titles(): Promise<string[]> {
  // Selectors, not role queries: computing 20 rows' accessible names costs jsdom tens of ms.
  await waitFor(() => expect(rowTitles().length).toBeGreaterThan(0));
  return rowTitles();
}

function rowTitles(): string[] {
  return [...document.querySelectorAll("a.row .row-title")].map((title) => title.textContent!);
}

/** The filters of the last list request, as `field op value`. */
function lastFilters(sent: readonly Sent[]): string[] {
  const query = new URLSearchParams(sent.at(-1)!.query);
  return query.getAll("filter").map((raw) => Object.values(JSON.parse(raw)).join(" ").trim());
}

async function pick(menuButton: RegExp, ...options: (string | RegExp)[]) {
  fireEvent.click(screen.getByRole("button", { name: menuButton }));
  for (const option of options) {
    fireEvent.click(await screen.findByRole("option", { name: option }));
  }
}

beforeEach(() => {
  history.replaceState(null, "", "#/list/Issue");
  sessionStorage.clear();
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
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test("the focused row scrolls into view in the next frame, not in the task that rendered the rows", async () => {
  const frames: FrameRequestCallback[] = [];
  vi.stubGlobal("requestAnimationFrame", (frame: FrameRequestCallback) => frames.push(frame));
  vi.stubGlobal("cancelAnimationFrame", () => {});
  const nextFrame = () => act(() => frames.splice(0).forEach((frame) => frame(performance.now())));
  const scrolled = vi.spyOn(Element.prototype, "scrollIntoView");
  serve([row({ title: "First" }), row({ title: "Second" })]);
  show(<ListView kind="Issue" />);
  expect(await titles()).toEqual(["Second", "First"]);
  expect(scrolled).not.toHaveBeenCalled();
  nextFrame();
  expect(scrolled.mock.contexts.map((element) => (element as Element).querySelector(".row-title")?.textContent)).toEqual(["Second"]);
  await userEvent.setup().keyboard("j");
  nextFrame();
  expect(scrolled.mock.contexts.map((element) => (element as Element).querySelector(".row-title")?.textContent)).toEqual(["Second", "First"]);
});

test("a row the pointer rests on for 150 ms has its detail read ahead, as does one j moves to, but not the row a list opens on (SP6)", async () => {
  const [a, b, c] = [row({ title: "Row a" }), row({ title: "Row b" }), row({ title: "Row c" })];
  const sent = serve([a!, b!, c!]);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  show(<ListView kind="Issue" />);
  // Testing Library's own waits run on the timers this test holds; Vitest's do not.
  await vi.waitFor(() => expect(rowTitles()).toEqual(["Row c", "Row b", "Row a"]), { interval: 1 });
  const details = () => sent.filter((request) => request.path.startsWith("/api/web/get/")).map((request) => request.path.split("/").at(-1));
  await vi.advanceTimersByTimeAsync(150);
  expect(details()).toEqual([]);
  fireEvent.mouseOver(screen.getByText("Row a"));
  await vi.advanceTimersByTimeAsync(150);
  expect(details()).toEqual([a!.id]);
  // A key event of its own: user-event waits on timers, which this test holds.
  fireEvent.keyDown(document.body, { key: "j", code: "KeyJ" });
  await vi.waitFor(() => expect(document.querySelector('[aria-current="true"] .row-title')?.textContent).toBe("Row b"), { interval: 1 });
  await vi.advanceTimersByTimeAsync(150);
  expect(details()).toEqual([a!.id, b!.id]);
});

test("a list asks for the fields its rows show, and not the description", async () => {
  const sent = serve([row({ title: "Row back" })]);
  show(<ListView kind="Issue" />);
  expect(await titles()).toEqual(["Row back"]);
  const fields = new URLSearchParams(sent[0]!.query).getAll("fields");
  expect(fields).toContain("title");
  expect(fields).not.toContain("description");
});

test("a refused request shows the server's message, and Retry asks again", async () => {
  let refuse = true;
  const sent = serve([row({ title: "Row back" })], () =>
    refuse ? Response.json({ detail: "statement timeout" }, { status: 400 }) : null,
  );
  show(<ListView kind="Issue" />);
  expect((await screen.findByRole("alert")).textContent).toBe("statement timeoutRetry");
  refuse = false;
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(await titles()).toEqual(["Row back"]);
  expect(sent).toHaveLength(2);
});

test("several kinds load 20 rows each, and Load more asks one kind for its next 20", async () => {
  const sent = serve([...Array.from({ length: 21 }, () => row()), row({ kind: "Paper", title: "A paper" })]);
  show(<InquiryList id="mixed" kinds={["Issue", "Paper"]} title="Mine" icon={<KindIcon kind="Issue" />} />);
  // A screenful first, then the rest.
  await waitFor(() => expect(rowTitles()).toHaveLength(21));
  // By text: a query by role works out the style of every row first, some 10 ms a call.
  const loadMore = () => screen.queryAllByText(/^Load more/, { selector: "button" });
  expect(loadMore().map((b) => b.textContent)).toEqual(["Load more issues"]);
  fireEvent.click(loadMore()[0]!);
  await waitFor(() => expect(rowTitles()).toHaveLength(22));
  expect(loadMore()).toEqual([]);
  const pages = sent.map((request) => {
    const query = new URLSearchParams(request.query);
    return `${query.getAll("kind")} ${query.get("limit")} ${query.get("offset")}`;
  });
  // The next page starts after the last row loaded; all 20 share its time, so
  // it asks for 20 more beyond those ties (PAGE-02).
  expect(pages.toSorted()).toEqual(["Issue 20 0", "Issue 40 0", "Paper 20 0"]);
  expect(lastFilters(sent.filter((request) => request.query.includes("limit=40")))).toEqual([
    "status is active",
    "created le 2026-09-20 00:00:00+00:00",
  ]);
  expect(screen.getByText("22 loaded")).toBeTruthy();
});

test("tabs and filters change the request, and the trax line always shows the same query", { tags: ["manual"] }, async () => {
  const sent = serve([row({ owner: "josh" }), row({ owner: "dan.k" })]);
  show(<ListView kind="Issue" />);
  await titles();
  const trax = () => screen.getByTitle("The same query from the CLI").textContent;
  expect(lastFilters(sent)).toEqual(["status is active"]);
  expect(trax()).toBe("trax issue status is active");
  expect(new URLSearchParams(sent[0]!.query).get("limit")).toBe("50");

  fireEvent.click(screen.getByRole("button", { name: "Closed" }));
  await waitFor(() => expect(lastFilters(sent)).toEqual(["status ne active"]));
  expect(trax()).toBe("trax issue status ne active");

  await pick(/Filter/, "Owner", "josh", "dan.k");
  await waitFor(() => expect(lastFilters(sent)).toEqual(["status ne active", "owner re ^(josh|dan\\.k)$"]));
  expect(trax()).toBe("trax issue status ne active owner re '^(josh|dan\\.k)$'");
  fireEvent.keyDown(screen.getByRole("combobox"), { key: "Escape" });
  const chip = screen.getByText("Owner").closest(".fchip")!;
  expect(chip.textContent).toBe("Owneris any of josh, dan.k");

  fireEvent.click(within(chip as HTMLElement).getByRole("button", { name: "Remove the Owner filter" }));
  await waitFor(() => expect(lastFilters(sent)).toEqual(["status ne active"]));
});

/** COLD-01's rows: Papers with null, empty and two authors, and an Issue. */
function servePapers(): void {
  serve([
    row({ kind: "Paper", title: "A paper with null authors", authors: null, owner: "josh" }),
    row({ kind: "Paper", title: "A paper with no authors", authors: [], venue: "NeurIPS" }),
    row({ kind: "Paper", title: "A paper by two", authors: ["Ada Lovelace", "Alan Turing"] }),
    row({ kind: "Issue", title: "Row issue" }),
  ]);
}

const PAPERS = ["A paper by two", "A paper with no authors", "A paper with null authors"];

test("rows that load after the list shows draw in a render after the one they land in; rows already loaded draw at once", async () => {
  // What each commit showed: an observer runs after every commit, before any later task.
  const commits: string[] = [];
  const observer = new MutationObserver(() => {
    const scroll = document.querySelector(".view .scroll");
    const now = scroll && `busy ${scroll.getAttribute("aria-busy")}, ${document.querySelector(".list-loading") ? "Loading…" : `${rowTitles().length} rows`}`;
    if (now && now !== commits.at(-1)) commits.push(now);
  });
  observer.observe(document.body, { childList: true, subtree: true, attributes: true });
  serve([row(), row()]);
  const client = createQueryClient(() => {});
  show(<ListView kind="Issue" />, client);
  await titles();
  cleanup();
  commits.push("again");
  show(<ListView kind="Issue" />, client);
  await titles();
  observer.disconnect();
  // Shown again, the list draws the rows it holds at once, reading them afresh behind them.
  expect(commits).toEqual(["busy true, Loading…", "busy false, Loading…", "busy false, 2 rows", "again", "busy true, 2 rows"]);
});

test("a first visit draws a screenful of rows, then the rest, each in a render of its own", async () => {
  // A window 10 rows tall (40 px a row).
  vi.stubGlobal("innerHeight", 400);
  const counts: number[] = [];
  const observer = new MutationObserver(() => {
    const count = rowTitles().length;
    if (count && count !== counts.at(-1)) counts.push(count);
  });
  observer.observe(document.body, { childList: true, subtree: true });
  serve(Array.from({ length: 30 }, () => row()));
  show(<ListView kind="Issue" />);
  await waitFor(() => expect(rowTitles()).toHaveLength(30));
  observer.disconnect();
  vi.unstubAllGlobals();
  expect(counts).toEqual([10, 30]);
});

// A first open that waits for the detail's chunk took 0.8 s.
test("once its rows show, a list loads the detail's code, so the first open is at once", async () => {
  const preload = vi.spyOn(DetailView, "preload");
  history.replaceState(null, "", "#/list/Paper");
  servePapers();
  show(<ListView kind="Paper" />);
  await titles();
  await waitFor(() => expect(preload).toHaveBeenCalledTimes(1));
  preload.mockRestore();
});

test("COLD-01: a Paper with no authors renders in the Papers list", async () => {
  history.replaceState(null, "", "#/list/Paper");
  servePapers();
  show(<ListView kind="Paper" />);
  expect(await titles()).toEqual(PAPERS);
  expect(screen.getByText("Lovelace et al.")).toBeTruthy();
  expect(screen.getByText("NeurIPS")).toBeTruthy();
});

test.each(["Owner", "No grouping", "Status"])("COLD-01: a Paper with no authors renders grouped by %s", async (grouping) => {
  history.replaceState(null, "", "#/list/Paper");
  servePapers();
  show(<ListView kind="Paper" />);
  await titles();
  await pick(/^Group/, grouping);
  expect((await titles()).toSorted()).toEqual(PAPERS);
});

test("COLD-01: a Paper with no authors renders in a list of several kinds", async () => {
  servePapers();
  show(<InquiryList id="mixed" kinds={["Issue", "Paper"]} title="Mine" icon={<KindIcon kind="Issue" />} />);
  expect((await titles()).toSorted()).toEqual(["Row issue", ...PAPERS].toSorted());
  expect(screen.getByRole("button", { name: /Papers/ })).toBeTruthy();
});

test("j and k move the focus, and Enter opens the focused row", async () => {
  serve([row({ title: "Row a" }), row({ title: "Row b" }), row({ title: "Row c" })]);
  show(<ListView kind="Issue" />);
  await titles();
  const focused = () => document.querySelector('[aria-current="true"] .row-title')?.textContent;
  expect(focused()).toBe("Row c");
  const user = userEvent.setup();
  const key = (key: string) => user.keyboard(key);
  await key("j");
  expect(focused()).toBe("Row b");
  await key("j");
  await key("j");
  expect(focused()).toBe("Row a");
  await key("k");
  expect(focused()).toBe("Row b");
  const seq = screen.getByText("Row b").closest("a")!.getAttribute("href");
  await key("{Enter}");
  expect(location.hash).toBe(seq);
});

test("Space peeks at the focused row, the peek follows j, and Escape closes it", async () => {
  serve([row({ title: "Row a" }), row({ title: "Row b" })]);
  show(<ListView kind="Issue" />);
  await titles();
  const user = userEvent.setup();
  const key = (key: string) => user.keyboard(key);
  const peek = () => screen.queryByRole("complementary", { name: "Peek" });
  await key(" ");
  expect(within(peek()!).getByText(/Detail of/).textContent).toMatch(/Issue#\d+/);
  const first = within(peek()!).getByText(/Detail of/).textContent;
  await key("j");
  expect(within(peek()!).getByText(/Detail of/).textContent).not.toBe(first);
  await key("{Escape}");
  expect(peek()).toBeNull();
  expect(location.hash).toBe("#/list/Issue");
});

test("] collapses Peek to a strip that follows j; Space expands a collapsed Peek, and peeks expanded after a close", async () => {
  serve([row({ title: "Row a" }), row({ title: "Row b" })]);
  show(<ListView kind="Issue" />);
  await titles();
  const user = userEvent.setup();
  const key = (key: string) => user.keyboard(key);
  const peek = () => screen.queryByRole("complementary", { name: "Peek" });
  const detail = () => peek()!.querySelector("p")?.textContent ?? null;
  await key(" ]");
  expect(peek()!.classList).toContain("panel-strip");
  expect(detail()).toBeNull();
  const first = peek()!.textContent;
  await key("j");
  expect(peek()!.textContent).not.toBe(first);
  await key(" ");
  expect(detail()).toMatch(/^Detail of Issue#\d+$/);
  await key("]{Escape} ");
  expect(detail()).toMatch(/^Detail of Issue#\d+$/);
});

test("S8: a priority filter on Issues and Beliefs asks for Issues only, and says so", async () => {
  const sent = serve([row({ title: "Row issue", priority: 10 }), row({ kind: "Belief", title: "Row belief" })]);
  show(<InquiryList id="mixed" kinds={["Issue", "Belief"]} title="Mine" icon={<KindIcon kind="Issue" />} />);
  expect((await titles()).toSorted()).toEqual(["Row belief", "Row issue"]);

  await pick(/Filter/, "Priority", "P1 High");
  await waitFor(() => expect(screen.queryByText("Row belief")).toBeNull());
  expect(screen.getByText("Not shown: Beliefs. They lack a filtered field.")).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  const kinds = sent.map((request) => new URLSearchParams(request.query).getAll("kind"));
  expect(kinds.at(-1)).toEqual(["Issue"]);
  expect(screen.getByTitle("The same query from the CLI").textContent).toBe(
    "trax issue status is active priority ge 10 priority lt 20",
  );
});

test("Me in the owner filter matches the email and every alias ticked in Settings (the plan's Who me is)", async () => {
  const aliases = ["ada", "Agent Ada"];
  localStorage.setItem(storageKey(PROFILE.email), JSON.stringify({ ...EMPTY_STATE, aliases }));
  const sent = serve([row({ owner: "ada" })]);
  show(<ListView kind="Issue" />);
  await titles();
  await pick(/Filter/, "Owner", /^Me/);
  const me = meFilter("owner", meNames(PROFILE.email, aliases));
  await waitFor(() => expect(lastFilters(sent)).toEqual(["status is active", `owner re ${me.value}`]));
  expect(screen.getByTitle("The same query from the CLI").textContent).toBe(
    `trax issue status is active owner re '${me.value}'`,
  );
  localStorage.clear();
});

test.each([
  { label: "an empty state", saved: "{}" },
  { label: "a tab this build does not have", saved: JSON.stringify({ tab: "open", choices: [], grouping: "none", ordering: "created", pages: {}, collapsed: [], focus: null }) },
  {
    label: "priorities that are not one range",
    saved: JSON.stringify({ tab: "all", choices: [{ field: "priority", values: ["0", "2"] }], grouping: "none", ordering: "created", pages: {}, collapsed: [], focus: null }),
  },
  {
    label: "pages and collapsed groups of the wrong type",
    saved: JSON.stringify({ tab: "all", choices: [], grouping: "none", ordering: "created", pages: { Issue: "many" }, collapsed: 7, focus: null }),
  },
])("a list state this build cannot read falls back to the list's start (STATE-01): $label", async ({ saved }) => {
  sessionStorage.setItem("trackinizer.v2.list.Issue", saved);
  const sent = serve([row({ title: "Row back" })]);
  show(<ListView kind="Issue" />);
  expect(await titles(), saved).toEqual(["Row back"]);
  expect(lastFilters(sent), saved).toEqual(["status is active"]);
});

test("a copy the browser refuses shows a failure toast, not a success (WEB-26)", async () => {
  serve([row()]);
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: () => Promise.reject(new Error("denied")) } });
  show(<ListView kind="Issue" />);
  await titles();
  fireEvent.click(screen.getByRole("button", { name: "Copy as a trax command" }));
  const toast = await screen.findByText("Could not copy: the browser refused the clipboard.");
  expect(toast.closest(".toast")!.querySelector(".ic.failed")).not.toBeNull();
});

test("the footer names this platform's palette key (WEB-19)", async () => {
  serve([row()]);
  show(<ListView kind="Issue" />);
  await titles();
  expect(document.querySelector(".list-foot")!.textContent).toMatch(/Ctrl\+K commands$/);
});

test("MENU-01: Enter picks the active option, the first match; what was typed is the last option", async () => {
  const sent = serve([row({ owner: "josh" }), row({ owner: "joan" })]);
  show(<ListView kind="Issue" />);
  await titles();
  fireEvent.click(screen.getByRole("button", { name: /Filter/ }));
  fireEvent.click(await screen.findByRole("option", { name: "Owner" }));
  const search = screen.getByRole("combobox");
  fireEvent.change(search, { target: { value: "jo" } });
  expect(screen.getAllByRole("option").map((option) => option.querySelector(".lbl")!.textContent)).toEqual([
    "joan",
    "josh",
    "Owner is “jo”",
  ]);
  fireEvent.keyDown(search, { key: "Enter" });
  await waitFor(() => expect(lastFilters(sent)).toEqual(["status is active", "owner is joan"]));
  fireEvent.change(search, { target: { value: "jo" } });
  fireEvent.keyDown(search, { key: "ArrowUp" });
  fireEvent.keyDown(search, { key: "Enter" });
  await waitFor(() => expect(lastFilters(sent)).toEqual(["status is active", "owner re ^(joan|jo)$"]));
});

test("COLLAPSE-QUERY-01: a collapsed group stays collapsed across a tab or filter change, as the mock keeps it", async () => {
  serve([row({ title: "Row open" }), row({ title: "Row done", status: "complete" })]);
  show(<ListView kind="Issue" />);
  await titles();
  await pick(/^Group/, "Status");
  const group = (name: string) => [...document.querySelectorAll<HTMLElement>(".group-h")].find((h) => h.textContent!.startsWith(name));
  await waitFor(() => expect(group("Active")).toBeTruthy());
  fireEvent.click(group("Active")!);
  expect(group("Active")!.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  await waitFor(() => expect(group("Complete")).toBeTruthy());
  expect(group("Active")!.getAttribute("aria-expanded")).toBe("false");
  expect(rowTitles()).toEqual(["Row done"]);
});

test("WEB-15: a filter the server refuses shows its message by the filters, and removing the filter recovers", { tags: ["manual"] }, async () => {
  // Four of these make `^(…|…)$` 539 characters, past the server's 512.
  const owners = Array.from({ length: 5 }, (_, n) => `owner-${n}-${"x".repeat(123)}`);
  vi.stubGlobal("fetch", async (request: Request) => {
    const filters = new URL(request.url).searchParams.getAll("filter").map((raw) => JSON.parse(raw) as { value: string });
    if (filters.some((filter) => filter.value.length > 512)) {
      return Response.json({ detail: "filter value exceeds 512 characters" }, { status: 400 });
    }
    return Response.json(owners.map((owner) => row({ owner })));
  });
  show(<ListView kind="Issue" />);
  await titles();
  await pick(/Filter/, "Owner", ...owners.slice(0, 4));
  expect((await screen.findByRole("alert")).textContent).toBe("filter value exceeds 512 charactersRetry");
  fireEvent.keyDown(screen.getByRole("combobox"), { key: "Escape" });
  fireEvent.click(screen.getByRole("button", { name: "Remove the Owner filter" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(rowTitles()).toHaveLength(5);
});

test("keys an input method is composing stay the input method's: Enter picks nothing, Backspace goes nowhere (R2-X1)", async () => {
  const sent = serve([row({ owner: "josh" })]);
  show(<ListView kind="Issue" />);
  await titles();
  fireEvent.click(screen.getByRole("button", { name: /Filter/ }));
  fireEvent.keyDown(await screen.findByRole("combobox", { name: "Filter by…" }), { key: "Enter", isComposing: true });
  expect(screen.getByRole("combobox", { name: "Filter by…" })).toBeTruthy();
  fireEvent.click(screen.getByRole("option", { name: "Owner" }));
  const search = screen.getByRole("combobox", { name: "Owner is…" });
  fireEvent.keyDown(search, { key: "Backspace", isComposing: true });
  fireEvent.keyDown(search, { key: "Enter", isComposing: true });
  expect(screen.getByRole("combobox", { name: "Owner is…" })).toBeTruthy();
  expect(lastFilters(sent)).toEqual(["status is active"]);
});

test("after clicks on a tab, the view switch and a group's heading, Enter opens the focused row rather than pressing a button again (BF1, README TODO 1)", async () => {
  serve([row({ title: "Row a" }), row({ title: "Row b" })]);
  show(<ListView kind="Issue" />);
  expect(await titles()).toEqual(["Row b", "Row a"]);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Active" }));
  await user.click(screen.getByRole("button", { name: "List" }));
  const heading = document.querySelector<HTMLElement>(".group-h")!;
  await user.click(heading);
  await user.click(heading);
  await user.keyboard("j");
  const href = screen.getByText("Row a").closest("a")!.getAttribute("href");
  await user.keyboard("{Enter}");
  expect(location.hash).toBe(href);
});

test("a group's count says when Load more may find more of it (BF1, README TODO 3)", async () => {
  // A full page of Issues, so more may wait; one Paper, which is all of them.
  serve([...Array.from({ length: 20 }, () => row()), row({ kind: "Paper", title: "A paper" })]);
  show(<InquiryList id="mixed" kinds={["Issue", "Paper"]} title="Mine" icon={<KindIcon kind="Issue" />} />);
  await waitFor(() => expect(rowTitles()).toHaveLength(21));
  const counts = [...document.querySelectorAll(".group-h")].map((group) => group.querySelector(".count")!.textContent);
  expect(counts).toEqual(["20+", "1"]);
});

test("with a label filter on, the label menu still offers the labels the list loaded before it (BF1, README TODO 4)", async () => {
  const rows = [row({ title: "Alpha row", labels: ["alpha"] }), row({ title: "Beta row", labels: ["beta"] })];
  // The server keeps only rows with the filtered label.
  const sent = stubFetch((request) => {
    const label = new URL(request.url).searchParams
      .getAll("filter")
      .map((raw) => JSON.parse(raw) as { field: string; value: string })
      .find((filter) => filter.field === "labels")?.value;
    return Response.json(rows.filter((r) => !label || r.labels?.includes(label)));
  });
  show(<ListView kind="Issue" />);
  await titles();
  await pick(/Filter/, "Label", "alpha");
  await waitFor(() => expect(lastFilters(sent)).toEqual(["status is active", "labels is alpha"]));
  await waitFor(() => expect(rowTitles()).toEqual(["Alpha row"]));
  expect(screen.getAllByRole("option").map((option) => option.querySelector(".lbl")!.textContent)).toEqual(["alpha", "beta"]);
});

/**
 * Answer `GET /api/inquiries` as the server does for one kind: `status is`, and
 * `created le` compared as the server's text, applied before `offset` and
 * `limit`, newest created first.
 */
function serveFiltered(rows: () => readonly InquiryRow[]): Sent[] {
  return stubFetch((request) => {
    const query = new URL(request.url).searchParams;
    const filters = query.getAll("filter").map((raw) => JSON.parse(raw) as { field: string; op: string; value: string });
    const matching = rows()
      .filter((row) =>
        filters.every(({ field, op, value }) =>
          field === "status" ? row.status === value : field !== "created" || (op === "le" && row.created.replace("T", " ") <= value),
        ),
      )
      .toSorted((a, b) => (a.created < b.created ? 1 : -1));
    const offset = Number(query.get("offset") ?? 0);
    return Response.json(matching.slice(offset, offset + Number(query.get("limit"))));
  });
}

test("Load more continues after the last row loaded, so a row leaving ahead of it skips nothing (PAGE-02)", { tags: ["manual"] }, async () => {
  // 55 Issues, one a second; the newest 50 fill the first page.
  let rows = Array.from({ length: 55 }, (_, n) =>
    row({ title: `Row at ${n}`, created: new Date(Date.UTC(2026, 8, 20, 0, 0, n, 250)).toISOString().replace("Z", "000+00:00") }),
  );
  serveFiltered(() => rows);
  show(<ListView kind="Issue" />);
  await waitFor(() => expect(rowTitles()).toHaveLength(50));
  // One loaded row closes before Load more: the server's order moves up by one.
  rows = rows.map((r) => (r.title === "Row at 40" ? { ...r, status: "complete" } : r));
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  await waitFor(() => expect(rowTitles()).toHaveLength(55));
  expect(rowTitles()).toContain("Row at 4");
  expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
});
