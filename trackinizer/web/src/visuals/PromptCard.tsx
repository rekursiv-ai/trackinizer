import { useRef, useState } from "react";
import type { AnswerPayload } from "../api/chats";
import { type Keyed, keyed } from "../api/idempotency";
import { Markdown } from "../markdown/Markdown";
import type { Prompt, PromptQuestion, Resolution } from "./chatLines";
import "./PromptCard.css";

/** What an answer says in the chat, and the decision the claude run takes from it. */
export type PromptAnswer = { readonly text: string; readonly payload: AnswerPayload };

/**
 * Post an answer under its key: true once the chat has queued it, false after a refusal
 * that Chat shows. A retry of the same answer goes under the same key.
 */
export type AnswerPrompt = (attempt: Keyed<PromptAnswer>) => Promise<boolean>;

/** A permission's input shows this many lines before it folds. */
const FOLD_LINES = 12;

/** The option that stands for a free-text "Other" answer. */
const OTHER = -1;

/** One question's picks: the option indexes chosen, and the words of Other. */
type Picked = { readonly chosen: readonly number[]; readonly other: string };

/**
 * A prompt a run asked, answered from Chat: a claude permission (Yes, or No with a
 * comment), a question (each question's options, or a free-text Other), a plan
 * (approve, or ask for changes), or a trust prompt (trust the folder the CLI waits to
 * start in, or stop the run). Once it is resolved it collapses to one line that says how.
 */
export function PromptCard({ prompt, kinds, onAnswer }: {
  readonly prompt: Prompt;
  readonly kinds: readonly string[];
  /** Absent where Chat cannot answer the run now; the card then says to answer it in the terminal. */
  readonly onAnswer?: AnswerPrompt;
}) {
  if (prompt.resolution) {
    return <article className="chat-line chat-line-interaction prompt-card prompt-card-resolved"><p className="chat-line-body">{resolvedWords(prompt, prompt.resolution)}</p></article>;
  }
  return <OpenPrompt prompt={prompt} kinds={kinds} onAnswer={onAnswer} />;
}

function OpenPrompt({ prompt, kinds, onAnswer }: {
  readonly prompt: Prompt;
  readonly kinds: readonly string[];
  readonly onAnswer?: AnswerPrompt;
}) {
  const [busy, setBusy] = useState(false);
  // The last answer sent. Sent again unchanged, it keeps its key, so the server queues it once.
  const attempt = useRef<Keyed<PromptAnswer> | null>(null);
  const answerable = onAnswer !== undefined;
  async function submit(answer: PromptAnswer) {
    if (!onAnswer) return;
    const held = attempt.current;
    const next = held && JSON.stringify(held.body) === JSON.stringify(answer) ? held : keyed(answer);
    attempt.current = next;
    setBusy(true);
    // Sent, the card stays busy until the run's resolution arrives and collapses it.
    if (!(await onAnswer(next))) setBusy(false);
  }
  const disabled = busy || !answerable;
  return <article className={`chat-line chat-line-interaction prompt-card prompt-card-${prompt.type}`} aria-busy={busy}>
    {prompt.type === "question" && <Questions prompt={prompt} busy={disabled} submit={submit} />}
    {prompt.type === "plan" && <Plan prompt={prompt} kinds={kinds} busy={disabled} submit={submit} />}
    {prompt.type === "permission" && <Permission prompt={prompt} busy={disabled} submit={submit} />}
    {prompt.type === "trust" && <Trust prompt={prompt} busy={disabled} submit={submit} />}
    {prompt.type === "trust" && answerable && <p className="chat-note">Or answer in the terminal.</p>}
    {!answerable && <p className="chat-note">Answer it in the terminal.</p>}
  </article>;
}

/**
 * A CLI waiting to start in a folder it does not trust yet. Trust lets the CLI run the folder's
 * own settings and hooks, so the words say what that allows and whether it is remembered; the
 * answer is the person's own.
 */
