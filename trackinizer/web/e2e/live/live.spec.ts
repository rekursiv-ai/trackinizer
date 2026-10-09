import { type APIRequestContext, expect, type Page, test } from "@playwright/test";
import {
  accelerate,
  checkSpacing,
  forget,
  inFlight,
  installProbes,
  LiveServer,
  filterTo,
  openDetail,
  openList,
  overlaps,
  p95,
  type Probed,
  probed,
  requests,
  type Sent,
  sleep,
  startHolding,
  timeToShow,
  type Written,
  Writer,
} from "./harness";
import { abortStream, STREAM_ROUTES, STREAM_URL, streamOpened } from "../fixtures";

// The live and responsiveness suite (the plan's testing layer 7): a writer
// drives the suite's own server through the Python client while the page
// records, in itself, how long each change takes to show, its long tasks and
// layout shifts, what the user holds, and every request it makes. Each test
// holds the page to the plan's requirements table and logs what it measured.
//
// One file, run in order: the tests share one server, and a burst or a
// restart in one would skew another's timings.

test.describe.configure({ mode: "serial" });

// The canvas is on by default, and its Chat pane (360 px), its toolbar and a pane
// header (70 px) take room from the page. This screen leaves the page the 1035 x
// 650 it had on the default 1280 x 720, so what the suite measures (the width rows
// wrap at, the height they scroll in) is the page's, not the screen's.
test.use({ viewport: { width: 1640, height: 790 } });

let server: LiveServer;
let writer: Writer;
/** Every test's measurements, logged as one JSON line when the suite ends. */
const measured: { [test: string]: unknown } = {};
/** What the probes found while the paused bar showed, for the test that holds it to the stability rule. */
let whenPaused: Probed | undefined;

/** The stream's route, as `page.route` matches it. */
// The page's live stream, whichever it uses: the canvas's events stream (the default) or /api/web/subscribe (opted out).
const SUBSCRIBE = STREAM_ROUTES;

test.beforeAll(async () => {
  test.setTimeout(180_000);
  server = await LiveServer.start();
  // No Issue is locked on this server, so no rules are in force and the welcome flow never stands over a page.
  writer = new Writer(server.url);
});

test.afterAll(async () => {
  writer?.close();
  await server?.close();
  console.log(`live suite: ${JSON.stringify(measured)}`);
});

test.beforeEach(async ({ page }) => {
  await page.addInitScript(installProbes);
});

test("the probes catch a long task, a shift, a slow key, an open request and a pointer on no row the moment they happen, so their silence elsewhere means none", async ({
  page,
  request,
}) => {
  const label = fresh("control");
  await seed(request, label, 30);
  await openList(page, server.url, label);
  const { pointer } = await holdThings(page);
  // Aimed at the list's header, the pointer rests on no row: a stillness check must not pass on that.
  const header = (await page.locator(".view-h").boundingBox())!;
  await startHolding(page, { x: header.x + header.width / 2, y: header.y + header.height / 2 });
  const onNoRow = await probed(page);
  expect(() => expectHeldStill(onNoRow)).toThrow("a row under the pointer");
  await startHolding(page, pointer);
  expect(await probed(page)).toMatchObject({ longTasks: [], shifts: [] });
  // An 80 ms task of the page's own (one run from here would be DevTools', which
  // the Long Tasks API does not count), then a banner pushing the rows down,
  // read back as soon as the next frame has painted.
  await page.evaluate(
    () =>
      new Promise<void>((done) =>
        setTimeout(() => {
          const end = performance.now() + 80;
          while (performance.now() < end);
          document.querySelector(".view .scroll")!.before(Object.assign(document.createElement("div"), { style: "height: 40px" }));
          requestAnimationFrame(() => setTimeout(done, 0));
        }, 0),
      ),
  );
  const probe = await probed(page);
  expect(probe.longTasks.map((task) => task.ms >= 80)).toEqual([true]);
  expect(rowShifts(probe), "layout shifts that moved list rows").not.toEqual([]);
  expect(() => expectHeldStill(probe)).toThrow();
  // A key whose handler takes 120 ms: Event Timing sees it take over 100 ms to the next paint.
  await page.evaluate(() =>
    addEventListener(
      "keydown",
      () => {
        const end = performance.now() + 120;
        while (performance.now() < end);
      },
      { capture: true, once: true },
    ),
  );
  await page.keyboard.press("Shift");
  await expect.poll(async () => (await probed(page)).keys.filter((key) => key.ms > 100)).toHaveLength(1);
  // A request with no answer yet, and one whose headers came but not its body (the stream's): both still open.
  await page.route("**/api/inquiries?probe=*", () => {});
  const headers = page.waitForResponse((response) => response.url().endsWith("/api/web/subscribe?probe"));
  await page.evaluate(() => {
    void fetch("/api/inquiries?probe=1");
    void fetch("/api/web/subscribe?probe");
  });
  await headers;
  const open = (await probed(page)).fetches.filter((sent) => sent.url.includes("probe"));
  expect(open.map((sent) => [sent.url, sent.end])).toEqual([
    ["/api/inquiries?probe=1", null],
    ["/api/web/subscribe?probe", null],
  ]);
});

