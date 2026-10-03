import { QueryClient } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";
import type { SessionPart, SessionRecord } from "../../api/sessions";
import { type Sent, stubFetch } from "../../api/testing";
import type { DetailRow } from "../../api/detail";
import { stubClipboard } from "../../debug/testing";
import { detailQueries } from "../queries";
import { detail, PROFILE, renderDetail, row } from "../testing";
import { transcriptQueries } from ".";

const SESSION = row("AgentSession", 3);

/** A part's listing entry. */
function part(number: number, records: number): SessionPart {
  return { part: number, name: number === -1 ? "legacy" : `p${number}.jsonl`, format: number === -1 ? "" : "claude", records, metadata: {}, ir_id: null };
}

/**
 * Record `idx` of a part: a tool's output, or an unparsed line for most, since
 * each rendered message costs jsdom about a millisecond and bookkeeping hides.
 */
function record(idx: number, text = `Turn ${idx}`): SessionRecord {
  const kind = idx % 50 ? "IncompleteRecord" : "ShellCommandResult";
  return { idx, kind, context_id: null, timestamp: null, model: null, payload: { content: text }, text, ciphertext: null };
}

/**
 * Serve the session's detail, `parts` as its listing, and each part's records
 * as the server pages them: after `after_idx`, up to `limit`, each made by
 * `make`. `fail` answers for a request first when it returns a response.
 */
function serve(
  parts: readonly SessionPart[],
  fail: (query: URLSearchParams) => Response | null = () => null,
  make: (idx: number) => SessionRecord = record,
): Sent[] {
  return stubFetch((request) => {
    const url = new URL(request.url);
    const failure = fail(url.searchParams);
    if (failure) return failure;
    if (url.pathname === `/api/web/get/${SESSION.id}`) return Response.json(detail(SESSION));
    if (url.pathname === `/api/sessions/${SESSION.id}/parts`) return Response.json({ parts });
    if (url.pathname === `/api/sessions/${SESSION.id}/records`) {
      const query = url.searchParams;
      const listed = parts.find((p) => p.part === Number(query.get("part")))!;
      const after = Number(query.get("after_idx"));
      const count = Math.max(0, Math.min(Number(query.get("limit")), listed.records - after - 1));
      return Response.json({ part: listed.part, records: Array.from({ length: count }, (_, k) => make(after + 1 + k)) });
    }
    return Response.json({ detail: "not found" }, { status: 404 });
  });
}

/** The records each request asked for, as `part:after_idx:limit`. */
function pages(sent: readonly Sent[]): string[] {
  return sent
    .filter((request) => request.path.endsWith("/records"))
    .map((request) => {
      const query = new URLSearchParams(request.query);
      return `${query.get("part")}:${query.get("after_idx")}:${query.get("limit")}`;
    });
}

const shown = (part: number) => document.querySelectorAll(`[data-part="${part}"] .turn`).length;

// The highlighter is a chunk of its own; loaded here, its first evaluation is not
// counted against a test's 100 ms.
beforeAll(() => import("../../markdown/highlight"));

