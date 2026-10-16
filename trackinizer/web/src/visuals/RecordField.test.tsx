import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Detail, DetailRow } from "../api/detail";
import type { InquiryRow } from "../api/inquiries";
import { stubFetch } from "../api/testing";
import { type Meta, MetaContext } from "../app/boot";
import type { FieldProps } from "../composer/Composer";
import { markOpened } from "../detail/queries";
import { SEARCH_DEBOUNCE_MS } from "../palette/sources";
import { RecordField } from "./RecordField";

const KINDS = ["Issue", "Belief", "AgentSession"];
const META: Meta = { kinds: KINDS, enums: { inquiry_kind_all: KINDS }, fieldOwners: {}, edges: {} };

function uuid(n: number): string {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

function row(kind: string, seq: number, title: string): InquiryRow & DetailRow {
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
  };
}

function detailOf(self: DetailRow): Detail {
  return { self, edges: {}, backlinks: {}, changes: [] };
}

/**
 * Serve Issue searches with `hits(q)`, a list of a kind's newest rows from `rows`,
 * `Kind#seq` lookups from `rows`, and each row's detail.
 */
function serve(rows: readonly DetailRow[], hits: (q: string) => DetailRow[] = () => []) {
  return stubFetch((request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/web/search") {
      return Response.json(url.searchParams.get("kind") === "Issue" ? hits(url.searchParams.get("q")!) : []);
    }
    if (url.pathname === "/api/inquiries") {
      const kinds = url.searchParams.getAll("kind");
      return Response.json(rows.filter((one) => kinds.includes(one.kind)).slice(0, Number(url.searchParams.get("limit"))));
    }
    const ref = /^\/api\/inquiries\/(\w+)\/(\d+)$/.exec(url.pathname);
    const found = ref
      ? rows.find((one) => one.kind === ref[1] && one.seq === Number(ref[2]))
      : rows.find((one) => url.pathname === `/api/web/get/${one.id}`);
    if (!found) return Response.json({ detail: "not found" }, { status: 404 });
    return Response.json(ref ? found : detailOf(found));
  });
}

/** A cache holding a list page of `rows`, and `opened` as details opened in that order. */
function cacheOf(rows: readonly InquiryRow[], opened: readonly DetailRow[] = []): QueryClient {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["inquiries", "list", [], "Issue", 50, 0], rows);
  for (const self of opened) {
    queryClient.setQueryData(["detail", self.id], detailOf(self));
    vi.advanceTimersByTime(1);
    markOpened(queryClient, self.id);
  }
  return queryClient;
}

/** The record field over `queryClient`, focused; its box's own Enter calls `sent`. */
function show(queryClient: QueryClient) {
  const sent = vi.fn();
  function Host() {
    const [value, setValue] = useState("");
    const field: FieldProps = {
      id: "box",
      value,
      placeholder: "",
      disabled: false,
      rows: 3,
      onChange: (event) => setValue(event.currentTarget.value),
      onKeyDown: (event) => {
        if (!event.defaultPrevented && event.key === "Enter") sent(event.altKey ? "alt" : "plain");
      },
    };
    return <RecordField field={field} edit={setValue} />;
  }
  render(
    <QueryClientProvider client={queryClient}>
      <MetaContext value={META}>
        <Host />
      </MetaContext>
    </QueryClientProvider>,
  );
  const box = screen.getByRole<HTMLTextAreaElement>("textbox");
  box.focus();

  const type = (text: string, caret = text.length) => {
    fireEvent.change(box, { target: { value: text } });
    box.setSelectionRange(caret, caret);
    fireEvent.keyUp(box, { key: "ArrowLeft" });
  };
  return { sent, box, type };
}

function offered(): string[] {
  return screen.queryAllByRole("option").map((option) => option.title);
}

async function pass(ms = 0) {
  await act(() => vi.advanceTimersByTimeAsync(ms));
  await act(() => vi.advanceTimersByTimeAsync(0));
}

