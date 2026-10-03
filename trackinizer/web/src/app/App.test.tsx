import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Profiler } from "react";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";
import { stubFetch } from "../api/testing";
import { logRenderError } from "../debug/install";
import { recentEvents } from "../debug/log";
import { stubClipboard } from "../debug/testing";
import { FakeEventSource } from "../live/testing";
import { ActivityView, GraphView, SearchView } from "../router/views";
import { App } from "./App";
import * as boot from "./boot";
import { LOGIN_URL } from "./session";

// The graph draws on a canvas, which jsdom lacks; its own tests replace its renderer.
vi.mock("../graph", () => ({ GraphView: () => <h1>Graph</h1> }));

const BODIES: { [path: string]: unknown } = {
  "/api/meta/enums": { inquiry_kind_all: ["Issue", "Belief", "CodeChange"], status: ["active"] },
  "/api/meta/fields": { priority: "issue" },
  "/api/meta/edges": {},
  "/api/me/profile": {
    user_id: "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33",
    email: "ada@example.com",
    name: "Ada",
    role: "writer",
    last_login: null,
  },
};

/** Serve the boot reads; `answer` overrides a path's response. */
function serve(answer: { [path: string]: () => Response } = {}) {
  return stubFetch((request) => {
    const path = new URL(request.url).pathname;
    return answer[path]?.() ?? Response.json(BODIES[path]);
  });
}

function sidebar() {
  return within(screen.getByRole("navigation", { name: "Sidebar" }));
}

// Activity, the graph and search are chunks of their own; loaded first, each
// shows at once, as it does once the app has loaded it.
beforeAll(() => Promise.all([ActivityView.preload(), GraphView.preload(), SearchView.preload()]));

