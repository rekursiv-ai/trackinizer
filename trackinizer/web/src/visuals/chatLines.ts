import type { Provider } from "../api/providers";
import type { SessionRecord } from "../api/sessions";

/** What the key of a line not yet a record starts with, before the key of its send. */
export const PENDING = "pending:";

/**
 * One part of a science chat's session: its file's number, the format its writer logs in
 * (`sagent` for the assistant, `claude` or `codex` for a CLI run, `""` for the runner's own part),
 * and the records read from it.
 */
export type ChatPart = { readonly part: number; readonly format: string; readonly records: readonly SessionRecord[] };

/** Where a stored line is in its session: its part and its position in it. */
export type LineAt = { readonly part: number; readonly idx: number };

/**
 * What starts a line the person's Console sent to agents directly, which the
 * canvas's chat records as theirs: `→ lead-2, tiles-a:ops: text`.
 */
export const RELAY_MARK = "→ ";

/** One question of a prompt, as AskUserQuestion asks it. */
export type PromptQuestion = {
  readonly question: string;
  /** What the answer is keyed by in `updated_input.answers`; the question itself when the prompt names no header. */
  readonly header: string;
  readonly multiSelect: boolean;
  readonly options: readonly { readonly label: string; readonly description: string }[];
  /** The agent's id for the question, which an answer is keyed by in place of its header; empty when it gives none. */
  readonly id?: string;
  /** The agent asks for words nobody else should read, such as a token: the box hides them and the line names none. */
  readonly secret?: true;
};

/**
 * How a prompt was closed: its decision, and who closed it: a person in Chat or in the
 * terminal, the run itself, a timeout, or a release (the run ended with the prompt open).
 * A record in the unified form also names the option taken (empty when none was) and
 * the person who took it.
 */
export type Resolution = {
  readonly decision: "allow" | "deny" | "answered" | "released";
  readonly by: "chat" | "terminal" | "run" | "timeout" | "released";
  readonly option?: string;
  readonly who?: string;
};

/** What an option of an ask does, which is how its button looks and what its resolution says. */
export type OptionKind = "allow_once" | "allow_session" | "allow_always" | "deny" | "deny_stop";

/** One choice an agent offers on an ask, in its own words; an answer names its `id`. */
export type PromptOption = { readonly id: string; readonly label: string; readonly kind: OptionKind };

/**
 * What an ask in the unified form says beyond the prompt's old fields: the agent that
 * asked (`provider`: `claude`, `codex` or the assistant's own name), the `kind` of ask
 * (permission, command, file_change, permissions, question, plan, elicitation, trust,
 * dialog), the `title` the agent gave it, the tool call it is about (`toolCallId`,
 * empty when the agent could not name it), the `command`, `cwd`, `reason` and `diff`
 * it shows, the agent's own `options`, and whether Chat can answer it at all.
 */
export type AskCard = {
  readonly provider: string;
  readonly kind: string;
  readonly title: string;
  readonly toolCallId: string;
  readonly command: string;
  readonly cwd: string;
  readonly reason: string;
  readonly diff: string;
  readonly options: readonly PromptOption[];
  readonly answerable: boolean;
};

/**
 * A prompt a run asked: a claude permission names its tool and input, a question its
 * questions, a plan its text. A trust prompt is a CLI waiting to start in a folder it does
 * not trust yet: `cli`, `folder`, `machine` (null for the person's own machine) and
 * `remembered` (whether the CLI keeps the trust for that folder). Every other type leaves
 * the trust fields null. `resolution` is null while the prompt is open. `ask` is present
 * when the record is in the unified form, with options; an older record has none, and its
 * card offers the two answers it always did.
 */
export type Prompt = {
  readonly id: string;
  readonly type: "permission" | "question" | "plan" | "trust";
  readonly tool: string;
  readonly input: unknown;
  readonly questions: readonly PromptQuestion[] | null;
  readonly plan: string | null;
  readonly cli: "claude" | "codex" | null;
  readonly folder: string | null;
  readonly machine: string | null;
  readonly remembered: boolean | null;
  readonly resolution: Resolution | null;
  readonly ask?: AskCard;
};

