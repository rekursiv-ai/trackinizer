import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, type Mock, test, vi } from "vitest";
import { ToastProvider } from "../ui/toast";
import type { Prompt } from "./chatLines";
import { type AnswerPrompt, PromptCard } from "./PromptCard";

afterEach(() => cleanup());

const permission = (fields: Partial<Prompt> = {}): Prompt => ({
  id: "p1", type: "permission", tool: "Bash", input: { command: "make test" }, questions: null, plan: null,
  cli: null, folder: null, machine: null, remembered: null, resolution: null, ...fields,
});

/** A claude or codex run waiting to start in an untrusted folder; `machine` null is the person's own machine. */
const trust = (fields: Partial<Prompt> = {}): Prompt => permission({
  id: "t1", type: "trust", tool: "claude", input: { folder: "~/proj" }, cli: "claude", folder: "~/proj", machine: "gx10", remembered: false, ...fields,
});

function card(prompt: Prompt, onAnswer?: AnswerPrompt) {
  return render(<ToastProvider><PromptCard prompt={prompt} kinds={[]} onAnswer={onAnswer} /></ToastProvider>);
}

/** The body and key of the answer the card posted as its `n`th. */
function posted(onAnswer: Mock<AnswerPrompt>, n = 0) {
  const [attempt] = onAnswer.mock.calls[n]!;
  return { body: attempt.body, key: attempt.key };
}

test("a permission shows its tool and input, and Yes posts the allow under a key", async () => {
  const onAnswer = vi.fn<AnswerPrompt>(async () => true);
  card(permission(), onAnswer);
  expect(screen.getByText("Claude wants to use Bash")).toBeTruthy();
  expect(document.querySelector(".prompt-card-input")!.textContent).toContain("make test");
  fireEvent.click(screen.getByRole("button", { name: "Yes" }));
  await waitFor(() => expect(onAnswer).toHaveBeenCalledOnce());
  expect(posted(onAnswer).body).toEqual({ text: "Yes", payload: { prompt_id: "p1", decision: "allow", updated_input: null, message: "" } });
  expect(posted(onAnswer).key).toEqual(expect.any(String));
});

test("No posts a deny with its comment as the message, and with no comment says only No", async () => {
  const onAnswer = vi.fn<AnswerPrompt>(async () => true);
  card(permission(), onAnswer);
  fireEvent.change(screen.getByLabelText("Comment"), { target: { value: "  too risky  " } });
  fireEvent.click(screen.getByRole("button", { name: "No" }));
  await waitFor(() => expect(onAnswer).toHaveBeenCalledOnce());
  expect(posted(onAnswer).body).toEqual({ text: "No: too risky", payload: { prompt_id: "p1", decision: "deny", updated_input: null, message: "too risky" } });
});

test("a permission input past twelve lines folds, and shows in full on request", () => {
  card(permission({ input: Object.fromEntries(Array.from({ length: 14 }, (_, at) => [`k${at}`, at])) }));
  const shown = () => document.querySelector(".prompt-card-input")!.textContent!.split("\n").length;
  expect(shown()).toBe(12);
  fireEvent.click(screen.getByRole("button", { name: "Show all 16 lines" }));
  expect(shown()).toBe(16);
});