beforeEach(() => {
  history.replaceState(null, "", location.pathname);
  sessionStorage.clear();
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

test("boot reads all four calls in parallel, then shows the kinds and the profile", async () => {
  const waiting: (() => void)[] = [];
  const sent = stubFetch(async (request) => {
    // Answer nothing until all four are in flight: one after another would hang.
    await new Promise<void>((resolve) => {
      waiting.push(resolve);
      if (waiting.length === 4) waiting.forEach((release) => release());
    });
    return Response.json(BODIES[new URL(request.url).pathname]);
  });
  render(<App assign={vi.fn()} />);
  // A group, so a screen reader reads its name: ARIA forbids a name on a plain div.
  const me = await screen.findByRole("group", { name: "Signed in" });
  expect(sidebar().getAllByRole("link").map((link) => link.textContent)).toEqual([
    "Trackinizer",
    "Graph",
    "Console",
    "Activity",
    "Issues",
    "Beliefs",
    "Code changes",
    "Your settings",
  ]);
  expect(within(me).getByText("ada@example.com")).toBeTruthy();
  expect(within(me).getByText("writer")).toBeTruthy();
  // Each boot call went out exactly once. The graph the empty hash opens is a
  // stand-in here, which reads nothing.
  const boot = sent.map((request) => request.path).filter((path) => path in BODIES);
  expect(boot.sort()).toEqual(Object.keys(BODIES).sort());
});

test("the app's first render draws its frame alone; boot and the rest draw in a render after it", async () => {
  stubFetch(() => new Promise<Response>(() => {}));
  const useBoot = vi.spyOn(boot, "useBoot");
  // Each commit, with how often boot had rendered by then.
  const commits: string[] = [];
  render(
    <Profiler id="app" onRender={(_id, phase) => commits.push(`${phase} ${useBoot.mock.calls.length}`)}>
      <App assign={vi.fn()} />
    </Profiler>,
  );
  await waitFor(() => expect(useBoot).toHaveBeenCalled());
  expect(commits[0]).toBe("mount 0");
});

test("the shell draws in a render after the one the boot reads land in, which shows the frame", async () => {
  serve();
  const useBoot = vi.spyOn(boot, "useBoot");
  // Each commit: whether boot was ready, and whether the shell showed.
  const commits: string[] = [];
  render(
    <Profiler
      id="app"
      onRender={() => {
        const ready = useBoot.mock.results.at(-1)?.value.state === "ready";
        const shown = document.querySelector('[aria-label="Signed in"]') !== null;
        const commit = `ready ${ready}, shell ${shown}`;
        if (commit !== commits.at(-1)) commits.push(commit);
      }}
    >
      <App assign={vi.fn()} />
    </Profiler>,
  );
  await screen.findByLabelText("Signed in");
  expect(commits).toEqual(["ready false, shell false", "ready true, shell false", "ready true, shell true"]);
});

test("an empty hash opens the graph, the home view", async () => {
  serve();
  render(<App assign={vi.fn()} />);
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Graph");
  expect(location.hash).toBe("#/graph");
  expect(sidebar().getByRole("link", { name: "Graph" }).getAttribute("aria-current")).toBe("page");
});

test("a deep link opens its view", async () => {
  serve();
  history.replaceState(null, "", "#/list/codechange");
  render(<App assign={vi.fn()} />);
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Code changes");
  expect(location.hash).toBe("#/list/CodeChange");
  expect(sidebar().getByRole("link", { name: "Code changes" }).getAttribute("aria-current")).toBe("page");
});

test("a 401 at boot saves the hash and goes to the login page once", async () => {
  serve({ "/api/me/profile": () => Response.json({ detail: "not signed in" }, { status: 401 }) });
  history.replaceState(null, "", "#/ref/Issue/7");
  const assign = vi.fn();
  render(<App assign={assign} />);
  await waitFor(() => expect(assign).toHaveBeenCalledWith(LOGIN_URL));
  expect(assign).toHaveBeenCalledTimes(1);
  expect(sessionStorage.getItem("trackinizer.v2.return_hash")).toBe("#/ref/Issue/7");
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a failed boot shows the server's message, and Retry starts the app", async () => {
  let refuse = true;
  serve({
    "/api/me/profile": () =>
      refuse
        ? Response.json({ detail: "account disabled" }, { status: 403 })
        : Response.json(BODIES["/api/me/profile"]),
  });
  render(<App assign={vi.fn()} />);
  expect((await screen.findByRole("alert")).textContent).toBe("account disabled");
  refuse = false;
  await userEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByLabelText("Signed in")).toBeTruthy();
});

test("a failed boot offers Copy details with the failed request's id", async () => {
  const copied = stubClipboard();
  serve({ "/api/meta/edges": () => Response.json({ detail: "schema mismatch" }, { status: 400 }) });
  render(<App assign={vi.fn()} />);
  expect((await screen.findByRole("alert")).textContent).toBe("schema mismatch");
  fireEvent.click(screen.getByRole("button", { name: "Copy details" }));
  await waitFor(() => expect(copied).toHaveLength(1));
  expect(copied[0]).toMatch(/^Trackinizer web app: Could not start: schema mismatch\n/);
  expect(copied[0]).toMatch(/\nfailed: request method=GET path=\/api\/meta\/edges status=400 ms=\d+ request_id=[0-9a-f-]{36} attempt=\d+ at=/);
});

test("a render crash shows what happened, with Copy details and Reload, in place of a blank page", async () => {
  const copied = stubClipboard();
  vi.spyOn(console, "error").mockImplementation(() => {});
  // Server drift: a kind list that is not a list, which the router reads.
  serve({ "/api/meta/enums": () => Response.json({ inquiry_kind_all: 5 }) });
  const reload = vi.fn();
  render(<App assign={vi.fn()} reload={reload} />, { onCaughtError: logRenderError });
  expect((await screen.findByRole("alert")).textContent).toBe("kinds.find is not a function");
  expect(screen.getByRole("heading", { name: "Trackinizer stopped on an error" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Copy details" }));
  await waitFor(() => expect(copied).toHaveLength(1));
  expect(copied[0]).toContain("Trackinizer web app: The page crashed: kinds.find is not a function\n");
  expect(copied[0]).toContain("\nerror: TypeError: kinds.find is not a function\n");
  expect(copied[0]).toMatch(/\n\S+ error render.crash error=TypeError message="kinds.find is not a function"/);
  fireEvent.click(screen.getByRole("button", { name: "Reload" }));
  expect(reload).toHaveBeenCalledTimes(1);
});

test("boot logs the build's commit and the role", async () => {
  serve();
  render(<App assign={vi.fn()} />);
  await screen.findByLabelText("Signed in");
  expect(recentEvents().filter(({ event }) => event === "boot").at(-1)?.fields).toEqual({ commit: __COMMIT__, role: "writer" });
});

/** The command palette, when it is open. */
const palette = () => screen.queryByRole("dialog", { name: "Command menu" });

test("⌘K toggles the palette", async () => {
  serve();
  const user = userEvent.setup();
  render(<App assign={vi.fn()} />);
  await screen.findByLabelText("Signed in");
  await user.keyboard("{Control>}k{/Control}");
  expect(palette()).not.toBeNull();
  await user.keyboard("{Control>}k{/Control}");
  expect(palette()).toBeNull();
});

test("⌘P opens the palette, and so does the search button", async () => {
  serve();
  const user = userEvent.setup();
  render(<App assign={vi.fn()} />);
  await screen.findByLabelText("Signed in");
  await user.keyboard("{Control>}p{/Control}");
  expect(palette()).not.toBeNull();
  await user.keyboard("{Escape}");
  expect(palette()).toBeNull();
  await user.click(sidebar().getByRole("button", { name: "Search" }));
  expect(palette()).not.toBeNull();
});

test("the search button names this platform's shortcut: Ctrl+K off a Mac, ⌘K on one (WEB-19)", async () => {
  serve();
  render(<App assign={vi.fn()} />);
  await screen.findByLabelText("Signed in");
  expect(sidebar().getByRole("button", { name: "Search" }).title).toBe("Search and commands (Ctrl+K)");
  cleanup();
  vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
  render(<App assign={vi.fn()} />);
  await screen.findByLabelText("Signed in");
  expect(sidebar().getByRole("button", { name: "Search" }).title).toBe("Search and commands (⌘K)");
  vi.restoreAllMocks();
});

test("a search link opens the search page on its query, over no palette", async () => {
  serve({ "/api/web/search": () => Response.json([]) });
  history.replaceState(null, "", "#/list/Belief");
  render(<App assign={vi.fn()} />);
  await screen.findByLabelText("Signed in");

  act(() => {
    location.hash = "#/search/what%3F";
  });
  expect((await screen.findByRole("heading", { level: 1, name: "Search: what? (0)" })).textContent).toBe("Search: what? (0)");
  expect((screen.getByRole("searchbox", { name: "Search query" }) as HTMLInputElement).value).toBe("what?");
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("g then a goes to Activity; an old UI link lands on its v2 route", async () => {
  serve();
  const user = userEvent.setup();
  history.replaceState(null, "", "#/recent");
  render(<App assign={vi.fn()} />);
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Activity");
  expect(location.hash).toBe("#/activity");

  await user.click(sidebar().getByRole("link", { name: "Issues" }));
  await waitFor(() => expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Issues"));
  await user.keyboard("ga");
  await waitFor(() => expect(location.hash).toBe("#/activity"));
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Activity");
});

test("g then g goes to the graph, and its sidebar entry marks it current", async () => {
  serve();
  const user = userEvent.setup();
  history.replaceState(null, "", "#/list/Issue");
  render(<App assign={vi.fn()} />);
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Issues");
  await user.keyboard("gg");
  await waitFor(() => expect(location.hash).toBe("#/graph"));
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Graph");
  expect(sidebar().getByRole("link", { name: "Graph" }).getAttribute("aria-current")).toBe("page");
});

test("a link that names nothing says so and offers the graph, the home view", async () => {
  serve();
  history.replaceState(null, "", "#/nowhere");
  render(<App assign={vi.fn()} />);
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Not found");
  expect(location.hash).toBe("#/nowhere");
  expect(screen.getByRole("link", { name: "Go to the graph" }).getAttribute("href")).toBe("#/graph");
});

test("the logo leads home, to the graph", async () => {
  serve();
  history.replaceState(null, "", "#/list/Issue");
  render(<App assign={vi.fn()} />);
  await screen.findByLabelText("Signed in");
  expect(sidebar().getByRole("link", { name: "Trackinizer" }).getAttribute("href")).toBe("#/graph");
});

test("a link straight to the create form opens it over the graph, the home view", async () => {
  serve();
  history.replaceState(null, "", "#/new/Issue");
  render(<App assign={vi.fn()} />);
  // Hidden from the accessibility tree once the form's dialog has opened over it.
  expect((await screen.findByRole("heading", { level: 1, hidden: true })).textContent).toBe("Graph");
  expect(location.hash).toBe("#/new/Issue");
});