test("an edit to a row on screen shows in the list and the peek within 2 s, and moves nothing the user holds", async ({
  page,
  request,
}) => {
  test.setTimeout(120_000);
  const label = fresh("edits");
  await seed(request, label, 30);
  await openList(page, server.url, label);
  const { peeked, pointer } = await holdThings(page);
  const onScreen = await rowsOnScreen(page);
  await startHolding(page, pointer);
  const latencies: number[] = [];
  for (let n = 0; n < 20; n++) {
    // Every fourth edit is to the peeked row, which shows in the list and the peek.
    const id = n % 4 === 0 ? peeked : onScreen[(n * 7) % onScreen.length]!;
    const title = `Edited ${n} ${label}`;
    latencies.push(
      await timeToShow(
        page,
        ([id, title, peeked]) =>
          document.querySelector(`a.row[data-row="${id}"] .row-title`)?.textContent === title &&
          (id !== peeked || document.querySelector(".peek .d-title")?.textContent === title) &&
          Date.now(),
        [id, title, peeked],
        () => writer.edit(id, "title", title),
      ),
    );
    // Spread the writes over the batch window's phases.
    await sleep(250 + ((n * 373) % 900));
  }
  const probe = await probed(page);
  const load = requestLoad(requests(probe));
  measured.edits = { latencies, p95: p95(latencies), ...load, longTasks: probe.longTasks, shifts: probe.shifts };
  expect(p95(latencies)).toBeLessThan(2_000);
  expectHeldStill(probe);
  expectNoLongTasks(probe);
  expectWithinCaps(load);
});

test("a new matching row is counted in the pill within 3 s, one that does not match never is, and the pill brings them in", async ({
  page,
  request,
}) => {
  test.setTimeout(120_000);
  const label = fresh("creates");
  await seed(request, label, 30);
  await openList(page, server.url, label);
  const { pointer } = await holdThings(page);
  await startHolding(page, pointer);
  const latencies: number[] = [];
  for (let n = 1; n <= 15; n++) {
    await writer.create(`Unrelated ${n} ${label}`, [fresh("other")]);
    latencies.push(
      await timeToShow(
        page,
        ([count]) => document.querySelector(".live-pill")?.textContent === `${count} new` && Date.now(),
        [String(n)],
        () => writer.create(`Created ${n} ${label}`, [label]),
      ),
    );
    await sleep(250 + ((n * 373) % 900));
  }
  // Every check has answered once the pill has stood still for a check's 2 s.
  await sleep(3_000);
  await expect(page.locator(".live-pill")).toHaveText("15 new");
  const probe = await probed(page);
  const load = requestLoad(requests(probe));
  measured.creates = { latencies, p95: p95(latencies), ...load, longTasks: probe.longTasks, shifts: probe.shifts };
  expect(p95(latencies)).toBeLessThan(3_000);
  expectHeldStill(probe);
  expectNoLongTasks(probe);
  expectWithinCaps(load);

  await page.locator(".live-pill").click();
  await expect(page.locator(".live-pill")).toHaveCount(0);
  await expect(page.locator("a.row")).toHaveCount(45);
  const titles = await page.locator("a.row .row-title").allTextContents();
  expect(titles.slice(0, 15)).toEqual(Array.from({ length: 15 }, (_, n) => `Created ${15 - n} ${label}`));
  expect(titles.filter((title) => title.startsWith("Unrelated"))).toEqual([]);
  expect(await page.locator(".view .scroll").evaluate((scroller) => scroller.scrollTop)).toBe(0);
});

test("at the top of an idle list, a new matching row shows in the list itself within 3 s", async ({ page, request }) => {
  const label = fresh("idle");
  await seed(request, label, 3);
  await openList(page, server.url, label);
  await expect(page.locator("a.row")).toHaveCount(3);
  // Off the list: a pointer resting on its rows keeps new ones behind the pill.
  await page.mouse.move(0, 0);
  const latencies: number[] = [];
  for (let n = 1; n <= 5; n++) {
    const title = `Joined ${n} ${label}`;
    latencies.push(
      await timeToShow(
        page,
        ([title]) =>
          [...document.querySelectorAll("a.row .row-title")].some((row) => row.textContent === title) && Date.now(),
        [title],
        () => writer.create(title, [label]),
      ),
    );
    await sleep(500);
  }
  measured.idleJoins = { latencies, p95: p95(latencies) };
  expect(p95(latencies)).toBeLessThan(3_000);
});

