import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { stubFetch } from "./api/testing";
import { adoptLegacyPreferences, choosePreferences, choosePreferencesSoon, DEFAULT_PREFERENCES, PAUSE_MS, type Preferences, preferences, receivePreferences, sendWaiting, subscribePreferences } from "./preferences";

const DEFAULTS = DEFAULT_PREFERENCES;

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
  const kept = choosePreferences({ chat_side: "right" });
  receivePreferences(DEFAULTS);
  expect(preferences().chat_side).toBe("right");
  await vi.waitFor(() => expect(answer).not.toBeNull(), { interval: 1 });
  answer!(Response.json({ ...DEFAULTS, chat_side: "right" }));
  expect(await kept).toBe(true);
  expect(preferences().chat_side).toBe("right");
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
    theme: "light", chat_side: "right", highlight_mentions: false,
  }]]);
  expect(preferences()).toEqual({ ...DEFAULTS, theme: "light", chat_side: "right", highlight_mentions: false });
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

test("a profile from a server older than a key leaves that key's default", () => {
  const { stars: _, ...older } = { ...DEFAULTS, theme: "light" as const };
  receivePreferences(older);
  expect(preferences()).toEqual({ ...DEFAULTS, theme: "light" });
});

test("a key an older server still sends, as the retired chat_style, is dropped and breaks nothing", async () => {
  receivePreferences({ ...DEFAULTS, theme: "light", chat_style: "terminal" } as Partial<Preferences>);
  expect(preferences().theme).toBe("light");
  expect("chat_style" in preferences()).toBe(false);
  server({ ...DEFAULTS, chat_style: "terminal", chat_side: "right" } as Preferences);
  expect(await choosePreferences({ chat_side: "right" })).toBe(true);
  expect("chat_style" in preferences()).toBe(false);
  expect(preferences().chat_side).toBe("right");
});

test("a value equal to the one shown keeps its object, so what reads only that key does not render again", () => {
  receivePreferences({ ...DEFAULTS, stars: ["a"] });
  const stars = preferences().stars;
  const told = vi.fn();
  const stop = subscribePreferences(told);
  receivePreferences({ ...DEFAULTS, stars: ["a"] });
  expect(preferences().stars).toBe(stars);
  expect(told).not.toHaveBeenCalled();
  stop();
});

test("changes sent one after another reach the server in the order they were chosen", async () => {
  const answers: ((response: Response) => void)[] = [];
  const sent = stubFetch(() => new Promise<Response>((resolve) => answers.push(resolve)));
  const first = choosePreferences({ stars: ["a"] });
  const second = choosePreferences({ stars: ["a", "b"] });
  await vi.waitFor(() => expect(answers).toHaveLength(1), { interval: 1 });
  // The second waits for the first's answer.
  expect(sent).toHaveLength(1);
  answers[0]!(Response.json({ ...DEFAULTS, stars: ["a"] }));
  await vi.waitFor(() => expect(answers).toHaveLength(2), { interval: 1 });
  answers[1]!(Response.json({ ...DEFAULTS, stars: ["a", "b"] }));
  expect([await first, await second]).toEqual([true, true]);
  expect(patches(sent)).toEqual([["PATCH", "/api/me/preferences", { stars: ["a"] }], ["PATCH", "/api/me/preferences", { stars: ["a", "b"] }]]);
  expect(preferences().stars).toEqual(["a", "b"]);
});

describe("a change made as the user types", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  });

  afterEach(async () => {
    vi.useRealTimers();
    await sendWaiting();
  });

  test("shows at once and is sent once typing pauses, however many keys were typed", async () => {
    const sent = server();
    choosePreferencesSoon({ drafts: { a: { text: "h", base: null } } });
    choosePreferencesSoon({ drafts: { a: { text: "hi", base: null } } });
    choosePreferencesSoon({ create_draft: null, lens: "graph" });
    expect(preferences().drafts).toEqual({ a: { text: "hi", base: null } });
    await vi.advanceTimersByTimeAsync(PAUSE_MS - 1);
    expect(sent).toEqual([]);
    choosePreferencesSoon({ drafts: { a: { text: "hi!", base: null } } });
    await vi.advanceTimersByTimeAsync(PAUSE_MS - 1);
    expect(sent).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    expect(patches(sent)).toEqual([["PATCH", "/api/me/preferences", { drafts: { a: { text: "hi!", base: null } }, lens: "graph" }]]);
    expect(sent).toHaveLength(1);
  });

  test("a profile read while it waits does not undo it", async () => {
    server();
    choosePreferencesSoon({ lens: "graph" });
    receivePreferences(DEFAULTS);
    expect(preferences().lens).toBe("graph");
    await vi.advanceTimersByTimeAsync(PAUSE_MS);
    await sendWaiting();
    expect(preferences().lens).toBe("graph");
  });

  test("a change that leaves things as they are sends nothing", async () => {
    const sent = server();
    choosePreferencesSoon({ lens: null, drafts: {} });
    await vi.advanceTimersByTimeAsync(PAUSE_MS * 2);
    expect(sent).toEqual([]);
  });

  test("the page closing sends what waits, asking the request to outlive the page", async () => {
    const requests: Request[] = [];
    stubFetch((request) => {
      requests.push(request);
      return Response.json({ ...DEFAULTS, lens: "graph" });
    });
    choosePreferencesSoon({ lens: "graph" });
    dispatchEvent(new Event("pagehide"));
    await vi.waitFor(() => expect(requests).toHaveLength(1), { interval: 1 });
    expect(requests[0]!.keepalive).toBe(true);
    await vi.advanceTimersByTimeAsync(PAUSE_MS);
    expect(requests).toHaveLength(1);
  });
});
