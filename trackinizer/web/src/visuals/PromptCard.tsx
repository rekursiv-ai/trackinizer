import { useRef, useState } from "react";
import type { AnswerPayload } from "../api/chats";
import { type Keyed, keyed } from "../api/idempotency";
import { Markdown } from "../markdown/Markdown";
import type { Prompt, PromptOption, PromptQuestion, Resolution } from "./chatLines";
import "./PromptCard.css";

/** What an answer says in the chat, and the choice the run takes from it. */
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

/** What a button sends: the agent's option by id, or, on a card from before options, the old allow or deny. */
type Choice = { readonly option: string } | { readonly decision: "allow" | "deny" };

/** One button of a card: its words, whether it refuses, and what it sends. */
type Action = {
  readonly label: string;
  readonly deny: boolean;
  readonly choice: Choice;
  /** What the chat says for a refusal with words, when it is not the label. */
  readonly said?: string;
};

type Submit = (answer: PromptAnswer) => Promise<void>;

/**
 * A prompt a run asked, answered from Chat: a permission, a command, a file change, a
 * question (each question's options, a box for words, or the agent's choices as buttons),
 * a plan (approve, or ask for changes), or a trust prompt (trust the folder the CLI waits
 * to start in, or stop the run). Its buttons are the agent's own options when the record
 * lists them, else the two answers a claude run always offered. Once it is resolved it
 * collapses to one line that says how. `assistant` is what the assistant is called here,
 * for the asks it makes itself; `attached` sits the card flush under its tool call's card.
 */
export function PromptCard({ prompt, kinds, onAnswer, assistant = "The assistant", attached = false }: {
  readonly prompt: Prompt;
  readonly kinds: readonly string[];
  /** Absent where Chat cannot answer the run now; the card then says to answer it in the terminal. */
  readonly onAnswer?: AnswerPrompt;
  readonly assistant?: string;
  readonly attached?: boolean;
}) {
  const under = attached ? " prompt-card-attached" : "";
  if (prompt.resolution) {
    return <article className={`chat-line chat-line-interaction prompt-card prompt-card-resolved${under}`}><p className="chat-line-body">{resolvedWords(prompt, prompt.resolution)}</p></article>;
  }
  return <OpenPrompt prompt={prompt} kinds={kinds} onAnswer={onAnswer} assistant={assistant} under={under} />;
}

function OpenPrompt({ prompt, kinds, onAnswer, assistant, under }: {
  readonly prompt: Prompt;
  readonly kinds: readonly string[];
  readonly onAnswer?: AnswerPrompt;
  readonly assistant: string;
  readonly under: string;
}) {
  const [busy, setBusy] = useState(false);
  // The last answer sent. Sent again unchanged, it keeps its key, so the server queues it once.
  const attempt = useRef<Keyed<PromptAnswer> | null>(null);
  // An ask the agent says only its terminal can answer is answered there, whatever Chat could post.
  const answerable = onAnswer !== undefined && prompt.ask?.answerable !== false;
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
  const who = nameOf(prompt, assistant);
  return <article className={`chat-line chat-line-interaction prompt-card prompt-card-${prompt.type}${under}`} aria-busy={busy}>
    {prompt.type === "question" && (offersChoices(prompt)
      ? <Choices prompt={prompt} who={who} busy={disabled} submit={submit} />
      : <Questions prompt={prompt} who={who} busy={disabled} submit={submit} />)}
    {prompt.type === "plan" && <Plan prompt={prompt} who={who} kinds={kinds} busy={disabled} submit={submit} />}
    {prompt.type === "permission" && <Permission prompt={prompt} who={who} busy={disabled} submit={submit} />}
    {prompt.type === "trust" && <Trust prompt={prompt} busy={disabled} submit={submit} />}
    {prompt.type === "trust" && answerable && <p className="chat-note">Or answer in the terminal.</p>}
    {!answerable && <p className="chat-note">Answer it in the terminal.</p>}
  </article>;
}

/** Who asks, as the card names it: an older record is a claude run's, and the assistant is called what the server calls it. */
function nameOf(prompt: Prompt, assistant: string): string {
  const provider = prompt.ask?.provider ?? "claude";
  return provider === "claude" ? "Claude" : provider === "codex" ? "Codex" : assistant;
}