test("a row that stops matching dims within 2 s and stays where it was", async ({ page, request }) => {
  test.setTimeout(60_000);
  const label = fresh("leaving");
  await seed(request, label, 30);
  await openList(page, server.url, label);
  const { pointer } = await holdThings(page);
  const onScreen = await rowsOnScreen(page);
  await startHolding(page, pointer);
  expect(onScreen.length).toBeGreaterThanOrEqual(8);
  const latencies: number[] = [];
  for (let n = 0; n < 8; n++) {
    const id = onScreen[n]!;
    latencies.push(
      await timeToShow(
        page,
        ([id]) =>
          (document.querySelector(".view .scroll style")?.textContent ?? "").includes(id!) &&
          getComputedStyle(document.querySelector(`a.row[data-row="${id}"]`)!).opacity === "0.45" &&
          Date.now(),
        [id],
        () => writer.edit(id, "labels", [fresh("moved")]),
      ),
    );
    await sleep(250 + ((n * 373) % 900));
  }
  const probe = await probed(page);
  measured.leaving = { latencies, p95: p95(latencies), longTasks: probe.longTasks, shifts: probe.shifts };
  expect(p95(latencies)).toBeLessThan(2_000);
  expectHeldStill(probe);
  expectNoLongTasks(probe);
});

test("a relation added or removed by the other client shows in an open detail within 2 s", async ({ page, request }) => {
  test.setTimeout(60_000);
  const label = fresh("edges");
  const [focus, ...peers] = await seed(request, label, 6);
  const titles = await page.request.get(`${server.url}/api/inquiries?kind=Issue&limit=10&filter=${filter("labels", label)}`);
  const titleOf = new Map(((await titles.json()) as { id: string; title: string }[]).map((row) => [row.id, row.title]));
  await openDetail(page, server.url, focus!);
  await startHolding(page);
  const latencies: number[] = [];
  // In the Parents rail, by its `requires` edge: the first edge between two rows
  // also infers a `produced_by` edge, which stays when the `requires` one goes.
  const shown = ([title, present]: string[]) =>
    [...document.querySelectorAll(".rail-peer")]
      .filter((peer) => [...peer.querySelectorAll(".edge-name")].some((edge) => edge.textContent === "requires"))
      .some((peer) => peer.querySelector(".rail-title")!.textContent!.startsWith(`${title} `)) ===
      (present === "yes") && Date.now();
  for (const peer of peers) {
    const edge = { op: "edge", from: focus, to: peer, kind: "requires" };
    latencies.push(await timeToShow(page, shown, [titleOf.get(peer)!, "yes"], () => writer.send<Written>(edge)));
    await sleep(400);
    latencies.push(await timeToShow(page, shown, [titleOf.get(peer)!, "no"], () => writer.send<Written>({ ...edge, remove: true })));
    await sleep(400);
  }
  const probe = await probed(page);
  measured.edges = { latencies, p95: p95(latencies), longTasks: probe.longTasks, shifts: probe.shifts };
  expect(p95(latencies)).toBeLessThan(2_000);
  expectHeldStill(probe);
  expectNoLongTasks(probe);
});

test("Activity: a change joins the top of the feed within 3 s, at most one ask per kind every 2 s", async ({ page, request }) => {
  test.setTimeout(90_000);
  const label = fresh("activity");
  const ids = await seed(request, label, 6);
  const subscribed = streamOpened(page);
  await page.goto(`${server.url}/app/#/activity`);
  await subscribed;
  await expect(page.locator(".feed-row").first()).toBeVisible();
  await startHolding(page);
  const latencies: number[] = [];
  for (let n = 0; n < 12; n++) {
    const lines = await page.locator(".feed-row").count();
    // Each row closes, then opens again: a status it already has would write nothing.
    latencies.push(
      await timeToShow(
        page,
        ([lines]) => document.querySelectorAll(".feed-row").length > Number(lines) && Date.now(),
        [String(lines)],
        () => writer.edit(ids[n % ids.length]!, "status", n < ids.length ? "complete" : "active"),
      ),
    );
    await sleep(250 + ((n * 373) % 900));
  }
  // Scrolled down, lines join above the one the user reads, which stays put.
  const feed = page.locator(".activity .scroll");
  const box = (await feed.boundingBox())!;
  const at = [box.x + box.width / 2, box.y + box.height / 2];
  await page.mouse.move(at[0]!, at[1]!);
  await page.mouse.wheel(0, 300);
  await expect.poll(() => feed.evaluate((scroller) => scroller.scrollTop)).toBeGreaterThan(0);
  await sleep(500);
  const underPointer = () =>
    page.evaluate(([x, y]) => {
      const line = document.elementFromPoint(x!, y!)?.closest(".feed-row");
      return line ? `${Math.round(line.getBoundingClientRect().top)} ${line.querySelector("time")?.getAttribute("datetime")}` : null;
    }, at);
  const read = await underPointer();
  const readLater: (string | null)[] = [];
  for (let n = 0; n < 3; n++) {
    const lines = await page.locator(".feed-row").count();
    await writer.edit(ids[n]!, "status", "complete");
    await expect.poll(() => page.locator(".feed-row").count()).toBeGreaterThan(lines);
    readLater.push(await underPointer());
  }
  const probe = await probed(page);
  const sent = requests(probe);
  const load = requestLoad(sent);
  // Today's event rate behind an open feed: edits no tab shows still ask.
  await forget(page);
  await writer.send({ op: "steady", ids, rate: 3.1, seconds: 20 });
  await sleep(2_000);
  const steadyProbe = await probed(page);
  const steady = requestLoad(requests(steadyProbe));
  measured.activity = {
    latencies,
    p95: p95(latencies),
    ...load,
    steady,
    underPointer: [read, ...readLater],
    longTasks: probe.longTasks,
    shifts: probe.shifts,
  };
  expect(p95(latencies)).toBeLessThan(3_000);
  expect(readLater, "the line under the pointer, scrolled down, as lines join above").toEqual([read, read, read]);
  expectNoLongTasks(probe);
  expectNoLongTasks(steadyProbe);
  expectWithinCaps(load);
  expectWithinCaps(steady);
  expect(steady.perSecond, "requests a second at today's event rate").toBeLessThanOrEqual(3);
  // The asks of one kind, one every 2 s at most.
  const asks = [...sent, ...requests(steadyProbe)].filter((s) => s.path === "/api/change_log" && s.query.has("since"));
  for (const kind of new Set(asks.map((s) => s.query.get("kind")))) {
    const starts = asks.filter((s) => s.query.get("kind") === kind).map((s) => s.start);
    for (let n = 1; n < starts.length; n++) expect(starts[n]! - starts[n - 1]!).toBeGreaterThanOrEqual(1_950);
  }
});