/**
 * A goal a run is working toward, as the CLI's own log and the commands typed say it
 * stands. `lastReason` is the words of the last check, or null before one.
 */
export type Goal = {
  readonly condition: string;
  readonly state: "active" | "paused" | "blocked";
  readonly lastReason: string | null;
};

/**
 * A tool call and the result answering it: the card Chat shows when the user asks to
 * see tool calls. The result is null while the call runs.
 */
export type ToolExchange = { readonly call: SessionRecord; readonly result: SessionRecord | null };

/**
 * One line of the transcript: a person's, an answer, a relay of what they told agents,
 * a prompt a run asked, a slash command typed into a run, a goal the run's log reports,
 * what a CLI printed for a command, a compaction of the agent's context, an interrupt
 * the CLI logged, a tool call, or one still being sent. A `command` line is its sender's, and its text is what was
 * typed, or `refused: <reason>` when the run refused it. A `tool` line's text is the
 * tool's name.
 */
export type Line = {
  readonly key: string;
  /** The record the line is, which a fork starts from; null for a line not yet stored. */
  readonly at: LineAt | null;
  readonly role: "user" | "assistant" | "relay" | "interaction" | "command" | "goal" | "reply" | "compaction" | "interrupt" | "tool";
  /** The poster's attested email for a person's or a command's line; null for an answer. */
  readonly author: string | null;
  readonly text: string;
  /** When the record was written, ISO 8601; null for a line not yet stored, or a record with no time. */
  readonly sentAt: string | null;
  /** Sent from this browser and not yet a record of the session. */
  readonly pending: boolean;
  /** A failed delivery, distinguished from a captured user turn. */
  readonly delivery?: { readonly refused: boolean; readonly reason: string };
  /** The prompt an interaction line shows, which Chat answers; absent on every other line. */
  readonly prompt?: Prompt;
  /**
   * The agent that wrote an answer, by its part's format: a chat that switched provider keeps
   * each answer's own. Absent on every other line, and on an answer from a part of no known format.
   */
  readonly agent?: Provider;
  /** The call a tool line shows; absent on every other line. */
  readonly tool?: ToolExchange;
  /** An interaction line that sits under the card of the tool call it asks about. */
  readonly attached?: true;
};

/** A line sent from this browser; it shows until its record arrives. */
export type PendingLine = {
  readonly key: string;
  readonly text: string;
  /** The conversation it was sent to; null for a new one. */
  readonly conversationId: string | null;
  /** How many lines by the sender showed, stored or sending, when it was sent. */
  readonly baseline: number;
  /** When the browser submitted this post, for confirmation waits across reloads. */
  readonly submittedAt?: number;
};

/** What a chat's records say: its lines, and what the agent is doing about the last one. */
export type Transcript = {
  readonly lines: readonly Line[];
  /**
   * What the agent is doing when the last line is a person's and no answer has come: the
   * name of the last tool it called since, else `""` (it has not called one, or this
   * is the last record). Null when the last line is an answer, or there is none.
   */
  readonly working: string | null;
  /** The keys of the answers whose turn called the Highlight tool before it answered. */
  readonly pointed: ReadonlySet<string>;
  /** The goal open at the last record; null once it is met, stopped or cleared. */
  readonly goal: Goal | null;
};

/**
 * The lines of a science chat, read from its session's records in the order they happened:
 * a person's line is an `AgentToAgentMessage` from its poster, or a `UserMessage` (a line sent
 * mid-turn is a `queued_command` `ContextState` instead) a CLI run
 * recorded for a person (see `personOf`); an answer is an `AssistantMessage` with words, named
 * by the agent its part's format says wrote it. A prompt a run asked is a line, which its
 * resolution record closes. A slash command a run typed is a `command` line, and the goal its
 * CLI logs is a `goal` line (see `goalStep`). A `ContextCompaction` is a `compaction` line,
 * one for the records that name one act. An interrupted turn, as the CLI logs it (see
 * `interrupted`), is an `interrupt` line, and no answer is owed after it. Other tool calls and results are not lines, unless
 * `tools` asks for them: each call is then a `tool` line, in time order, which the first
 * record after it with its `call_id` answers. Either way the last call since the last
 * person's line says what the agent is doing.
 */
