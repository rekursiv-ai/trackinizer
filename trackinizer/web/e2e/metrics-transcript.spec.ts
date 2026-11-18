import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test, streamOpened } from "./fixtures";
import { expectControlSeen, longTasks, watchLongTasks } from "./longTasks";

// An Experiment's metrics and an AgentSession's transcript on the detail page,
// written here through the same routes a run and `trax run` use. The part -1
// backfill cannot be written through the API (only migration 020 made one), so
// its listing and reading are covered by the screen tests.

/** Create one inquiry of `kind`; return its id and seq. */
async function create(request: APIRequestContext, kind: string, title: string): Promise<{ id: string; seq: number }> {
  const response = await request.post(`/api/inquiries/${kind.toLowerCase()}`, {
    data: { title, idempotency_key: crypto.randomUUID() },
  });
  expect(response.ok(), await response.text()).toBe(true);
  const { id } = await response.json();
  const { self } = await (await request.get(`/api/web/get/${id}`)).json();
  return { id, seq: self.seq };
}

/** Log `count` points of `key` to an Experiment, with `value(step)`. */
async function logMetric(request: APIRequestContext, id: string, key: string, count: number, value: (step: number) => number) {
  const points = Array.from({ length: count }, (_, step) => ({ key, step, value: value(step) }));
  const response = await request.post(`/api/experiments/${id}/metrics`, { data: { points } });
  expect(response.ok(), await response.text()).toBe(true);
}

/**
 * Start a session and append one file of `count` records to it per entry of
 * `files`, each made by `make`; return its id and seq.
 */
async function session(
  request: APIRequestContext,
  title: string,
  files: readonly [string, number][],
  make: (name: string, idx: number) => object = record,
): Promise<{ id: string; seq: number }> {
  const started = await request.post("/api/sessions/start", { data: { cli: "claude", title } });
  expect(started.ok(), await started.text()).toBe(true);
  const { id } = await started.json();
  for (const [name, count] of files) await append(request, id, name, crypto.randomUUID(), 0, count, make);
  // `seq` on the start response is vestigial; the row's own seq names its link.
  const { self } = await (await request.get(`/api/web/get/${id}`)).json();
  return { id, seq: self.seq };
}

/** Append records `from` to `to - 1` of file `name`, as `trax run` does. */
async function append(
  request: APIRequestContext,
  id: string,
  name: string,
  irId: string,
  from: number,
  to: number,
  make: (name: string, idx: number) => object = record,
) {
  const manifest = { name, metadata: {}, ir_id: irId, format: "claude", records: to };
  for (let start = from; start < to; start += 1000) {
    const records = Array.from({ length: Math.min(1000, to - start) }, (_, k) => make(name, start + k));
    const appended = await request.post(`/api/sessions/${id}/records`, { data: { name, manifest, records } });
    expect(appended.ok(), await appended.text()).toBe(true);
  }
}

/** Record `idx` of file `name`: a user turn every tenth, a tool call after it, bookkeeping otherwise. */
function record(name: string, idx: number) {
  if (idx % 10 === 0) return { idx, kind: "UserMessage", payload: { content: `${name} turn ${idx}` }, text: `${name} turn ${idx}` };
  if (idx % 10 === 1) return { idx, kind: "ToolCall", payload: { name: "Bash", arguments: { command: `echo ${idx}` } } };
  return { idx, kind: "TokenUsage", payload: { info: { input_tokens: idx } } };
}

/**
 * Record `idx` shaped as a production claude session's are: a user turn, reading,
 * a shell call, its 45 lines of output, and 1,500 characters of Markdown with a
 * table and code (the replace-v1 perf seed, `seed_transcript.py`).
 */
