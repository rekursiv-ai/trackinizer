import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, type Mock, test, vi } from "vitest";
import { ToastProvider } from "../ui/toast";
import type { AskCard, Prompt, PromptOption } from "./chatLines";
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

function card(prompt: Prompt, onAnswer?: AnswerPrompt, assistant?: string) {
  return render(<ToastProvider><PromptCard prompt={prompt} kinds={[]} onAnswer={onAnswer} assistant={assistant} /></ToastProvider>);
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

// The unified form: the card offers the agent's own options, and sends the one taken by its id.
const option = (id: string, label: string, kind: PromptOption["kind"]): PromptOption => ({ id, label, kind });
const YES_NO = [option("allow_once", "Yes", "allow_once"), option("allow_session", "Yes, for this session", "allow_session"), option("deny", "No", "deny")];

/** A codex command ask unless the fields say otherwise. */
const asked = (ask: Partial<AskCard> = {}, fields: Partial<Prompt> = {}): Prompt => permission({
  tool: "shell", input: { command: "make test", cwd: "/work" }, ...fields,
  ask: { provider: "codex", kind: "command", title: "Run a command", toolCallId: "", command: "make test", cwd: "/work", reason: "", diff: "", options: YES_NO, answerable: true, ...ask },
});

test("a command ask names its provider, shows the command with its folder and reason, and each option is a button", async () => {
  const onAnswer = vi.fn<AnswerPrompt>(async () => true);
  card(asked({ reason: "to run the suite" }), onAnswer);
  expect(screen.getByText("Codex wants to run a command")).toBeTruthy();
  expect(document.querySelector(".prompt-card-input")!.textContent).toBe("make test");
  expect(screen.getByText("in /work")).toBeTruthy();
  expect(screen.getByText("to run the suite")).toBeTruthy();
  expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual(["Yes", "Yes, for this session", "No"]);
  fireEvent.click(screen.getByRole("button", { name: "Yes, for this session" }));
  await waitFor(() => expect(onAnswer).toHaveBeenCalledOnce());
  expect(posted(onAnswer).body).toEqual({ text: "Yes, for this session", payload: { prompt_id: "p1", option: "allow_session", updated_input: null, message: "" } });
});

test("a comment goes with whichever option is taken: a denial's reason, and the words said with an allow", async () => {
  // The first answer is refused, so the card is open for the second.
  const onAnswer = vi.fn<AnswerPrompt>().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
  const stop = [...YES_NO, option("deny_stop", "No, and stop", "deny_stop")];
  card(asked({ options: stop }), onAnswer);
  fireEvent.change(screen.getByLabelText("Comment"), { target: { value: " not here " } });
  fireEvent.click(screen.getByRole("button", { name: "No, and stop" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Yes" })).toHaveProperty("disabled", false));
  expect(onAnswer).toHaveBeenCalledOnce();
  expect(posted(onAnswer).body).toEqual({ text: "No, and stop: not here", payload: { prompt_id: "p1", option: "deny_stop", updated_input: null, message: "not here" } });
  fireEvent.click(screen.getByRole("button", { name: "Yes" }));
  await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(2));
  expect(posted(onAnswer, 1).body.payload).toEqual({ prompt_id: "p1", option: "allow_once", updated_input: null, message: "not here" });
});

test("a file change shows its diff, folded past twelve lines, in place of the tool's input", () => {
  const diff = Array.from({ length: 15 }, (_, at) => `+line ${at}`).join("\n");
  card(asked({ kind: "file_change", command: "", cwd: "", diff }, { tool: "apply_patch", input: { grant_root: "/work" } }));
  expect(screen.getByText("Codex wants to change files")).toBeTruthy();
  const shown = () => document.querySelector(".prompt-card-diff")!.textContent!.split("\n").length;
  expect(shown()).toBe(12);
  expect(document.querySelector(".prompt-card-input")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Show all 15 lines" }));
  expect(shown()).toBe(15);
});

test("a tool permission shows its tool and, when the agent gives no command or diff, its input", () => {
  card(asked({ provider: "claude", kind: "permission", command: "", cwd: "", title: "Claude wants to use Read" }, { tool: "Read", input: { file_path: "/x" } }));
  expect(screen.getByText("Claude wants to use Read")).toBeTruthy();
  expect(document.querySelector(".prompt-card-input")!.textContent).toContain("/x");
});

test("the assistant's ask is titled with the assistant's own name, and a kind with no body of its own uses the agent's words", () => {
  card(asked({ provider: "sagent", kind: "permission", command: "", cwd: "" }, { tool: "Launch", input: {} }), undefined, "scout");
  expect(screen.getByText("scout wants to use Launch")).toBeTruthy();
  cleanup();
  card(asked({ kind: "elicitation", title: "Pick a branch", command: "", cwd: "" }, { input: { server: "git" } }));
  expect(screen.getByText("Codex asks: Pick a branch")).toBeTruthy();
  cleanup();
  card(asked({ kind: "permissions", command: "", cwd: "" }, { tool: "request_permissions", input: { permissions: { network: true } } }));
  expect(screen.getByText("Codex asks for more access")).toBeTruthy();
});

test("an ask only the terminal can answer has its buttons off and says to answer there", () => {
  card(asked({ answerable: false }), vi.fn<AnswerPrompt>(async () => true));
  for (const button of screen.getAllByRole("button")) expect(button).toHaveProperty("disabled", true);
  expect(screen.getByText("Answer it in the terminal.")).toBeTruthy();
});

const QUESTIONS = [{ question: "Which size?", header: "Size", multiSelect: false, options: [{ label: "S", description: "" }, { label: "L", description: "" }] }];

test("a question offered Answer and Skip sends its answers with the first and a bare Skip with the second", async () => {
  const onAnswer = vi.fn<AnswerPrompt>().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
  const question = asked({ provider: "claude", kind: "question", command: "", cwd: "", options: [option("allow_once", "Answer", "allow_once"), option("deny", "Skip", "deny")] },
    { type: "question", tool: "AskUserQuestion", questions: QUESTIONS });
  card(question, onAnswer);
  expect(screen.getByText("Claude has a question")).toBeTruthy();
  const send = screen.getByRole("button", { name: "Answer" });
  expect(send).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "Skip" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Skip" })).toHaveProperty("disabled", false));
  expect(onAnswer).toHaveBeenCalledOnce();
  expect(posted(onAnswer).body).toEqual({ text: "Skip", payload: { prompt_id: "p1", option: "deny", updated_input: null, message: "" } });
  fireEvent.click(within(screen.getByRole("group", { name: "Size" })).getByRole("radio", { name: /^L/ }));
  fireEvent.click(send);
  await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(2));
  expect(posted(onAnswer, 1).body).toEqual({ text: "Size: L", payload: { prompt_id: "p1", option: "allow_once", updated_input: { answers: { Size: "L" } }, message: "" } });
});

test("a question that wants words has a box for them, and its answer is keyed by the agent's id for it", async () => {
  const onAnswer = vi.fn<AnswerPrompt>(async () => true);
  card(asked({ kind: "question", command: "", cwd: "", options: [option("answer", "Send answers", "allow_once")] },
    { type: "question", tool: "request_user_input", questions: [{ id: "dir", question: "Which folder?", header: "Which folder?", multiSelect: false, options: [] }] }), onAnswer);
  expect(screen.queryByRole("radio")).toBeNull();
  const send = screen.getByRole("button", { name: "Send answers" });
  expect(send).toHaveProperty("disabled", true);
  fireEvent.change(screen.getByRole("textbox", { name: "Answer to Which folder?" }), { target: { value: "src" } });
  fireEvent.click(send);
  await waitFor(() => expect(onAnswer).toHaveBeenCalledOnce());
  expect(posted(onAnswer).body).toEqual({ text: "Which folder?: src", payload: { prompt_id: "p1", option: "answer", updated_input: { answers: { dir: "src" } }, message: "" } });
});

test("a secret question takes its words hidden, and the line says none of them", async () => {
  const onAnswer = vi.fn<AnswerPrompt>(async () => true);
  card(asked({ kind: "question", command: "", cwd: "", options: [option("answer", "Send answers", "allow_once")] },
    { type: "question", tool: "request_user_input", questions: [{ id: "tok", question: "API token?", header: "Token", multiSelect: false, options: [], secret: true }] }), onAnswer);
  const box = screen.getByLabelText("Answer to Token");
  expect(box.getAttribute("type")).toBe("password");
  fireEvent.change(box, { target: { value: "s3cret" } });
  fireEvent.click(screen.getByRole("button", { name: "Send answers" }));
  await waitFor(() => expect(onAnswer).toHaveBeenCalledOnce());
  expect(posted(onAnswer).body).toEqual({ text: "Token: (hidden)", payload: { prompt_id: "p1", option: "answer", updated_input: { answers: { tok: "s3cret" } }, message: "" } });
});

test("a question that offers several choices is a button for each, and a click answers with that option", async () => {
  const onAnswer = vi.fn<AnswerPrompt>(async () => true);
  const choices = ["gx10", "gpu-b", "laptop"].map((label, at) => option(String(at + 1), label, "allow_once"));
  card(asked({ provider: "sagent", kind: "question", title: "Which machine?", command: "", cwd: "", options: choices },
    { type: "question", tool: "AskUserQuestion", questions: [{ question: "Which machine?", header: "Machine", multiSelect: false,
      options: choices.map(({ label }) => ({ label, description: "" })) }] }), onAnswer, "scout");
  expect(screen.getByText("scout has a question")).toBeTruthy();
  expect(screen.getByText("Which machine?")).toBeTruthy();
  expect(screen.queryByRole("radio")).toBeNull();
  expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual(["gx10", "gpu-b", "laptop"]);
  fireEvent.click(screen.getByRole("button", { name: "gpu-b" }));
  await waitFor(() => expect(onAnswer).toHaveBeenCalledOnce());
  expect(posted(onAnswer).body).toEqual({ text: "gpu-b", payload: { prompt_id: "p1", option: "2", updated_input: null, message: "" } });
});

test("a plan offered options approves with the first and keeps planning with the second; feedback blocks only the approval", async () => {
  const onAnswer = vi.fn<AnswerPrompt>(async () => true);
  card(asked({ provider: "claude", kind: "plan", command: "", cwd: "", options: [option("allow_once", "Approve plan", "allow_once"), option("deny", "Keep planning", "deny")] },
    { type: "plan", tool: "ExitPlanMode", input: null, plan: "Rename the module first." }), onAnswer);
  expect(screen.getByText("Claude's plan is ready for approval")).toBeTruthy();
  const keep = screen.getByRole("button", { name: "Keep planning" });
  const approve = screen.getByRole("button", { name: "Approve plan" });
  expect(keep).toHaveProperty("disabled", false);
  fireEvent.change(screen.getByLabelText("Feedback"), { target: { value: "split the step" } });
  expect(approve).toHaveProperty("disabled", true);
  fireEvent.click(keep);
  await waitFor(() => expect(onAnswer).toHaveBeenCalledOnce());
  expect(posted(onAnswer).body).toEqual({ text: "Keep planning: split the step", payload: { prompt_id: "p1", option: "deny", updated_input: null, message: "split the step" } });
});

test("a resolved ask says which option was taken and how it ended", () => {
  const choices = ["gx10", "gpu-b"].map((label, at) => option(String(at + 1), label, "allow_once"));
  const stop = [...YES_NO, option("deny_stop", "No, and stop", "deny_stop"), option("allow_always", "Always", "allow_always")];
  const cases: [Prompt, string][] = [
    [asked({ options: stop }, { resolution: { decision: "allow", by: "chat", option: "allow_session", who: "ada@example.com" } }), "Allowed for this session from Chat"],
    [asked({ options: stop }, { resolution: { decision: "allow", by: "chat", option: "allow_always" } }), "Allowed always from Chat"],
    [asked({ options: stop }, { resolution: { decision: "deny", by: "chat", option: "deny_stop" } }), "Denied and stopped from Chat"],
    [asked({ options: stop }, { resolution: { decision: "deny", by: "terminal", option: "deny" } }), "Denied in the terminal"],
    [asked({ options: stop }, { resolution: { decision: "released", by: "timeout" } }), "Released"],
    [asked({ options: stop }, { resolution: { decision: "released", by: "released" } }), "Released"],
    [asked({ kind: "question", options: [option("allow_once", "Answer", "allow_once"), option("deny", "Skip", "deny")] }, { type: "question", questions: QUESTIONS,
      resolution: { decision: "deny", by: "chat", option: "deny" } }), "Skipped from Chat"],
    [asked({ kind: "question", options: choices }, { type: "question", questions: QUESTIONS, resolution: { decision: "answered", by: "chat", option: "2" } }), "Chose gpu-b from Chat"],
    [asked({ kind: "question", options: choices }, { type: "question", questions: QUESTIONS, resolution: { decision: "answered", by: "chat", option: "" } }), "Answered from Chat"],
  ];
  for (const [prompt, words] of cases) {
    const { container, unmount } = card(prompt, vi.fn<AnswerPrompt>(async () => true));
    expect(container.textContent).toBe(words);
    unmount();
  }
});

test("a card attached to a tool call is marked, so it sits flush under the call's card", () => {
  const { container } = render(<ToastProvider><PromptCard prompt={asked()} kinds={[]} attached /></ToastProvider>);
  expect(container.querySelector("article")!.className).toContain("prompt-card-attached");
});