test("search results stay the snapshot they were, labelled with its time, while their rows change", async ({ page }) => {
  const token = crypto.randomUUID().slice(0, 8);
  const { id } = await writer.create(`Snapshot ${token}`, [fresh("search")]);
  await page.goto(`${server.url}/app/#/activity`);
  await expect(page.locator(".feed-row").first()).toBeVisible();
  await page.keyboard.press("ControlOrMeta+k");
  await page.getByRole("combobox", { name: "Command" }).fill(token);
  const hit = page.locator(".pal-item", { hasText: `Snapshot ${token}` });
  await expect(hit).toBeVisible();
  const heading = page.locator(".pal-sec", { hasText: "From the server" });
  const label = await heading.textContent();
  expect(label).toMatch(/^From the server · as of \d/);
  const searches = requests(await probed(page)).filter((s) => s.path === "/api/web/search").length;
  await writer.edit(id, "title", `Renamed ${token}`);
  await sleep(3_000);
  await expect(hit).toBeVisible();
  await expect(heading).toHaveText(label!);
  expect(requests(await probed(page)).filter((s) => s.path === "/api/web/search")).toHaveLength(searches);
  measured.search = { label, searches };
});

test("an open draft keeps its text, caret and focus through every live update of its row", async ({ page, request }) => {
  const [id, neighbour] = await seed(request, fresh("draft"), 2);
  await openDetail(page, server.url, id!);
  await page.getByRole("button", { name: "Edit Description" }).click();
  const editor = page.getByRole("textbox", { name: "Description" });
  await editor.pressSequentially("Half-written");
  await editor.press("ArrowLeft");
  await startHolding(page);
  const titles: string[] = [];
  for (let n = 0; n < 4; n++) {
    titles.push(`Retitled ${n}`);
    await writer.edit(id!, "title", titles.at(-1));
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(titles.at(-1)!);
  }
  await writer.edit(id!, "priority", 10);
  await writer.send({ op: "edge", from: neighbour, to: id, kind: "requires" });
  // The neighbour that requires it is one of its children, in the rail.
  await expect(page.getByRole("region", { name: "Children", exact: true }).locator(".rail-peer")).not.toHaveCount(0);
  const probe = await probed(page);
  measured.draft = { held: probe.held };
  expectHeldStill(probe);
  await expect(editor).toHaveValue("Half-written");
});

test("at today's event rate, a list and its peek stay within the request caps", async ({ page, request }) => {
  test.setTimeout(90_000);
  const label = fresh("rate");
  await seed(request, label, 30);
  const outsiders = await seed(request, fresh("elsewhere"), 2);
  await openList(page, server.url, label);
  const { peeked, pointer } = await holdThings(page);
  const onScreen = await rowsOnScreen(page);
  await startHolding(page, pointer);
  // Six distinct ids, as a 20 s sample of the production stream had: three
  // rows on screen, the peeked one, and two the list does not hold.
  const ids = [...onScreen.filter((id) => id !== peeked).slice(0, 3), peeked, ...outsiders];
  const { ats } = await writer.send<{ ats: number[] }>({ op: "steady", ids, rate: 3.1, seconds: 30 });
  await sleep(2_000);
  const probe = await probed(page);
  const load = requestLoad(requests(probe));
  measured.todaysRate = { writes: ats.length, ...load, longTasks: probe.longTasks, shifts: probe.shifts };
  expect(load.perSecond, "requests a second").toBeLessThanOrEqual(3);
  expectWithinCaps(load);
  expectHeldStill(probe);
  expectNoLongTasks(probe);
});

