import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { BalanceBody } from "../api/billing";
import type { Profile } from "../api/me";
import { NO_ASSISTANT } from "../api/providers";
import { AGREED, type Sent, stubFetch } from "../api/testing";
import type { TerminalMachine } from "../api/terminals";
import type { WorkspacePartner, WorkspaceState } from "../api/workspaces";
import { ProfileContext } from "../app/boot";
import { ProviderPicker } from "./ProviderPicker";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  localStorage.clear();
});

const WORKSPACE_ID = "c5286865-67b6-4bd8-ab51-e06e10c326c5";
const LIVE_CLAUDE = { session_id: "s-1", actor: null, cli: "claude", kind: "shared", status: "live", label: "claude · gx10", provider: "claude", machine: "gx10" } as const;
const LIVE_IN_PROJECT: WorkspacePartner = { ...LIVE_CLAUDE, cwd: "~/project" };
const LIVE_ASSISTANT = { session_id: "kb", actor: "scout", cli: "sagent", kind: "shared", status: "live", label: "scout", provider: "assistant", machine: null } as const;
const UNAVAILABLE = { session_id: null, actor: "scout", cli: null, kind: "shared", status: "unavailable", label: "scout", provider: "assistant", machine: null } as const;
const GX10: TerminalMachine = { name: "gx10", status: "online", terminal_ready: true, confirmed: true, clis: ["claude", "codex"] };
const LAPTOP: TerminalMachine = { name: "laptop", status: "online", terminal_ready: true, confirmed: true, clis: ["codex"] };
const BOX: TerminalMachine = { name: "box", status: "online", terminal_ready: false, confirmed: true, clis: ["claude"] };
const CODEX_REASON = "Connect a machine and install codex on it";
const CLAUDE_REASON = "Connect a machine and install claude on it";
const BALANCE: BalanceBody = { usd: 498.2, granted_usd: 500, purchased_usd: 0, spent_usd: 1.8, topup_usd: [10, 25], enforced: true, grace_until: null, own_login: false,
  can_run: true, reason: "You have credit left." };
const SPENT: BalanceBody = { ...BALANCE, usd: 0, spent_usd: 500, can_run: false, reason: "You are out of credit." };

type Options = {
  readonly machines?: readonly TerminalMachine[];
  /** A ledger's balance; none (the default) is a site with no ledger, a 404. */
  readonly balance?: BalanceBody;
  /** The server refuses every provider choice with this reason, as a 409. */
  readonly refuse?: string;
  readonly preferences?: Record<string, unknown>;
  /** The partner the server names after a CLI is chosen; by default its run is offline, naming the chat's session. */
  readonly cliPartner?: WorkspaceState["partner"];
};

/** A CLI's partner with no run on the chat, naming the chat's session a run attaches to (`cli_partner`). */
function offline(cli: "claude" | "codex", session: string | null = "s-chat") {
  return { session_id: session, actor: cli, cli, kind: "shared", status: "offline", label: `${cli}: no run`, provider: cli, machine: null } as const;
}

/** The station as the server holds it: what a choice changes, and what a poll reads. */
function station(over: Partial<WorkspaceState> = {}): WorkspaceState {
  return { id: WORKSPACE_ID, name: "Scratch chat", conversation_id: "c1", page: null, revision: 3, focused_instance: null, visuals: [],
    partner: UNAVAILABLE, assistant: "scout", ...over } as WorkspaceState;
}