beforeEach(() => {
  // jsdom has no ResizeObserver, which the open list uses.
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  vi.setSystemTime(new Date("2026-09-26T12:34:56Z"));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("@ at the start or after a space lists recent then cached records; in a word it is an email", () => {
  serve([]);
  const { type } = show(cacheOf([row("Issue", 1, "Upload"), row("Issue", 2, "Retry")], [row("Belief", 3, "Opened")]));
  type("@");
  expect(offered()).toEqual(["Belief#3", "Issue#2", "Issue#1"]);
  expect(screen.getByRole("listbox", { name: "Records" })).toBeTruthy();
  type("mail me@ex");
  expect(offered()).toEqual([]);
  type("see @ret");
  expect(offered()).toEqual(["Issue#2"]);
});

test("bare @ without cached records shows a dismissible search hint, not an option", () => {
  serve([]);
  const { box, type } = show(cacheOf([]));
  type("@");
  expect(screen.getByRole("status").textContent).toBe("Type to search records…");
  expect(offered()).toEqual([]);
  expect(box.getAttribute("aria-activedescendant")).toBeNull();
  expect(fireEvent.keyDown(box, { key: "ArrowDown" })).toBe(true);
  expect(fireEvent.keyDown(box, { key: "Tab" })).toBe(true);
  expect(box.value).toBe("@");
  fireEvent.keyDown(box, { key: "Escape" });
  expect(screen.queryByRole("status")).toBeNull();
  type("");
  type("@");
  expect(screen.getByRole("status")).toBeTruthy();
  fireEvent.blur(box);
  expect(screen.queryByRole("status")).toBeNull();
});

test("the cached match shows at once; the server's hits after the pause, once each", async () => {
  const server = serve([], () => [row("Issue", 2, "Retry"), row("Issue", 9, "Retry forever")]);
  const { type } = show(cacheOf([row("Issue", 2, "Retry")]));
  type("@retry");
  await pass(SEARCH_DEBOUNCE_MS - 1);
  expect(offered()).toEqual(["Issue#2"]);
  expect(server.filter((request) => request.path === "/api/web/search")).toEqual([]);
  await pass(1);
  expect(offered()).toEqual(["Issue#2", "Issue#9"]);
});

test("a pick inserts Kind#seq and a space, keeping the suffix, the caret after it; Enter does not send", () => {
  serve([]);
  const { sent, box, type } = show(cacheOf([row("Issue", 1, "Upload"), row("Issue", 2, "Retry")]));
  type("fix @up now", 7);
  expect(offered()).toEqual(["Issue#1"]);
  fireEvent.keyDown(box, { key: "Enter" });
  expect(box.value).toBe("fix Issue#1 now");
  expect([box.selectionStart, box.selectionEnd]).toEqual([12, 12]);
  expect(offered()).toEqual([]);
  expect(sent).not.toHaveBeenCalled();
  type("@");
  fireEvent.keyDown(box, { key: "Tab" });
  expect(box.value).toBe("Issue#2 ");
});

test("an exact Kind#seq outside the cache is offered once the server finds it, and never when it does not", async () => {
  serve([row("Issue", 7, "Far away")]);
  const { type } = show(cacheOf([]));
  type("@issue#7");
  expect(offered()).toEqual([]);
  await pass();
  await pass();
  expect(offered()).toEqual(["Issue#7"]);
  expect(screen.getByRole("option").textContent).toContain("Far away");
  type("@Issue#8");
  await pass();
  await pass();
  expect(offered()).toEqual([]);
});

test("arrows wrap, Esc closes without leaving the box, and modified or composing keys never pick", () => {
  serve([]);
  const { sent, box, type } = show(cacheOf([row("Issue", 1, "Upload"), row("Issue", 2, "Retry")]));
  type("@");
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(screen.getByRole("option", { selected: true }).title).toBe("Issue#1");
  fireEvent.keyDown(box, { key: "Enter", altKey: true });
  fireEvent.keyDown(box, { key: "Tab", ctrlKey: true });
  fireEvent.keyDown(box, { key: "Enter", isComposing: true });
  expect(box.value).toBe("@");
  expect(sent).toHaveBeenCalledWith("alt");
  const page = vi.fn();
  document.addEventListener("keydown", page);
  fireEvent.keyDown(box, { key: "Escape" });
  expect(offered()).toEqual([]);
  document.removeEventListener("keydown", page);
  expect(page).not.toHaveBeenCalled();
  fireEvent.keyDown(box, { key: "Enter" });
  expect(sent).toHaveBeenLastCalledWith("plain");
});

test("@Ag matches records by their kind, any case, cached at once and on a cold cache from the kind's list", async () => {
  const sessions = [row("AgentSession", 4, "Bisect"), row("AgentSession", 5, "Rerun")];
  const server = serve(sessions);
  const { type } = show(cacheOf([row("Issue", 1, "Agenda"), row("Issue", 2, "Unrelated")]));
  type("@ag");
  // "Agenda" holds the text; no AgentSession is held yet, and the search is due, so the list stays open.
  expect(offered()).toEqual(["Issue#1"]);
  type("@Ag");
  await pass(SEARCH_DEBOUNCE_MS);
  await pass();
  expect(offered()).toEqual(["Issue#1", "AgentSession#4", "AgentSession#5"]);
  const listed = server.filter((request) => request.path === "/api/inquiries");
  expect(listed.map((request) => new URLSearchParams(request.query).getAll("kind"))).toEqual([["AgentSession"]]);
  type("@agentsession#5");
  await pass();
  expect(offered()).toContain("AgentSession#5");
  expect(offered()).not.toContain("AgentSession#4");
});

test("after @, unmatched text hides the box while searching and after no results", async () => {
  serve([]);
  const { type } = show(cacheOf([]));
  type("@");
  expect(screen.getByRole("status")).toBeTruthy();
  for (const text of ["@b", "@be", "@bet"]) {
    type(text);
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByRole("listbox")).toBeNull();
    await pass(SEARCH_DEBOUNCE_MS);
    await pass();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByRole("listbox")).toBeNull();
  }
  type("@");
  expect(screen.getByRole("status")).toBeTruthy();
});