test("a short drop: the paused bar never shows, and gap recovery shows what the stream missed", async ({ page, request }) => {
  test.setTimeout(90_000);
  const label = fresh("drop");
  const [id] = await seed(request, label, 5);
  await openList(page, server.url, label);
  await expect(page.locator("a.row")).toHaveCount(5);
  await page.evaluate(() => {
    Object.assign(window, { __pausedSeen: false });
    new MutationObserver(() => document.querySelector(".bar.paused") && Object.assign(window, { __pausedSeen: true })).observe(
      document.body,
      { childList: true, subtree: true },
    );
  });
  const reconnected = page.waitForResponse(
    (response) => STREAM_URL.test(response.url()) && response.status() === 200,
    { timeout: 60_000 },
  );
  const downAt = Date.now();
  await holdOffStream(page);
  await server.down();
  await server.up();
  const title = `Changed in the gap ${label}`;
  // Written while the stream is held off, so it never carries the change: only gap recovery can show it.
  const latency = await timeToShow(
    page,
    ([id, title]) => document.querySelector(`a.row[data-row="${id}"] .row-title`)?.textContent === title && Date.now(),
    [id!, title],
    async () => {
      const written = await writer.edit(id!, "title", title);
      for (const route of SUBSCRIBE) await page.unroute(route);
      return written;
    },
  );
  await reconnected;
  const downMs = Date.now() - downAt;
  const pausedSeen = await page.evaluate(() => (window as unknown as { __pausedSeen: boolean }).__pausedSeen);
  measured.shortDrop = { downMs, shownAfterWriteMs: latency, pausedSeen };
  expect(downMs, "a drop under 10 s, as this test needs").toBeLessThan(10_000);
  expect(pausedSeen, "no paused bar at any time during a drop under 10 s").toBe(false);
});

test("a long drop shows the paused bar after 10 s, and the restart clears it and recovers what the stream missed", async ({
  page,
  request,
}) => {
  test.setTimeout(120_000);
  const label = fresh("restart");
  const [id] = await seed(request, label, 30);
  await openList(page, server.url, label);
  const { pointer } = await holdThings(page);
  await startHolding(page, pointer);
  const downAt = Date.now();
  await holdOffStream(page);
  await server.down();
  const barAt = await (
    await page.waitForFunction(() => document.querySelector(".bar.paused") !== null && Date.now(), undefined, { polling: "raf", timeout: 30_000 })
  ).jsonValue();
  const paused = await probed(page);
  await server.up();
  const title = `Changed after the restart ${label}`;
  // Written while the stream is held off, so only gap recovery can show it.
  const latency = await timeToShow(
    page,
    ([id, title]) =>
      document.querySelector(".bar.paused") === null &&
      document.querySelector(`a.row[data-row="${id}"] .row-title`)?.textContent === title &&
      Date.now(),
    [id!, title],
    async () => {
      const written = await writer.edit(id!, "title", title);
      for (const route of SUBSCRIBE) await page.unroute(route);
      return written;
    },
  );
  measured.longDrop = {
    barAfterMs: Number(barAt) - downAt,
    recoveredAfterWriteMs: latency,
    heldWhenPaused: paused.held,
    shiftsWhenPaused: paused.shifts,
  };
  whenPaused = paused;
  expect(Number(barAt) - downAt).toBeGreaterThanOrEqual(10_000);
});

test("the paused bar moves nothing the user holds", () => {
  // It floats over the view (`BarStack` in src/ui/bars.tsx); in the view's
  // flow it pushed the list down 34 px (measured).
  expectHeldStill(whenPaused!);
});

test("a burst of 20 matching creates a second for 60 s: the page stays responsive, within the caps, and counts every row", async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const label = fresh("burst");
  await seed(request, label, 30);
  await openList(page, server.url, label);
  const { pointer } = await holdThings(page);
  await page.keyboard.press("Escape");
  await expect(page.locator(".peek")).toHaveCount(0);
  await startHolding(page, pointer);
  // Whether each j or k shows its move in the very next frame.
  await page.evaluate(() => {
    const frames: boolean[] = [];
    addEventListener(
      "keydown",
      () => {
        const before = document.querySelector<HTMLElement>("a.row.is-focused")?.dataset.row;
        requestAnimationFrame(() => frames.push(document.querySelector<HTMLElement>("a.row.is-focused")?.dataset.row !== before));
      },
      { capture: true },
    );
    Object.assign(window, { __frames: frames });
  });
  let done = false;
  const burst = writer
    .send<{ ids: string[]; ats: number[] }>({ op: "burst", rate: 20, seconds: 60, title: `Burst ${label}`, labels: [label] })
    .finally(() => (done = true));
  let presses = 0;
  while (!done) {
    await page.keyboard.press(presses++ % 2 ? "k" : "j");
    await sleep(250);
  }
  const { ids, ats } = await burst;
  const writtenFor = (ats.at(-1)! - ats[0]!) / 1_000;
  // The last ids wait at most one check of 63 every 2 s.
  await expect(page.locator(".live-pill")).toHaveText(`${ids.length} new`, { timeout: 30_000 });
  const probe = await probed(page);
  const load = requestLoad(requests(probe));
  const frames = await page.evaluate(() => (window as unknown as { __frames: boolean[] }).__frames);
  measured.burst = {
    created: ids.length,
    writtenForSeconds: writtenFor,
    presses,
    movedNextFrame: frames.filter(Boolean).length,
    // Event Timing reports only keys that took 16 ms or more to the next paint;
    // the probes' own test shows it catching a slow one.
    keysOver16Ms: probe.keys.length,
    slowestKeyMs: Math.max(0, ...probe.keys.map((key) => key.ms)),
    ...load,
    longTasks: probe.longTasks,
  };
  expect(ids.length).toBe(1_200);
  // Under 19 a second, the writer fell behind and the page was never held to the burst.
  expect(ids.length / writtenFor, "creates a second, as written").toBeGreaterThanOrEqual(19);
  expect(load.perSecond, "requests a second").toBeLessThanOrEqual(3);
  expect(frames, "each j and k shows in the next frame").toEqual(frames.map(() => true));
  expect(probe.keys.filter((key) => key.ms > 100), "keys without feedback within 100 ms").toEqual([]);
  expectNoLongTasks(probe);
  expectWithinCaps(load);
});