/** A stand-in for the server's routes the picker calls; `held.station` is what it holds now. */
function server(initial: WorkspaceState, options: Options) {
  const held = { station: initial, preferences: options.preferences ?? {} };
  const sent: Sent[] = stubFetch(async (request) => {
    const path = new URL(request.url).pathname;
    const text = request.method === "GET" ? "" : await request.clone().text();
    const body = text ? JSON.parse(text) as Record<string, unknown> : undefined;
    if (request.method === "GET" && path === "/api/addons/billing/balance") {
      return options.balance ? Response.json(options.balance) : Response.json({ detail: "Not Found" }, { status: 404 });
    }
    if (request.method === "GET" && path === "/api/terminals/machines") return Response.json({ machines: options.machines ?? [] });
    if (request.method === "GET" && path === `/api/workspaces/${WORKSPACE_ID}`) return Response.json(held.station);
    if (request.method === "POST" && path === `/api/workspaces/${WORKSPACE_ID}/operations`) {
      if (options.refuse) return Response.json({ detail: options.refuse, code: "provider_unavailable" }, { status: 409 });
      const { provider } = body?.operation as { provider: "assistant" | "claude" | "codex" };
      const partner = provider === "assistant" ? held.station.partner : options.cliPartner ?? offline(provider);
      held.station = { ...held.station, provider, partner, revision: held.station.revision + 1 };
      return Response.json(held.station);
    }
    if (request.method === "PATCH" && path === "/api/me/preferences") {
      held.preferences = { ...held.preferences, ...body };
      return Response.json(held.preferences);
    }
    if (request.method === "POST" && path === "/api/terminals") return Response.json({ terminal_id: "t1", ticket: "tk" });
    if (request.method === "POST" && path.startsWith("/api/terminals/machines/") && path.endsWith("/confirm")) {
      // An allowed machine that has a CLI makes that CLI available on the next read.
      const name = path.split("/")[4];
      const clis = (options.machines ?? []).find((each) => each.name === name)?.clis ?? [];
      held.station = { ...held.station, available: { ...held.station.available, ...Object.fromEntries(clis.map((each) => [each, true])) } };
      return Response.json({ ok: true });
    }
    return Response.json({}, { status: 404 });
  });
  return { sent, held };
}

const PROFILE = { ...AGREED, user_id: "u1", email: "ada@example.com", name: "Ada", role: "writer", last_login: null, visual_workspace_enabled: true };

/** The picker as Chat renders it: its station read from the query cache the stream keeps. */
function Host({ initial }: { readonly initial: WorkspaceState }) {
  const { data } = useQuery({ queryKey: ["workspace", initial.id], queryFn: () => initial, initialData: initial, staleTime: Infinity });
  return <ProviderPicker station={data} />;
}

function picker(initial: WorkspaceState, options: Options = {}) {
  const { sent, held } = server(initial, options);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const profile = { ...PROFILE, preferences: options.preferences } as unknown as Profile;
  render(<QueryClientProvider client={client}><ProfileContext value={profile}>
    <Host initial={initial} />
  </ProfileContext></QueryClientProvider>);
  return { sent, held, client };
}

const posted = (sent: Sent[]) => sent.filter((each) => each.method !== "GET").map((each) => [each.method, each.path, each.body]);

test("a machine just connected waits for its owner's Allow in the card, which makes its CLI available", async () => {
  const fresh: TerminalMachine = { name: "newbox", status: "online", terminal_ready: true, confirmed: false, clis: ["claude"] };
  const { sent } = picker(station({ available: { claude: false }, reasons: { claude: CLAUDE_REASON } }), { machines: [fresh] });
  expect((screen.getByRole("radio", { name: "claude" }) as HTMLInputElement).disabled).toBe(true);
  fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
  await waitFor(() => expect((screen.getByRole("radio", { name: "claude" }) as HTMLInputElement).disabled).toBe(false));
  expect(posted(sent)).toEqual([["POST", "/api/terminals/machines/newbox/confirm", undefined]]);
});

test("no machine is listed while every provider is available and the chosen one needs none", async () => {
  const { sent } = picker(station({ provider: "claude", partner: LIVE_CLAUDE }), { machines: [GX10] });
  await waitFor(() => expect(sent.some((each) => each.path === "/api/addons/billing/balance")).toBe(true));
  expect(sent.some((each) => each.path === "/api/terminals/machines")).toBe(false);
  expect(screen.queryByRole("button", { name: "Allow" })).toBeNull();
});