export function readTranscript(parts: readonly ChatPart[], { tools = false }: { readonly tools?: boolean } = {}): Transcript {
  const lines: Line[] = [];
  let working: string | null = null;
  const pointed = new Set<string>();
  let pointing = false;
  let goal: Goal | null = null;
  // The tool lines still waiting for their result, by call id, and each one's place
  // in `lines`.
  const asked = new Map<string, number>();
  for (const { part, format, record } of inTimeOrder(parts)) {
    const key = `${part}:${record.idx}`;
    const payload = record.payload ?? {};
    const at = { part, idx: record.idx };
    const sentAt = record.timestamp ?? null;
    const extra = objectOf(payload.extra);
    const person = personOf(record.kind, payload);
    const step = goalStep(record.kind, payload, goal);
    const printed = printedOf(payload, lines.at(-1));
    if (extra.$delivery !== undefined) {
      const delivery = objectOf(extra.$delivery);
      const kind = text(delivery.kind);
      const content = text(delivery.text);
      const reason = text(delivery.reason);
      if (content && reason && typeof delivery.refused === "boolean") {
        const role = kind === "line" ? "user" : kind === "command" ? "command" : "reply";
        lines.push({ key, at, role, author: typeof delivery.source === "string" ? delivery.source : null,
          text: content, sentAt, pending: false, delivery: { refused: delivery.refused, reason } });
      }
    } else if (extra.$interaction !== undefined) {
      const prompt = promptOf(extra.$interaction);
      if (prompt) lines.push({ key, at, role: "interaction", author: null, text: "", sentAt, pending: false, prompt: { ...prompt, resolution: null } });
    } else if (extra.$interaction_resolved !== undefined) {
      const resolved = resolvedOf(extra.$interaction_resolved);
      if (resolved) settle(lines, resolved);
    } else if (extra.$command !== undefined) {
      const command = commandOf(extra.$command);
      if (command) {
        lines.push({ key, at, role: "command", author: command.source, text: command.typed ? command.text : `refused: ${command.reason}`, sentAt, pending: false });
        if (command.typed && setsGoal(command)) {
          // A goal set from Chat is work owed, as a person's line is; the CLI's log then says what it became.
          working = "";
          pointing = false;
        }
        // Codex logs no clear, so a clear typed here is the only record that its goal stopped.
        if (command.typed && clearsGoal(command)) goal = null;
      }
    } else if (step) {
      goal = step.goal;
      if (step.text !== null) lines.push({ key, at, role: "goal", author: null, text: step.text, sentAt, pending: false });
    } else if (printed !== null) {
      lines.push({ key, at, role: "reply", author: null, text: printed, sentAt, pending: false });
    } else if (person && record.kind === "AgentToAgentMessage" && person.text.startsWith(RELAY_MARK)) {
      // News for the partner, not a question to it: no answer is owed, so the agent is no busier.
      lines.push({ key, at, role: "relay", author: person.author, text: person.text, sentAt, pending: false });
    } else if (person) {
      lines.push({ key, at, role: "user", author: person.author, text: person.text, sentAt, pending: false });
      working = "";
      pointing = false;
    } else if (interrupted(record.kind, payload)) {
      lines.push({ key, at, role: "interrupt", author: null, text: "Interrupted", sentAt, pending: false });
      working = null;
    } else if (record.kind === "AssistantMessage" && text(payload.content)) {
      if (pointing) pointed.add(key);
      const agent = AGENTS.get(format);
      lines.push({ key, at, role: "assistant", author: null, text: text(payload.content), sentAt, pending: false, ...(agent && { agent }) });
      working = null;
    } else if (record.kind === "ContextCompaction") {
      // Claude's in-place compaction and codex's marker beside its `compacted` line each name the act once more.
      if (lines.at(-1)?.role !== "compaction") lines.push({ key, at, role: "compaction", author: null, text: "Context compacted", sentAt, pending: false });
    } else if (record.kind === "ToolCall") {
      if (isHighlight(text(payload.name))) pointing = true;
      if (working !== null) working = text(payload.name);
      if (tools) {
        const id = text(payload.call_id);
        if (id) asked.set(id, lines.length);
        lines.push({ key, at, role: "tool", author: null, text: text(payload.name), sentAt, pending: false, tool: { call: record, result: null } });
      }
    } else if (tools && asked.has(text(payload.call_id))) {
      // The first record after a call with its id is its result; a later one is not.
      const id = text(payload.call_id);
      const index = asked.get(id)!;
      lines[index] = { ...lines[index]!, tool: { call: lines[index]!.tool!.call, result: record } };
      asked.delete(id);
    }
  }
  return { lines: tools ? placedUnderCalls(lines) : lines, working, pointed, goal };
}