test("an accelerated soak of 8 hours of batches leaves the heap within 20% of its start", async ({ page, request }) => {
  // Today's stream has a frame in nearly every second (64 frames, 6 distinct
  // ids in 20 s), so 8 hours is 28,800 one-second batches of one or two ids.
  // The page's clock runs 100x, so a batch is 10 ms, and the writer edits one
  // of six ids per accelerated second: three rows on screen, the peeked one,
  // and two the list does not hold.
  test.setTimeout(900_000);
  const scale = 100;
  const label = fresh("soak");
  await seed(request, label, 30);
  const outsiders = await seed(request, fresh("elsewhere"), 2);
  await page.addInitScript(accelerate, scale);
  await openList(page, server.url, label);
  const { peeked, pointer } = await holdThings(page);
  await startHolding(page, pointer);
  const onScreen = await rowsOnScreen(page);
  const ids = [...onScreen.filter((id) => id !== peeked).slice(0, 3), peeked, ...outsiders];
  const cdp = await page.context().newCDPSession(page);
  const heap = async () => {
    await forget(page);
    await cdp.send("HeapProfiler.collectGarbage");
    return (await cdp.send("Runtime.getHeapUsage")).usedSize;
  };
  // A warm-up of 10 accelerated minutes fills the caches the soak then reuses.
  await writer.send({ op: "steady", ids, rate: scale, seconds: 600 / scale });
  const hours = [await heap()];
  const from = Date.now();
  let longTasks = 0;
  // Read each hour's requests before the heap is measured, which empties the probes.
  const soak: Sent[] = [];
  // Each hour's live reads: refetches of the rows on screen and checks of the ids the list does not hold.
  const liveReads: { seq: number; check: number }[] = [];
  for (let hour = 1; hour <= 8; hour++) {
    await writer.send({ op: "steady", ids, rate: scale, seconds: 3_600 / scale });
    const probe = await probed(page);
    longTasks += probe.longTasks.length;
    const sent = requests(probe);
    soak.push(...sent);
    liveReads.push({ seq: sent.filter((s) => s.type === "seq").length, check: sent.filter((s) => s.type === "check").length });
    hours.push(await heap());
  }
  const growth = hours.at(-1)! / hours[0]! - 1;
  measured.soak = {
    realSeconds: (Date.now() - from) / 1_000,
    writes: 8 * 3_600,
    ...requestLoad(soak),
    heapMb: hours.map((bytes) => Math.round(bytes / 10_000) / 100),
    growth,
    longTasks,
    liveReads,
  };
  // A page that did no live work would keep its heap for free: every hour must have done some.
  for (const [hour, reads] of liveReads.entries()) {
    expect(reads.seq, `refetches of rows on screen in hour ${hour + 1}`).toBeGreaterThan(0);
    expect(reads.check, `membership checks in hour ${hour + 1}`).toBeGreaterThan(0);
  }
  expect(growth, "heap growth over the soak").toBeLessThanOrEqual(0.2);
});