beforeEach(() => {
  history.replaceState(null, "", "#/ref/AgentSession/3");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("every part shows in full, part -1 included, its pages read at once", async () => {
  // Bookkeeping for most records, which hides, and so costs jsdom no render.
  const sent = serve([part(-1, 3), part(0, 250)], undefined, (idx) => ({ ...record(idx), kind: idx % 50 ? "TokenUsage" : "IncompleteRecord" }));
  renderDetail({ id: SESSION.id });
  await waitFor(() => expect(shown(0)).toBe(5));
  expect(shown(-1)).toBe(1);
  expect([...document.querySelectorAll(".tr-noise")].map((toggle) => toggle.textContent)).toEqual([
    "Show 2 bookkeeping records",
    "Show 245 bookkeeping records",
  ]);
  expect(pages(sent).toSorted()).toEqual(["-1:-1:3", "0:-1:200", "0:199:50"]);
  // Selectors, not role queries: jsdom computes 250 rows' accessible names slowly.
  expect(document.querySelector('[data-section="transcript"] h2')!.textContent).toBe("Transcript 253 records");
  expect([...document.querySelectorAll(".tr-part-h")].map((h) => h.textContent)).toEqual([
    "Legacy turns, backfilled · 3 records",
    "Part 0 · p0.jsonl · claude · 250 records",
  ]);
  const last = document.querySelector<HTMLElement>('[data-part="0"] [data-idx="200"]')!;
  expect(within(last).getByText("Turn 200")).toBeTruthy();
  expect(document.querySelector('[role="status"]')).toBeNull();
});

test("a record's raw JSON shows on request; a tool step's, its call's and its result's, once the step is open", async () => {
  serve([part(0, 2)]);
  renderDetail({ id: SESSION.id });
  await waitFor(() => expect(shown(0)).toBe(2));
  expect(document.querySelector(".tr-part-h")).toBeNull();
  const plain = document.querySelector<HTMLElement>('[data-idx="1"]')!;
  const raw = within(plain).getByRole("button", { name: "Raw" });
  expect(plain.querySelector(".turn-json")).toBeNull();
  fireEvent.click(raw);
  expect(raw.getAttribute("aria-expanded")).toBe("true");
  expect(JSON.parse(plain.querySelector(".turn-json")!.textContent!)).toMatchObject({ idx: 1, kind: "IncompleteRecord", text: "Turn 1" });
  const step = document.querySelector<HTMLElement>('[data-idx="0"]')!;
  expect(within(step).queryByRole("button", { name: "Raw" })).toBeNull();
  fireEvent.click(step.querySelector("summary")!);
  fireEvent.click(await within(step).findByRole("button", { name: "Raw" }));
  expect(JSON.parse(step.querySelector(".turn-json")!.textContent!)).toMatchObject([{ idx: 0, kind: "ShellCommandResult", text: "Turn 0" }]);
});
/** The part's alert, when it shows one. */
const alert = () => document.querySelector('[data-part="0"] [role="alert"]');

test("a read that fails shows the server's message, and Retry reads it again", async () => {
  let refuse = true;
  // Bookkeeping for most records, which hides, and so costs jsdom no render.
  const sent = serve(
    [part(0, 210)],
    (query) => (refuse && query.get("after_idx") === "199" ? Response.json({ detail: "statement timeout" }, { status: 500 }) : null),
    (idx) => ({ ...record(idx), kind: idx % 50 ? "TokenUsage" : "IncompleteRecord" }),
  );
  renderDetail({ id: SESSION.id });
  await waitFor(() => expect(alert()?.textContent).toBe("statement timeoutRetry"));
  expect(shown(0)).toBe(0);
  refuse = false;
  fireEvent.click(alert()!.querySelector("button")!);
  await waitFor(() => expect(shown(0)).toBe(5));
  expect(document.querySelector('[data-part="0"] .tr-noise')!.textContent).toBe("Show 205 bookkeeping records");
  expect(pages(sent).toSorted()).toEqual(["0:-1:200", "0:-1:200", "0:199:10", "0:199:10"]);
});

test("a part reads its newest 1,000 records, the 1,000 before on request, and later ones up to its listed count (DRV-03)", () => {
  const options = (records: number) => transcriptQueries.records(SESSION.id, part(0, records));
  expect(options(250).initialPageParam).toEqual({ after: -1, count: 250 });
  expect(options(3000).initialPageParam).toEqual({ after: 1999, count: 1000 });
  const earlier = (after: number, count: number) => options(3000).getPreviousPageParam!([], [[]], { after, count }, [{ after, count }]);
  expect(earlier(1999, 1000)).toEqual({ after: 999, count: 1000 });
  expect(earlier(204, 1000)).toEqual({ after: -1, count: 205 });
  expect(earlier(-1, 205)).toBeUndefined();
  const later = (records: number, read: readonly SessionRecord[]) =>
    options(records).getNextPageParam([...read], [[...read]], { after: 1999, count: 1000 }, [{ after: 1999, count: 1000 }]);
  // Read up to the listed count, from the last record read.
  expect(later(3000, [record(2999)])).toBeUndefined();
  expect(later(3001, [record(2999)])).toEqual({ after: 2999, count: 1 });
  expect(later(9000, [record(2999)])).toEqual({ after: 2999, count: 1000 });
  // A read the server answered short of its range goes on from what it did answer.
  expect(later(3000, [record(2500)])).toEqual({ after: 2500, count: 499 });
  expect(later(3000, [])).toEqual({ after: 1999, count: 1000 });
});

test("a read that lands before the records its listing counts reads the rest on the next listing, once each (CR-03)", async () => {
  // The server lists a part's count before it writes the records (`session_ir_routes.py`).
  let written = 2;
  const sent = serve([part(0, 3)], (query) => {
    if (!query.has("part")) return null;
    const after = Number(query.get("after_idx"));
    const end = Math.min(written - 1, after + Number(query.get("limit")));
    return Response.json({ part: 0, records: Array.from({ length: Math.max(0, end - after) }, (_, k) => record(after + 1 + k)) });
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderDetail({ id: SESSION.id }, client);
  await waitFor(() => expect(shown(0)).toBe(2));
  // What the stream layer does when the session's id arrives (src/live/detail.ts).
  const listing = () => client.refetchQueries({ queryKey: transcriptQueries.parts(SESSION.id).queryKey });
  await listing();
  // A read sent changes nothing on screen, so only the interval checks again.
  await waitFor(() => expect(pages(sent)).toEqual(["0:-1:3", "0:1:1"]), { interval: 1 });
  written = 3;
  await listing();
  await waitFor(() => expect(shown(0)).toBe(3));
  expect(within(at(2)).getByText("Turn 2")).toBeTruthy();
  expect(pages(sent)).toEqual(["0:-1:3", "0:1:1", "0:1:1"]);
});

test("a long part shows its newest 1,000 records, read in parallel pages, and Load earlier reads the rest (B3)", async () => {
  const sent = serve([part(0, 1205)]);
  renderDetail({ id: SESSION.id });
  const more = await waitFor(() => {
    const line = document.querySelector<HTMLElement>('[data-part="0"] .tr-more');
    expect(line).not.toBeNull();
    return line!;
  });
  expect(more.textContent).toBe("Showing 1,000 of 1,205 records.Load earlier");
  expect(pages(sent).toSorted()).toEqual(["0:1004:200", "0:204:200", "0:404:200", "0:604:200", "0:804:200"]);
  expect(document.querySelector('[data-idx="1204"]')).not.toBeNull();
  fireEvent.click(within(more).getByRole("button", { name: "Load earlier" }));
  await waitFor(() => expect(document.querySelector('[data-part="0"] .tr-more')).toBeNull());
  expect(pages(sent).slice(5).toSorted()).toEqual(["0:-1:200", "0:199:5"]);
});

test("a live append reads only what follows the last record, a failed one keeps the rest, and a restart reads afresh (B4)", async () => {
  const parts = [part(0, 2)];
  let refuse = false;
  const sent = serve(parts, (query) =>
    refuse && query.has("part") ? Response.json({ detail: "statement timeout" }, { status: 500 }) : null,
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderDetail({ id: SESSION.id }, client);
  await waitFor(() => expect(shown(0)).toBe(2));
  // What the stream layer does when the session's id arrives (src/live/detail.ts).
  const append = async (records: number) => {
    parts[0] = part(0, records);
    sent.length = 0;
    await client.refetchQueries({ queryKey: transcriptQueries.parts(SESSION.id).queryKey });
  };
  const asked = () => sent.map((request) => (request.path.endsWith("/parts") ? "parts" : pages([request])[0]));
  refuse = true;
  await append(3);
  await waitFor(() => expect(alert()?.textContent).toBe("statement timeoutRetry"));
  expect(shown(0)).toBe(2);
  refuse = false;
  fireEvent.click(alert()!.querySelector("button")!);
  await waitFor(() => expect(shown(0)).toBe(3));
  expect(asked()).toEqual(["parts", "0:1:1", "0:1:1"]);
  await append(4);
  await waitFor(() => expect(shown(0)).toBe(4));
  expect(asked()).toEqual(["parts", "0:2:1"]);
  // Fewer records than were read: the capture restarted.
  await append(1);
  await waitFor(() => expect(shown(0)).toBe(1));
  expect(asked()).toEqual(["parts", "0:-1:1"]);
});

test("a part draws its newest records first, then 100 more per background render, so no one render holds the page", async () => {
  const drawn = () => [...document.querySelectorAll<HTMLElement>('[data-part="0"] .turn')].map((line) => Number(line.dataset.idx));
  // What each commit drew: an observer runs after every commit, before any later task.
  const draws: number[][] = [];
  const observer = new MutationObserver(() => {
    const now = drawn();
    if (now.length !== (draws.at(-1)?.length ?? 0)) draws.push(now);
  });
  observer.observe(document.body, { childList: true, subtree: true });
  // Model switches, each a one-line note that costs jsdom little; the first switches nothing, and hides.
  serve([part(0, 110)], undefined, (idx) => ({ ...record(idx), kind: "TurnContext", payload: { model: idx % 2 ? "a" : "b" } }));
  renderDetail({ id: SESSION.id });
  await waitFor(() => expect(drawn()).toHaveLength(109));
  observer.disconnect();
  expect(draws[0]).toEqual([107, 108, 109]);
  expect(draws.map((idx) => idx.length)).toEqual([3, 103, 109]);
});

/** The transcript section's header line. */
const header = () => within(document.querySelector<HTMLElement>('[data-section="transcript"] .sec-h')!);

test("a refresh that fails keeps the records, says so in the header, and Retry reads them again (READ-01, DRV-02)", async () => {
  let refuse: string | null = null;
  const sent = serve([part(0, 2)], (query) =>
    refuse !== null && (refuse === "records" ? query.has("part") : !query.has("part"))
      ? Response.json({ detail: "statement timeout" }, { status: 500 })
      : null,
  );
  renderDetail({ id: SESSION.id });
  await waitFor(() => expect(shown(0)).toBe(2));
  for (const failing of ["records", "parts"]) {
    refuse = failing;
    fireEvent.click(header().getByRole("button", { name: "Refresh" }));
    expect((await header().findByRole("alert")).textContent).toBe("Could not refresh: statement timeoutRetry");
    expect(shown(0)).toBe(2);
    refuse = null;
    const asked = pages(sent).length;
    fireEvent.click(header().getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(header().queryByRole("alert")).toBeNull());
    expect(pages(sent).length).toBeGreaterThan(asked);
    expect(shown(0)).toBe(2);
  }
});

test("a session with no parts says so; other kinds have no transcript", async () => {
  serve([]);
  renderDetail({ id: SESSION.id });
  expect(await screen.findByText("No records captured yet.")).toBeTruthy();
  cleanup();
  const issue = row("Issue", 8);
  const sent = stubFetch((request) =>
    new URL(request.url).pathname === `/api/web/get/${issue.id}` ? Response.json(detail(issue)) : Response.json({}, { status: 404 }),
  );
  renderDetail({ id: issue.id });
  await screen.findByRole("heading", { level: 1 });
  expect(screen.queryByRole("region", { name: /^Transcript/ })).toBeNull();
  expect(sent.map((request) => request.path)).toEqual([`/api/web/get/${issue.id}`]);
});

/**
 * Record `idx` as the server sends one: `payload` in its dataclass codec's
 * shape, tuples as `py/tuple`, and `text` its search projection. The shapes are
 * a seeded claude session's (replace-v1 `sessions/zoo_part0.json`).
 */
function captured(idx: number, kind: string, payload: { [field: string]: unknown }, text = ""): SessionRecord {
  return {
    idx,
    kind,
    context_id: null,
    timestamp: "2026-10-01T10:00:00Z",
    model: "claude-opus-5-5",
    payload: { "py/object": `trackinizer.lib.agent.types.sessions.${kind}`, ...payload },
    text,
    ciphertext: null,
  };
}

/** Record `idx`'s line in part 0. */
const at = (idx: number) => document.querySelector<HTMLElement>(`[data-part="0"] [data-idx="${idx}"]`)!;

/** The text of each line of a code block or diff (`.ln`), in order. */
const rowsOf = (code: HTMLElement) => [...code.querySelectorAll(".ln")].map((line) => line.textContent);

/** What `selector` finds in the open body of the step in `line`, once it shows. */
const opened = (line: HTMLElement, selector: string) =>
  waitFor(() => {
    const found = line.querySelector<HTMLElement>(`.tr-step-body ${selector}`);
    expect(found).not.toBeNull();
    return found!;
  });

/** Wait until the highlighter has coloured something under `element`. */
const coloured = (element: HTMLElement) =>
  waitFor(() => expect(element.querySelector("[class^=hljs-]")).not.toBeNull(), { interval: 5 });

/** The idx of each line part 0 shows, in order. */
const shownIdx = () => [...document.querySelectorAll<HTMLElement>('[data-part="0"] .turn')].map((line) => line.dataset.idx);

/** Show part 0's bookkeeping. */
const showBookkeeping = () => fireEvent.click(document.querySelector('[data-part="0"] .tr-noise')!);

/**
 * Render the session with `records` as its one part; record `idx`'s line, once it
 * shows. A part draws its newest records first, so a test that reads older ones
 * names the oldest it shows.
 */
async function renderRecords(records: readonly SessionRecord[], idx = 0): Promise<HTMLElement> {
  serve([part(0, records.length)], undefined, (k) => records[k]!);
  renderDetail({ id: SESSION.id });
  return waitFor(() => {
    const line = document.querySelector<HTMLElement>(`[data-part="0"] [data-idx="${idx}"]`);
    expect(line).not.toBeNull();
    return line!;
  });
}

test("an agent's Markdown image shows as its text, so the browser fetches nothing (B1)", async () => {
  const content = "Summary chart: ![chart](https://example.com/track.png?session=SECRET)\n\nSee [the docs](https://docs.python.org/3/).";
  const line = await renderRecords([captured(0, "AssistantMessage", { content, attachments: { "py/tuple": [] } }, content)]);
  expect(document.querySelector("img")).toBeNull();
  expect(line.querySelector(".turn-body")!.textContent).toContain("chart (https://example.com/track.png?session=SECRET)");
});

test("a cleared context shows the summary it carries, its system prompt folded, and the session it continues (B2)", async () => {
  const prompt = "You are Claude Code.\n\n# Tools\nUse the Read tool to read files.";
  const line = await renderRecords([
    captured(0, "ContextClear", {
      cleared_session_id: "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
      system_prompt: prompt,
      summary: "## Summary\n\nWe swapped `time.time()` for `time.monotonic()`.",
      history: { "py/tuple": [] },
      extra: {},
    }),
  ]);
  expect(line.querySelector(".turn-h b")!.textContent).toBe("Context cleared");
  expect(line.querySelector(".turn-body h4")!.textContent).toBe("Summary");
  expect(line.querySelector(".turn-body")!.textContent).toContain("We swapped time.time() for time.monotonic().");
  const folded = line.querySelector("details")!;
  expect(folded.open).toBe(false);
  expect(folded.querySelector("summary")!.textContent).toBe(`System prompt · ${prompt.length} characters`);
  expect(folded.querySelector("pre")!.textContent).toBe(prompt);
  expect(line.textContent).toContain("Continues session 0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b");
});

test("bookkeeping, Claude Code's own lines included, hides behind one toggle; a model change shows (B5)", async () => {
  const claude = (idx: number, type: string, fields: { [field: string]: unknown }) =>
    captured(idx, "UncategorizedRecord", { kind: type, payload: { type, sessionId: "1", ...fields } });
  await renderRecords(
    [
      captured(0, "TurnContext", { model: "claude-opus-5-5", extra: { cwd: "/work/loop" } }),
      captured(1, "UserMessage", { content: "Fix the flake." }, "Fix the flake."),
      captured(2, "TokenUsage", { info: { input_tokens: 12000 } }),
      captured(3, "ContextState", { kind: "environment_context", content: "<cwd>/work/loop</cwd>" }),
      captured(4, "SystemMessage", { subtype: "developer", content: "You are Codex." }),
      claude(5, "last-prompt", { lastPrompt: "Fix the flake." }),
      claude(6, "mode", { mode: "normal" }),
      claude(7, "permission-mode", { permissionMode: "bypassPermissions" }),
      claude(8, "file-history-snapshot", { messageId: "c", snapshot: { trackedFileBackups: {} } }),
      claude(9, "ai-title", { aiTitle: "Fix the flake" }),
      claude(10, "atis-latch", { atis: "" }),
      captured(11, "UncategorizedRecord", { kind: "event_msg/task_started", payload: { type: "task_started" } }),
      // The server names on each record the model of the last TurnContext.
      { ...captured(12, "TurnContext", { model: "claude-sonnet-5" }), model: "claude-sonnet-5" },
      { ...captured(13, "AssistantMessage", { content: "Fixed." }, "Fixed."), model: "claude-sonnet-5" },
    ],
    1,
  );
  const transcript = document.querySelector<HTMLElement>('[data-part="0"]')!;
  const shownIdx = () => [...transcript.querySelectorAll<HTMLElement>(".turn")].map((line) => line.dataset.idx);
  expect(shownIdx()).toEqual(["1", "12", "13"]);
  expect(transcript.querySelector('[data-idx="12"] .turn-h b')!.textContent).toBe("Model: claude-sonnet-5");
  // The note names the model; its header does not again (B21).
  expect(transcript.querySelector('[data-idx="12"] .turn-model')).toBeNull();
  expect(transcript.textContent).not.toContain("lastPrompt");
  const toggle = within(transcript).getByRole("button", { name: "Show 11 bookkeeping records" });
  fireEvent.click(toggle);
  expect(shownIdx()).toHaveLength(14);
  expect(toggle.textContent).toBe("Hide 11 bookkeeping records");
});

test("a tool call is one line, what it did and to what; its arguments show once opened, code in its language (B6)", async () => {
  const command = "cd /work/loop && \\\n  uv --quiet run --frozen pytest loop/foo_test.py -x -q";
  const before = "    old_line_0 = compute(0)\n    old_line_1 = compute(1)";
  await renderRecords(
    [
      captured(0, "ToolCall", { call_id: "t1", name: "Bash", arguments: { command, description: "Run the test file", timeout: 600000 } }),
      captured(1, "AssistantMessage", { content: "Then:" }, "Then:"),
      captured(2, "ToolCall", {
        call_id: "t10",
        name: "Edit",
        arguments: { file_path: "/work/loop/loop/big.py", old_string: before, new_string: "    new_line_0 = compute_v2(0)" },
      }),
    ],
    0,
  );
  const bash = at(0);
  expect(bash.querySelector(".tool-row .name")!.textContent).toBe("Ran");
  expect(bash.querySelector(".tool-arg")!.textContent).toBe("cd /work/loop && \\ …");
  expect(bash.querySelector(".tr-outcome")).toBeNull();
  expect(bash.querySelector("dl")).toBeNull();
  fireEvent.click(bash.querySelector("summary")!);
  const rest = await opened(bash, "dl");
  expect([...rest.querySelectorAll("dt")].map((term) => term.textContent)).toEqual(["description", "timeout"]);
  expect([...rest.querySelectorAll("dd")].map((value) => value.textContent)).toEqual(["Run the test file", "600000"]);
  // A command over one line shows whole, coloured as bash.
  const script = bash.querySelector<HTMLElement>(".tr-code")!;
  expect(rowsOf(script)).toEqual(command.split("\n"));
  await coloured(script);

  const edit = at(2);
  expect(edit.querySelector(".tool-row .name")!.textContent).toBe("Edited");
  expect(edit.querySelector(".tool-arg")!.textContent).toBe("big.py");
  expect(edit.querySelector(".tool-dir")!.textContent).toBe("/work/loop/loop");
  fireEvent.click(edit.querySelector("summary")!);
  // Code over a line in the file's language, Python; a line of it inline.
  const code = await opened(edit, ".tr-code");
  expect(rowsOf(code)).toEqual(before.split("\n"));
  await coloured(code);
  expect([...edit.querySelectorAll("dt")].map((term) => term.textContent)).toEqual(["old_string", "new_string"]);
  expect(edit.querySelectorAll("dd")[1]!.textContent).toBe("    new_line_0 = compute_v2(0)");
});
test("long output keeps its head and its tail, where failures are, and shows the rest in place (B7)", async () => {
  const lines = Array.from({ length: 299 }, (_, k) => `loop/foo_test.py::test_${k} PASSED`);
  const stdout = [...lines, "FAILED loop/foo_test.py::test_elapsed - AssertionError"].join("\n");
  const shell = (idx: number, exit: number) =>
    captured(idx, "ShellCommandResult", { call_id: `t${idx}`, command: { "py/tuple": ["pytest"] }, stdout, stderr: "", exit_code: exit }, `pytest\n${stdout}`);
  await renderRecords([shell(0, 1), captured(1, "AssistantMessage", { content: "And:" }, "And:"), shell(2, 0)]);
  // A failure starts open.
  const failed = at(0);
  const output = () => failed.querySelector(".tool-out")!.textContent!;
  expect(output()).toContain("FAILED loop/foo_test.py::test_elapsed - AssertionError");
  expect(output()).toContain("test_19 PASSED");
  expect(output()).not.toContain("test_20 PASSED");
  fireEvent.click(within(failed).getByRole("button", { name: "Show 240 more lines" }));
  expect(output()).toBe(stdout);
  // A success shows its first three lines until opened, as the Claude and Codex CLIs do.
  const passed = at(2);
  expect(passed.querySelector("details")!.open).toBe(false);
  // A row a line, each cut at the edge, so a preview is three rows high.
  expect(rowsOf(passed.querySelector<HTMLElement>(".tr-preview .tool-out")!)).toEqual(lines.slice(0, 3));
  fireEvent.click(within(passed).getByRole("button", { name: "+297 lines" }));
  await waitFor(() => expect(passed.querySelector(".tr-preview")).toBeNull());
  expect(passed.querySelector("details")!.open).toBe(true);
  // The button went as the step opened, so the focus moves to its line.
  expect(document.activeElement).toBe(passed.querySelector("summary"));
  expect(within(passed).getByRole("button", { name: "Show 240 more lines" })).toBeTruthy();
});
test("a shell result shows its command and exit code, and stderr apart from stdout (B8)", async () => {
  const stdout = "1 failed, 411 passed in 9.12s";
  const stderr = "warning: `VIRTUAL_ENV=/old/venv` does not match the project environment path `.venv`";
  const line = await renderRecords([
    captured(
      0,
      "ShellCommandResult",
      {
        call_id: "t1",
        extra: {},
        command: { "py/tuple": ["bash", "-lc", "cd /work/loop && uv run pytest loop/foo_test.py -x -q"] },
        stdout,
        stderr,
        exit_code: 1,
      },
      `bash -lc cd /work/loop && uv run pytest loop/foo_test.py -x -q\n${stdout}\n${stderr}`,
    ),
  ]);
  // The script, without the shell that ran it, as Codex shows it.
  expect(line.querySelector(".tool-arg")!.textContent).toBe("cd /work/loop && uv run pytest loop/foo_test.py -x -q");
  expect(line.querySelector(".tr-outcome")!.textContent).toBe("exit 1");
  expect(line.querySelector(".tr-step")!.classList.contains("failed")).toBe(true);
  const [out, err] = line.querySelectorAll(".tool-out");
  expect(out!.textContent).toBe(stdout);
  expect(out!.classList.contains("failed")).toBe(false);
  expect(err!.textContent).toBe(stderr);
  expect(err!.classList.contains("failed")).toBe(true);
});
test("a file edit counts its lines added and removed, and shows each line numbered and coloured in its file's language (B9)", async () => {
  const splice = (before: string, after: string, lead: string | null = null, start: number | null = null) => ({
    "py/object": "trackinizer.lib.agent.types.sessions.Splice",
    before,
    after,
    lead,
    trail: null,
    start,
    count: start === null ? null : 1,
    bare: { "py/set": [] },
  });
  const edit = (idx: number, path: string, ...splices: object[]) =>
    captured(idx, "FileEditResult", { call_id: `t${idx}`, extra: {}, path, edits: { "py/tuple": splices } }, path);
  await renderRecords([
    edit(
      0,
      "/work/loop/loop/foo.py",
      splice("    return time.time() - _START\n", "    return time.monotonic() - _START\n"),
      splice("_START = time.time()\n", "_START = time.monotonic()\n"),
    ),
    captured(1, "AssistantMessage", { content: "And:" }, "And:"),
    // Codex's, with its own header and context.
    edit(2, "src/a.ts", splice("const a = 1;\n", "const a = 2;\n", "@@ -9,2 +9,2 @@\n // a\n", 10)),
  ]);
  const line = at(0);
  expect(line.querySelector(".tool-row .name")!.textContent).toBe("Edited");
  expect(line.querySelector(".tool-arg")!.textContent).toBe("foo.py");
  expect(line.querySelector(".tr-outcome")!.textContent).toBe("+2 −2");
  // Closed, its first three lines show.
  expect(rowsOf(line.querySelector<HTMLElement>(".tr-preview .tr-diff")!)).toHaveLength(3);
  fireEvent.click(line.querySelector("summary")!);
  const diff = await opened(line, ".tr-diff");
  expect(rowsOf(diff)).toEqual([
    "-    return time.time() - _START",
    "+    return time.monotonic() - _START",
    "-_START = time.time()",
    "+_START = time.monotonic()",
  ]);
  expect([diff.querySelectorAll(".d-del").length, diff.querySelectorAll(".d-add").length]).toEqual([2, 2]);
  // Claude gives no line numbers; Codex's header does.
  expect(diff.querySelector("[data-n]")).toBeNull();
  await coloured(diff);
  fireEvent.click(at(2).querySelector("summary")!);
  const numbered = await opened(at(2), ".tr-diff");
  expect([...numbered.querySelectorAll<HTMLElement>(".ln")].map((row) => [row.dataset.n ?? "", row.textContent])).toEqual([
    ["", "@@ -9,2 +9,2 @@"],
    ["9", " // a"],
    ["10", "-const a = 1;"],
    ["10", "+const a = 2;"],
  ]);
});
test("a canvas message shows what was said, its injected context folded and linked to its record (B12)", async () => {
  const context =
    '{"workspace_id":"1f3b8a52-6a4c-4a11-9d0f-0d2a1a7e9b10","record_id":"6d0f9b7e-1c2d-4f3a-8b9c-2e1d0c3b4a59",' +
    '"record":{"id":"6d0f9b7e-1c2d-4f3a-8b9c-2e1d0c3b4a59","kind":"Issue","seq":1,"title":"Web app preview: seeded root"},"artifact_content":null}';
  const commands = "Canvas commands: trax workspace 1f3b8a52-6a4c-4a11-9d0f-0d2a1a7e9b10";
  const content = `no-auth@localhost: What changed in Issue#1 since yesterday?\nTrackinizer context (verify with trax): ${context}\n${commands}`;
  const line = await renderRecords([captured(0, "UserMessage", { content, attachments: { "py/tuple": [] } }, content)]);
  expect(line.querySelector(".turn-body")!.textContent).toBe("no-auth@localhost: What changed in Issue#1 since yesterday?");
  const folded = line.querySelector("details")!;
  expect(folded.open).toBe(false);
  expect(folded.querySelector("summary")!.textContent).toBe("Canvas context · Issue#1 Web app preview: seeded root");
  expect(folded.querySelector("summary a")!.getAttribute("href")).toBe("#/lookup/6d0f9b7e-1c2d-4f3a-8b9c-2e1d0c3b4a59");
  expect(folded.querySelector("pre")!.textContent).toBe(`${context}\n${commands}`);
});

test("a web search says what it asked and how many results came; each result's title links to its page, over its snippet (B10)", async () => {
  const line = await renderRecords([
    captured(0, "WebSearchResults", {
      call_id: "t5",
      extra: {},
      query: "python monotonic clock flaky test",
      duration_sec: 1.2,
      content: {
        "py/tuple": [
          {
            "py/object": "trackinizer.lib.agent.types.sessions.WebSearchResult",
            url: "https://docs.python.org/3/library/time.html#time.monotonic",
            title: "time -- Time access and conversions",
            snippet: "Return the value of a monotonic clock.",
          },
          { "py/object": "trackinizer.lib.agent.types.sessions.WebSearchResult", url: "javascript:alert(1)", title: "Not a page", snippet: "Refused." },
        ],
      },
    }, "python monotonic clock flaky test\ntime -- Time access and conversions\nNot a page\nReturn the value of a monotonic clock.\nRefused."),
  ]);
  expect(line.querySelector(".tool-row .name")!.textContent).toBe("Searched the web");
  expect(line.querySelector(".tool-arg")!.textContent).toBe("python monotonic clock flaky test");
  expect(line.querySelector(".tr-outcome")!.textContent).toBe("2 results");
  fireEvent.click(line.querySelector("summary")!);
  const results = await waitFor(() => {
    const found = [...line.querySelectorAll(".tr-search li")];
    expect(found).toHaveLength(2);
    return found;
  });
  expect(results.map((result) => result.textContent)).toEqual([
    "time -- Time access and conversionsReturn the value of a monotonic clock.",
    "Not a pageRefused.",
  ]);
  const link = results[0]!.querySelector("a")!;
  expect(link.getAttribute("href")).toBe("https://docs.python.org/3/library/time.html#time.monotonic");
  expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  expect(results[1]!.querySelector("a")).toBeNull();
});
test("file, fetch and agent results show their payload once, with a fetch's code and size and an agent's state (B11)", async () => {
  const between = (idx: number) => captured(idx, "AssistantMessage", { content: `Next ${idx}.` }, `Next ${idx}.`);
  await renderRecords(
    [
      captured(0, "FileReadResult", { call_id: "t2", extra: {}, path: "/work/foo.py", content: "     1\timport os\n     2\timport time\n", ranges: { "py/tuple": [] } }, "/work/foo.py\nimport os\nimport time"),
      between(1),
      captured(2, "WebFetchResult", { call_id: "t6", extra: {}, url: "https://docs.python.org/3/", content: "# time", code: 200, duration_sec: 0.8, size: 123456 }, "https://docs.python.org/3/\n# time"),
      between(3),
      captured(4, "AgentStatusResult", {
        call_id: "t7",
        extra: {},
        agent_id: "a-7f3",
        agent_kind: "Explore",
        prompt: "Find every time.time() used for durations.",
        content: "Found 3 uses.",
        model: "claude-sonnet-5",
        state: "completed",
        tokens: 12000,
        duration_sec: 44.0,
        tool_calls: 12,
        output_file: null,
      }, "Find every time.time() used for durations.\nFound 3 uses."),
    ],
    0,
  );
  const open = (idx: number, selector: string) => {
    fireEvent.click(at(idx).querySelector("summary")!);
    return opened(at(idx), selector);
  };
  expect(at(0).querySelector(".tr-outcome")!.textContent).toBe("2 lines");
  // A read's text in its file's language, its line numbers apart from it.
  const read = await open(0, ".tr-code");
  expect(rowsOf(read)).toEqual(["import os", "import time"]);
  expect([...read.querySelectorAll<HTMLElement>(".ln")].map((row) => row.dataset.n)).toEqual(["1", "2"]);
  await coloured(read);
  expect(at(2).querySelector(".tr-outcome")!.textContent).toBe("HTTP 200 · 120.6 KB");
  expect((await open(2, ".tool-out")).textContent).toBe("# time");
  expect(at(4).querySelector(".tr-outcome")!.textContent).toBe("completed · Explore · 44 s");
  expect((await open(4, ".tool-out")).textContent).toBe("Found 3 uses.");
});
test("harness tags such as <system-reminder> fold apart from the prose around them (B13)", async () => {
  const content = "Please fix the flaky test in `loop/foo_test.py`.\n\n<system-reminder>\nThe user opened loop/foo.py in the IDE.\n</system-reminder>";
  const line = await renderRecords([captured(0, "UserMessage", { content, attachments: { "py/tuple": [] } }, content)]);
  expect(line.querySelector(".turn-body")!.textContent!.trim()).toBe("Please fix the flaky test in loop/foo_test.py.");
  const folded = line.querySelector("details")!;
  expect(folded.open).toBe(false);
  expect(folded.querySelector("summary")!.textContent).toBe("system-reminder");
  expect(folded.querySelector("pre")!.textContent).toBe("The user opened loop/foo.py in the IDE.");
});

test("a message's attachments show as a chip naming each one's type and size, and nothing is fetched (B14)", async () => {
  const attachment = (mime: string, b64: string) => ({ "py/object": "trackinizer.lib.agent.types.sessions.Attachment", mime_descriptor: mime, data: { "py/b64": b64 } });
  await renderRecords(
    [
      captured(0, "UserMessage", { content: "Here is the CI failure:", attachments: { "py/tuple": [attachment("image/png", "A".repeat(2868))] } }, "Here is the CI failure:"),
      captured(1, "UserMessage", {
        content: null,
        attachments: { "py/tuple": [attachment("image/png", "A".repeat(2868)), attachment("image/jpeg", `${"A".repeat(683)}=`)] },
      }),
    ],
    1,
  );
  expect(at(0).querySelector(".tr-chip")!.textContent).toBe("1 attachment (image/png, 2.1 KB)");
  expect(at(1).querySelector(".tr-chip")!.textContent).toBe("2 attachments (image/png, 2.1 KB; image/jpeg, 512 B)");
  expect(document.querySelector("img")).toBeNull();
});

test("a tool result's attachments, and a long system message's, show as a chip too, and nothing is fetched (CR-05)", async () => {
  const png = { "py/object": "trackinizer.lib.agent.types.sessions.Attachment", mime_descriptor: "image/png", data: { "py/b64": "A".repeat(2868) } };
  const prompt = `You are Codex.\n${"- Be concise.\n".repeat(80)}`;
  await renderRecords(
    [
      captured(0, "ToolCall", { call_id: "t4", name: "mcp__browser__screenshot", arguments: {} }),
      captured(1, "UncategorizedToolResult", { call_id: "t4", extra: {}, content: null, attachments: { "py/tuple": [png] } }),
      captured(2, "SystemMessage", { subtype: "developer", content: prompt, attachments: { "py/tuple": [png] }, extra: {} }, prompt),
      captured(3, "UserMessage", { content: "hi" }, "hi"),
    ],
    3,
  );
  showBookkeeping();
  expect(at(0).dataset.result).toBe("1");
  fireEvent.click(at(0).querySelector("summary")!);
  expect((await opened(at(0), ".tr-chip")).textContent).toBe("1 attachment (image/png, 2.1 KB)");
  expect(at(2).querySelector("details")).not.toBeNull();
  expect(at(2).querySelector(".tr-chip")!.textContent).toBe("1 attachment (image/png, 2.1 KB)");
  expect(document.querySelector("img")).toBeNull();
});

test("an empty message folds into the bookkeeping, and a compaction says how it was asked for (B15)", async () => {
  await renderRecords(
    [
      captured(0, "ContextCompaction", { summary: null, extra: { trigger: "manual", directions: "keep the API notes" } }),
      captured(1, "AssistantMessage", { content: "", attachments: { "py/tuple": [] } }),
      captured(2, "AssistantMessage", { content: "Done.", attachments: { "py/tuple": [] } }, "Done."),
    ],
    2,
  );
  expect(shownIdx()).toEqual(["0", "2"]);
  expect(at(0).querySelector(".turn-h b")!.textContent).toBe("Context compacted");
  expect(at(0).querySelector(".turn-meta")!.textContent).toBe("manual · directions: keep the API notes");
  expect(document.querySelector('[data-part="0"] .tr-noise')!.textContent).toBe("Show 1 bookkeeping record");
});

test("context state shows its kind, with its content folded (B16)", async () => {
  const content = "<environment_context>\n  <cwd>/work/loop</cwd>\n</environment_context>";
  await renderRecords([captured(0, "ContextState", { kind: "environment_context", content, extra: {} }, content), captured(1, "UserMessage", { content: "hi" }, "hi")], 1);
  showBookkeeping();
  const folded = at(0).querySelector("details")!;
  expect(folded.open).toBe(false);
  expect(folded.querySelector("summary")!.textContent).toBe("environment_context");
  expect(folded.querySelector("pre")!.textContent).toBe(content);
});

test("an opaque tool result that reports an error shows as failed, and open (B17)", async () => {
  const line = await renderRecords([
    captured(0, "UncategorizedToolResult", { call_id: "t8", extra: { is_error: true }, content: "Error: missing_scope (chat:write)", attachments: { "py/tuple": [] } }, "Error: missing_scope (chat:write)"),
  ]);
  expect(line.querySelector(".tool-row .name")!.textContent).toBe("Tool result");
  expect(line.querySelector(".tr-step")!.classList.contains("failed")).toBe(true);
  const output = line.querySelector(".tool-out")!;
  expect(output.textContent).toBe("Error: missing_scope (chat:write)");
  expect(output.classList.contains("failed")).toBe(true);
});
test("a tool call and its result show as one step, matched by call_id however far on (B18, TX-07)", async () => {
  await renderRecords(
    [
      captured(0, "ToolCall", { call_id: "t8", name: "mcp__slack__post_message", arguments: { channel: "#eng" } }),
      captured(1, "TokenUsage", { info: { input_tokens: 3 } }),
      captured(2, "UncategorizedToolResult", { call_id: "t8", extra: {}, content: "posted" }, "posted"),
      captured(3, "ToolCall", { call_id: "t9", name: "Bash", arguments: { command: "ls" } }),
      ...Array.from({ length: 9 }, (_, k) => captured(4 + k, "AssistantMessage", { content: `Still going ${k}.` }, `Still going ${k}.`)),
      captured(13, "ShellCommandResult", { call_id: "t9", extra: {}, command: { "py/tuple": ["ls"] }, stdout: "a.py", stderr: "", exit_code: 0 }),
    ],
    4,
  );
  // The two steps in a row fold into one group.
  expect(shownIdx()).toEqual(["4", "5", "6", "7", "8", "9", "10", "11", "12"]);
  const group = document.querySelector<HTMLElement>('[data-part="0"] [data-group="0"]')!;
  expect(group.querySelector("summary .name")!.textContent).toBe("Called 1 tool, ran 1 command");
  fireEvent.click(group.querySelector("summary")!);
  await waitFor(() => expect(shownIdx().slice(0, 2)).toEqual(["0", "3"]));
  expect(at(0).dataset.result).toBe("2");
  expect(at(0).querySelector(".tool-row .name")!.textContent).toBe("mcp__slack__post_message");
  expect(at(0).querySelector(".tool-arg")!.textContent).toBe("channel: #eng");
  expect(rowsOf(at(0).querySelector<HTMLElement>(".tr-preview .tool-out")!)).toEqual(["posted"]);
  expect(at(3).dataset.result).toBe("13");
});
test("ANSI colours show as coloured text, not as escape codes (B19)", async () => {
  const line = await renderRecords([
    captured(0, "ShellCommandResult", { call_id: "t9", extra: {}, command: { "py/tuple": ["pytest"] }, stdout: "\u001b[32m412 passed\u001b[0m in \u001b[1m9.01s\u001b[0m\n", stderr: "", exit_code: 0 }),
  ]);
  const preview = line.querySelector<HTMLElement>(".tr-preview .tool-out")!;
  expect(rowsOf(preview)).toEqual(["412 passed in 9.01s"]);
  expect([...preview.querySelectorAll(".ln span")].map((run) => [run.className, run.textContent])).toEqual([
    ["a-green", "412 passed"],
    ["a-bold", "9.01s"],
  ]);
  // A colour opened on one line holds on the next.
  cleanup();
  const carried = await renderRecords([
    captured(0, "ShellCommandResult", { call_id: "t9", extra: {}, command: null, stdout: "\u001b[31mfirst\nsecond\u001b[0m\nthird\nfourth", stderr: "", exit_code: 0 }),
  ]);
  const rows = [...carried.querySelectorAll(".tr-preview .ln")];
  expect(rows.map((row) => [row.querySelector("span")?.className ?? "", row.textContent])).toEqual([
    ["a-red", "first"],
    ["a-red", "second"],
    ["", "third"],
  ]);
  expect(within(carried).getByRole("button", { name: "+1 line" })).toBeTruthy();
});
test("reasoning is one line, its first (its summary's, when its text is sealed), and renders as Markdown without images once opened; sealed reasoning with no summary is not drawn, nor counted (B20)", async () => {
  const summary = "**Investigating the flake**\n\n![chart](https://example.com/t.png)";
  await renderRecords(
    [captured(0, "Thinking", { content: null, encrypted: "", summary, extra: {} }, summary), captured(1, "Thinking", { content: null, encrypted: "", summary: null, extra: {} })],
    0,
  );
  expect(shownIdx()).toEqual(["0"]);
  const readable = at(0).querySelector("details")!;
  expect(readable.querySelector("summary .name")!.textContent).toBe("Thinking");
  expect(readable.querySelector("summary .tr-think-line")!.textContent).toBe("Investigating the flake");
  expect(readable.querySelector(".md")).toBeNull();
  fireEvent.click(readable.querySelector("summary")!);
  await waitFor(() => expect(readable.querySelector("strong")?.textContent).toBe("Investigating the flake"));
  expect(document.querySelector("img")).toBeNull();
  expect(document.querySelector('[data-part="0"] .tr-noise')).toBeNull();
  expect(document.querySelector('[data-idx="1"]')).toBeNull();
});
test("a record's model and time show only when they differ from the record before (B21)", async () => {
  const said = (idx: number, model: string, timestamp: string) => ({
    ...captured(idx, "AssistantMessage", { content: `Turn ${idx}.` }, `Turn ${idx}.`),
    model,
    timestamp,
  });
  await renderRecords(
    [
      said(0, "claude-opus-5-5", "2026-10-01T10:00:05Z"),
      said(1, "claude-opus-5-5", "2026-10-01T10:00:40Z"),
      said(2, "claude-sonnet-5", "2026-10-01T10:00:50Z"),
      said(3, "claude-sonnet-5", "2026-10-01T10:02:00Z"),
    ],
    0,
  );
  expect([0, 1, 2, 3].map((idx) => at(idx).querySelector(".turn-model")?.textContent ?? "")).toEqual(["claude-opus-5-5", "", "claude-sonnet-5", ""]);
  expect([0, 1, 2, 3].map((idx) => at(idx).querySelector("time")?.getAttribute("dateTime") ?? "")).toEqual([
    "2026-10-01T10:00:05Z",
    "",
    "",
    "2026-10-01T10:02:00Z",
  ]);
});

test("a trax run sh session's output reads as one terminal block, its input as prompt lines (B22)", async () => {
  const line = (idx: number, kind: string, text: string) => captured(idx, kind, { text }, text);
  await renderRecords(
    [
      line(0, "Stdin", "python train.py --steps 2\n"),
      line(1, "Stdout", "step 0 loss 1.0000\n"),
      line(2, "Stdout", "step 1 loss \u001b[1m0.5000\u001b[0m\n"),
      line(3, "Stderr", "warning: slow\n"),
      line(4, "Stdout", "done\n"),
    ],
    0,
  );
  expect(shownIdx()).toEqual(["0"]);
  expect(at(0).querySelector(".turn-h b")!.textContent).toBe("Terminal");
  const terminal = at(0).querySelector(".tr-term")!;
  expect(terminal.textContent).toBe("$ python train.py --steps 2\nstep 0 loss 1.0000\nstep 1 loss 0.5000\nwarning: slow\ndone\n");
  expect(terminal.querySelector(".t-err")!.textContent).toBe("warning: slow\n");
});

test("Refresh says what it reads again: new records arrive on their own (B23)", async () => {
  serve([part(0, 2)]);
  renderDetail({ id: SESSION.id });
  await waitFor(() => expect(shown(0)).toBe(2));
  expect(header().getByRole("button", { name: "Refresh" }).getAttribute("title")).toBe("Read every loaded record again; new ones arrive on their own");
});

test("an offloaded record shows the head of its text, and says the rest is offloaded (B24)", async () => {
  const text = `pytest\n${"loop/foo_test.py::test PASSED\n".repeat(80)}`.slice(0, 2000);
  const line = await renderRecords([{ ...captured(0, "ShellCommandResult", {}, text), payload: { $body: "offloaded" } }]);
  expect(line.querySelector(".tool-row .name")!.textContent).toBe("Ran");
  expect(line.querySelector(".tr-outcome")!.textContent).toBe("the first 2,000 characters; the rest is offloaded");
  fireEvent.click(within(line).getByRole("button", { name: /^\+\d+ lines$/ }));
  fireEvent.click(await within(line).findByRole("button", { name: /^Show \d+ more lines$/ }));
  expect(line.querySelector(".tool-out")!.textContent).toBe(text);
});
test("a long system or developer prompt starts folded; a short system message shows as is", async () => {
  const prompt = `You are Codex.\n${"- Be concise.\n".repeat(80)}`;
  await renderRecords(
    [
      captured(0, "SystemMessage", { subtype: "developer", content: prompt, attachments: { "py/tuple": [] }, extra: {} }, prompt),
      captured(1, "SystemMessage", { subtype: "informational", content: "Auto-update available: 2.1.201" }, "Auto-update available: 2.1.201"),
      captured(2, "UserMessage", { content: "hi" }, "hi"),
    ],
    2,
  );
  showBookkeeping();
  expect(at(0).querySelector(".turn-h b")!.textContent).toBe("System · developer");
  const folded = at(0).querySelector("details")!;
  expect(folded.open).toBe(false);
  expect(folded.querySelector("summary")!.textContent).toBe(`${prompt.length.toLocaleString("en")} characters`);
  expect(folded.querySelector("pre")!.textContent).toBe(prompt);
  expect(at(1).querySelector(".turn-body")!.textContent!.trim()).toBe("Auto-update available: 2.1.201");
});

test("a run of tool steps between messages folds into one line that counts them, its failures said and open within", async () => {
  const call = (idx: number, name: string, args: object) => captured(idx, "ToolCall", { call_id: `c${idx}`, name, arguments: args });
  const ran = (idx: number, exit: number, stdout: string) =>
    captured(idx, "ShellCommandResult", { call_id: `c${idx - 1}`, extra: {}, command: null, stdout, stderr: "", exit_code: exit });
  await renderRecords(
    [
      captured(0, "UserMessage", { content: "Fix it." }, "Fix it."),
      call(1, "Bash", { command: "pytest -x" }),
      ran(2, 0, "1 passed\n"),
      captured(3, "Thinking", { content: "**Now the module**" }, "Now the module"),
      call(4, "Read", { file_path: "/w/foo.py" }),
      captured(5, "FileReadResult", { call_id: "c4", extra: {}, path: "/w/foo.py", content: "x = 1\n" }),
      call(6, "Bash", { command: "pytest" }),
      ran(7, 1, "1 failed\n"),
      captured(8, "AssistantMessage", { content: "One fails." }, "One fails."),
    ],
    0,
  );
  expect(shownIdx()).toEqual(["0", "8"]);
  const group = document.querySelector<HTMLElement>('[data-part="0"] [data-group="1"]')!;
  expect(group.querySelector("summary .name")!.textContent).toBe("Ran 2 commands, read 1 file");
  expect(group.querySelector("summary .tr-outcome")!.textContent).toBe("1 failed");
  fireEvent.click(group.querySelector("summary")!);
  await waitFor(() => expect(shownIdx()).toEqual(["0", "1", "3", "4", "6", "8"]));
  expect([1, 4, 6].map((idx) => at(idx).querySelector("details")!.open)).toEqual([false, false, true]);
  expect(at(6).querySelector(".tool-out")!.textContent).toBe("1 failed\n");
  // A command previews its output; a read, as the CLIs' exploring rows, does not.
  expect([1, 4].map((idx) => at(idx).querySelector(".tr-preview") !== null)).toEqual([true, false]);
});

test("a search whose result is opaque shows what it searched for and no preview, as a read does", async () => {
  const line = await renderRecords([
    captured(0, "ToolCall", { call_id: "g1", name: "Grep", arguments: { pattern: "time\\.time", path: "loop" } }),
    captured(1, "UncategorizedToolResult", { call_id: "g1", extra: {}, content: "loop/a.py\nloop/b.py" }, "loop/a.py\nloop/b.py"),
  ]);
  expect([...line.querySelectorAll("summary :is(.name, .tool-arg, .tr-outcome)")].map((part) => part.textContent)).toEqual([
    "Searched",
    "time\\.time",
    "2 lines",
  ]);
  expect(line.querySelector(".tr-preview")).toBeNull();
});

test("a command's JSON output shows as a JSON view, its values in the code colours", async () => {
  const stdout = '{\n  "status": "active",\n  "owner": null,\n  "seq": 21760\n}\n';
  const line = await renderRecords([
    captured(0, "ShellCommandResult", { call_id: "t1", extra: {}, command: { "py/tuple": ["trax", "issue", "21760"] }, stdout, stderr: "", exit_code: 0 }),
  ]);
  fireEvent.click(line.querySelector("summary")!);
  const json = await waitFor(() => within(line).getByRole("group", { name: "JSON" }));
  expect([...json.querySelectorAll(".jv-key")].map((key) => key.textContent)).toEqual(['"status"', '"owner"', '"seq"']);
  expect(json.querySelector(".jv-number")!.textContent).toBe("21760");
});

test("a person's long message folds to its first lines, and shows whole on request", async () => {
  const content = Array.from({ length: 30 }, (_, k) => `Step ${k}: do the thing.`).join("\n\n");
  const line = await renderRecords([captured(0, "UserMessage", { content }, content)]);
  const clamp = line.querySelector<HTMLElement>(".tr-clamp")!;
  expect(clamp.dataset.open).toBe("false");
  const toggle = within(line).getByRole("button", { name: "Show all" });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(toggle);
  expect(clamp.dataset.open).toBe("true");
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(toggle.textContent).toBe("Show less");
});

/**
 * Render the session with `records` as its one part, as `renderRecords` does, and
 * a function that appends records to it as a live capture does, read as the
 * stream layer reads them (src/live/detail.ts).
 */
async function renderLive(records: SessionRecord[], idx = 0): Promise<(...more: SessionRecord[]) => Promise<void>> {
  const parts = [part(0, records.length)];
  serve(parts, undefined, (k) => records[k]!);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderDetail({ id: SESSION.id }, client);
  await waitFor(() => expect(document.querySelector(`[data-part="0"] [data-idx="${idx}"]`)).not.toBeNull());
  return async (...more) => {
    records.push(...more);
    parts[0] = part(0, records.length);
    await client.refetchQueries({ queryKey: transcriptQueries.parts(SESSION.id).queryKey });
  };
}

/** A Bash call `c<idx>` running `command`. */
const bash = (idx: number, command = "pytest") => captured(idx, "ToolCall", { call_id: `c${idx}`, name: "Bash", arguments: { command } });

/** The result of call `c<call>`, as Claude's shell result at `idx`. */
const ran = (idx: number, call: number, stdout: string, exit = 0, stderr = "") =>
  captured(idx, "ShellCommandResult", { call_id: `c${call}`, extra: {}, command: null, stdout, stderr, exit_code: exit });

test("a written file shows and copies its text as written, a column of numbers included (TX-01)", async () => {
  const copied = stubClipboard();
  const content = "1\tAlice\n2\tBob\n";
  const line = await renderRecords([
    captured(0, "ToolCall", { call_id: "w1", name: "Write", arguments: { file_path: "/w/people.tsv", content } }),
    captured(1, "FileWriteResult", { call_id: "w1", extra: {}, path: "/w/people.tsv", content }),
  ]);
  fireEvent.click(line.querySelector("summary")!);
  const code = await opened(line, ".tr-code");
  expect(rowsOf(code)).toEqual(["1\tAlice", "2\tBob"]);
  expect(code.querySelector("[data-n]")).toBeNull();
  fireEvent.click(within(line).getByRole("button", { name: "Copy code" }));
  await waitFor(() => expect(copied).toEqual([content]));
});

test("an opened step shows each whole primary argument, whatever its result says it did (TX-02)", async () => {
  const patch = "*** Begin Patch\n*** Update File: a.py\n-x\n+y\n*** End Patch";
  await renderRecords(
    [
      captured(0, "ToolCall", { call_id: "g1", name: "Grep", arguments: { pattern: "first\nsecond", path: "loop" } }),
      captured(1, "AssistantMessage", { content: "And:" }, "And:"),
      captured(2, "ToolCall", { call_id: "p1", name: "apply_patch", arguments: { input: patch } }),
      captured(3, "AssistantMessage", { content: "And:" }, "And:"),
      bash(4, "cat a.py"),
      captured(5, "FileReadResult", { call_id: "c4", extra: {}, path: "a.py", content: "x = 1\n" }),
    ],
    0,
  );
  const code = async (idx: number) => {
    fireEvent.click(at(idx).querySelector("summary")!);
    return rowsOf(await opened(at(idx), ".tr-code"));
  };
  expect(await code(0)).toEqual(["first", "second"]);
  expect(await code(2)).toEqual(patch.split("\n"));
  // The read's text is the result's; the command that read it is the call's.
  expect(at(4).querySelector(".tool-arg")!.textContent).toBe("a.py");
  expect(await code(4)).toEqual(["cat a.py"]);
});

test("a failed write keeps the content it tried to write beside the reason it failed (TX-03)", async () => {
  const line = await renderRecords([
    captured(0, "ToolCall", { call_id: "w1", name: "Write", arguments: { file_path: "/w/notes.md", content: "valuable content" } }),
    captured(1, "FileWriteResult", {
      call_id: "w1",
      path: null,
      content: null,
      extra: { $result: { block: { is_error: true }, tool_name: "Write", text: "Permission denied: /w/notes.md" } },
    }),
  ]);
  expect(line.querySelector("details")!.open).toBe(true);
  expect([...line.querySelectorAll("dt")].map((term) => term.textContent)).toEqual(["content"]);
  expect(line.querySelector("dd")!.textContent).toBe("valuable content");
  expect(line.querySelector(".tool-out.failed")!.textContent).toBe("Permission denied: /w/notes.md");
});

test("a typed result that failed counts as failed, in its step and its group, and a failed edit shows why (TX-04)", async () => {
  await renderRecords(
    [
      captured(0, "UserMessage", { content: "Go." }, "Go."),
      bash(1),
      // Sagent's: no exit code, its failure in `is_error`.
      captured(2, "ShellCommandResult", { call_id: "c1", command: { "py/tuple": ["pytest"] }, stdout: "boom\n", stderr: "", exit_code: null, extra: { is_error: true } }),
      captured(3, "ToolCall", { call_id: "c3", name: "Read", arguments: { file_path: "/w/secret.txt" } }),
      captured(4, "FileReadResult", { call_id: "c3", path: "/w/secret.txt", content: "Denied", extra: { is_error: true } }),
      captured(5, "ToolCall", { call_id: "c5", name: "Edit", arguments: { file_path: "/w/a.py", old_string: "x = 1", new_string: "x = 2" } }),
      // Claude's: no edits, and what the model read says why.
      captured(6, "FileEditResult", {
        call_id: "c5",
        path: null,
        edits: { "py/tuple": [] },
        extra: { $result: { block: { is_error: true }, tool_name: "Edit", text: "String to replace not found in file." } },
      }),
      captured(7, "AssistantMessage", { content: "Stuck." }, "Stuck."),
    ],
    0,
  );
  const group = document.querySelector<HTMLElement>('[data-part="0"] [data-group="1"]')!;
  expect(group.querySelector("summary .name")!.textContent).toBe("Ran 1 command, read 1 file, edited 1 file");
  expect(group.querySelector("summary .tr-outcome")!.textContent).toBe("3 failed");
  fireEvent.click(group.querySelector("summary")!);
  await waitFor(() => expect(shownIdx()).toEqual(["0", "1", "3", "5", "7"]));
  expect([1, 3, 5].map((idx) => at(idx).querySelector(".tr-step")!.classList.contains("failed"))).toEqual([true, true, true]);
  expect([1, 3, 5].map((idx) => at(idx).querySelector("details")!.open)).toEqual([true, true, true]);
  expect(at(3).querySelector(".tool-out.failed")!.textContent).toBe("Denied");
  expect(at(5).querySelector(".tr-outcome")!.textContent).not.toBe("+0 −0");
  expect(at(5).querySelector(".tool-out.failed")!.textContent).toBe("String to replace not found in file.");
});

test("a failure that arrives after its step drew opens it, unless the reader opened or closed it (TX-05)", async () => {
  const append = await renderLive([bash(0), captured(1, "AssistantMessage", { content: "Meanwhile." }, "Meanwhile."), bash(2)]);
  const step = (idx: number) => at(idx).querySelector("details")!;
  // The reader opens the second step and closes it again.
  fireEvent.click(at(2).querySelector("summary")!);
  await waitFor(() => expect(step(2).open).toBe(true));
  fireEvent.click(at(2).querySelector("summary")!);
  await waitFor(() => expect(step(2).open).toBe(false));
  await append(ran(3, 0, "boom\n", 1), ran(4, 2, "boom\n", 1));
  await waitFor(() => expect(at(0).dataset.result).toBe("3"));
  await waitFor(() => expect(step(0).open).toBe(true));
  expect(at(2).dataset.result).toBe("4");
  expect(step(2).open).toBe(false);
});

test("a step the reader opened stays open, and keeps the focus, when a later step folds it into a group (TX-06)", async () => {
  const append = await renderLive([captured(0, "UserMessage", { content: "Go." }, "Go."), bash(1), ran(2, 1, "1 passed\n")]);
  fireEvent.click(at(1).querySelector("summary")!);
  at(1).querySelector<HTMLElement>("summary")!.focus();
  await opened(at(1), ".tool-out");
  await append(captured(3, "ToolCall", { call_id: "c3", name: "Read", arguments: { file_path: "/w/a.py" } }));
  await waitFor(() => expect(document.querySelector('[data-part="0"] [data-group="1"]')).not.toBeNull());
  await waitFor(() => expect(at(1)?.querySelector("details")?.open).toBe(true));
  expect(at(1).querySelector(".tool-out")!.textContent).toBe("1 passed\n");
  expect(document.activeElement).toBe(at(1).querySelector("summary"));
  // A step the reader is only on, closed: its group opens about it, and it stays closed.
  await append(captured(4, "AssistantMessage", { content: "Next." }, "Next."), bash(5));
  await waitFor(() => expect(at(5)).not.toBeNull());
  at(5).querySelector<HTMLElement>("summary")!.focus();
  await append(captured(6, "ToolCall", { call_id: "c6", name: "Read", arguments: { file_path: "/w/b.py" } }));
  await waitFor(() => expect(document.querySelector('[data-part="0"] [data-group="5"]')).not.toBeNull());
  await waitFor(() => expect(document.activeElement).toBe(at(5)?.querySelector("summary")));
  expect(at(5).querySelector("details")!.open).toBe(false);
});

test("a live append to a part drawn whole at once leaves its lines drawn, and what the reader opened on them", async () => {
  const said = (idx: number) => captured(idx, "AssistantMessage", { content: `Turn ${idx}.` }, `Turn ${idx}.`);
  const append = await renderLive([said(0), said(1), said(2)]);
  fireEvent.click(within(at(0)).getByRole("button", { name: "Raw" }));
  await append(said(3));
  await waitFor(() => expect(shownIdx()).toEqual(["0", "1", "2", "3"]));
  expect(at(0).querySelector(".turn-json")).not.toBeNull();
});

test("a command's preview, and its +N lines, count its errors as its line does (TX-08)", async () => {
  const line = await renderRecords([ran(0, 0, "a\nb\nc\n", 0, "warning: slow\n")]);
  expect(line.querySelector(".tr-outcome")!.textContent).toBe("4 lines");
  expect(rowsOf(line.querySelector<HTMLElement>(".tr-preview .tool-out")!)).toEqual(["a", "b", "c"]);
  expect(within(line).getByRole("button", { name: "+1 line" })).toBeTruthy();
});

test("a colour turned on before the lines output leaves out holds in its tail, and across a terminal's records (TX-09)", async () => {
  const stdout = `\u001b[31m${Array.from({ length: 80 }, (_, k) => `line ${k}`).join("\n")}\u001b[0m\n`;
  const terminal = (idx: number, text: string) => captured(idx, "Stdout", { text }, text);
  await renderRecords([ran(0, 0, stdout, 1), captured(1, "AssistantMessage", { content: "And:" }, "And:"), terminal(2, "\u001b[31mred\n"), terminal(3, "still red\u001b[0m\n")], 0);
  const red = [...at(0).querySelectorAll(".tool-out .a-red")].map((run) => run.textContent);
  expect(red.at(-1)).toMatch(/^line 40\n[^]*\nline 79$/);
  expect([...at(2).querySelectorAll(".tr-term .a-red")].map((run) => run.textContent)).toEqual(["red\n", "still red"]);
});

test("a diff colours its old side and its new side each as a file of its own (TX-10)", async () => {
  const splice = { before: 'old\n"""\n', after: 'new\n"""\n', lead: ' doc = """\n', trail: " end = True\n", start: null, count: null, bare: { "py/set": [] } };
  const line = await renderRecords([captured(0, "FileEditResult", { call_id: "e1", extra: {}, path: "/w/a.py", edits: { "py/tuple": [splice] } })]);
  fireEvent.click(line.querySelector("summary")!);
  const diff = await opened(line, ".tr-diff");
  await coloured(diff);
  const rows = [...diff.querySelectorAll<HTMLElement>(".ln")];
  expect(rows.map((row) => row.textContent)).toEqual([' doc = """', "-old", '-"""', "+new", '+"""', " end = True"]);
  // Each side's string runs from its opening quotes to its own closing ones.
  expect([1, 3].map((k) => rows[k]!.querySelector(".hljs-string")?.textContent)).toEqual(["old", "new"]);
  expect(rows[5]!.querySelector(".hljs-string")).toBeNull();
  expect(rows[5]!.querySelector(".hljs-literal")!.textContent).toBe("True");
});

test("sealed reasoning with no summary is not drawn: the next record that is shows the model and time that changed (TX-12)", async () => {
  await renderRecords(
    [
      { ...captured(0, "AssistantMessage", { content: "Thinking it over." }, "Thinking it over."), model: "claude-opus-5-5", timestamp: "2026-10-01T10:00:00Z" },
      { ...captured(1, "Thinking", { content: null, encrypted: "", summary: null, extra: {} }), model: "claude-sonnet-5", timestamp: "2026-10-01T10:05:00Z" },
      { ...captured(2, "AssistantMessage", { content: "Done." }, "Done."), model: "claude-sonnet-5", timestamp: "2026-10-01T10:05:00Z" },
    ],
    0,
  );
  expect(shownIdx()).toEqual(["0", "2"]);
  expect(at(2).querySelector(".turn-model")!.textContent).toBe("claude-sonnet-5");
  expect(at(2).querySelector("time")!.getAttribute("dateTime")).toBe("2026-10-01T10:05:00Z");
  expect(document.querySelector('[data-part="0"] .tr-noise')).toBeNull();
});

/**
 * Serve session `self` with no records yet, `answer` for its detail on each read,
 * and `inbound` for its messages.
 */
function serveLive(self: DetailRow, inbound: () => Response, answer: () => DetailRow = () => self): Sent[] {
  return stubFetch((request) => {
    const path = new URL(request.url).pathname;
    if (path === `/api/web/get/${self.id}`) return Response.json(detail(answer()));
    if (path === `/api/sessions/${self.id}/parts`) return Response.json({ parts: [] });
    if (path === `/api/sessions/${self.id}/inbound`) return inbound();
    return Response.json({ detail: "not found" }, { status: 404 });
  });
}

const LIVE = row("AgentSession", 7, { owner: "codex-arm" });

/** The transcript section, once its composer shows. */
async function composer() {
  const section = await waitFor(() => {
    const found = document.querySelector<HTMLElement>('[data-section="transcript"]');
    expect(found?.querySelector("textarea")).toBeTruthy();
    return found!;
  });
  return { section: within(section), box: within(section).getByRole("textbox", { name: "Message" }) };
}

test("a writer messages a live session from the foot of its transcript: one keyed POST, a receipt, and no read of its queue (SE4)", async () => {
  const sent = serveLive(LIVE, () => Response.json({ queued: 1 }));
  renderDetail({ id: LIVE.id });
  const { section, box } = await composer();
  const empty = await section.findByText("No records captured yet.");
  expect(box.closest("form")!.compareDocumentPosition(empty) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
  fireEvent.change(box, { target: { value: "Please stop and summarize." } });
  fireEvent.click(section.getByRole("button", { name: "Send message" }));
  expect((await section.findByRole("status")).textContent).toBe("Queued for codex-arm");
  expect(box).toHaveProperty("value", "");
  expect(sent.filter((request) => request.path.endsWith("/inbound"))).toEqual([
    {
      method: "POST",
      path: `/api/sessions/${LIVE.id}/inbound`,
      query: "",
      headers: { "content-type": "application/json", "idempotency-key": expect.stringMatching(/^[0-9a-f-]{36}$/) },
      body: { text: "Please stop and summarize." },
    },
  ]);
});

test("a 409 says trax run is not polling this session, and keeps the draft (SE4)", async () => {
  serveLive(LIVE, () => Response.json({ detail: "No active inbound poller" }, { status: 409 }));
  renderDetail({ id: LIVE.id });
  const { section, box } = await composer();
  fireEvent.change(box, { target: { value: "Are you there?" } });
  fireEvent.click(section.getByRole("button", { name: "Send message" }));
  expect((await section.findByRole("alert")).textContent).toBe("Not connected: trax run is not polling this session (No active inbound poller).");
  expect(box).toHaveProperty("value", "Are you there?");
  expect(section.getByRole("button", { name: "Retry message" })).toBeTruthy();
});

test("a viewer gets no composer, nor does a session that has ended (SE4)", async () => {
  serveLive(LIVE, () => Response.json({ queued: 1 }));
  renderDetail({ id: LIVE.id }, undefined, { profile: { ...PROFILE, role: "viewer" } });
  expect(await screen.findByText("No records captured yet.")).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
  cleanup();
  const ended = row("AgentSession", 8, { owner: "codex-arm", ended: "2026-09-30T10:00:00+00:00" });
  serveLive(ended, () => Response.json({ queued: 1 }));
  renderDetail({ id: ended.id });
  expect(await screen.findByText("The session has ended, so it takes no messages.")).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
});

test("a draft survives a live refetch, and the composer goes once the session ends (SE4, R42)", async () => {
  let current = LIVE;
  serveLive(LIVE, () => Response.json({ queued: 1 }), () => current);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderDetail({ id: LIVE.id }, client);
  const { box } = await composer();
  fireEvent.change(box, { target: { value: "Half a thought" } });
  await client.refetchQueries({ queryKey: detailQueries.detail(LIVE.id).queryKey });
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("value", "Half a thought");
  current = { ...LIVE, ended: "2026-10-01T10:00:00+00:00" };
  await client.refetchQueries({ queryKey: detailQueries.detail(LIVE.id).queryKey });
  expect(await screen.findByText("The session has ended, so it takes no messages.")).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
});