test("a question's answers go under their headers, one label per pick, with Other as typed words", async () => {
  const onAnswer = vi.fn<AnswerPrompt>(async () => true);
  card(permission({ type: "question", tool: "AskUserQuestion", questions: [
    { question: "Which colors?", header: "Color", multiSelect: true, options: [{ label: "red", description: "" }, { label: "blue", description: "cool" }] },
    { question: "Which size?", header: "Size", multiSelect: false, options: [{ label: "S", description: "" }, { label: "L", description: "" }] },
  ] }), onAnswer);
  const send = screen.getByRole("button", { name: "Send answers" });
  expect(send).toHaveProperty("disabled", true);
  const colors = screen.getByRole("group", { name: "Color" });
  fireEvent.click(within(colors).getByRole("checkbox", { name: /^red/ }));
  fireEvent.click(within(colors).getByRole("checkbox", { name: /^blue/ }));
  expect(send).toHaveProperty("disabled", true);
  const sizes = screen.getByRole("group", { name: "Size" });
  fireEvent.click(within(sizes).getByRole("radio", { name: /^Other/ }));
  fireEvent.change(screen.getByRole("textbox", { name: "Other answer to Size" }), { target: { value: "XL" } });
  expect(send).toHaveProperty("disabled", false);
  fireEvent.click(send);
  await waitFor(() => expect(onAnswer).toHaveBeenCalledOnce());
  expect(posted(onAnswer).body).toEqual({
    text: "Color: red, blue; Size: XL",
    payload: { prompt_id: "p1", decision: "allow", updated_input: { answers: { Color: "red, blue", Size: "XL" } }, message: "" },
  });
});

test("a plan shows its text, asks for changes only with feedback, and approves only with the feedback box empty", async () => {
  // The first answer is refused, so the card is open for the second: a sent answer waits for the run's resolution.
  const onAnswer = vi.fn<AnswerPrompt>().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
  card(permission({ type: "plan", tool: "ExitPlanMode", input: null, plan: "Rename the module first." }), onAnswer);
  expect(screen.getByText("Rename the module first.")).toBeTruthy();
  const changes = screen.getByRole("button", { name: "Ask for changes" });
  const approve = screen.getByRole("button", { name: "Approve" });
  expect(changes).toHaveProperty("disabled", true);
  fireEvent.change(screen.getByLabelText("Feedback"), { target: { value: "split the step" } });
  expect(changes).toHaveProperty("disabled", false);
  expect(approve).toHaveProperty("disabled", true);
  fireEvent.click(changes);
  await waitFor(() => expect(changes).toHaveProperty("disabled", false));
  expect(posted(onAnswer).body).toEqual({ text: "Changes: split the step", payload: { prompt_id: "p1", decision: "deny", updated_input: null, message: "split the step" } });
  // With the feedback cleared, Approve sends no message, since the bridge would drop one on an allow.
  fireEvent.change(screen.getByLabelText("Feedback"), { target: { value: "" } });
  expect(approve).toHaveProperty("disabled", false);
  fireEvent.click(approve);
  await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(2));
  expect(posted(onAnswer, 1).body).toEqual({ text: "Approve", payload: { prompt_id: "p1", decision: "allow", updated_input: null, message: "" } });
  // Approval is a different answer from the refused denial, so it goes under a new key.
  expect(posted(onAnswer, 1).key).not.toBe(posted(onAnswer).key);
});

test("a refused answer is sent again under the same key, and the card is open again", async () => {
  const onAnswer = vi.fn<AnswerPrompt>().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
  card(permission(), onAnswer);
  const yes = screen.getByRole("button", { name: "Yes" });
  fireEvent.click(yes);
  await waitFor(() => expect(yes).toHaveProperty("disabled", false));
  fireEvent.click(yes);
  await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(2));
  expect(posted(onAnswer, 1).key).toBe(posted(onAnswer).key);
});

test("a resolved prompt collapses to one line that says how it was closed, and offers no answer", () => {
  const cases: [Prompt["resolution"], string][] = [
    [{ decision: "allow", by: "chat" }, "Allowed from Chat"],
    [{ decision: "deny", by: "terminal" }, "Denied in the terminal"],
    [{ decision: "answered", by: "chat" }, "Answered from Chat"],
    [{ decision: "released", by: "terminal" }, "Released"],
  ];
  for (const [resolution, words] of cases) {
    const { container, unmount } = card(permission({ resolution }), vi.fn<AnswerPrompt>(async () => true));
    expect(container.textContent).toBe(words);
    expect(screen.queryByRole("button")).toBeNull();
    unmount();
  }
});