/**
 * How many prompts no resolution has closed: what the chat waits on the person for,
 * when the server does not say.
 */
export function openPrompts(transcript: Transcript): number {
  return transcript.lines.filter((line) => line.prompt !== undefined && line.prompt.resolution === null).length;
}

/**
 * The lines with each ask that names a tool call moved to sit right under that call's
 * card, marked `attached`; an ask whose call has no card here stays where it happened.
 * The cards keep their order, and so do the asks under one call. Two cards with one call
 * id hold its asks under the first only, so no ask shows twice.
 */
function placedUnderCalls(lines: readonly Line[]): Line[] {
  const calls = new Set(lines.flatMap((line) => (line.tool ? [text(line.tool.call.payload?.call_id)] : [])).filter((id) => id !== ""));
  const under = new Map<string, Line[]>();
  const rest: Line[] = [];
  for (const line of lines) {
    const id = line.prompt?.ask?.toolCallId ?? "";
    if (id !== "" && calls.has(id)) under.set(id, [...(under.get(id) ?? []), { ...line, attached: true }]);
    else rest.push(line);
  }
  return rest.flatMap((line) => {
    if (!line.tool) return [line];
    const id = text(line.tool.call.payload?.call_id);
    const asks = under.get(id) ?? [];
    under.delete(id);
    return [line, ...asks];
  });
}

/** The goal a run is working toward at the end of its records, or null when none is open. */
export function goalOf(transcript: Transcript): Goal | null {
  return transcript.goal;
}

/**
 * The transcript's lines, then what this browser sent that is not a record yet. A
 * sent line is a record once the session holds more lines by `me` than it did when the
 * line was sent, in-flight ones counted. The words are not compared: the assistant may
 * have changed them (a stored key is replaced) before recording the line.
 */
export function withPending(
  transcript: Transcript,
  pending: readonly PendingLine[],
  { me }: { readonly me: string },
): Line[] {
  const stored = sentBefore(transcript.lines, { me });
  const waiting = pending
    .filter((line) => stored < line.baseline + 1)
    .map((line): Line => ({ key: `${PENDING}${line.key}`, at: null, role: "user", author: me, text: line.text, sentAt: null, pending: true }));
  return [...transcript.lines, ...waiting];
}

/** How many of `lines`, sent or still being sent, are `me`'s: a person's line, or a command `me` typed. */
export function sentBefore(lines: readonly Line[], { me }: { readonly me: string }): number {
  return lines.filter((line) => (line.role === "user" || line.role === "command") && line.author === me).length;
}

