// The live suite's harness: its own server, the writer as the second client,
// the probes it installs in the page, and the log of the page's requests.
import { type ChildProcess, spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { expect, type Page } from "@playwright/test";

const ROOT = fileURLToPath(new URL("../../../../..", import.meta.url));
const WEB = fileURLToPath(new URL("../..", import.meta.url));

/** How a live server starts: its command and arguments, given its port and data directory. */
export type Serve = (port: number, data: string) => readonly [string, readonly string[]];

/** The trackinizer server, serving the build the e2e script made (as `playwright.config.ts` finds it). */
const TRACKINIZER: Serve = (port, data) => {
  const dist = join(WEB, "dist-e2e", process.env.TRACKINIZER_E2E_PORT ?? "8797");
  const args = ["--quiet", "run", "--frozen", "python", "-m", "trackinizer.server"];
  args.push("--datadir", join(data, "pgdata"), "--no-auth", "--app-dir", dist);
  args.push("--host", "127.0.0.1", "--port", String(port));
  return ["uv", args];
};

/**
 * A trackinizer server of the suite's own, on a free port. Its data lives in
 * a directory of its own, so `restart` keeps it. Its own because the suite
 * stops and restarts it, and because its bursts would slow the shared server
 * under every other spec file.
 */
export class LiveServer {
  readonly url: string;
  readonly #port: number;
  readonly #data: string;
  readonly #serve: Serve;
  #process: ChildProcess | null = null;

  private constructor(port: number, serve: Serve) {
    this.#port = port;
    this.url = `http://127.0.0.1:${port}`;
    this.#data = mkdtempSync(join(tmpdir(), "trackinizer-web-live-"));
    this.#serve = serve;
  }

  /** Start one on a free port; `serve` stands in for the trackinizer server in the harness's own tests. */
  static async start(serve = TRACKINIZER): Promise<LiveServer> {
    const server = new LiveServer(await freePort(), serve);
    try {
      await server.up();
    } catch (error) {
      // The caller never gets the server to close, so its process and data would outlive the suite.
      await server.close();
      throw error;
    }
    return server;
  }

  /** Start the server and wait until it serves the app, at most 2 minutes, and not after it has exited. */
  async up(): Promise<void> {
    const [command, args] = this.#serve(this.#port, this.#data);
    // Its own process group, so a stop reaches the Python server and the
    // PGlite engine it starts, not only `uv`.
    const child = spawn(command, args, { cwd: ROOT, stdio: "ignore", detached: true });
    this.#process = child;
    const deadline = Date.now() + 120_000;
    while (Date.now() < deadline && !exited(child)) {
      if (await answers(`${this.url}/app/`)) return;
      await sleep(250);
    }
    throw new Error(`The live suite's server did not start on port ${this.#port}.`);
  }

  /** Stop the server and wait until it has exited. */
  async down(): Promise<void> {
    const child = this.#process;
    if (!child || exited(child)) return;
    const ended = new Promise((resolve) => child.once("exit", resolve));
    kill(child, "SIGTERM");
    const timer = setTimeout(() => kill(child, "SIGKILL"), 5_000);
    await ended;
    clearTimeout(timer);
    this.#process = null;
  }

  /** Stop for good and remove the data. */
  async close(): Promise<void> {
    await this.down();
    rmSync(this.#data, { recursive: true, force: true });
  }
}

/** A write's answer: when it returned, in epoch milliseconds. */
export type Written = { readonly at: number };

/**
 * `scripts/writer.py`, the second client: one command per line in, one answer
 * per line out, in order. Once it has exited, every command fails.
 */
export class Writer {
  readonly #process: ChildProcess;
  readonly #waiting: { answer: (line: string) => void; fail: (error: Error) => void }[] = [];
  #exited: Error | null = null;

  /** `program` runs as the writer; the harness's own tests pass another. */
  constructor(url: string, program = join(WEB, "scripts", "writer.py")) {
    this.#process = spawn(program, ["--url", url], { stdio: ["pipe", "pipe", "inherit"] });
    createInterface({ input: this.#process.stdout! }).on("line", (line) => this.#waiting.shift()?.answer(line));
    // A write racing the writer's exit fails with EPIPE; its close below fails the command instead.
    this.#process.stdin!.on("error", () => {});
    // On close, not exit: the answers it wrote before exiting have been read by then.
    this.#process.once("close", (code, signal) => {
      this.#exited = new Error(`The writer exited (${signal ?? code}).`);
      for (const { fail } of this.#waiting.splice(0)) fail(this.#exited);
    });
  }

  /** Run `command`; resolves with its answer, and fails with the writer's error or its exit. */
  async send<Answer>(command: { readonly op: string; readonly [field: string]: unknown }): Promise<Answer> {
    const line = await new Promise<string>((answer, fail) => {
      if (this.#exited) return fail(this.#exited);
      this.#waiting.push({ answer, fail });
      this.#process.stdin!.write(`${JSON.stringify(command)}\n`);
    });
    const answer = JSON.parse(line) as Answer & { error?: string };
    if (answer.error !== undefined) throw new Error(`The writer failed ${command.op}: ${answer.error}`);
    return answer;
  }

  create(title: string, labels: readonly string[]): Promise<Written & { id: string }> {
    return this.send({ op: "create", title, labels });
  }

  edit(id: string, field: string, value: unknown): Promise<Written> {
    return this.send({ op: "edit", id, field, value });
  }

  close(): void {
    this.#process.stdin!.end();
  }
}

/** What the probes found in the page; see `installProbes`. */
export type Probed = {
  /** Main-thread tasks over 50 ms: when they started (page time) and how long they took. */
  readonly longTasks: readonly { start: number; ms: number }[];
  /** Layout shifts no recent input explains, and what moved. */
  readonly shifts: readonly { start: number; value: number; moved: string[] }[];
  /** Key presses of 16 ms or more: each keydown's duration to the next paint, which Event Timing measures. */
  readonly keys: readonly { start: number; ms: number }[];
  /** Each change to what the user holds (see `holdings`), with when it happened (epoch ms). */
  readonly held: readonly { at: number; state: string }[];
  /**
   * Each request the app made: its URL, when it was sent, and when the last of
   * its answer came or it failed (the page's epoch ms); null while it is open.
   */
  readonly fetches: readonly { url: string; start: number; end: number | null }[];
  /** When the probes were read (the page's epoch ms). */
  readonly now: number;
};

/**
 * The page's own recorders, installed before any of its scripts run: long
 * tasks, layout shifts and key timings through `PerformanceObserver`, what the
 * user holds, sampled every frame once `window.__probe.hold()` starts it, and
 * every request the app makes, through `fetch`.
 */
export function installProbes(): void {
  const longTasks: { start: number; ms: number }[] = [];
  const shifts: { start: number; value: number; moved: string[] }[] = [];
  const keys: { start: number; ms: number }[] = [];
  const held: { at: number; state: string }[] = [];
  const fetches: { url: string; start: number; end: number | null }[] = [];
  // Stamped by the app's own clock in the app's own order: a request sent once
  // the one before it has answered always starts after that one's end. The
  // browser's network stamps can read up to 2 ms out of order (measured).
  // Logged when sent, so one still open counts, and ended when its whole body
  // has come: once the app has read it, or, for one it never reads, once a copy
  // read to its end says so. The copy alone can end just after the app sent its
  // next request, which then reads as two in flight (seen in the soak).
  const realFetch = window.fetch.bind(window);
  const readEnds = new WeakMap<Response, () => void>();
  for (const read of ["arrayBuffer", "blob", "json", "text"] as const) {
    const original = Response.prototype[read] as (this: Response) => Promise<unknown>;
    Object.assign(Response.prototype, {
      [read](this: Response) {
        return original.call(this).finally(() => readEnds.get(this)?.());
      },
    });
  }
  window.fetch = (input, init) => {
    const sent: { url: string; start: number; end: number | null } = {
      url: input instanceof Request ? input.url : String(input),
      start: Date.now(),
      end: null,
    };
    fetches.push(sent);
    const done = () => {
      sent.end ??= Date.now();
    };
    const answer = realFetch(input, init);
    answer.then((response) => {
      readEnds.set(response, done);
      void response.clone().arrayBuffer().then(done, done);
    }, done);
    return answer;
  };
  const name = (node: Node | null | undefined) => {
    const element = node instanceof Element ? node : node?.parentElement;
    return element ? `${element.tagName.toLowerCase()}.${[...element.classList].join(".")}` : "?";
  };
  type Shift = PerformanceEntry & { value: number; hadRecentInput: boolean; sources: { node: Node | null }[] };
  const recorders: [PerformanceObserverInit, (entries: PerformanceEntryList) => void][] = [
    [
      { type: "longtask", buffered: true },
      (entries) => longTasks.push(...entries.map((task) => ({ start: Math.round(task.startTime), ms: Math.round(task.duration) }))),
    ],
    [
      { type: "layout-shift", buffered: true },
      (entries) => {
        for (const shift of entries as Shift[]) {
          if (!shift.hadRecentInput) shifts.push({ start: Math.round(shift.startTime), value: shift.value, moved: shift.sources.map((s) => name(s.node)) });
        }
      },
    ],
    [
      { type: "event", durationThreshold: 16, buffered: true } as PerformanceObserverInit,
      (entries) => {
        for (const key of entries.filter((entry) => entry.name === "keydown")) {
          keys.push({ start: Math.round(key.startTime), ms: Math.round(key.duration) });
        }
      },
    ],
  ];
  const observers = recorders.map(([init, record]) => {
    const observer = new PerformanceObserver((list) => record(list.getEntries()));
    observer.observe(init);
    return { observer, record };
  });
  // An observer's callback runs later, as a task of its own; `takeRecords`
  // hands over what it has not yet delivered, so a task just ended counts.
  const drain = () => {
    for (const { observer, record } of observers) record(observer.takeRecords());
  };
  let pointer: { x: number; y: number } | null = null;
  const holdings = () => {
    const list = document.querySelector<HTMLElement>(".view .scroll");
    const detail = document.querySelector<HTMLElement>(".d-scroll");
    const focused = document.querySelector<HTMLElement>("a.row.is-focused");
    const under = pointer && document.elementFromPoint(pointer.x, pointer.y)?.closest<HTMLElement>("[data-row]");
    const active = document.activeElement;
    const draft = active instanceof HTMLTextAreaElement ? active : null;
    return JSON.stringify({
      scrolled: [list?.scrollTop, detail?.scrollTop],
      focused: focused && [focused.dataset.row, Math.round(focused.getBoundingClientRect().top)],
      // Aimed at no row, the pointer would hold nothing to move.
      pointer: pointer && (under ? [under.dataset.row, Math.round(under.getBoundingClientRect().top)] : "no row"),
      selected: [...document.querySelectorAll<HTMLElement>(".row-line.is-selected a.row")].map((row) => row.dataset.row),
      active: active && `${active.tagName}${active.getAttribute("aria-label") ?? ""}`,
      draft: draft && [draft.value, draft.selectionStart, draft.selectionEnd],
      menus: document.querySelectorAll("[role=menu], [role=dialog], .peek").length,
    });
  };
  let sampling = false;
  const sample = () => {
    const state = holdings();
    if (held.at(-1)?.state !== state) held.push({ at: Date.now(), state });
    requestAnimationFrame(sample);
  };
  const forget = () => {
    drain();
    for (const record of [longTasks, shifts, keys, held, fetches]) record.length = 0;
  };
  Object.assign(window, {
    __probe: {
      /** Start sampling what the user holds, the pointer at `at` if given, and record afresh from now on. */
      hold: (at?: { x: number; y: number }) => {
        pointer = at ?? null;
        forget();
        if (!sampling) sample();
        sampling = true;
      },
      forget,
      read: () => {
        drain();
        return { longTasks, shifts, keys, held, fetches, now: Date.now() };
      },
    },
  });
}

/**
 * Run the page's clock `scale` times faster: `Date.now` and every timer, which
 * is all the app keeps time with (the one-second batch window, the 2 s between
 * checks, the minute clock). Page and network work take their real time, so a
 * soak packs hours of batches into minutes. `performance.now` stays real, so
 * long tasks keep their true length.
 */
export function accelerate(scale: number): void {
  const realNow = Date.now;
  const start = realNow();
  const [realTimeout, realInterval] = [setTimeout, setInterval];
  Date.now = () => start + (realNow() - start) * scale;
  Object.assign(window, {
    setTimeout: (run: TimerHandler, ms = 0, ...args: unknown[]) => realTimeout(run, ms / scale, ...args),
    setInterval: (run: TimerHandler, ms = 0, ...args: unknown[]) => realInterval(run, ms / scale, ...args),
  });
}

/** Empty the probes' records, so what they held is not counted in a heap measurement. */
export function forget(page: Page): Promise<void> {
  return page.evaluate(() => (window as unknown as { __probe: { forget: () => void } }).__probe.forget());
}

/** What the probes have found so far. */
export function probed(page: Page): Promise<Probed> {
  return page.evaluate(() => (window as unknown as { __probe: { read: () => Probed } }).__probe.read());
}

/** Start sampling what the user holds, with the pointer resting at `at`. */
export async function startHolding(page: Page, at?: { x: number; y: number }): Promise<void> {
  if (at) await page.mouse.move(at.x, at.y);
  await page.evaluate((at) => (window as unknown as { __probe: { hold: (at?: object) => void } }).__probe.hold(at), at);
}

/** One request the app made: what it asked, and when (the page's epoch ms). */
export type Sent = {
  readonly path: string;
  readonly query: URLSearchParams;
  /** The query it serves: requests of one query never overlap (see `inFlight`). */
  readonly queryKey: string;
  /** For a list: a membership check, a seq-range refetch, or a page. */
  readonly type: "check" | "lookup" | "seq" | "page" | "other";
  readonly start: number;
  readonly end: number;
};

/** The app's API requests the probes recorded, but the stream's; one still open ends when they were read. */
export function requests(probe: Probed): Sent[] {
  return probe.fetches.flatMap(({ url, start, end }) => {
    const parsed = new URL(url, "http://page");
    if (!parsed.pathname.startsWith("/api/") || parsed.pathname === "/api/web/subscribe") return [];
    return [{ ...classify(parsed), start, end: end ?? probe.now }];
  });
}

/** The most requests of one query in flight at once, per query. */
export function inFlight(sent: readonly Sent[]): Map<string, number> {
  const most = new Map<string, number>();
  for (const [key, requests] of groupBy(sent, (s) => s.queryKey)) {
    const edges = requests.flatMap((s) => [
      { at: s.start, step: 1 },
      { at: s.end, step: -1 },
    ]);
    // Ends before starts at one instant: a request that ends as the next starts is not two in flight.
    edges.sort((a, b) => a.at - b.at || a.step - b.step);
    let now = 0;
    for (const { step } of edges) most.set(key, Math.max(most.get(key) ?? 0, (now += step)));
  }
  return most;
}

/** Requests of one query that were in flight at once: each pair, the later one second. */
export function overlaps(sent: readonly Sent[]): [Sent, Sent][] {
  const pairs: [Sent, Sent][] = [];
  for (const requests of groupBy(sent, (s) => s.queryKey).values()) {
    const sorted = requests.toSorted((a, b) => a.start - b.start);
    for (let n = 1; n < sorted.length; n++) {
      const open = sorted.slice(0, n).find((earlier) => earlier.end > sorted[n]!.start);
      if (open) pairs.push([open, sorted[n]!]);
    }
  }
  return pairs;
}

/** The least time between two membership checks of one list, per list. */
export function checkSpacing(sent: readonly Sent[]): Map<string, number> {
  const least = new Map<string, number>();
  for (const [key, list] of groupBy(
    sent.filter((s) => s.type === "check"),
    (s) => s.queryKey,
  )) {
    const starts = list.map((s) => s.start).sort((a, b) => a - b);
    for (let n = 1; n < starts.length; n++) least.set(key, Math.min(least.get(key) ?? Infinity, starts[n]! - starts[n - 1]!));
  }
  return least;
}

/** The 95th percentile of `values`: the smallest value at least 95% of them do not exceed. */
export function p95(values: readonly number[]): number {
  const sorted = values.toSorted((a, b) => a - b);
  return sorted[Math.max(0, Math.ceil(sorted.length * 0.95) - 1)]!;
}

/**
 * Time from a write to its effect on screen. `shownAt` runs in the page every
 * frame with `args`, from before `write` runs, and returns `Date.now()` once
 * the change shows (false until then). Resolves with the milliseconds from the
 * write's return to that frame.
 */
export async function timeToShow(
  page: Page,
  shownAt: (args: string[]) => number | false,
  args: string[],
  write: () => Promise<Written>,
): Promise<number> {
  const seen = page.waitForFunction(shownAt, args, { polling: "raf", timeout: 30_000 });
  const { at } = await write();
  return Number(await (await seen).jsonValue()) - at;
}

/** Open the Issue list, filtered to `label` through the Filter menu, once the stream is connected. */
export async function openList(page: Page, url: string, label: string): Promise<void> {
  const subscribed = page.waitForResponse((response) => response.url().includes("/api/web/subscribe"));
  await page.goto(`${url}/app/#/list/Issue`);
  await subscribed;
  await filterTo(page, label);
}

/** Filter the open list to `label` through the Filter menu. */
export async function filterTo(page: Page, label: string): Promise<void> {
  await page.getByRole("button", { name: /^Filter$/ }).click();
  await page.getByRole("option", { name: "Label", exact: true }).click();
  await page.getByRole("combobox").fill(label);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Escape");
  await expect(page.getByTitle("The same query from the CLI")).toContainText(`labels is ${label}`);
}

/** Open the detail of inquiry `id`, once the stream is connected and the detail has loaded. */
export async function openDetail(page: Page, url: string, id: string): Promise<void> {
  const subscribed = page.waitForResponse((response) => response.url().includes("/api/web/subscribe"));
  await page.goto(`${url}/app/#/lookup/${id}`);
  await subscribed;
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Which query a request serves, and for a list, what kind of request it is. */
function classify(url: URL): Pick<Sent, "path" | "query" | "queryKey" | "type"> {
  const query = url.searchParams;
  const path = url.pathname;
  if (path !== "/api/inquiries") return { path, query, queryKey: `${path}?${query.get("kind") ?? ""}`, type: "other" };
  const filters = query.getAll("filter").map((raw) => JSON.parse(raw) as { field: string; op: string; value: string });
  const id = filters.find((filter) => filter.field === "id");
  const rest = filters.filter((filter) => filter !== id);
  const queryKey = `list ${query.getAll("kind").join(",")} ${JSON.stringify(rest)}`;
  // A live membership check names ids by their ends, `(…)$`; the Activity
  // feed's lookups name whole ids, `^(…)$`.
  const type = id ? (id.value.startsWith("^") ? "lookup" : "check") : query.has("seq_range") ? "seq" : "page";
  return { path, query, queryKey: type === "lookup" ? `lookup ${id!.value}` : queryKey, type };
}

function groupBy<T>(items: readonly T[], key: (item: T) => string): Map<string, T[]> {
  const groups = new Map<string, T[]>();
  for (const item of items) groups.set(key(item), [...(groups.get(key(item)) ?? []), item]);
  return groups;
}

/**
 * Whether `url` answers OK within a second: a server that takes the request and
 * never answers would otherwise hold a startup past its deadline.
 */
export async function answers(url: string): Promise<boolean> {
  try {
    return (await fetch(url, { signal: AbortSignal.timeout(1_000) })).ok;
  } catch {
    return false;
  }
}

/** Whether `child` has exited: by a code, or, with no code, by a signal. */
function exited(child: ChildProcess): boolean {
  return child.exitCode !== null || child.signalCode !== null;
}

function kill(child: ChildProcess, signal: NodeJS.Signals): void {
  try {
    process.kill(-child.pid!, signal);
  } catch {
    // Already gone.
  }
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const address = probe.address();
      probe.close(() => (typeof address === "object" && address ? resolve(address.port) : reject(new Error("No port."))));
    });
  });
}