test("with the CPU slowed 4x: first load, live batches, and a busy hub's detail", async ({ browser, request }) => {
  test.setTimeout(180_000);
  // Enough Issues for a full first page, and a hub: 60 children, each also
  // produced by it, and the Artifact it produced, as phase 1's check has.
  await seed(request, fresh("page"), 50);
  const hub = await seedHub(request, 60);
  const results: { [case_: string]: unknown } = {};
  for (const rate of [1, 4]) {
    // A first visit each time: a new context has no cached code, and the
    // slowdown holds from its first navigation (not across a later one).
    const visit = async (path: string) => {
      const context = await browser.newContext();
      await context.addInitScript(installProbes);
      const page = await context.newPage();
      await (await context.newCDPSession(page)).send("Emulation.setCPUThrottlingRate", { rate });
      const subscribed = streamOpened(page);
      await page.goto(`${server.url}/app/${path}`);
      return { context, page, subscribed };
    };
    // First load of the Issue list, to 50 rows, then live batches on it with a peek.
    const list = await visit("#/list/Issue");
    const usableMs = await (
      await list.page.waitForFunction(() => document.querySelectorAll("a.row").length >= 50 && performance.now(), undefined, {
        polling: "raf",
      })
    ).jsonValue();
    const boot = (await probed(list.page)).longTasks;
    await list.subscribed;
    const label = fresh(`slow${rate}`);
    await seed(request, label, 30);
    await filterTo(list.page, label);
    const { peeked, pointer } = await holdThings(list.page);
    const onScreen = await rowsOnScreen(list.page);
    await startHolding(list.page, pointer);
    for (let n = 0; n < 10; n++) {
      const id = n % 2 ? peeked : onScreen[n]!;
      const title = `Slow ${n} ${label}`;
      await timeToShow(
        list.page,
        ([id, title]) => document.querySelector(`a.row[data-row="${id}"] .row-title`)?.textContent === title && Date.now(),
        [id, title],
        () => writer.edit(id, "title", title),
      );
      await sleep(300);
    }
    const batches = (await probed(list.page)).longTasks;
    await list.context.close();
    // The hub's detail from a first visit: its long tasks after its response.
    const detail = await visit(`#/lookup/${hub}`);
    const children = detail.page.getByRole("region", { name: "Children", exact: true });
    await expect(children.locator(".rail-peer")).toHaveCount(10);
    await expect(children.getByRole("button", { name: /^Show all/ })).toBeVisible();
    const hubTasks = await detail.page.evaluate(() => {
      const response = performance
        .getEntriesByType("resource")
        .find((entry): entry is PerformanceResourceTiming => entry.name.includes("/api/web/get/"))!;
      return (window as unknown as { __probe: { read: () => Probed } }).__probe
        .read()
        .longTasks.filter((task) => task.start + task.ms > response.responseEnd);
    });
    // The probe sees a long task at this rate too, so its silence above means none.
    await expectBusyTaskSeen(detail.page, `${rate}x`);
    await detail.context.close();
    results[`${rate}x`] = { usableMs, bootLongTasks: boot, batchLongTasks: batches, hubLongTasks: hubTasks };
    if (rate === 1) expect(usableMs, "a list usable within 1.5 s of a fresh load").toBeLessThan(1_500);
    expect(boot, `long tasks at boot, ${rate}x`).toEqual([]);
    expect(batches, `long tasks while applying live batches, ${rate}x`).toEqual([]);
    expect(hubTasks, `long tasks rendering the hub's detail, ${rate}x`).toEqual([]);
  }
  measured.cpu = results;
});

/** An Issue narrowed by `children` Issues, which the first such edge also makes it produce, and one Artifact it produced. */
async function seedHub(request: APIRequestContext, children: number): Promise<string> {
  const tag = crypto.randomUUID().slice(0, 8);
  const items = [
    { kind: "Issue", title: `Hub ${tag}`, description: "A hub with **many** children." },
    ...Array.from({ length: children }, (_, n) => ({ kind: "Issue", title: `Hub child ${n} ${tag}` })),
    { kind: "Artifact", title: `Hub artifact ${tag}` },
  ].map((item) => ({ ...item, idempotency_key: crypto.randomUUID() }));
  const edges = [
    ...Array.from({ length: children }, (_, n) => ({ from_index: n + 1, to_index: 0, edge_kind: "narrows" })),
    { from_index: children + 1, to_index: 0, edge_kind: "produced_by" },
  ];
  const response = await request.post(`${server.url}/api/inquiries/batch`, { data: { items, edges } });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).ids[0];
}

/** Refuse the page's stream connections until the `SUBSCRIBE` routes are unrouted, so the stream stays down whatever the server does. */
async function holdOffStream(page: Page): Promise<void> {
  await abortStream(page);
}

/** A label no other test uses. */
function fresh(name: string): string {
  return `live-${name}-${crypto.randomUUID().slice(0, 8)}`;
}

/** A list filter as the `filter` query parameter carries it. */
function filter(field: string, value: string): string {
  return encodeURIComponent(JSON.stringify({ field, op: "is", value }));
}

/** Create `count` Issues labelled `label` in one batch, oldest first; returns their ids. */
async function seed(request: APIRequestContext, label: string, count: number): Promise<string[]> {
  const items = Array.from({ length: count }, (_, n) => ({
    kind: "Issue",
    title: `Row ${n} ${label}`,
    labels: [label],
    idempotency_key: crypto.randomUUID(),
  }));
  const response = await request.post(`${server.url}/api/inquiries/batch`, { data: { items, edges: [] } });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).ids;
}

/**
 * Hold what a user holds in a list: focus the fourth row, select two rows,
 * scroll a little, rest the pointer on a row, and peek at the focused row.
 * Resolves with the peeked row and where the pointer rests.
 */