/** The lines of a transcript up to and including the one stored at `at`: what a fork from it opens with. */
export function linesThrough(lines: readonly Line[], at: LineAt): Line[] {
  const end = lines.findIndex((line) => line.at?.part === at.part && line.at.idx === at.idx);
  return end < 0 ? [] : lines.slice(0, end + 1);
}

/** Whether a tool call's name is the canvas Highlight tool, which a host may prefix. */
function isHighlight(name: string): boolean {
  return name === "Highlight" || name.endsWith("__Highlight");
}

/** A person's words and who said them: `author` is null when the record names no one. */
type Person = { readonly author: string | null; readonly text: string };

/**
 * The person a record speaks for, or null when it is no person's line. An
 * `AgentToAgentMessage` is one from its sender. A `UserMessage`, or a claude `queued_command`
 * (a line sent mid-turn, which claude queues instead of recording as a user turn), is one when a CLI run
 * recorded it for a person: the bot delivered it, so the agent runner stamped `$inbound` with the
 * attested sender, or (a `UserMessage` only) it carries an `author`. A CLI-written user turn and
 * claude's own queued notices carry neither. The
 * sender is read from the stamp and never from the text, which anyone can type. This is
 * the loader's rule in `client/conversation.py`.
 */
function personOf(kind: string, payload: Readonly<Record<string, unknown>>): Person | null {
  if (kind === "AgentToAgentMessage") return { author: text(payload.sender) || null, text: text(payload.content) };
  const queued = kind === "ContextState" && payload.kind === "queued_command";
  if (kind !== "UserMessage" && !queued) return null;
  const extra = objectOf(payload.extra);
  const stamp = objectOf(extra.$inbound);
  if (Object.keys(stamp).length > 0) {
    const words = delivered(text(payload.content), stamp);
    return words ? { author: text(stamp.source) || null, text: words } : null;
  }
  if (queued) return null;
  const author = text(extra.author);
  const words = text(payload.content);
  return author && words ? { author, text: words } : null;
}

/**
 * What a delivery said: its text without the `[room] sender: ` prefix the agent runner wrote,
 * and the context trailer after it. Mirrors `client/conversation.py::delivered_text`.
 */
function delivered(content: string, stamp: Readonly<Record<string, unknown>>): string {
  const room = text(stamp.room);
  const source = text(stamp.source);
  const unroomed = room && content.startsWith(`[${room}] `) ? content.slice(room.length + 3) : content;
  const sender = source ? `${source}: ` : "";
  const body = sender && unroomed.startsWith(sender) ? unroomed.slice(sender.length) : unroomed;
  // The same trailer `_TRAILER` names in `client/conversation.py`, which the loader cuts too.
  return body.split("\nTrackinizer context (verify with trax): ")[0];
}

/** A slash command a run wrote as a `$command`: what was typed, its verb and words, and whether it was typed or refused. */
type Command = {
  readonly text: string;
  readonly verb: string;
  readonly args: string;
  readonly source: string | null;
  readonly typed: boolean;
  readonly reason: string;
};

/** A `$command` from its record, or null when it is malformed. */
function commandOf(value: unknown): Command | null {
  const raw = objectOf(value);
  const state = text(raw.state);
  if (state !== "typed" && state !== "refused") return null;
  return {
    text: text(raw.text), verb: text(raw.verb), args: text(raw.args), source: text(raw.source) || null,
    typed: state === "typed", reason: text(raw.reason),
  };
}

/** The words a `/goal` takes that stop the goal. Chat sends `/goal clear` for each of them, normalised. */
const CLEARING = ["clear", "stop", "off", "reset", "none", "cancel"];
/** The words a `/goal` takes that set no condition: a clear, a pause, a resume, or the editor. */
const NOT_A_CONDITION = [...CLEARING, "pause", "resume", "edit"];

/** Whether a typed command sets a goal: `/goal` with a condition, which the CLI's log then reports. */
function setsGoal(command: Command): boolean {
  return command.verb === "goal" && command.args !== "" && !NOT_A_CONDITION.includes(command.args.toLowerCase());
}