test("the card lists the three providers, the chosen one with its live label, and the default starred", () => {
  picker(station({ provider: "claude", partner: LIVE_CLAUDE }));
  expect(screen.getAllByRole("radio").map((each) => each.getAttribute("value"))).toEqual(["assistant", "claude", "codex"]);
  expect((screen.getByRole("radio", { name: "claude" }) as HTMLInputElement).checked).toBe(true);
  expect(screen.getByText("claude · gx10")).toBeTruthy();
  expect(screen.getByRole("button", { name: "New chats start on scout" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("button", { name: "New chats start on claude" }).getAttribute("aria-pressed")).toBe("false");
});

test("the default starred is the user's preference, which the profile carries", () => {
  picker(station({ provider: "claude", partner: LIVE_CLAUDE }), { preferences: { chat_provider: "codex" } });
  expect(screen.getByRole("button", { name: "New chats start on codex" }).getAttribute("aria-pressed")).toBe("true");
});

test("choosing the assistant sends the operation at the station's revision and starts no run", async () => {
  const { sent } = picker(station({ provider: "claude", partner: LIVE_CLAUDE }));
  fireEvent.click(screen.getByRole("radio", { name: "scout" }));
  await waitFor(() => expect(posted(sent)).toHaveLength(1));
  expect(posted(sent)).toEqual([["POST", `/api/workspaces/${WORKSPACE_ID}/operations`, { revision: 3, operation: { kind: "provider", provider: "assistant" } }]]);
});

test("choosing claude while a run is live sends only the operation", async () => {
  const { sent } = picker(station({ provider: "assistant", partner: LIVE_ASSISTANT }), { machines: [GX10], cliPartner: LIVE_CLAUDE });
  fireEvent.click(screen.getByRole("radio", { name: "claude" }));
  await waitFor(() => expect(posted(sent)).toHaveLength(1));
  expect(posted(sent).map(([, path]) => path)).toEqual([`/api/workspaces/${WORKSPACE_ID}/operations`]);
  expect(sent.some((each) => each.path === "/api/terminals/machines")).toBe(false);
});

test("choosing claude with no live run offers only the confirmed, ready machines that have it, and starts its run on the one picked", async () => {
  const { sent } = picker(station({ provider: "assistant", partner: UNAVAILABLE }), { machines: [GX10, LAPTOP, BOX] });
  fireEvent.click(screen.getByRole("radio", { name: "claude" }));
  expect(await screen.findByRole("button", { name: "Start claude on gx10" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Start claude on laptop/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /Start claude on box/ })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Start claude on gx10" }));
  expect(await screen.findByText("starting…")).toBeTruthy();
  expect(posted(sent).at(-1)).toEqual(["POST", "/api/terminals",
    { machine: "gx10", workspace: WORKSPACE_ID, rows: 24, cols: 80, surface: null, command: "claude", attach: "s-chat" }]);
});

test("a run that is starting says so until the partner is live, then shows its label", async () => {
  const { held } = picker(station({ provider: "claude", partner: offline("claude") }), { machines: [GX10] });
  const start = await screen.findByRole("button", { name: "Start claude on gx10" });
  // The station is read again every 2 s until its run is live; the clock is run by hand, the reads' I/O is real.
  // Only the interval is run by hand: the reads and the request timeouts run on the real clock.
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  fireEvent.click(start);
  expect(screen.getByText("starting…")).toBeTruthy();
  held.station = station({ provider: "claude", partner: LIVE_CLAUDE, revision: 4 });
  await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
  vi.useRealTimers();
  expect(await screen.findByText("claude · gx10")).toBeTruthy();
  expect(screen.queryByText("starting…")).toBeNull();
});

test("a CLI with no capable machine says how to connect one, with the setup command", async () => {
  picker(station({ provider: "codex", partner: offline("codex") }), { machines: [{ ...GX10, clis: ["claude"] }] });
  await waitFor(() => expect(document.querySelector(".provider-machines p")?.textContent).toBe(`Connect a machine and install codex on it.`));
  expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy();
});

test("a CLI partner that names no chat session starts no run: a run is never started without an attach", async () => {
  const { sent } = picker(station({ provider: "claude", partner: offline("claude", null) }), { machines: [GX10] });
  fireEvent.click(await screen.findByRole("button", { name: "Start claude on gx10" }));
  expect((await screen.findByRole("status")).textContent).toBe("This chat has no session yet. Try again in a moment.");
  expect(posted(sent)).toEqual([]);
});

test("a provider out of reach is grayed, cannot be chosen or starred, and says why", () => {
  picker(station({ provider: "assistant", partner: UNAVAILABLE, available: { claude: false }, reasons: { claude: CLAUDE_REASON } }));
  expect((screen.getByRole("radio", { name: "claude" }) as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "New chats start on claude" }) as HTMLButtonElement).disabled).toBe(true);
  expect(document.querySelector(".provider-row:nth-child(2) .provider-reason")?.textContent).toBe(CLAUDE_REASON);
  expect((screen.getByRole("radio", { name: "codex" }) as HTMLInputElement).disabled).toBe(false);
});

test("the assistant the server says may not run is grayed, and its reason links to Settings", async () => {
  picker(station({ provider: "claude", partner: LIVE_CLAUDE }), { balance: SPENT });
  await waitFor(() => expect((screen.getByRole("radio", { name: "scout" }) as HTMLInputElement).disabled).toBe(true));
  expect(screen.getByRole("link", { name: "Settings" }).getAttribute("href")).toBe("#/settings");
  expect(document.querySelector(".provider-row:nth-child(1) .provider-reason")?.textContent).toBe("You are out of credit. Top up in Settings.");
});

test("the card shows what is left and what was spent, and no Out of credit", async () => {
  picker(station({ provider: "claude", partner: LIVE_CLAUDE }), { balance: BALANCE });
  expect(await screen.findByText("$498.20 left · spent $1.80")).toBeTruthy();
  expect((screen.getByRole("radio", { name: "scout" }) as HTMLInputElement).disabled).toBe(false);
  expect(screen.queryByText(/out of credit/i)).toBeNull();
  expect(screen.queryByRole("status")).toBeNull();
});

test("a user in grace past zero is not grayed: the server's verdict lets them run", async () => {
  picker(station({ provider: "claude", partner: LIVE_CLAUDE }), {
    balance: { ...BALANCE, usd: -20, spent_usd: 520, grace_until: "2026-12-31", reason: "No limit until 2026-12-31." } });
  expect(await screen.findByText("-$20.00 left · spent $520.00")).toBeTruthy();
  expect((screen.getByRole("radio", { name: "scout" }) as HTMLInputElement).disabled).toBe(false);
  expect(screen.queryByText(/out of credit/i)).toBeNull();
});

test("a user on their own login sees -- for the balance and what they spent", async () => {
  picker(station({ provider: "claude", partner: LIVE_CLAUDE }), { balance: { ...BALANCE, usd: 0, spent_usd: 0, own_login: true, reason: "Chat runs on your own ChatGPT login." } });
  expect(await screen.findByText("-- · spent $0.00")).toBeTruthy();
  expect((screen.getByRole("radio", { name: "scout" }) as HTMLInputElement).disabled).toBe(false);
});

test("under $5 left, a user whose credit decides is told before the hard stop", async () => {
  picker(station({ provider: "claude", partner: LIVE_CLAUDE }), { balance: { ...BALANCE, usd: 4.2, spent_usd: 495.8 } });
  const notice = await screen.findByRole("status");
  expect(notice.textContent).toBe("Low credit: $4.20 left. Top up in Settings.");
  expect(notice.querySelector("a")?.getAttribute("href")).toBe("#/settings");
  expect((screen.getByRole("radio", { name: "scout" }) as HTMLInputElement).disabled).toBe(false);
});

test("a user in grace gets no low-balance notice, however little is left", async () => {
  picker(station({ provider: "claude", partner: LIVE_CLAUDE }), { balance: { ...BALANCE, usd: 1, grace_until: "2026-12-31" } });
  await screen.findByText("$1.00 left · spent $1.80");
  expect(screen.queryByRole("status")).toBeNull();
});

test("the server's refusal stands even with credit left: the card keeps no balance rule of its own", async () => {
  picker(station({ provider: "claude", partner: LIVE_CLAUDE }), { balance: { ...BALANCE, can_run: false, reason: "Chat is paused for this site." } });
  await waitFor(() => expect((screen.getByRole("radio", { name: "scout" }) as HTMLInputElement).disabled).toBe(true));
  expect(document.querySelector(".provider-row:nth-child(1) .provider-reason")?.textContent).toBe("Chat is paused for this site. Top up in Settings.");
});

test("a choice the server refuses as unavailable shows the server's reason, and changes nothing", async () => {
  const { held } = picker(station({ provider: "assistant", partner: UNAVAILABLE }), { refuse: CODEX_REASON });
  fireEvent.click(screen.getByRole("radio", { name: "codex" }));
  expect((await screen.findByRole("status")).textContent).toBe(CODEX_REASON);
  expect(held.station.provider).toBe("assistant");
  expect((screen.getByRole("radio", { name: "scout" }) as HTMLInputElement).checked).toBe(true);
});

test("the star moves to the provider a click names, and the click patches the preference", async () => {
  const { sent, held } = picker(station({ provider: "claude", partner: LIVE_CLAUDE }));
  fireEvent.click(screen.getByRole("button", { name: "New chats start on codex" }));

  await waitFor(() => expect(screen.getByRole("button", { name: "New chats start on codex" }).getAttribute("aria-pressed")).toBe("true"));
  expect(screen.getByRole("button", { name: "New chats start on scout" }).getAttribute("aria-pressed")).toBe("false");
  expect(sent.filter((each) => each.method === "PATCH")).toMatchObject([{ path: "/api/me/preferences", body: { chat_provider: "codex" } }]);
  expect(held.preferences).toEqual({ chat_provider: "codex" });
});

test("the assistant is named by the station's assistant, and a server with none grays it with the reason and offers no star", () => {
  picker(station({ provider: "claude", partner: LIVE_CLAUDE, assistant: null }));
  expect(document.querySelector(".provider-row:nth-child(1) .provider-name")?.textContent).toBe("assistant");
  expect((screen.getByRole("radio", { name: "assistant" }) as HTMLInputElement).disabled).toBe(true);
  expect(document.querySelector(".provider-row:nth-child(1) .provider-reason")?.textContent).toBe(NO_ASSISTANT);
  expect((screen.getByRole("button", { name: "New chats start on assistant" }) as HTMLButtonElement).disabled).toBe(true);
});

test("the live line names the folder a CLI run starts in, when the station says it", () => {
  picker(station({ provider: "claude", partner: LIVE_IN_PROJECT }));
  expect(screen.getByText("claude · gx10 · ~/project")).toBeTruthy();
});

test("the folder a run starts in is prefilled per machine from the last one started there, and the button names it", async () => {
  localStorage.setItem("chat-run-folder:gx10", "~/project");
  localStorage.setItem("chat-run-folder:laptop", "~/notes");
  picker(station({ provider: "assistant", partner: UNAVAILABLE }), { machines: [GX10, LAPTOP] });
  fireEvent.click(screen.getByRole("radio", { name: "codex" }));
  expect(((await screen.findByRole("textbox", { name: "Folder on laptop" })) as HTMLInputElement).value).toBe("~/notes");
  expect(((await screen.findByRole("textbox", { name: "Folder on gx10" })) as HTMLInputElement).value).toBe("~/project");
  expect(screen.getByRole("button", { name: "Start codex on gx10 in ~/project" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Start codex on laptop in ~/notes" })).toBeTruthy();
});

test("a run starts in the folder typed for its machine, which the browser keeps; the folder is sent as cwd", async () => {
  const { sent } = picker(station({ provider: "claude", partner: offline("claude") }), { machines: [GX10] });
  fireEvent.change(await screen.findByRole("textbox", { name: "Folder on gx10" }), { target: { value: "  ~/project  " } });
  fireEvent.click(screen.getByRole("button", { name: "Start claude on gx10 in ~/project" }));
  await waitFor(() => expect(posted(sent).at(-1)).toEqual(["POST", "/api/terminals",
    { machine: "gx10", workspace: WORKSPACE_ID, rows: 24, cols: 80, surface: null, command: "claude", attach: "s-chat", cwd: "~/project" }]));
  expect(localStorage.getItem("chat-run-folder:gx10")).toBe("~/project");
});

test("an empty folder sends no cwd, starts the run at the home directory, and forgets the folder kept for the machine", async () => {
  localStorage.setItem("chat-run-folder:gx10", "~/project");
  const { sent } = picker(station({ provider: "claude", partner: offline("claude") }), { machines: [GX10] });
  fireEvent.change(await screen.findByRole("textbox", { name: "Folder on gx10" }), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Start claude on gx10" }));
  await waitFor(() => expect(posted(sent).at(-1)).toEqual(["POST", "/api/terminals",
    { machine: "gx10", workspace: WORKSPACE_ID, rows: 24, cols: 80, surface: null, command: "claude", attach: "s-chat" }]));
  expect(localStorage.getItem("chat-run-folder:gx10")).toBeNull();
});