function heavy(_name: string, idx: number) {
  switch (idx % 5) {
    case 0:
      return { idx, kind: "UserMessage", payload: { content: `Step ${idx}: check Issue#${idx} and report.` } };
    case 1:
      return { idx, kind: "Thinking", payload: { content: "Reasoning ".repeat(80) } };
    case 2:
      return { idx, kind: "ToolCall", payload: { call_id: `c${idx}`, name: "Bash", arguments: { command: `trax issue ${idx} && rg -n x loop/ | head -50` } } };
    case 3:
      return { idx, kind: "ShellCommandResult", payload: { call_id: `c${idx - 1}`, stdout: "loop/x.py:12: some matched line of output text\n".repeat(45), stderr: "", exit_code: 0 } };
    default: {
      const block =
        `## Progress\n\nMeasured **Issue#${idx}** and Belief#357: the [plan](https://example.com/plan) says why.\n\n` +
        "- First point, with `inline code`.\n- Second point: 0.19 to 0.23 s per check.\n\n" +
        "| Measurement | Before | After |\n|---|---|---|\n| list | 0.63 s | 0.16 s |\n\n" +
        '```python\nrows = client.list_kind("Issue", limit=50)\n```\n\n';
      return { idx, kind: "AssistantMessage", payload: { content: block.repeat(Math.ceil(1500 / block.length)).slice(0, 1500) } };
    }
  }
}

const turns = (page: Page, part: number) => page.locator(`[data-part="${part}"] .turn`);

test("an Experiment's metrics show as sparklines, and a full page says it is truncated", async ({ page, request }) => {
  const small = await create(request, "Experiment", "E2E metrics");
  await logMetric(request, small.id, "loss", 40, (s) => 2 - s / 40);
  await logMetric(request, small.id, "ece", 5, () => 0.03);
  await page.goto(`/app/#/ref/Experiment/${small.seq}`);
  const metrics = page.getByRole("region", { name: /^Metrics/ });
  await expect(metrics.getByRole("heading", { level: 2 })).toHaveText("Metrics 2");
  await expect(metrics.locator('[data-metric="loss"] .v')).toHaveText("1.02");
  await expect(metrics.locator('[data-metric="loss"] .r')).toHaveText("40 points · steps 0–39 · range 1.02–2.00");
  await expect(metrics.locator('[data-metric="loss"] svg path').nth(1)).toHaveAttribute("d", /^M2\.0 4\.0L/);
  await expect(metrics.getByRole("note")).toHaveCount(0);

  const big = await create(request, "Experiment", "E2E metrics over the cap");
  await logMetric(request, big.id, "a_first", 700, (s) => s);
  await logMetric(request, big.id, "b_second", 400, (s) => -s);
  await page.goto(`/app/#/ref/Experiment/${big.seq}`);
  await expect(metrics.getByRole("note")).toContainText("Later points of b_second and any metric after it are not shown.");
  await expect(metrics.locator('[data-metric="b_second"] .r')).toHaveText("300 points · steps 0–299 · range -299–0 · partial");
});

test("a transcript shows every part in full, past 200 records, its bookkeeping on request, with raw records", async ({ page }) => {
  const { seq } = await session(page.request, "E2E transcript", [
    ["main.jsonl", 250],
    ["compacted.jsonl", 12],
  ]);
  await page.goto(`/app/#/ref/AgentSession/${seq}`);
  const transcript = page.getByRole("region", { name: /^Transcript/ });
  await expect(transcript.getByRole("heading", { level: 2 })).toHaveText("Transcript 262 records");
  // A turn and a tool call in every ten records; the token usage between hides until asked.
  await expect(turns(page, 0)).toHaveCount(50);
  await expect(turns(page, 1)).toHaveCount(4);
  await expect(page.locator(".tr-part-h")).toHaveText([
    "Part 0 · main.jsonl · claude · 250 records",
    "Part 1 · compacted.jsonl · claude · 12 records",
  ]);
  const last = page.locator('[data-part="0"] [data-idx="240"]');
  await expect(last).toContainText("main.jsonl turn 240");
  await expect(page.locator('[data-part="0"] [data-idx="241"] .tool-row')).toHaveText(/^Ran\s*echo 241$/);

  await last.getByRole("button", { name: "Raw" }).click();
  await expect(last.locator(".turn-json")).toContainText('"kind": "UserMessage"');
  await page.locator('[data-part="0"]').getByRole("button", { name: "Show 200 bookkeeping records" }).click();
  await expect(turns(page, 0)).toHaveCount(250);
});