test("refining the text keeps earlier hits that still match while the next search waits, and drops those that do not", async () => {
  serve([], (q) => (q === "re" ? [row("Issue", 8, "Retry forever"), row("Issue", 9, "Rename")] : []));
  const { type } = show(cacheOf([]));
  type("@re");
  await pass(SEARCH_DEBOUNCE_MS);
  await pass();
  expect(offered()).toEqual(["Issue#8", "Issue#9"]);
  // An `@` word holds no space, so refining it is typing more of it; the next search is still due.
  type("@ret");
  expect(offered()).toEqual(["Issue#8"]);
});

test("a full Kind#seq offers only that record from the kind's list, and none when it is missing", async () => {
  serve([row("AgentSession", 4, "Bisect"), row("AgentSession", 5, "Rerun")]);
  const { type } = show(cacheOf([]));
  type(`@AgentSession#${"9".repeat(4)}`);
  await pass(SEARCH_DEBOUNCE_MS);
  await pass();
  await pass();
  expect(offered()).toEqual([]);
  expect(screen.queryByRole("status")).toBeNull();
});

test("a failed search without suggestions leaves the box hidden", async () => {
  stubFetch(() => Response.json({ detail: "database unavailable" }, { status: 503 }));
  const { type } = show(cacheOf([]));
  type("@zzz");
  await pass(SEARCH_DEBOUNCE_MS);
  await pass();
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.queryByRole("listbox")).toBeNull();
});

test("once the refined search answers, its answer stands: a held hit it leaves out goes", async () => {
  serve([], (q) => (q === "re" ? [row("Issue", 8, "Retry forever")] : []));
  const { type } = show(cacheOf([]));
  type("@re");
  await pass(SEARCH_DEBOUNCE_MS);
  await pass();
  type("@ret");
  expect(offered()).toEqual(["Issue#8"]);
  await pass(SEARCH_DEBOUNCE_MS);
  await pass();
  expect(offered()).toEqual([]);
  expect(screen.queryByRole("status")).toBeNull();
});

test("a new @ starts afresh: hits held for an earlier one are not offered", async () => {
  serve([], (q) => (q === "re" ? [row("Issue", 8, "Retry forever")] : []));
  const { type } = show(cacheOf([]));
  type("@re");
  await pass(SEARCH_DEBOUNCE_MS);
  await pass();
  type("");
  type("@ret");
  expect(offered()).toEqual([]);
});

test("the server's hits for a field regex are offered as it answers them, though the page cannot run the regex", async () => {
  serve([], (q) => (q === "title:^Re" ? [row("Issue", 8, "Retry forever")] : []));
  const { type } = show(cacheOf([]));
  type("@title:^Re");
  await pass(SEARCH_DEBOUNCE_MS);
  await pass();
  expect(offered()).toEqual(["Issue#8"]);
});

test("@session#4 finds AgentSession#4 on a cold cache: a kind matches wherever its name holds the letters", async () => {
  serve([row("AgentSession", 4, "Bisect"), row("AgentSession", 5, "Rerun")]);
  const { type } = show(cacheOf([]));
  type("@session#4");
  await pass(SEARCH_DEBOUNCE_MS);
  await pass();
  await pass();
  expect(offered()).toEqual(["AgentSession#4"]);
});
