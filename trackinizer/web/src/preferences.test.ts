import { afterEach, expect, test, vi } from "vitest";
import { stubFetch } from "./api/testing";
import { adoptLegacyPreferences, choosePreferences, type Preferences, preferences, receivePreferences } from "./preferences";

const DEFAULTS: Preferences = { theme: "dark", chat_style: "default", chat_side: "left", highlight_mentions: true };

afterEach(() => {
  receivePreferences(DEFAULTS);
  localStorage.clear();
});

/** A server that merges what it is sent over `stored`, as `PATCH /api/me/preferences` does. */
function server(stored: Preferences = DEFAULTS) {
  let kept = stored;
  return stubFetch(async (request) => {
    kept = { ...kept, ...(await request.json() as Partial<Preferences>) };
    return Response.json(kept);
  });
}

const patches = (sent: ReturnType<typeof stubFetch>) => sent.map(({ method, path, body }) => [method, path, body]);

test("the defaults show until the profile brings the user's; a profile without them, from an older server, leaves them", () => {
  expect(preferences()).toEqual(DEFAULTS);
  receivePreferences(undefined);
  expect(preferences()).toEqual(DEFAULTS);
  receivePreferences({ ...DEFAULTS, theme: "light" });
  expect(preferences().theme).toBe("light");
});

test("a change shows at once, is sent as only its keys, and the server's answer is what stays", async () => {
  const sent = server({ ...DEFAULTS, chat_side: "right" });
  const kept = choosePreferences({ theme: "system" });
  expect(preferences().theme).toBe("system");
  expect(await kept).toBe(true);
  expect(patches(sent)).toEqual([["PATCH", "/api/me/preferences", { theme: "system" }]]);
  // The server held a side this page had not read yet: its answer brings it.
  expect(preferences()).toEqual({ ...DEFAULTS, theme: "system", chat_side: "right" });
});

test("a profile read while a change is on its way does not undo it", async () => {
  let answer: ((response: Response) => void) | null = null;
  stubFetch(() => new Promise<Response>((resolve) => (answer = resolve)));
  const kept = choosePreferences({ chat_style: "terminal" });
  receivePreferences(DEFAULTS);
  expect(preferences().chat_style).toBe("terminal");
  await vi.waitFor(() => expect(answer).not.toBeNull(), { interval: 1 });
  answer!(Response.json({ ...DEFAULTS, chat_style: "terminal" }));
  expect(await kept).toBe(true);
  expect(preferences().chat_style).toBe("terminal");
});

test("a change the server did not take stays on the page, and the next profile read brings the server's back", async () => {
  stubFetch(() => Response.json({ detail: "offline" }, { status: 503 }));
  expect(await choosePreferences({ theme: "light" })).toBe(false);
  expect(preferences().theme).toBe("light");
  receivePreferences(DEFAULTS);
  expect(preferences().theme).toBe("dark");
});

test("what this browser kept before is sent once, then forgotten", async () => {
  localStorage.setItem("trackinizer.theme", "light");
  localStorage.setItem("trackinizer.chat.style", "terminal");
  localStorage.setItem("trackinizer.chat.side", "right");
  localStorage.setItem("trackinizer.v2.highlight-mentions.ada@example.com", "off");
  localStorage.setItem("trackinizer.v2.highlight-mentions.grace@example.com", "off");
  const sent = server();
  await adoptLegacyPreferences("ada@example.com");
  expect(patches(sent)).toEqual([["PATCH", "/api/me/preferences", {
    theme: "light", chat_style: "terminal", chat_side: "right", highlight_mentions: false,
  }]]);
  expect(preferences()).toEqual({ theme: "light", chat_style: "terminal", chat_side: "right", highlight_mentions: false });
  // Another user's switch on this browser is theirs to send.
  expect(Object.keys(localStorage)).toEqual(["trackinizer.v2.highlight-mentions.grace@example.com"]);
  await adoptLegacyPreferences("ada@example.com");
  expect(sent).toHaveLength(1);
});

test("nothing kept, or nothing a key takes, sends nothing; a send that fails keeps them for the next load", async () => {
  localStorage.setItem("trackinizer.theme", "neon");
  const sent = server();
  await adoptLegacyPreferences("ada@example.com");
  expect(sent).toEqual([]);
  expect(localStorage.length).toBe(0);

  localStorage.setItem("trackinizer.chat.side", "right");
  stubFetch(() => Response.json({ detail: "offline" }, { status: 503 }));
  await adoptLegacyPreferences("ada@example.com");
  expect(localStorage.getItem("trackinizer.chat.side")).toBe("right");
});
