import { cleanup, fireEvent, render, within } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import type { SessionRecord } from "../api/sessions";
import { ToastProvider } from "../ui/toast";
import { ChatToolCard } from "./ChatToolCard";
import type { ToolExchange } from "./chatLines";

/** A record as a chat part holds it, stamped `at` so a call and its result can be timed. */
function record(idx: number, kind: string, payload: { [field: string]: unknown }, at: string): SessionRecord {
  return {
    idx, kind, payload: { "py/object": `trackinizer.lib.agent.types.sessions.${kind}`, ...payload },
    text: "", context_id: null, timestamp: at, model: null, ciphertext: null,
  } as unknown as SessionRecord;
}

afterEach(cleanup);

function card(step: ToolExchange) {
  const { container } = render(<ToastProvider><ChatToolCard step={step} kinds={[]} /></ToastProvider>);
  const root = container.querySelector<HTMLElement>(".chat-line-tool")!;
  return {
    root,
    name: () => root.querySelector(".verb")!.textContent,
    target: () => root.querySelector(".arg")?.textContent ?? null,
    gutter: () => root.querySelector(".t")?.textContent ?? null,
    output: () => root.querySelector(".out .lvl")?.textContent ?? null,
    state: () => root.dataset.state,
  };
}

// What a claude part records for `ls`: the call's own id, the command in the result, and
// the exit. The call is stamped 10:00:00 and its answer 1.62 s later, which is what the
// row's margin prints.
const claude: ToolExchange = {
  call: record(1, "ToolCall", { call_id: "toolu_01", name: "Bash", arguments: { command: "ls -la /home", description: "List home" } }, "2026-10-21T10:00:00Z"),
  result: record(2, "ShellCommandResult", { call_id: "toolu_01", command: ["ls -la /home"], stdout: "total 8\nada\n", stderr: "", exit_code: 0 }, "2026-10-21T10:00:01.62Z"),
};

test("a claude call is the CLI's own row: its tool, what it ran, how long it took, and what came back under a └", () => {
  const shown = card(claude);
  expect([shown.name(), shown.target(), shown.gutter()]).toEqual(["Bash", "ls -la /home", "1.62 s"]);
  expect(shown.state()).toBe("done");
  // Nothing is folded away: a terminal shows what a command said, so there is no summary to press.
  expect(shown.root.querySelector("details")).toBeNull();
  expect(shown.output()).toBe("total 8\nada\n");
  expect(shown.root.querySelector(".out .branch")!.textContent).toBe("└");
  // The arguments the call carried besides its command are not a second ledger.
  expect(shown.root.querySelector("dl")).toBeNull();
  expect(shown.root.textContent).not.toContain("List home");
});

test("a codex call is the same row: its shell's argv reads as the command it ran", () => {
  const shown = card({
    call: record(1, "ToolCall", { call_id: "call_9", name: "shell", arguments: { command: ["bash", "-lc", "ls -la /home"], workdir: "/home" } }, "2026-10-21T10:00:00Z"),
    result: record(2, "ShellCommandResult", { call_id: "call_9", command: ["bash", "-lc", "ls -la /home"], stdout: "total 8\nada\n", stderr: "", exit_code: 0 }, "2026-10-21T10:00:00.44Z"),
  });
  expect([shown.name(), shown.target(), shown.gutter()]).toEqual(["shell", "ls -la /home", "0.44 s"]);
  expect(shown.output()).toBe("total 8\nada\n");
  expect(shown.root.textContent).not.toContain("workdir");
});

test("a sagent call is the same row: sagent's shell result, and a tool of its own with the text it returned", () => {
  const shell = card({
    call: record(1, "ToolCall", { call_id: "kb1", name: "Bash", arguments: { command: "sleep 200; echo done &" } }, "2026-10-21T10:00:00Z"),
    result: record(2, "ShellCommandResult", { call_id: "kb1", command: ["sleep 200; echo done &"], stdout: "[1] 4242\n", extra: { is_error: false } }, "2026-10-21T10:00:00.11Z"),
  });
  expect([shell.name(), shell.target(), shell.gutter()]).toEqual(["Bash", "sleep 200; echo done &", "0.11 s"]);
  expect(shell.output()).toBe("[1] 4242\n");
  cleanup();
  // A tool that takes no argument, and returns text no shell shape holds.
  const jobs = card({
    call: record(3, "ToolCall", { call_id: "kb2", name: "ListJobs", arguments: {} }, "2026-10-21T10:00:05Z"),
    result: record(4, "UncategorizedToolResult", { call_id: "kb2", content: "job 1: sleep 200 (running)", extra: { is_error: false } }, "2026-10-21T10:00:05.5Z"),
  });
  expect(jobs.name()).toBe("ListJobs");
  expect(jobs.target()).toBeNull();
  expect(jobs.gutter()).toBe("0.50 s");
  expect(jobs.output()).toBe("job 1: sleep 200 (running)");
});

