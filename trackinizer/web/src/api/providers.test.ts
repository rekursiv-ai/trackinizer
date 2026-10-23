import { afterEach, expect, test, vi } from "vitest";
import { AGREED, stubFetch } from "./testing";
import type { Profile } from "./me";
import type { BalanceBody } from "./billing";
import { NO_ASSISTANT, noMachineReason, OUT_OF_CREDIT, partnerLabel, profileChatProvider, providerName, providerStatuses, setChatProviderPreference, stationProvider } from "./providers";

afterEach(() => vi.unstubAllGlobals());

const SPENT: BalanceBody = { usd: 0, granted_usd: 10, purchased_usd: 0, spent_usd: 10, topup_usd: [10, 25], enforced: true, grace_until: null, own_login: false,
  can_run: false, reason: "You are out of credit." };
const BALANCE: BalanceBody = { ...SPENT, usd: 0.5, spent_usd: 9.5, can_run: true, reason: "You have credit left." };

test("a station with no provider fields talks to the default provider, and every provider is available", () => {
  expect(stationProvider({})).toBe("assistant");
  expect(stationProvider(null)).toBe("assistant");
  expect(stationProvider({ provider: "claude" })).toBe("claude");
  expect(stationProvider({ provider: "gemini" as "claude" })).toBe("assistant");
  expect(providerStatuses({}, null)).toEqual({
    assistant: { ok: true, reason: "" }, claude: { ok: true, reason: "" }, codex: { ok: true, reason: "" },
  });
});

test("the assistant is out when the server says the caller may not run, and says why in the server's words", () => {
  expect(providerStatuses({}, SPENT).assistant).toEqual({ ok: false, reason: "You are out of credit.", credit: true });
  expect(providerStatuses({}, BALANCE).assistant).toEqual({ ok: true, reason: "" });
  // The server's own word about the assistant does not decide it: the site's verdict does.
  expect(providerStatuses({ available: { assistant: true } }, { ...SPENT, usd: -1 }).assistant.reason).toBe("You are out of credit.");
});

test("the card obeys can_run and reason alone: no balance of zero or less greys the assistant by itself", () => {
  const grace = { ...SPENT, usd: -20, grace_until: "2026-12-31", can_run: true, reason: "No limit until 2026-12-31." };
  const login = { ...SPENT, own_login: true, can_run: true, reason: "Chat runs on your own ChatGPT login." };
  const open = { ...SPENT, enforced: false, can_run: true, reason: "Credit is not enforced here." };
  for (const balance of [grace, login, open]) expect(providerStatuses({}, balance).assistant).toEqual({ ok: true, reason: "" });
  const refused = { ...BALANCE, usd: 100, can_run: false, reason: "Because the server says so." };
  expect(providerStatuses({}, refused).assistant).toEqual({ ok: false, reason: "Because the server says so.", credit: true });
});

test("a station that marks the assistant out without a reason says the credit is spent", () => {
  expect(providerStatuses({ available: { assistant: false } }, null).assistant).toEqual({ ok: false, reason: OUT_OF_CREDIT, credit: true });
  expect(providerStatuses({ available: { assistant: false }, reasons: { assistant: "Down for repairs" } }, null).assistant).toEqual({ ok: false, reason: "Down for repairs" });
});

test("a CLI is out when the station says so, with the server's reason or the machine's", () => {
  const reason = noMachineReason("claude");
  expect(reason).toBe("Connect a machine and install claude on it");
  expect(providerStatuses({ available: { claude: false }, reasons: { claude: reason } }, null).claude).toEqual({ ok: false, reason });
  expect(providerStatuses({ available: { codex: false } }, null).codex).toEqual({ ok: false, reason: noMachineReason("codex") });
  expect(providerStatuses({ available: { claude: false }, reasons: { claude: reason } }, null).codex.ok).toBe(true);
});

test("a partner is named by its label, else its actor, else the name the caller gives its provider", () => {
  expect(partnerLabel({ label: "claude · gx10", actor: null }, "claude")).toBe("claude · gx10");
  expect(partnerLabel({ label: null, actor: "scout" }, "assistant")).toBe("scout");
  expect(partnerLabel(null, providerName("assistant", { assistant: "scout" }))).toBe("scout");
  expect(partnerLabel(null, "codex")).toBe("codex");
});

test("the assistant is named by the station's assistant, and a CLI by its own name", () => {
  expect(providerName("assistant", { assistant: "scout" })).toBe("scout");
  expect(providerName("assistant", { assistant: null })).toBe("assistant");
  expect(providerName("assistant", {})).toBe("assistant");
  expect(providerName("assistant", null)).toBe("assistant");
  expect(providerName("claude", { assistant: "scout" })).toBe("claude");
  expect(providerName("codex", null)).toBe("codex");
});

test("a server with no assistant grays the assistant with the reason, and leaves the CLIs as they are", () => {
  expect(providerStatuses({ assistant: null }, null).assistant).toEqual({ ok: false, reason: NO_ASSISTANT });
  expect(providerStatuses({ assistant: null, available: { codex: false }, reasons: { codex: "x" } }, null).codex).toEqual({ ok: false, reason: "x" });
  expect(providerStatuses({ assistant: "scout" }, null).assistant).toEqual({ ok: true, reason: "" });
  expect(providerStatuses({ assistant: null }, SPENT).assistant.reason).toBe(NO_ASSISTANT);
});

test("the default provider is the profile's preference, the assistant until one is chosen", () => {
  const profile = (preferences: Record<string, unknown> | undefined) =>
    ({ ...AGREED, user_id: "u1", email: "ada@example.com", name: "Ada", role: "writer", last_login: null,
      visual_workspace_enabled: true, preferences: preferences as Profile["preferences"] }) satisfies Profile;
  expect(profileChatProvider(profile({ chat_provider: "codex" }))).toBe("codex");
  expect(profileChatProvider(profile({ chat_provider: "gemini" }))).toBe("assistant");
  expect(profileChatProvider(profile(undefined))).toBe("assistant");
});

test("making a provider the default patches the preference, and only that key", async () => {
  const sent = stubFetch(() => Response.json({ chat_provider: "codex" }));
  await setChatProviderPreference("codex");
  expect(sent).toMatchObject([{ method: "PATCH", path: "/api/me/preferences", body: { chat_provider: "codex" } }]);
});