test("without a way to answer, the buttons are off and the card says to answer in the terminal", () => {
  card(permission());
  expect(screen.getByRole("button", { name: "Yes" })).toHaveProperty("disabled", true);
  expect(screen.getByText("Answer it in the terminal.")).toBeTruthy();
});

test("a claude trust card names the CLI, machine and folder, and Trust posts the allow under a key", async () => {
  const onAnswer = vi.fn<AnswerPrompt>(async () => true);
  card(trust({ remembered: true }), onAnswer);
  expect(screen.getByText("Trust this folder?")).toBeTruthy();
  expect(screen.getByText("claude on gx10 is waiting to start in ~/proj.")).toBeTruthy();
  expect(screen.getByText("Claude Code has not been trusted there. Trusting lets it read, edit and run files in this folder, and lets the folder's own settings and hooks run.")).toBeTruthy();
  expect(screen.getByText("Claude Code remembers this for the folder.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Trust ~/proj" }));
  await waitFor(() => expect(onAnswer).toHaveBeenCalledOnce());
  expect(posted(onAnswer).body).toEqual({ text: "Trust ~/proj", payload: { prompt_id: "t1", decision: "allow", updated_input: null, message: "" } });
  expect(posted(onAnswer).key).toEqual(expect.any(String));
});

test("the home folder's trust card says Claude Code does not remember it there, and no machine means this one", () => {
  card(trust({ folder: "~", machine: null, remembered: false }));
  expect(screen.getByText("claude on your machine is waiting to start in ~.")).toBeTruthy();
  expect(screen.getByText("Claude Code does not remember trust for your home folder: it asks on every run there.")).toBeTruthy();
});

test("a codex trust card says what trusting lets codex do, and that codex remembers it for the folder", () => {
  card(trust({ cli: "codex", tool: "codex", folder: "~/repo", machine: "gx10", remembered: true }), vi.fn<AnswerPrompt>(async () => true));
  expect(screen.getByText("codex on gx10 is waiting to start in ~/repo.")).toBeTruthy();
  expect(screen.getByText("Codex has not been trusted there. Trusting lets it read, edit and run files here, and lets the folder's own config, hooks and exec policies run.")).toBeTruthy();
  expect(screen.getByText("Codex remembers this for the folder (for a git repository, its root).")).toBeTruthy();
});

test("Don't trust posts the deny under a key, and the card says the terminal answers too", async () => {
  const onAnswer = vi.fn<AnswerPrompt>(async () => true);
  card(trust(), onAnswer);
  expect(screen.getByText("Or answer in the terminal.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Don't trust: stop the run" }));
  await waitFor(() => expect(onAnswer).toHaveBeenCalledOnce());
  expect(posted(onAnswer).body).toEqual({ text: "Don't trust ~/proj", payload: { prompt_id: "t1", decision: "deny", updated_input: null, message: "" } });
});

test("a trust card with no way to answer has its buttons off and says to answer in the terminal", () => {
  card(trust());
  expect(screen.getByRole("button", { name: "Trust ~/proj" })).toHaveProperty("disabled", true);
  expect(screen.getByText("Answer it in the terminal.")).toBeTruthy();
});

test("a resolved trust card collapses to the line that says who decided", () => {
  const cases: [Prompt["resolution"], string][] = [
    [{ decision: "allow", by: "chat" }, "Trusted from Chat"],
    [{ decision: "allow", by: "terminal" }, "Trusted in the terminal"],
    [{ decision: "deny", by: "chat" }, "Not trusted: the run stopped"],
    [{ decision: "deny", by: "terminal" }, "Not trusted in the terminal: the run stopped"],
    [{ decision: "released", by: "run" }, "No longer waiting for trust"],
  ];
  for (const [resolution, words] of cases) {
    const { container, unmount } = card(trust({ resolution }), vi.fn<AnswerPrompt>(async () => true));
    expect(container.textContent).toBe(words);
    expect(screen.queryByRole("button")).toBeNull();
    unmount();
  }
});