test("a failed call says how, and its error shows: a nonzero exit, and sagent's is_error flag", () => {
  const exited = card({
    call: claude.call,
    result: record(2, "ShellCommandResult", { call_id: "toolu_01", command: ["ls /nope"], stdout: "", stderr: "ls: cannot access '/nope'\n", exit_code: 2 }, "2026-10-21T10:00:01.62Z"),
  });
  expect(exited.state()).toBe("failed");
  expect(exited.gutter()).toBe("exit 2");
  expect(exited.root.querySelector(".t")!.classList.contains("failed")).toBe(true);
  expect(exited.output()).toBe("ls: cannot access '/nope'\n");
  cleanup();
  const flagged = card({
    call: record(3, "ToolCall", { call_id: "kb3", name: "ListJobs", arguments: {} }, "2026-10-21T10:00:05Z"),
    result: record(4, "UncategorizedToolResult", { call_id: "kb3", content: "no such tool", extra: { is_error: true } }, "2026-10-21T10:00:05.5Z"),
  });
  expect(flagged.state()).toBe("failed");
  expect(flagged.gutter()).toBe("failed");
  expect(flagged.output()).toBe("no such tool");
});

test("a call no result has answered yet says so in its margin, and holds no payload", () => {
  const shown = card({ call: claude.call, result: null });
  expect([shown.name(), shown.target(), shown.gutter()]).toEqual(["Bash", "ls -la /home", "running"]);
  expect(shown.state()).toBe("running");
  expect(shown.root.querySelector(".out")).toBeNull();
});

test("a long payload is clipped to its head and tail, and the rest shows in place on request", () => {
  const stdout = Array.from({ length: 100 }, (_, k) => `line ${k}`).join("\n");
  const shown = card({ call: claude.call, result: record(2, "ShellCommandResult", { call_id: "toolu_01", command: ["ls"], stdout, stderr: "", exit_code: 0 }, "2026-10-21T10:00:01.62Z") });
  expect(shown.output()).toContain("line 19");
  expect(shown.output()).not.toContain("line 20\n");
  fireEvent.click(within(shown.root).getByRole("button", { name: "Show 40 more lines" }));
  expect(shown.output()).toBe(stdout);
});

test("a record with no time at all prints no duration, and a row keeps its payload", () => {
  const shown = card({
    call: { ...claude.call, timestamp: null } as SessionRecord,
    result: { ...claude.result!, timestamp: null } as SessionRecord,
  });
  // What it did is still the row: the margin takes the result's own note, not a time.
  expect(shown.gutter()).toBe("2 lines");
  expect(shown.output()).toBe("total 8\nada\n");
});

test("what a call returned starts as one line, and its ▸ opens the rest in place", () => {
  const stdout = Array.from({ length: 12 }, (_, k) => `line ${k}`).join("\n");
  const { container } = render(<ToastProvider><ChatToolCard kinds={[]} step={{
    call: claude.call,
    result: record(2, "ShellCommandResult", { call_id: "toolu_01", command: ["ls"], stdout, stderr: "", exit_code: 0 }, "2026-10-21T10:00:01.62Z"),
  }} /></ToastProvider>);
  const out = container.querySelector<HTMLElement>(".out")!;
  const run = container.querySelector<HTMLElement>(".chat-run")!;
  const fold = within(out).getByRole("button");
  // Compressed: the row carries no `open`, so the rules that hold the argv and the payload to one
  // line apply. The whole of both is already in the DOM, so opening is one class, not a second read.
  expect(run.className).toBe("chat-run");
  expect(fold.getAttribute("aria-expanded")).toBe("false");
  expect(fold.getAttribute("title")).toBe("Show all 12 lines this returned");
  fireEvent.click(fold);
  expect(run.className).toBe("chat-run open");
  expect(fold.getAttribute("aria-expanded")).toBe("true");
  expect(fold.textContent).toBe("▾");
  fireEvent.click(fold);
  expect(run.className).toBe("chat-run");
});

