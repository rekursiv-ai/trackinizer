// What scripts/screenshots.ts and scripts/demo_video.ts share: a trackinizer
// server of their own on a fresh data directory (--no-auth), serving the build in
// dist-screenshots/ and seeded by scripts/seed_screenshots.py, and the waits that
// tell a view has finished drawing.
import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import type { BrowserContext, Page, Request } from "@playwright/test";

/** What the seed prints: the ids of what the screenshots and the demo open. */
export type Seeded = {
  readonly belief: string;
  readonly paper: string;
  readonly experiment: string;
  readonly issue: string;
  readonly artifact: string;
  /** The large graph's showcase island, and the Belief in it that is most argued over. */
  readonly root: string;
  readonly cited: string;
  /** The live session the demo messages, and its agent's name. */
  readonly session: string;
  readonly agent: string;
};

/** A seeded server: where it answers, what it seeded, its data directory, and how to stop it. */
export type Served = { readonly origin: string; readonly seeded: Seeded; readonly data: string; stop(): Promise<void> };

export const WEB = fileURLToPath(new URL("..", import.meta.url));
const ROOT = fileURLToPath(new URL("../../../..", import.meta.url));
const DIST = join(WEB, "dist-screenshots");

/**
 * Start a server on `port` on a fresh data directory, serving the build in
 * dist-screenshots/, and seed it. A SIGINT or SIGTERM stops it and removes its
 * data. The seed writes some 5,000 nodes and their edges, which takes minutes.
 */
export async function serveSeeded(port: number): Promise<Served> {
  if (!existsSync(join(DIST, "index.html"))) throw new Error(`No build in ${DIST}; run the npm script, which builds it.`);
  const origin = `http://127.0.0.1:${port}`;
  // Another server there would answer the start check, and the captures would show its data.
  if (await answers(`${origin}/app/`)) throw new Error(`Port ${port} is in use; stop the server on it first.`);
  const data = mkdtempSync(join(tmpdir(), "trackinizer-capture-"));
  const server = serve(data, port);
  const remove = () => rmSync(data, { recursive: true, force: true });
  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.once(signal, () => {
      kill(server, "SIGKILL");
      remove();
      process.exit(1);
    });
  }
  const stopped = async () => {
    await stop(server);
    remove();
  };
  try {
    await serving(server, origin);
    const seeded = JSON.parse(
      execFileSync(join(WEB, "scripts", "seed_screenshots.py"), ["--url", origin], { encoding: "utf8" }),
    ) as Seeded;
    // The seed locks no Issue, so no rules are in force and the welcome flow does not stand over the views.
    return { origin, seeded, data, stop: stopped };
  } catch (error) {
    await stopped();
    throw error;
  }
}

/**
 * Open the graph with the large graph's limit and the given panels shown, as
 * the tab's kept state (`trackinizer.v2.graph` in sessionStorage) holds them.
 */
export async function keepGraphState(context: BrowserContext, { roots }: { roots: boolean }): Promise<void> {
  const state = { limit: 5000, hiddenKinds: [], hiddenStatuses: [], skipEdges: [], only: false, key: true, roots };
  await context.addInitScript((kept) => {
    // Only once: a later visit keeps what the page itself changed.
    if (sessionStorage.getItem("trackinizer.v2.graph") === null) sessionStorage.setItem("trackinizer.v2.graph", kept);
  }, JSON.stringify(state));
}

/** The page's requests still open, but the live stream's, which stays open for as long as the page does. */
export function openRequests(page: Page): Set<Request> {
  const open = new Set<Request>();
  page.on("request", (request) => {
    if (!request.url().includes("/api/web/subscribe")) open.add(request);
  });
  page.on("requestfinished", (request) => open.delete(request));
  page.on("requestfailed", (request) => open.delete(request));
  return open;
}

/** Wait until `open` has stayed empty for `quietMs`, so what a view reads after it first draws has come. */
export async function settledRequests(page: Page, open: ReadonlySet<Request>, quietMs = 500): Promise<void> {
  let quietSince = Date.now();
  while (Date.now() - quietSince < quietMs) {
    if (open.size > 0) quietSince = Date.now();
    await page.waitForTimeout(50);
  }
}

/**
 * Wait until the graph has drawn, its layout has stopped and the view has framed
 * it: no node moved for a second, as the graph's e2e checks wait. The layout
 * starts from the same places with the same seeded randomness, and the server
 * answers the nodes in the order they were created, so it stops in the same
 * places on every run. Some 5,000 nodes take tens of seconds.
 */
export async function graphSettled(page: Page): Promise<void> {
  await page.locator(".graph-count").filter({ hasText: /\d nodes?$/ }).waitFor();
  const read = () =>
    page.evaluate(() => {
      const debug = (window as unknown as { trackinizer: { graph(): { nodes: { screen: unknown }[] } } }).trackinizer;
      return JSON.stringify(debug.graph().nodes.map((node) => node.screen));
    });
  let last = "";
  for (const deadline = Date.now() + 180_000; Date.now() < deadline; await page.waitForTimeout(1_000)) {
    const now = await read();
    if (now === last) return;
    last = now;
  }
  throw new Error("The graph's layout did not settle within 3 minutes.");
}

/** Throw unless `tool` runs, naming the package that installs it. */
export function requireTool(tool: string, args: readonly string[], install: string): void {
  try {
    execFileSync(tool, args, { stdio: "ignore" });
  } catch {
    throw new Error(`No ${tool} on PATH; install ${install}.`);
  }
}

/** The trackinizer server on `data`, serving the build in `DIST`. */
function serve(data: string, port: number): ChildProcess {
  const args = ["--quiet", "run", "--frozen", "python", "-m", "trackinizer.server"];
  args.push("--datadir", join(data, "pgdata"), "--no-auth", "--app-dir", DIST);
  args.push("--host", "127.0.0.1", "--port", String(port), "--log-level", "WARNING");
  // Its own process group, so a stop reaches the Python server and the PGlite
  // engine it starts, not only `uv`.
  return spawn("uv", args, { cwd: ROOT, stdio: ["ignore", "ignore", "inherit"], detached: true });
}

/** Wait until `server` serves the app, at most 2 minutes, and not after it has exited. */
async function serving(server: ChildProcess, origin: string): Promise<void> {
  for (const deadline = Date.now() + 120_000; Date.now() < deadline && !exited(server); await sleep(250)) {
    if (await answers(`${origin}/app/`)) return;
  }
  throw new Error(`The server did not start at ${origin}.`);
}

/** Stop `server` and wait until it has exited. */
async function stop(server: ChildProcess): Promise<void> {
  if (exited(server)) return;
  const ended = new Promise((resolve) => server.once("exit", resolve));
  kill(server, "SIGTERM");
  const timer = setTimeout(() => kill(server, "SIGKILL"), 5_000);
  await ended;
  clearTimeout(timer);
}

/** Whether `url` answers OK within a second. */
async function answers(url: string): Promise<boolean> {
  try {
    return (await fetch(url, { signal: AbortSignal.timeout(1_000) })).ok;
  } catch {
    return false;
  }
}

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

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