/** The card's heading, by what is asked and who asks. */
function titleOf(prompt: Prompt, who: string): string {
  if (prompt.type === "question") return `${who} has ${(prompt.questions?.length ?? 0) === 1 ? "a question" : "questions"}`;
  if (prompt.type === "plan") return `${who}'s plan is ready for approval`;
  const ask = prompt.ask;
  switch (ask?.kind) {
    case "command": return `${who} wants to run a command`;
    case "file_change": return `${who} wants to change files`;
    case "permissions": return `${who} asks for more access`;
    // These are asked in the agent's own words.
    case "elicitation":
    case "dialog":
    case "trust": return ask.title ? `${who} asks: ${ask.title}` : `${who} asks for an answer`;
    default: return `${who} wants to use ${prompt.tool || "a tool"}`;
  }
}

/** The buttons of a card: the agent's options, else the old pair (`no` null where the old card had no refusal). */
function actionsOf(prompt: Prompt, yes: string, no: string | null, said?: string): Action[] {
  if (prompt.ask) return prompt.ask.options.map((each) => ({ label: each.label, deny: refuses(each), choice: { option: each.id } }));
  return [
    { label: yes, deny: false, choice: { decision: "allow" } },
    ...(no === null ? [] : [{ label: no, deny: true, choice: { decision: "deny" } as const, ...(said && { said }) }]),
  ];
}

function refuses(option: PromptOption): boolean {
  return option.kind === "deny" || option.kind === "deny_stop";
}

/**
 * The payload that answers `prompt` with `action`: its id, the choice, the words and, for a
 * question, the answers. A card from before options sent words with a refusal only, since the
 * bridge dropped them on an allow; a card of the unified form sends them with any option.
 */
function payloadOf(prompt: Prompt, action: Action, words: string, updatedInput: AnswerPayload["updated_input"] = null): AnswerPayload {
  const message = action.deny || prompt.ask ? words : "";
  return { prompt_id: prompt.id, ...action.choice, updated_input: updatedInput, message };
}

/** What the chat says for `action` taken with `words`. */
function saying(prompt: Prompt, action: Action, words: string): string {
  return words && (action.deny || prompt.ask) ? `${action.said ?? action.label}: ${words}` : action.label;
}

/** Whether a question's agent offers several choices to click, each an option of its own, rather than a form to fill. */
function offersChoices(prompt: Prompt): boolean {
  return (prompt.ask?.options.filter((each) => !refuses(each)).length ?? 0) > 1;
}

/**
 * A CLI waiting to start in a folder it does not trust yet. Trust lets the CLI run the folder's
 * own settings and hooks, so the words say what that allows and whether it is remembered; the
 * answer is the person's own.
 */