function Trust({ prompt, busy, submit }: {
  readonly prompt: Prompt;
  readonly busy: boolean;
  readonly submit: (answer: PromptAnswer) => Promise<void>;
}) {
  const folder = prompt.folder ?? "";
  const codex = prompt.cli === "codex";
  const cli = codex ? "codex" : "claude";
  const where = prompt.machine ? `on ${prompt.machine}` : "on your machine";
  return <>
    <p className="prompt-card-title">Trust this folder?</p>
    <p className="prompt-card-trust">{`${cli} ${where} is waiting to start in ${folder}.`}</p>
    <p className="prompt-card-trust">{codex
      ? "Codex has not been trusted there. Trusting lets it read, edit and run files here, and lets the folder's own config, hooks and exec policies run."
      : "Claude Code has not been trusted there. Trusting lets it read, edit and run files in this folder, and lets the folder's own settings and hooks run."}</p>
    <p className="prompt-card-trust">{trustMemory(prompt)}</p>
    <div className="prompt-card-actions">
      <button className="btn primary" type="button" disabled={busy}
        onClick={() => void submit({ text: `Trust ${folder}`, payload: decided(prompt, "allow", "") })}>{`Trust ${folder}`}</button>
      <button className="btn" type="button" disabled={busy}
        onClick={() => void submit({ text: `Don't trust ${folder}`, payload: decided(prompt, "deny", "") })}>{"Don't trust: stop the run"}</button>
    </div>
  </>;
}

/** Whether the CLI keeps the trust, in its own words: Claude never keeps it for the home folder. */
function trustMemory(prompt: Prompt): string {
  if (prompt.cli === "codex") return "Codex remembers this for the folder (for a git repository, its root).";
  return prompt.remembered
    ? "Claude Code remembers this for the folder."
    : "Claude Code does not remember trust for your home folder: it asks on every run there.";
}

function Permission({ prompt, busy, submit }: {
  readonly prompt: Prompt;
  readonly busy: boolean;
  readonly submit: (answer: PromptAnswer) => Promise<void>;
}) {
  const [comment, setComment] = useState("");
  const [unfolded, setUnfolded] = useState(false);
  const shown = JSON.stringify(prompt.input, null, 2).split("\n");
  const folded = shown.length > FOLD_LINES && !unfolded;
  const words = comment.trim();
  return <>
    <p className="prompt-card-title">Claude wants to use {prompt.tool}</p>
    <pre className="prompt-card-input">{(folded ? shown.slice(0, FOLD_LINES) : shown).join("\n")}</pre>
    {shown.length > FOLD_LINES && <button className="btn ghost" type="button" onClick={() => setUnfolded(!unfolded)}>
      {folded ? `Show all ${shown.length} lines` : "Fold the input"}</button>}
    <label className="prompt-card-comment">Comment
      <textarea rows={2} value={comment} disabled={busy} onChange={(event) => setComment(event.target.value)} />
    </label>
    <div className="prompt-card-actions">
      <button className="btn" type="button" disabled={busy} onClick={() => void submit({ text: "Yes", payload: decided(prompt, "allow", "") })}>Yes</button>
      <button className="btn" type="button" disabled={busy}
        onClick={() => void submit({ text: words ? `No: ${words}` : "No", payload: decided(prompt, "deny", words) })}>No</button>
    </div>
  </>;
}

function Questions({ prompt, busy, submit }: {
  readonly prompt: Prompt;
  readonly busy: boolean;
  readonly submit: (answer: PromptAnswer) => Promise<void>;
}) {
  const questions = prompt.questions ?? [];
  const [picked, setPicked] = useState<readonly Picked[]>(() => questions.map(() => ({ chosen: [], other: "" })));
  const answer = answerSet(questions, picked);
  function pick(index: number, option: number, multiSelect: boolean) {
    setPicked((held) => held.map((each, at) => (at === index
      ? { ...each, chosen: multiSelect ? toggle(each.chosen, option) : [option] }
      : each)));
  }
  function write(index: number, other: string) {
    setPicked((held) => held.map((each, at) => (at === index ? { ...each, other } : each)));
  }
  return <>
    <p className="prompt-card-title">{questions.length === 1 ? "Claude has a question" : "Claude has questions"}</p>
    {questions.map((question, index) => {
      const { chosen, other } = picked[index];
      const type = question.multiSelect ? "checkbox" : "radio";
      const name = `${prompt.id}-${index}`;
      return <fieldset key={index} className="prompt-card-question" disabled={busy}>
        <legend>{question.header}</legend>
        {question.question !== question.header && <p className="chat-note">{question.question}</p>}
        {question.options.map((option, at) => <label key={at}>
          <input type={type} name={name} checked={chosen.includes(at)} onChange={() => pick(index, at, question.multiSelect)} />
          <span>{option.label}</span>
          {option.description && <small>{option.description}</small>}
        </label>)}
        <label>
          <input type={type} name={name} checked={chosen.includes(OTHER)} onChange={() => pick(index, OTHER, question.multiSelect)} />
          <span>Other</span>
          <input type="text" aria-label={`Other answer to ${question.header}`} value={other} disabled={!chosen.includes(OTHER)}
            onChange={(event) => write(index, event.target.value)} />
        </label>
      </fieldset>;
    })}
    <div className="prompt-card-actions">
      <button className="btn" type="button" disabled={busy || answer === null}
        onClick={() => answer && void submit({ text: answer.text, payload: decided(prompt, "allow", "", { answers: answer.answers }) })}>Send answers</button>
    </div>
  </>;
}