function clearsGoal(command: Command): boolean {
  return command.verb === "goal" && CLEARING.includes(command.args.toLowerCase());
}

/** What a goal record says: the goal after it, and the line it adds (null for none). */
type GoalStep = { readonly goal: Goal | null; readonly text: string | null };

/**
 * The goal a record reports, from the CLI's own log: claude's `goal_status` attachment, or
 * codex's `thread_goal_updated` event. Null when the record is no goal record. The fields
 * are the ones the agent runner normalises (`GoalWatch` in the runner's `commands.py`).
 */
function goalStep(kind: string, payload: Readonly<Record<string, unknown>>, goal: Goal | null): GoalStep | null {
  if (kind === "ContextState" && payload.kind === "goal_status") return claudeGoal(objectOf(objectOf(payload.extra).attachment), goal);
  if (kind === "UncategorizedRecord" && payload.kind === "event_msg/thread_goal_updated") return codexGoal(objectOf(objectOf(payload.payload).goal), goal);
  return null;
}

/** Claude's goal attachment: a set, a check, a met goal, a stopped one, or the clear of one. */
function claudeGoal(attachment: Readonly<Record<string, unknown>>, goal: Goal | null): GoalStep {
  const condition = text(attachment.condition);
  const reason = text(attachment.reason);
  if (attachment.sentinel === true) {
    return attachment.met === true
      ? { goal: null, text: `Goal cleared: ${condition}` }
      : { goal: { condition, state: "active", lastReason: null }, text: `Goal set: ${condition}` };
  }
  if (attachment.failed === true) return { goal: null, text: `Goal stopped: ${reason}` };
  if (attachment.met === true) return { goal: null, text: metLine(reason, attachment) };
  // A check that is not met yet adds no line; its words are the goal's last check.
  const named = condition || goal?.condition || "";
  return { goal: named ? { condition: named, state: "active", lastReason: reason || null } : null, text: null };
}

/** The line a met goal shows: why it was met, how many turns it took, and how long. */
function metLine(reason: string, attachment: Readonly<Record<string, unknown>>): string {
  const shown = [reason ? `Goal met: ${reason}` : "Goal met"];
  const turns = numberOf(attachment.iterations);
  if (turns !== null) shown.push(`${turns} ${turns === 1 ? "turn" : "turns"}`);
  const ms = numberOf(attachment.durationMs);
  if (ms !== null) shown.push(durationOf(ms));
  return shown.join(" · ");
}

/**
 * Codex's goal, as the status it is now in. A status the open goal already has adds no line:
 * a goal paused twice says so once. Codex logs no clear, so its clear is the typed command's.
 */
function codexGoal(value: Readonly<Record<string, unknown>>, goal: Goal | null): GoalStep | null {
  const status = text(value.status);
  const objective = text(value.objective);
  const held = goal !== null && goal.condition === objective ? goal : null;
  const condition = objective || goal?.condition || "";
  switch (status) {
    case "active":
      return held
        ? { goal: { ...held, state: "active" }, text: null }
        : { goal: { condition: objective, state: "active", lastReason: null }, text: `Goal set: ${objective}` };
    case "paused":
      return { goal: { condition, state: "paused", lastReason: goal?.lastReason ?? null }, text: held?.state === "paused" ? null : "Goal paused" };
    case "blocked":
      return { goal: { condition, state: "blocked", lastReason: goal?.lastReason ?? null }, text: held?.state === "blocked" ? null : "Goal blocked" };
    case "complete":
      return { goal: null, text: "Goal met" };
    case "budget_limited":
      return { goal: null, text: "Goal stopped: budget limit" };
    case "usage_limited":
      return { goal: null, text: "Goal stopped: usage limit" };
    default:
      return null;
  }
}

const MS_PER_SECOND = 1_000;
const SECONDS_PER_MINUTE = 60;