test("a call that returned one line offers nothing to open", () => {
  const { container } = render(<ToastProvider><ChatToolCard kinds={[]} step={{
    call: claude.call,
    result: record(2, "ShellCommandResult", { call_id: "toolu_01", command: ["pwd"], stdout: "/home\n", stderr: "", exit_code: 0 }, "2026-10-21T10:00:01.62Z"),
  }} /></ToastProvider>);
  expect(container.querySelector(".fold")).toBeNull();
  expect(container.querySelector<HTMLElement>(".chat-run")!.className).toBe("chat-run");
});

test("a command wider than its row gets its own ▸, and it opens the command alone", () => {
  // The row's ▸ is not a guess about length: it appears because the box measured narrower than
  // the command it prints, so this stubs the measurement jsdom cannot make.
  const held = ["scrollWidth", "clientWidth"].map((name) => [name, Object.getOwnPropertyDescriptor(HTMLElement.prototype, name)] as const);
  Object.defineProperty(HTMLElement.prototype, "scrollWidth", { configurable: true, get: () => 900 });
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 400 });
  try {
    const { container } = render(<ToastProvider><ChatToolCard kinds={[]} step={claude} /></ToastProvider>);
    const [command, payload] = [...container.querySelectorAll<HTMLButtonElement>(".fold")];
    // Two cuts, two promises, each said once.
    expect(command.getAttribute("title")).toBe("Show the whole command");
    expect(payload.getAttribute("title")).toBe("Show all 2 lines this returned");
    const run = container.querySelector<HTMLElement>(".chat-run")!;
    expect(run.className).toBe("chat-run");
    fireEvent.click(command);
    expect(run.className).toBe("chat-run argv-open");
    expect(command.getAttribute("title")).toBe("Show the command on one line again");
    fireEvent.click(command);
    expect(run.className).toBe("chat-run");
  } finally {
    for (const [name, descriptor] of held) {
      if (descriptor) Object.defineProperty(HTMLElement.prototype, name, descriptor);
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[name];
    }
  }
});

// What a part records for a web search: the query, and the hits with their addresses. One
// address is not a scheme that fetches, because a stored record is not a trusted author of `href`.
const searched: ToolExchange = {
  call: record(1, "ToolCall", { call_id: "toolu_02", name: "WebSearch", arguments: { query: "momentum crash volatility scaling" } }, "2026-10-21T10:00:00Z"),
  result: record(2, "WebSearchResults", {
    call_id: "toolu_02", query: "momentum crash volatility scaling",
    content: [
      { url: "https://www.crsp.com/momentum", title: "Momentum Crashes", snippet: "Daniel and Moskowitz, 2016." },
      { url: "http://arxiv.org/abs/1601.0", title: "Volatility scaling", snippet: "A risk-based overlay." },
      { url: "javascript:alert(1)", title: "An address that is not a door", snippet: "" },
    ],
  }, "2026-10-21T10:00:02.1Z"),
};

test("a web search's hits are a numbered source row; its ▸ gives what each said, and a scheme that cannot fetch is not a link", () => {
  const { container } = render(<ToastProvider><ChatToolCard step={searched} kinds={[]} /></ToastProvider>);
  const row = container.querySelectorAll<HTMLElement>(".chat-sources li");
  expect([...row].map((li) => li.querySelector(".src")!.textContent)).toEqual(["1crsp.com", "2arxiv.org", "3javascript:alert(1)"]);
  expect([...row].map((li) => li.querySelector(".src")!.tagName)).toEqual(["A", "A", "SPAN"]);
  expect(container.querySelector("a[href^='javascript']")).toBeNull();
  expect(row[0].querySelector("a")!.getAttribute("href")).toBe("https://www.crsp.com/momentum");
  // Closed, the chips are the whole row; the what of each is what the ▸ is for.
  expect(row[0].querySelector(".src-what")).toBeNull();
  const [fold] = [...container.querySelectorAll<HTMLButtonElement>(".out .fold")];
  expect(fold.getAttribute("title")).toBe("Show all 3 sources this returned");
  fireEvent.click(fold);
  expect(row[0].querySelector(".src-what")!.textContent).toBe("Momentum Crashes — Daniel and Moskowitz, 2016.");
  expect(fold.getAttribute("title")).toBe("Show the source chips again");
});