async function holdThings(page: Page): Promise<{ peeked: string; pointer: { x: number; y: number } }> {
  await expect(page.locator("a.row").first()).toBeVisible();
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  for (const key of ["j", "j", "j", "x", "j", "x", "k"]) await page.keyboard.press(key);
  await expect(page.locator(".row-line.is-selected")).toHaveCount(2);
  const box = (await page.locator(".view .scroll").boundingBox())!;
  // Left of where the peek opens, in a page a Chat pane has narrowed: the peek takes up to 45% of the page, from the right.
  const pointer = { x: box.x + Math.min(box.width / 3, 120), y: box.y + box.height / 2 };
  await page.mouse.move(pointer.x, pointer.y);
  await page.mouse.wheel(0, 80);
  await expect.poll(() => page.locator(".view .scroll").evaluate((scroller) => scroller.scrollTop)).toBeGreaterThan(0);
  await page.keyboard.press("Space");
  await expect(page.locator(".peek .d-title")).toBeVisible();
  const peeked = (await page.locator("a.row.is-focused").getAttribute("data-row"))!;
  // Let the peek's reads and any batch already under way settle.
  await sleep(1_500);
  return { peeked, pointer };
}

/** Ids of the rows wholly inside the list's scroller. */
function rowsOnScreen(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const box = document.querySelector(".view .scroll")!.getBoundingClientRect();
    return [...document.querySelectorAll<HTMLElement>("a.row")]
      .filter((row) => row.getBoundingClientRect().top >= box.top && row.getBoundingClientRect().bottom <= box.bottom)
      .map((row) => row.dataset.row!);
  });
}

/** The request caps' numbers over `sent`: rate, most in flight per query, least time between checks. */
function requestLoad(sent: readonly Sent[]) {
  const seconds = sent.length ? (Math.max(...sent.map((s) => s.end)) - Math.min(...sent.map((s) => s.start))) / 1_000 : 0;
  return {
    requests: sent.length,
    perSecond: seconds ? sent.length / seconds : 0,
    mostInFlight: Math.max(0, ...inFlight(sent).values()),
    // The first overlapping requests of one query, if any, to say which.
    overlapping: overlaps(sent)
      .slice(0, 3)
      .map((pair) => pair.map((s) => `${s.type} ${s.path}?${s.query} ${Math.round(s.start)}..${Math.round(s.end)}`)),
    leastCheckGapMs: Math.min(Infinity, ...checkSpacing(sent).values()),
    byType: Object.fromEntries(["check", "seq", "page", "lookup", "other"].map((type) => [type, sent.filter((s) => s.type === type).length])),
  };
}

function expectWithinCaps(load: ReturnType<typeof requestLoad>): void {
  expect(load.mostInFlight, "requests of one query in flight at once").toBeLessThanOrEqual(1);
  // The app counts its 2 s from a few promise hops before it calls `fetch`,
  // where the probe stamps a request, so two checks can read a ms or so closer.
  expect(load.leastCheckGapMs, "least time between two membership checks of one list").toBeGreaterThanOrEqual(1_990);
}

/**
 * Nothing the user holds moved: the focused row, the row under the pointer,
 * scroll positions, the selection, the focused element and any draft, sampled
 * every frame. No layout shift moved a list row either. A changed row's own
 * detail may reflow, as its peek does when a new title wraps differently;
 * those shifts are logged, not failed.
 */
function expectHeldStill(probe: Probed): void {
  expect(probe.held.map((sample) => sample.state), "what the user holds").toHaveLength(1);
  expect((JSON.parse(probe.held[0]!.state) as { pointer: unknown }).pointer, "a row under the pointer").not.toBe("no row");
  expect(rowShifts(probe), "layout shifts that moved list rows").toEqual([]);
}

/**
 * Layout shifts that moved a list's rows: the rows, their groups (sections with
 * no class, unlike a detail's `section.sec`), or the whole list, which Chrome
 * names by its scroller alone (measured: a bar pushed in above it).
 */
function rowShifts(probe: Probed): Probed["shifts"] {
  return probe.shifts.filter((shift) => shift.moved.some((moved) => /^(a\.row|div\.row-line)\b|^section\.$|^div\.scroll$/.test(moved)));
}

/**
 * The positive control: an 80 ms task of the page's own, run from a timer, must
 * reach the probe. One run from here would be DevTools' task, which the Long
 * Tasks API does not count. It runs until the clock has moved 80 ms, so it is
 * 80 ms at any CPU rate.
 */
async function expectBusyTaskSeen(page: Page, rate: string): Promise<void> {
  const before = (await probed(page)).longTasks.length;
  await page.evaluate(
    () =>
      new Promise<void>((done) =>
        setTimeout(() => {
          const end = performance.now() + 80;
          while (performance.now() < end);
          requestAnimationFrame(() => setTimeout(done, 0));
        }, 0),
      ),
  );
  const seen = (await probed(page)).longTasks.slice(before);
  expect(seen.filter((task) => task.ms >= 80), `the probe sees an 80 ms task of the page's own, ${rate}`).toHaveLength(1);
}

function expectNoLongTasks(probe: Awaited<ReturnType<typeof probed>>): void {
  expect(probe.longTasks, "main-thread tasks over 50 ms").toEqual([]);
}