/** A duration as a person reads it: seconds under a minute, then minutes and seconds. */
function durationOf(ms: number): string {
  const seconds = Math.round(ms / MS_PER_SECOND);
  return seconds < SECONDS_PER_MINUTE ? `${seconds} s` : `${Math.floor(seconds / SECONDS_PER_MINUTE)} min ${seconds % SECONDS_PER_MINUTE} s`;
}

/** What a CLI printed for the command before it: a `local_command` record's stdout, when it directly follows a command line. */
function printedOf(payload: Readonly<Record<string, unknown>>, previous: Line | undefined): string | null {
  if (previous?.role !== "command" || payload.subtype !== "local_command") return null;
  const printed = /^<local-command-stdout>([\s\S]*)<\/local-command-stdout>$/.exec(text(payload.content).trim());
  return printed?.[1] ? printed[1] : null;
}

/** The agent a part's format says wrote it: the assistant's own log, or a CLI's. */
const AGENTS: ReadonlyMap<string, Provider> = new Map([["sagent", "assistant"], ["claude", "claude"], ["codex", "codex"]]);

const PROMPT_TYPES = ["permission", "question", "plan", "trust"] as const;
const DECISIONS = ["allow", "deny", "answered", "released"] as const;
const DECIDERS = ["chat", "terminal", "run", "timeout", "released"] as const;
const OPTION_KINDS = ["allow_once", "allow_session", "allow_always", "deny", "deny_stop"] as const;
const CLIS = ["claude", "codex"] as const;

/** A prompt from its record's `$interaction`, or null when it is not a well-formed one. */
function promptOf(value: unknown): Omit<Prompt, "resolution"> | null {
  const raw = objectOf(value);
  const type = PROMPT_TYPES.find((each) => each === raw.type);
  if (typeof raw.id !== "string" || !type) return null;
  const questions = questionsOf(raw.questions);
  if (type === "question" && questions.length === 0) return null;
  const base = {
    id: raw.id, tool: text(raw.tool), input: raw.input ?? null,
    questions: questions.length > 0 ? questions : null,
    plan: typeof raw.plan === "string" ? raw.plan : null,
  };
  const ask = askOf(raw);
  const plain = { ...base, cli: null, folder: null, machine: null, remembered: null, ...(ask && { ask }) };
  if (type !== "trust") return { ...plain, type };
  const cli = CLIS.find((each) => each === raw.cli);
  const folder = text(raw.folder);
  if (cli && folder) return { ...base, type, cli, folder, machine: text(raw.machine) || null, remembered: raw.remembered === true, ...(ask && { ask }) };
  // A trust ask an agent made names no CLI or folder of ours: its own options are its card.
  return ask ? { ...plain, type: "permission" } : null;
}

/** The unified form of a record's `$interaction`, or null when it names no usable option (an older record). */
function askOf(raw: Readonly<Record<string, unknown>>): AskCard | null {
  const options = Array.isArray(raw.options) ? raw.options.flatMap((item): PromptOption[] => {
    const each = objectOf(item);
    const kind = OPTION_KINDS.find((known) => known === each.kind);
    return typeof each.id === "string" && each.id !== "" && typeof each.label === "string" && kind ? [{ id: each.id, label: each.label, kind }] : [];
  }) : [];
  if (options.length === 0) return null;
  return {
    provider: text(raw.provider), kind: text(raw.kind), title: text(raw.title), toolCallId: text(raw.tool_call_id),
    command: text(raw.command), cwd: text(raw.cwd), reason: text(raw.reason), diff: text(raw.diff),
    options, answerable: raw.answerable_in_chat !== false,
  };
}

/** The prompt a resolution record closes, and how, from its `$interaction_resolved`; null when malformed. */
function resolvedOf(value: unknown): { readonly id: string; readonly resolution: Resolution } | null {
  const raw = objectOf(value);
  const by = DECIDERS.find((each) => each === raw.by);
  // A record that names no decision is read from who ended it: a run's end or a timeout took none.
  const decision = raw.decision === undefined && by ? (by === "chat" || by === "terminal" ? "answered" : "released") : DECISIONS.find((each) => each === raw.decision);
  if (typeof raw.id !== "string" || !decision || !by) return null;
  const option = text(raw.option);
  const who = text(raw.who);
  return { id: raw.id, resolution: { decision, by, ...(option && { option }), ...(who && { who }) } };
}

