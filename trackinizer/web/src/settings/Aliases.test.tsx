import { QueryClient } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { stubFetch } from "../api/testing";
import { storageKey } from "../state/store";
import { AliasesSection } from "./Aliases";
import { ownerNames } from "./owners";
import { KINDS, profile, renderScreen, serveAccount } from "./testing";

const ADA = profile("writer");

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function stored(): unknown {
  return JSON.parse(localStorage.getItem(storageKey(ADA.email)) ?? "null");
}

function names() {
  return within(screen.getByRole("list", { name: "Your names" }))
    .getAllByRole("listitem")
    .map((item) => item.firstChild?.textContent);
}

test("owner names are counted, most frequent first, then by name", () => {
  const rows = [{ owner: "Agent" }, { owner: "dan" }, { owner: "Agent" }, { owner: null }, { owner: "/root" }, { owner: "dan" }, { owner: "Agent" }];
  expect(ownerNames(rows)).toEqual([
    { name: "Agent", rows: 3 },
    { name: "dan", rows: 2 },
    { name: "/root", rows: 1 },
  ]);
});

test("suggestions come from one list request, and none is ticked until the user ticks it", async () => {
  const server = serveAccount(ADA);
  server.rows = [{ owner: "Agent" }, { owner: "Agent" }, { owner: "ada" }];
  renderScreen(<AliasesSection />, ADA);
  const suggestions = await screen.findByRole("list", { name: "Owner names on rows under your account" });
  const [request] = server.sent.filter((sent) => sent.path === "/api/inquiries");
  const query = new URLSearchParams(request!.query);
  expect(query.getAll("kind")).toEqual(KINDS);
  expect(query.getAll("filter").map((filter) => JSON.parse(filter))).toEqual([
    { field: "account", op: "is", value: "ada@example.com" },
    { field: "owner", op: "notnull", value: "" },
    { field: "owner", op: "ne", value: "ada@example.com" },
  ]);
  expect([query.get("limit"), query.get("offset")]).toEqual(["200", "0"]);

  const boxes = within(suggestions).getAllByRole("checkbox") as HTMLInputElement[];
  expect(boxes.map((box) => [box.parentElement!.textContent, box.checked])).toEqual([
    ["Agent2 rows", false],
    ["ada1 row", false],
  ]);
  expect(names()).toEqual(["ada@example.com"]);
  expect(localStorage.length).toBe(0);

  fireEvent.click(within(suggestions).getByRole("checkbox", { name: /^ada/ }));
  expect(names()).toEqual(["ada@example.com", "ada"]);
  expect(stored()).toMatchObject({ aliases: ["ada"] });
  fireEvent.click(within(suggestions).getByRole("checkbox", { name: /^ada/ }));
  expect(names()).toEqual(["ada@example.com"]);
  expect(stored()).toMatchObject({ aliases: [] });
});

/** Owner rows for `count` names, `agent-0` the most frequent: `agent-i` owns `count - i` rows. */
function ranked(count: number): { owner: string }[] {
  return Array.from({ length: count }, (_, i) => Array.from({ length: count - i }, () => ({ owner: `agent-${i}` }))).flat();
}

function suggestionNames(): string[] {
  const list = screen.getByRole("list", { name: "Owner names on rows under your account" });
  return within(list)
    .getAllByRole("checkbox")
    .map((box) => box.nextElementSibling!.textContent!);
}

test("the 8 most frequent owner names show, and Show all lists the rest", async () => {
  const server = serveAccount(ADA);
  server.rows = ranked(11);
  renderScreen(<AliasesSection />, ADA);
  await screen.findByRole("list", { name: "Owner names on rows under your account" });
  expect(suggestionNames()).toEqual(["agent-0", "agent-1", "agent-2", "agent-3", "agent-4", "agent-5", "agent-6", "agent-7"]);
  fireEvent.click(screen.getByRole("button", { name: "Show all 11" }));
  expect(suggestionNames()).toHaveLength(11);
  expect(screen.queryByRole("button", { name: /^Show all/ })).toBeNull();
});

test("typing in the name field narrows the owner names to those holding it, past the first 8", async () => {
  const server = serveAccount(ADA);
  server.rows = [...ranked(9), { owner: "Dan-bot" }, { owner: "dan" }];
  renderScreen(<AliasesSection />, ADA);
  await screen.findByRole("list", { name: "Owner names on rows under your account" });
  const field = screen.getByRole("textbox", { name: "Name" });
  fireEvent.change(field, { target: { value: " DAN" } });
  expect(suggestionNames()).toEqual(["dan", "Dan-bot"]);
  expect(screen.queryByRole("button", { name: /^Show all/ })).toBeNull();
  fireEvent.change(field, { target: { value: "zz" } });
  expect(screen.queryByRole("list", { name: "Owner names on rows under your account" })).toBeNull();
  expect(screen.getByText("No owner name holds “zz”.")).toBeTruthy();
  fireEvent.change(field, { target: { value: "" } });
  expect(suggestionNames()).toHaveLength(8);
});

test("a name can be typed, is added once, and removed with its button; the email stays", async () => {
  serveAccount(ADA);
  renderScreen(<AliasesSection />, ADA);
  const field = screen.getByRole("textbox", { name: "Name" });
  for (const typed of [" dan ", "dan", "ada@example.com", "  "]) {
    fireEvent.change(field, { target: { value: typed } });
    fireEvent.submit(field);
  }
  expect(names()).toEqual(["ada@example.com", "dan"]);
  expect(screen.queryByRole("button", { name: "Remove ada@example.com" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Remove dan" }));
  expect(names()).toEqual(["ada@example.com"]);
  await screen.findByText("No other owner names on rows under your account.");
});

test("a name the browser refuses to store stays typed, with the reason", () => {
  serveAccount(ADA);
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("The quota has been exceeded.");
  });
  renderScreen(<AliasesSection />, ADA);
  const field = screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement;
  fireEvent.change(field, { target: { value: "dan" } });
  fireEvent.submit(field);
  expect(screen.getByRole("alert").textContent).toBe("Not saved: The quota has been exceeded.");
  expect(field.value).toBe("dan");
  expect(names()).toEqual(["ada@example.com"]);
});

test("a failed refetch keeps the suggestions on screen, beside the error and Retry", async () => {
  const server = serveAccount(ADA);
  server.rows = [{ owner: "Agent" }];
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderScreen(<AliasesSection />, ADA, queryClient);
  await screen.findByRole("checkbox", { name: /^Agent/ });
  server.failing.add("/api/inquiries");
  await act(() => queryClient.refetchQueries());
  expect((await screen.findByRole("alert")).textContent).toContain("database unavailable");
  expect(screen.getByRole("checkbox", { name: /^Agent/ })).toBeTruthy();
});

test("a failed suggestion request shows the server's message and Retry", async () => {
  let fail = true;
  stubFetch(() =>
    fail
      ? Response.json({ detail: "canceling statement due to statement timeout" }, { status: 400 })
      : Response.json([{ owner: "dan" }]),
  );
  renderScreen(<AliasesSection />, ADA);
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("canceling statement due to statement timeout");
  fail = false;
  fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(screen.getByRole("checkbox", { name: /^dan/ })).toBeTruthy());
});