function Trust({ prompt, busy, submit }: { readonly prompt: Prompt; readonly busy: boolean; readonly submit: Submit }) {
  const folder = prompt.folder ?? "";
  const codex = prompt.cli === "codex";
  const cli = codex ? "codex" : "claude";
  const where = prompt.machine ? `on ${prompt.machine}` : "on your machine";
  const actions = actionsOf(prompt, `Trust ${folder}`, "Don't trust: stop the run");
  const trust = actions.find((each) => !each.deny);
  const distrust = actions.find((each) => each.deny);
  return <>
    <p className="prompt-card-title">Trust this folder?</p>
    <p className="prompt-card-trust">{`${cli} ${where} is waiting to start in ${folder}.`}</p>
    <p className="prompt-card-trust">{codex
      ? "Codex has not been trusted there. Trusting lets it read, edit and run files here, and lets the folder's own config, hooks and exec policies run."
      : "Claude Code has not been trusted there. Trusting lets it read, edit and run files in this folder, and lets the folder's own settings and hooks run."}</p>
    <p className="prompt-card-trust">{trustMemory(prompt)}</p>
    <div className="prompt-card-actions">
      {trust && <button className="btn primary" type="button" disabled={busy}
        onClick={() => void submit({ text: `Trust ${folder}`, payload: payloadOf(prompt, trust, "") })}>{`Trust ${folder}`}</button>}
      {distrust && <button className="btn" type="button" disabled={busy}
        onClick={() => void submit({ text: `Don't trust ${folder}`, payload: payloadOf(prompt, distrust, "") })}>{"Don't trust: stop the run"}</button>}
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

/** Text that shows its first lines, and the rest on request. */
function Folded({ text, className }: { readonly text: string; readonly className: string }) {
  const [unfolded, setUnfolded] = useState(false);
  const shown = text.split("\n");
  const folded = shown.length > FOLD_LINES && !unfolded;
  return <>
    <pre className={className}>{(folded ? shown.slice(0, FOLD_LINES) : shown).join("\n")}</pre>
    {shown.length > FOLD_LINES && <button className="btn ghost" type="button" onClick={() => setUnfolded(!unfolded)}>
      {folded ? `Show all ${shown.length} lines` : "Fold the input"}</button>}
  </>;
}

/** What a permission is for: the command or the diff when the agent gives one, else the tool's input; and where, and why. */
function Detail({ prompt }: { readonly prompt: Prompt }) {
  const ask = prompt.ask;
  const input = JSON.stringify(prompt.input, null, 2);
  // An old card always showed the input; one of the unified form shows it when it has nothing better.
  const bare = ask !== undefined && (ask.command !== "" || ask.diff !== "" || input === "{}" || input === "null");
  return <>
    {ask?.command && <Folded text={ask.command} className="prompt-card-input" />}
    {ask?.diff && <Folded text={ask.diff} className="prompt-card-diff" />}
    {!bare && <Folded text={input} className="prompt-card-input" />}
    {ask?.cwd && <p className="prompt-card-meta">in {ask.cwd}</p>}
    {ask?.reason && <p className="prompt-card-meta">{ask.reason}</p>}
  </>;
}

function Permission({ prompt, who, busy, submit }: { readonly prompt: Prompt; readonly who: string; readonly busy: boolean; readonly submit: Submit }) {
  const [comment, setComment] = useState("");
  const words = comment.trim();
  return <>
    <p className="prompt-card-title">{titleOf(prompt, who)}</p>
    <Detail prompt={prompt} />
    <label className="prompt-card-comment">Comment
      <textarea rows={2} value={comment} disabled={busy} onChange={(event) => setComment(event.target.value)} />
    </label>
    <div className="prompt-card-actions">
      {actionsOf(prompt, "Yes", "No").map((action) => <button key={action.label} className="btn" type="button" disabled={busy}
        onClick={() => void submit({ text: saying(prompt, action, words), payload: payloadOf(prompt, action, words) })}>{action.label}</button>)}
    </div>
  </>;
}

/** A question whose agent offers several choices: each is a button, and a click is the answer. */
function Choices({ prompt, who, busy, submit }: { readonly prompt: Prompt; readonly who: string; readonly busy: boolean; readonly submit: Submit }) {
  return <>
    <p className="prompt-card-title">{titleOf(prompt, who)}</p>
    {(prompt.questions ?? []).map((question, index) => <p key={index} className="prompt-card-trust">{question.question}</p>)}
    <div className="prompt-card-actions">
      {actionsOf(prompt, "", null).map((action) => <button key={action.label} className="btn" type="button" disabled={busy}
        onClick={() => void submit({ text: action.label, payload: payloadOf(prompt, action, "") })}>{action.label}</button>)}
    </div>
  </>;
}

function Questions({ prompt, who, busy, submit }: { readonly prompt: Prompt; readonly who: string; readonly busy: boolean; readonly submit: Submit }) {
  const questions = prompt.questions ?? [];
  // A question with no options wants words, so Other is its only answer.
  const [picked, setPicked] = useState<readonly Picked[]>(() => questions.map((question) => ({ chosen: question.options.length === 0 ? [OTHER] : [], other: "" })));
  const answer = answerSet(questions, picked);
  const actions = actionsOf(prompt, "Send answers", null);
  const send = actions.find((each) => !each.deny);
  const skip = actions.find((each) => each.deny);
  function pick(index: number, option: number, multiSelect: boolean) {
    setPicked((held) => held.map((each, at) => (at === index
      ? { ...each, chosen: multiSelect ? toggle(each.chosen, option) : [option] }
      : each)));
  }
  function write(index: number, other: string) {
    setPicked((held) => held.map((each, at) => (at === index ? { ...each, other } : each)));
  }
  return <>
    <p className="prompt-card-title">{titleOf(prompt, who)}</p>
    {questions.map((question, index) => {
      const { chosen, other } = picked[index]!;
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
        {question.options.length === 0
          ? <label>
            <input type={question.secret ? "password" : "text"} aria-label={`Answer to ${question.header}`} value={other}
              onChange={(event) => write(index, event.target.value)} />
          </label>
          : <label>
            <input type={type} name={name} checked={chosen.includes(OTHER)} onChange={() => pick(index, OTHER, question.multiSelect)} />
            <span>Other</span>
            <input type={question.secret ? "password" : "text"} aria-label={`Other answer to ${question.header}`} value={other}
              disabled={!chosen.includes(OTHER)}
              onChange={(event) => write(index, event.target.value)} />
          </label>}
      </fieldset>;
    })}
    <div className="prompt-card-actions">
      {send && <button className="btn" type="button" disabled={busy || answer === null}
        onClick={() => answer && void submit({ text: answer.text, payload: payloadOf(prompt, send, "", { answers: answer.answers }) })}>{send.label}</button>}
      {skip && <button className="btn" type="button" disabled={busy}
        onClick={() => void submit({ text: skip.label, payload: payloadOf(prompt, skip, "") })}>{skip.label}</button>}
    </div>
  </>;
}

function Plan({ prompt, who, kinds, busy, submit }: {
  readonly prompt: Prompt;
  readonly who: string;
  readonly kinds: readonly string[];
  readonly busy: boolean;
  readonly submit: Submit;
}) {
  const [feedback, setFeedback] = useState("");
  const words = feedback.trim();
  // The bridge reads a message only on a denial, so feedback travels with the refusal alone;
  // Approve waits until the feedback box is empty rather than drop the words it was given.
  // A card from before options refuses only with words to give; one with options may keep planning with none.
  return <>
    <p className="prompt-card-title">{titleOf(prompt, who)}</p>
    <Markdown source={prompt.plan ?? ""} kinds={kinds} className="md prompt-card-plan" images={false} />
    <label className="prompt-card-comment">Feedback
      <textarea rows={2} value={feedback} disabled={busy} onChange={(event) => setFeedback(event.target.value)} />
    </label>
    <div className="prompt-card-actions">
      {actionsOf(prompt, "Approve", "Ask for changes", "Changes").map((action) => <button key={action.label} className="btn" type="button"
        disabled={busy || (action.deny ? prompt.ask === undefined && words === "" : words !== "")}
        onClick={() => void submit({ text: saying(prompt, action, words), payload: payloadOf(prompt, action, words) })}>{action.label}</button>)}
    </div>
  </>;
}

/**
 * The answers to every question, keyed by the agent's id or header, with the one-line summary of them; null while one has
 * none. The summary is the person's visible line, so a secret answer goes in the answers alone.
 */
function answerSet(questions: readonly PromptQuestion[], picked: readonly Picked[]): { readonly text: string; readonly answers: { readonly [key: string]: string } } | null {
  const pairs: [string, string, string][] = [];
  for (const [index, question] of questions.entries()) {
    const value = valueOf(question, picked[index]!);
    if (value === null) return null;
    pairs.push([question.id || question.header, question.header, value]);
  }
  const shown = pairs.map(([, header, value], index) => `${header}: ${questions[index]!.secret ? "(hidden)" : value}`);
  return { text: shown.join("; "), answers: Object.fromEntries(pairs.map(([key, , value]) => [key, value])) };
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
function resolvedWords(prompt: Prompt, { decision, by, option }: Resolution): string {
  if (prompt.type === "trust") return trustWords(decision, by);
  if (decision === "released") return "Released";
  const taken = option ? prompt.ask?.options.find((each) => each.id === option) : undefined;
  const where = by === "chat" ? " from Chat" : by === "terminal" ? " in the terminal" : "";
  return `${verbOf(prompt, decision, taken)}${where}`;
}

/** What was done, in the option's own terms when it is known: a grant for the session, a refusal that stops the run, a skipped question, a choice. */
function verbOf(prompt: Prompt, decision: Resolution["decision"], taken: PromptOption | undefined): string {
  if (taken?.kind === "allow_session") return "Allowed for this session";
  if (taken?.kind === "allow_always") return "Allowed always";
  if (taken?.kind === "deny_stop") return "Denied and stopped";
  if (prompt.type === "question") {
    if (decision === "deny") return "Skipped";
    if (taken && offersChoices(prompt)) return `Chose ${taken.label}`;
  }
  return decision === "allow" ? "Allowed" : decision === "deny" ? "Denied" : "Answered";
}

/** A trust prompt's resolution: trusted or not, and whether Chat or the terminal said so; the run stops on a refusal. */
function trustWords(decision: Resolution["decision"], by: Resolution["by"]): string {
  if (decision === "released") return "No longer waiting for trust";
  if (decision === "allow") return by === "chat" ? "Trusted from Chat" : "Trusted in the terminal";
  return by === "chat" ? "Not trusted: the run stopped" : "Not trusted in the terminal: the run stopped";
}