/**
 * Close the prompts a resolution names that are still open. Only a prompt before the
 * record is closed by it, so a prompt that comes later under the same id stays open
 * until its own resolution.
 */
function settle(lines: Line[], { id, resolution }: { readonly id: string; readonly resolution: Resolution }): void {
  for (const [index, line] of lines.entries()) {
    if (line.prompt?.id === id && line.prompt.resolution === null) lines[index] = { ...line, prompt: { ...line.prompt, resolution } };
  }
}

function questionsOf(value: unknown): PromptQuestion[] {
  if (!Array.isArray(value)) return [];
  const questions: PromptQuestion[] = [];
  for (const item of value) {
    const raw = objectOf(item);
    if (typeof raw.question !== "string") continue;
    questions.push({
      question: raw.question,
      header: text(raw.header) || raw.question,
      multiSelect: raw.multiSelect === true,
      // Codex gives a question that wants words no options at all.
      options: optionsOf(raw.options),
      ...(text(raw.id) && { id: text(raw.id) }),
      ...(raw.isSecret === true && { secret: true as const }),
    });
  }
  return questions;
}

function optionsOf(value: unknown): PromptQuestion["options"] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    const raw = objectOf(item);
    return { label: text(raw.label), description: text(raw.description) };
  });
}

/** The moment before every other: what a record in a part with no stamp at all takes. */
const NEVER = Number.NEGATIVE_INFINITY;

/**
 * Every record of every part, in the order they happened. A record with no stamp takes the
 * moment before it in its part, and a leading one the first stamp in the part, so a part's own
 * order holds whatever the other parts did. Equal moments keep their order, the parts' in
 * sequence. Mirrors `client/conversation.py::_in_time_order`.
 */
function inTimeOrder(parts: readonly ChatPart[]): { readonly part: number; readonly format: string; readonly record: SessionRecord }[] {
  const dated: { readonly at: number; readonly part: number; readonly format: string; readonly record: SessionRecord }[] = [];
  for (const { part, format, records } of parts) {
    const moments = momentsOf(records);
    for (const [index, record] of records.entries()) dated.push({ at: moments[index] ?? NEVER, part, format, record });
  }
  return dated.toSorted((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
}

/** When each record of one part happened, filled in from its neighbours. */
function momentsOf(records: readonly SessionRecord[]): number[] {
  const stamped = records.map(momentOf);
  let last = stamped.find((at) => at !== null) ?? NEVER;
  return stamped.map((at) => {
    last = at ?? last;
    return last;
  });
}

/** A record's stamp in milliseconds, or null when it has none or it will not parse. A stamp with no zone is UTC, as the loader reads it. */
function momentOf(record: SessionRecord): number | null {
  const stamp = record.timestamp;
  if (!stamp) return null;
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/i.test(stamp) ? stamp : `${stamp}Z`;
  const at = Date.parse(zoned);
  return Number.isNaN(at) ? null : at;
}

/**
 * Whether a record is the CLI's own log of a turn it stopped on an interrupt: claude's
 * `[Request interrupted by user]` user turn (`… for tool use]` mid-tool), or the
 * `<turn_aborted>` developer message codex adds. A person's line is read before this, so
 * one in the same words stays theirs.
 */
function interrupted(kind: string, payload: Readonly<Record<string, unknown>>): boolean {
  const content = text(payload.content);
  return (kind === "UserMessage" && content.startsWith("[Request interrupted by user"))
    || (kind === "SystemMessage" && payload.subtype === "developer" && content.startsWith("<turn_aborted>"));
}

/** A JSON object's fields, or none when the value is not an object. */
function objectOf(value: unknown): Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberOf(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