test("a long part shows its newest 1,000 records, and Load earlier reads the rest", async ({ page }) => {
  const { seq } = await session(page.request, "E2E long transcript", [["long.jsonl", 1050]]);
  await page.goto(`/app/#/ref/AgentSession/${seq}`);
  await expect(page.locator(".tr-more")).toHaveText("Showing 1,000 of 1,050 records.Load earlier");
  // Records 50 to 1,049: a turn and a tool call in every ten.
  await expect(turns(page, 0)).toHaveCount(200);
  await expect(page.locator('[data-part="0"] [data-idx="1040"]')).toContainText("long.jsonl turn 1040");
  await expect(page.locator('[data-part="0"] [data-idx="40"]')).toHaveCount(0);
  await page.locator(".tr-more").getByRole("button", { name: "Load earlier" }).click();
  await expect(turns(page, 0)).toHaveCount(210);
  await expect(page.locator('[data-part="0"] [data-idx="40"]')).toContainText("long.jsonl turn 40");
  await expect(page.locator(".tr-more")).toHaveCount(0);
});

test("a record appended to a live session shows with one read of what follows the last", async ({ page }) => {
  const { id, seq } = await session(page.request, "E2E live transcript", [["live.jsonl", 250]]);
  const { parts } = await (await page.request.get(`/api/sessions/${id}/parts`)).json();
  const reads: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === `/api/sessions/${id}/records`) reads.push(url.search);
  });
  const subscribed = streamOpened(page);
  await page.goto(`/app/#/ref/AgentSession/${seq}`);
  await expect(turns(page, 0)).toHaveCount(50);
  await subscribed;
  reads.length = 0;
  await append(page.request, id, "live.jsonl", parts[0].ir_id, 250, 251);
  await expect(page.locator('[data-part="0"] [data-idx="250"]')).toContainText("live.jsonl turn 250");
  expect(reads).toEqual(["?part=0&after_idx=249&limit=1&plaintext_only=true"]);
});

test("a 1,000-record transcript draws with no main-thread task over 50 ms, with the CPU slowed 4x", async ({ context }) => {
  test.setTimeout(120_000);
  const { seq } = await session(context.request, "E2E heavy transcript", [["heavy.jsonl", 1000]], heavy);
  // As in lists.spec.ts: other spec files load the same server, so load three
  // times, log every sample, and hold each to the budget. Each load is a new page,
  // since the slowdown holds from a page's first navigation.
  let page: Page | null = null;
  const samples = [];
  for (let attempt = 0; attempt < 3; attempt++) {
    await page?.close();
    page = await context.newPage();
    await watchLongTasks(page);
    await (await context.newCDPSession(page)).send("Emulation.setCPUThrottlingRate", { rate: 4 });
    await page.goto(`/app/?load=${attempt}#/ref/AgentSession/${seq}`);
    // Each shell result shows inside the call it answers: 800 lines, record 0 last drawn.
    await expect(turns(page, 0)).toHaveCount(800, { timeout: 30_000 });
    await expect(page.locator('[data-part="0"] [data-idx="0"]')).toBeVisible();
    const timing = await page.evaluate(() => {
      const reads = performance.getEntriesByType("resource").filter((entry) => entry.name.includes("/records?"));
      return { drawnMs: performance.now(), readStart: Math.min(...reads.map((read) => read.startTime)) };
    });
    const tasks = await longTasks(page);
    console.log(`transcript at 4x: 1,000 drawn at ${timing.drawnMs.toFixed(0)} ms, long tasks ${JSON.stringify(tasks)}`);
    samples.push({ ...timing, tasks });
  }
  // Evaluating the bundle and drawing the rest of the detail come before the
  // records are read, and are not the transcript's to answer for.
  for (const { tasks, readStart } of samples) {
    expect(tasks.filter((task) => task.start + task.ms > readStart)).toEqual([]);
  }
  await expectControlSeen(page!);
});