function Plan({ prompt, kinds, busy, submit }: {
  readonly prompt: Prompt;
  readonly kinds: readonly string[];
  readonly busy: boolean;
  readonly submit: (answer: PromptAnswer) => Promise<void>;
}) {
  const [feedback, setFeedback] = useState("");
  const words = feedback.trim();
  // The bridge reads a message only on a denial, so feedback travels with Ask for changes alone;
  // Approve waits until the feedback box is empty rather than drop the words it was given.
  return <>
    <p className="prompt-card-title">Claude's plan is ready for approval</p>
    <Markdown source={prompt.plan ?? ""} kinds={kinds} className="md prompt-card-plan" images={false} />
    <label className="prompt-card-comment">Feedback
      <textarea rows={2} value={feedback} disabled={busy} onChange={(event) => setFeedback(event.target.value)} />
    </label>
    <div className="prompt-card-actions">
      <button className="btn" type="button" disabled={busy || words !== ""}
        onClick={() => void submit({ text: "Approve", payload: decided(prompt, "allow", "") })}>Approve</button>
      <button className="btn" type="button" disabled={busy || words === ""}
        onClick={() => void submit({ text: `Changes: ${words}`, payload: decided(prompt, "deny", words) })}>Ask for changes</button>
    </div>
  </>;
}

/** The payload that decides `prompt`: its id, the decision, the words and, for a question, the answers. */
function decided(prompt: Prompt, decision: "allow" | "deny", message: string, updatedInput: AnswerPayload["updated_input"] = null): AnswerPayload {
  return { prompt_id: prompt.id, decision, updated_input: updatedInput, message };
}

/** The answers to every question, keyed by header, with the one-line summary of them; null while one has none. */
function answerSet(questions: readonly PromptQuestion[], picked: readonly Picked[]): { readonly text: string; readonly answers: { readonly [header: string]: string } } | null {
  const pairs: [string, string][] = [];
  for (const [index, question] of questions.entries()) {
    const value = valueOf(question, picked[index]);
    if (value === null) return null;
    pairs.push([question.header, value]);
  }
  return { text: pairs.map(([header, value]) => `${header}: ${value}`).join("; "), answers: Object.fromEntries(pairs) };
}

/** What a question's picks say, label after label; null while nothing is picked, or Other is picked with no words. */
function valueOf(question: PromptQuestion, picked: Picked): string | null {
  const labels = picked.chosen
    .map((option) => (option === OTHER ? picked.other.trim() : (question.options[option]?.label ?? "")))
    .filter((label) => label !== "");
  return picked.chosen.length > 0 && labels.length === picked.chosen.length ? labels.join(", ") : null;
}

function toggle(chosen: readonly number[], option: number): number[] {
  return chosen.includes(option) ? chosen.filter((each) => each !== option) : [...chosen, option];
}

/** The line a resolved prompt collapses to: what was decided, and from where. */
function resolvedWords(prompt: Prompt, { decision, by }: Resolution): string {
  if (prompt.type === "trust") return trustWords(decision, by);
  if (decision === "released") return "Released";
  const verb = decision === "allow" ? "Allowed" : decision === "deny" ? "Denied" : "Answered";
  const where = by === "chat" ? " from Chat" : by === "terminal" ? " in the terminal" : "";
  return `${verb}${where}`;
}

/** A trust prompt's resolution: trusted or not, and whether Chat or the terminal said so; the run stops on a refusal. */
function trustWords(decision: Resolution["decision"], by: Resolution["by"]): string {
  if (decision === "released") return "No longer waiting for trust";
  if (decision === "allow") return by === "chat" ? "Trusted from Chat" : "Trusted in the terminal";
  return by === "chat" ? "Not trusted: the run stopped" : "Not trusted in the terminal: the run stopped";
}
