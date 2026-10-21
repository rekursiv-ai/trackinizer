import { cleanup, fireEvent, render, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import type { SessionRecord } from "../api/sessions";
import { ToastProvider } from "../ui/toast";
import { ChatToolCard } from "./ChatToolCard";
import type { ToolExchange } from "./chatLines";

function record(idx: number, kind: string, payload: { [field: string]: unknown }): SessionRecord {
  return { idx, kind, payload: { "py/object": `trackinizer.lib.agent.types.sessions.${kind}`, ...payload }, text: "", context_id: null, timestamp: null, model: null, ciphertext: null } as unknown as SessionRecord;
}

afterEach(cleanup);

function card(step: ToolExchange) {
  const { container } = render(<ToastProvider><ChatToolCard step={step} kinds={[]} /></ToastProvider>);
  const root = container.querySelector<HTMLElement>(".chat-line-tool")!;
  return {
    root,
    name: () => root.querySelector(".tool-row .name")!.textContent,
    target: () => root.querySelector(".tool-row .tool-arg")?.textContent ?? null,
    state: () => root.querySelector(".chat-tool-state")!.textContent,
    output: () => root.querySelector(".tool-out")?.textContent ?? null,
    // The browser opens a details and then says so, which opens the step's body.
    open: async () => {
      fireEvent.click(root.querySelector("summary")!);
      await waitFor(() => expect(root.querySelector(".tr-step-body")).not.toBeNull());
    },
    isOpen: () => root.querySelector("details")!.open,
    terms: () => [...root.querySelectorAll("dt")].map((term) => term.textContent),
  };
}

// What a claude part records for `ls`: the call's own id, the command in the result,
// and the exit.
const claude: ToolExchange = {
  call: record(1, "ToolCall", { call_id: "toolu_01", name: "Bash", arguments: { command: "ls -la /home", description: "List home" } }),
  result: record(2, "ShellCommandResult", { call_id: "toolu_01", command: ["ls -la /home"], stdout: "total 8\nada\n", stderr: "", exit_code: 0 }),
};

test("a claude call is one collapsed card: its tool, what it ran, and that it is done; the arguments and output wait inside", async () => {
  const shown = card(claude);
  expect([shown.name(), shown.target(), shown.state()]).toEqual(["Ran", "ls -la /home", "done"]);
  expect(shown.isOpen()).toBe(false);
  // A closed card does not preview its output, as the transcript's step does.
  expect(shown.output()).toBeNull();
  expect(shown.root.textContent).not.toContain("ada");
  await shown.open();
  expect(shown.isOpen()).toBe(true);
  expect(shown.output()).toBe("total 8\nada\n");
  expect(shown.terms()).toEqual(["description"]);
});

test("a codex call is the same card: its shell's argv reads as the script it ran", async () => {
  const shown = card({
    call: record(1, "ToolCall", { call_id: "call_9", name: "shell", arguments: { command: ["bash", "-lc", "ls -la /home"], workdir: "/home" } }),
    result: record(2, "ShellCommandResult", { call_id: "call_9", command: ["bash", "-lc", "ls -la /home"], stdout: "total 8\nada\n", stderr: "", exit_code: 0 }),
  });
  expect([shown.name(), shown.target(), shown.state()]).toEqual(["Ran", "ls -la /home", "done"]);
  expect(shown.output()).toBeNull();
  await shown.open();
  expect(shown.output()).toBe("total 8\nada\n");
  expect(shown.terms()).toEqual(["workdir"]);
});

test("a sagent call is the same card: sagent's shell result, and a tool of its own with the text it returned", async () => {
  const shell = card({
    call: record(1, "ToolCall", { call_id: "kb1", name: "Bash", arguments: { command: "sleep 200; echo done &" } }),
    result: record(2, "ShellCommandResult", { call_id: "kb1", command: ["sleep 200; echo done &"], stdout: "[1] 4242\n", extra: { is_error: false } }),
  });
  expect([shell.name(), shell.target(), shell.state()]).toEqual(["Ran", "sleep 200; echo done &", "done"]);
  await shell.open();
  expect(shell.output()).toBe("[1] 4242\n");
  cleanup();
  const jobs = card({
    call: record(3, "ToolCall", { call_id: "kb2", name: "ListJobs", arguments: {} }),
    result: record(4, "UncategorizedToolResult", { call_id: "kb2", content: "job 1: sleep 200 (running)", extra: { is_error: false } }),
  });
  expect([jobs.name(), jobs.state()]).toEqual(["ListJobs", "done"]);
  expect(jobs.output()).toBeNull();
  await jobs.open();
  expect(jobs.output()).toBe("job 1: sleep 200 (running)");
});

test("a failed call says so and starts open, its error showing: a nonzero exit, and sagent's is_error flag", async () => {
  const exited = card({
    call: claude.call,
    result: record(2, "ShellCommandResult", { call_id: "toolu_01", command: ["ls /nope"], stdout: "", stderr: "ls: cannot access '/nope'\n", exit_code: 2 }),
  });
  expect(exited.state()).toBe("failed");
  expect(exited.isOpen()).toBe(true);
  expect(exited.output()).toBe("ls: cannot access '/nope'\n");
  expect(exited.root.querySelector(".tr-step.failed")).not.toBeNull();
  cleanup();
  const flagged = card({
    call: record(3, "ToolCall", { call_id: "kb3", name: "ListJobs", arguments: {} }),
    result: record(4, "UncategorizedToolResult", { call_id: "kb3", content: "no such tool", extra: { is_error: true } }),
  });
  expect(flagged.state()).toBe("failed");
  expect(flagged.isOpen()).toBe(true);
  expect(flagged.output()).toBe("no such tool");
});

test("a call with no result yet is running, and opens to its arguments", async () => {
  const shown = card({ call: claude.call, result: null });
  expect([shown.name(), shown.target(), shown.state()]).toEqual(["Ran", "ls -la /home", "running"]);
  await shown.open();
  expect(shown.terms()).toEqual(["description"]);
  expect(shown.output()).toBeNull();
});

test("a long output is clipped to its head and tail, and the rest shows in place on request", async () => {
  const stdout = Array.from({ length: 100 }, (_, k) => `line ${k}`).join("\n");
  const shown = card({ call: claude.call, result: record(2, "ShellCommandResult", { call_id: "toolu_01", command: ["ls"], stdout, stderr: "", exit_code: 0 }) });
  await shown.open();
  expect(shown.output()).toContain("line 19");
  expect(shown.output()).not.toContain("line 20\n");
  fireEvent.click(within(shown.root).getByRole("button", { name: "Show 40 more lines" }));
  expect(shown.output()).toBe(stdout);
});
