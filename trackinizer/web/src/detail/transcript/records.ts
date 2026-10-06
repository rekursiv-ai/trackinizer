import type { SessionPart, SessionRecord } from "../../api/sessions";

/**
 * How one record shows, read from its payload: prose, reasoning, a tool call, a
 * shell command's run, a file edit as a diff, a context window opening, a web
 * search's results, text folded under a summary, a tool's output, or a one-line
 * note. `meta` is what the record's header adds after its source.
 *
 * The payload is the record as the server's dataclass codec wrote it, so each
 * object names its class as `py/object` (`plain`). A kind with no case here, or a
 * payload without the fields its case reads, shows its text, the server's own
 * search projection, or what Claude's model read, so nothing new is dropped; so
 * does an offloaded body, whose payload is only a stub.
 */
export type RecordView = Shape & {
  /** Its attachments, as `2 attachments (image/png, 2.1 KB; …)`, whatever its shape; `""` when none. */
  readonly attachments: string;
  /** What the tool step it belongs to did, when it is a tool call or a tool's result; null for any other record. */
  readonly tool: ToolKind | null;
  /** Whether it is a result that says it failed (`failedOf`), whatever its shape. */
  readonly failed: boolean;
};

/**
 * What a tool step did: read from its result's kind, the server's neutral
 * vocabulary (`trackinizer/lib/agent/types/sessions.py`), else from its call's name;
 * `call` when neither says.
 */
export type ToolKind = "command" | "read" | "edit" | "write" | "search" | "list" | "web" | "fetch" | "agent" | "call";

/** What a record shows but its attachments (`RecordView`). */
type Shape =
  | {
      readonly shape: "message";
      readonly label: string;
      readonly text: string;
      readonly context: CanvasContext | null;
      /** A person's or another agent's message long enough to fold to its first lines; never the assistant's. */
      readonly long: boolean;
    }
  | { readonly shape: "thinking"; readonly label: "Thinking"; readonly text: string }
  | {
      readonly shape: "tool";
      readonly label: "Tool call";
      readonly name: string;
      /** The argument that says what the call does, such as its command or file; `""` when none does. */
      readonly primary: string;
      /** Every other argument, by name, as text. */
      readonly args: readonly (readonly [string, string])[];
    }
  | {
      readonly shape: "shell";
      readonly label: "Shell command";
      readonly command: string;
      readonly stdout: string;
      readonly stderr: string;
      readonly exit: number | null;
    }
  | { readonly shape: "edit"; readonly label: "File edit"; readonly source: string; readonly lines: readonly DiffLine[] }
  | {
      readonly shape: "clear";
      readonly label: "Context cleared";
      /** What the new window carries in place of the history it replaced. */
      readonly summary: string;
      readonly prompt: string;
      /** The provider's id of the session this one continues. */
      readonly continues: string;
    }
  | { readonly shape: "search"; readonly label: "Web search"; readonly source: string; readonly results: readonly SearchResult[] }
  | { readonly shape: "folded"; readonly label: string; readonly summary: string; readonly text: string }
  | {
      readonly shape: "output";
      readonly label: string;
      readonly source: string;
      readonly meta: string;
      readonly text: string;
    }
  | { readonly shape: "note"; readonly label: string; readonly meta: string };

/** One web search result. */
export type SearchResult = { readonly url: string; readonly title: string; readonly snippet: string };

/**
 * What the canvas appends to a message it sends (`_render_inbound` in
 * `trax/run/session.py`), and the record it names, when it names one.
 */
export type CanvasContext = { readonly text: string; readonly recordId: string; readonly title: string };

/**
 * One line of a unified diff, its mark included, and its number in the file it
 * is in (the new file's, or the old one's for a removed line) when the provider
 * gave a header or a line to count from.
 */
export type DiffLine = { readonly kind: "hunk" | "ctx" | "del" | "add"; readonly text: string; readonly line: number | null };

type Fields = { readonly [field: string]: unknown };

/** What `record` shows, read from its kind and payload. */
export function recordView(record: SessionRecord): RecordView {
  const payload = record.payload ?? {};
  return { ...shapeOf(record), attachments: attachmentsChip(payload.attachments), tool: toolKind(record), failed: failedOf(payload) };
}

/** What `record` did as a tool step (`ToolKind`); null when it is no tool call or result. */
export function toolKind(record: SessionRecord): ToolKind | null {
  if (record.kind !== "ToolCall") return TOOL_RESULTS.get(record.kind) ?? null;
  const name = stringField(record.payload ?? {}, "name").toLowerCase().replaceAll("_", "");
  return TOOL_NAMES.get(name) ?? "call";
}

/**
 * Whether `record` has nothing to read, which no view draws or counts: reasoning
 * sealed with no summary (only its ciphertext, which `plaintext_only` reads
 * leave out), and a codex peer message codex sealed (`peerPayload`) with no
 * attachment. A summary is what sealed reasoning shows.
 */
export function unreadable({ kind, payload }: Pick<SessionRecord, "kind" | "payload">): boolean {
  const fields = payload ?? {};
  if (kind === "Thinking") return !stringField(fields, "content") && !stringField(fields, "summary");
  const content = stringField(fields, "content");
  return kind === "AgentToAgentMessage" && peerPayload(content) !== content && !peerPayload(content).trim() && !attachmentsChip(fields.attachments);
}

/**
 * Which of `records` are bookkeeping, which the transcript hides until asked,
 * as the old UI did: settings, token counts, injected state, the harness's own
 * messages (codex's context on the user's turn among them), messages with
 * nothing in them, and every provider line with no neutral kind (Claude Code's
 * `last-prompt` or `file-history-snapshot`, codex's `event_msg/*` echoes) but a
 * legacy turn. A `TurnContext` that switches the model is not, so the switch
 * shows where it happened; nor is a message queued while the agent worked,
 * which Claude delivers as injected state.
 */
export function markBookkeeping(records: readonly SessionRecord[]): boolean[] {
  let model = "";
  return records.map((record) => {
    const payload = record.payload ?? {};
    switch (record.kind) {
      case "TurnContext": {
        const next = stringField(payload, "model");
        const switched = model !== "" && next !== "" && next !== model;
        model = next || model;
        return !switched;
      }
      case "ContextState":
        return stringField(payload, "kind") !== "queued_command";
      case "TokenUsage":
      case "SystemMessage":
        return true;
      case "UserMessage":
        if (codexContext(stringField(payload, "content"))) return true;
        return !stringField(payload, "content").trim() && !attachmentsChip(payload.attachments);
      case "AssistantMessage":
      case "AgentToAgentMessage":
        return !stringField(payload, "content").trim() && !attachmentsChip(payload.attachments);
      case "UncategorizedRecord":
        return !stringField(payload, "kind").startsWith("legacy/");
      default:
        return false;
    }
  });
}

/**
 * What a codex agent said to another: `content` without the envelope codex writes
 * before it (`Message Type`, `Task name`, `Sender`, `Payload:`), whose sender and
 * recipient the record keeps apart. `""` when codex sealed it, as it does a
 * `MESSAGE`, keeping its payload only as ciphertext.
 */
export function peerPayload(content: string): string {
  return content.replace(/^Message Type: [A-Z_]+\nTask name: [^\n]*\nSender: [^\n]*\nPayload:\n?/, "");
}

/** Whether `text` is wholly the context codex writes on the user's turn, in its own tag, which no person typed. */
export function codexContext(text: string): boolean {
  return /^<codex_internal_context(?: [a-z_]+="[^"]*")*>[\s\S]*<\/codex_internal_context>$/.test(text.trim());
}

/**
 * The fields of the notice Claude Code writes on the user's turn when a
 * background task ends (`<task-notification>`), by tag (`status`, `summary`,
 * `result`, …); null when `text` is not wholly one.
 */
export function taskNotification(text: string): { readonly [field: string]: string } | null {
  const notice = /^<task-notification>([\s\S]*)<\/task-notification>$/.exec(text.trim());
  if (!notice) return null;
  return Object.fromEntries([...notice[1]!.matchAll(/<([a-z-]+)>([\s\S]*?)<\/\1>/g)].map(([, tag, value]) => [tag!, value!.trim()]));
}

/** Whether a message runs over 1,500 characters or 20 lines, so it shows its first lines until asked. */
export function isLong(text: string): boolean {
  return text.length > 1500 || text.split("\n").length > 20;
}

/**
 * `content` without the context the canvas appended to it, and that context.
 * The marker is the poller's, and the last one wins, since a person's own text
 * comes first.
 */
export function canvasContext(content: string): { readonly text: string; readonly context: CanvasContext | null } {
  const marker = "\nTrackinizer context (verify with trax): ";
  const at = content.lastIndexOf(marker);
  if (at < 0) return { text: content, context: null };
  const folded = content.slice(at + marker.length);
  const about = parsed(folded.split("\n", 1)[0]!);
  const record = isFields(about) && isFields(about.record) ? about.record : {};
  const ref = typeof record.seq === "number" ? `${stringField(record, "kind")}#${record.seq}` : "";
  return {
    text: content.slice(0, at),
    context: {
      text: folded,
      recordId: isFields(about) ? stringField(about, "record_id") : "",
      title: [ref, stringField(record, "title")].filter(Boolean).join(" "),
    },
  };
}

/**
 * One line of a transcript: a record; the result answering it, when it is a tool
 * call; or, when it is terminal input or output, the run of those it opens. With
 * whether its model and its time, to the minute, differ from the line's before.
 */
export type TranscriptRow = {
  readonly record: SessionRecord;
  readonly result: SessionRecord | null;
  readonly stream: readonly SessionRecord[] | null;
  readonly showModel: boolean;
  readonly showTime: boolean;
};

/**
 * `records`, in order, as the lines they show as. A tool call takes the first
 * record after it with its `call_id`, however far on: a batch of calls has its
 * results after all of them. A run of `trax run sh` terminal records
 * (`types/streams.py`) is one line.
 */
export function transcriptRows(records: readonly SessionRecord[]): TranscriptRow[] {
  const rows: TranscriptRow[] = [];
  const results = new Map<string, SessionRecord>();
  const called = new Set<string>();
  for (const record of records) {
    const id = stringField(record.payload ?? {}, "call_id");
    if (record.kind === "ToolCall") called.add(id);
    else if (id && called.has(id) && !results.has(id)) results.set(id, record);
  }
  const answered = new Set<SessionRecord>();
  let model = "";
  let minute = Number.NaN;
  let k = 0;
  while (k < records.length) {
    const record = records[k]!;
    let end = k + 1;
    while (TERMINAL.has(record.kind) && end < records.length && TERMINAL.has(records[end]!.kind)) end++;
    if (!answered.has(record)) {
      const result = record.kind === "ToolCall" ? (results.get(stringField(record.payload ?? {}, "call_id")) ?? null) : null;
      if (result) answered.add(result);
      const at = record.timestamp ? Math.floor(Date.parse(record.timestamp) / 60_000) : Number.NaN;
      rows.push({
        record,
        result,
        stream: TERMINAL.has(record.kind) ? records.slice(k, end) : null,
        // A model switch's note names the model itself.
        showModel: Boolean(record.model) && record.model !== model && record.kind !== "TurnContext",
        showTime: !Number.isNaN(at) && at !== minute,
      });
      model = record.model || model;
      minute = Number.isNaN(at) ? minute : at;
    }
    k = end;
  }
  return rows;
}

/** A run of terminal records as lines: what the command read, wrote, and wrote as errors. */
export function terminalLines(records: readonly SessionRecord[]): { readonly kind: "in" | "out" | "err"; readonly text: string }[] {
  return records.map((record) => ({
    kind: record.kind === "Stdin" ? "in" : record.kind === "Stderr" ? "err" : "out",
    text: stringField(record.payload ?? {}, "text") || (record.text ?? ""),
  }));
}

/**
 * `text` cut into prose and the blocks a harness wraps in its own tags, such as
 * `<system-reminder>…</system-reminder>` or codex's `<INSTRUCTIONS>`, each block
 * with its tag and its attributes' values (codex's `<codex_internal_context
 * source="goal">` is `codex_internal_context · goal`). A harness's tag names
 * hold a `-` or `_`, or are capitals, and HTML's as people write them are
 * neither, so a person's `<b>` stays. So does a tag in the prose's Markdown
 * code, fenced or inline, which is the code's text; a block's own text is not
 * Markdown, and shows as it is.
 */
export function harnessBlocks(text: string): { readonly tag: string; readonly text: string }[] {
  const blocks: { tag: string; text: string }[] = [];
  let at = 0;
  // A fence opens a line, and a backtick one's info string has no backtick (CommonMark).
  const marks = /^ {0,3}(`{3,}(?!.*`)|~{3,})|(`+)|<([a-z][a-z0-9]*[-_][a-z0-9_-]*|[A-Z][A-Z0-9_-]+)((?: [a-z_]+="[^"]*")*)>/gm;
  for (let mark = marks.exec(text); mark; mark = marks.exec(text)) {
    const [, fence, ticks, tag, attributes] = mark;
    if (fence) marks.lastIndex = fenceEnd(text, fence, marks.lastIndex);
    else if (ticks) marks.lastIndex = codeSpanEnd(text, ticks, marks.lastIndex);
    else {
      const close = text.indexOf(`</${tag}>`, marks.lastIndex);
      if (close < 0) continue;
      const named = [tag!, ...[...attributes!.matchAll(/="([^"]*)"/g)].map(([, value]) => value!)].join(" · ");
      blocks.push({ tag: "", text: text.slice(at, mark.index) }, { tag: named, text: text.slice(marks.lastIndex, close).trim() });
      at = marks.lastIndex = close + tag!.length + 3;
    }
  }
  blocks.push({ tag: "", text: text.slice(at) });
  return blocks.filter((block) => block.tag || block.text.trim());
}

/** A run of text in one style (`ansiRuns`). */
export type AnsiRun = { readonly text: string; readonly style: string };

/**
 * `text` as runs of one style each, read from its ANSI colour and style codes
 * (SGR), as the Claude and Codex CLIs keep a command's colours: each run's style
 * is its classes (`a-bold a-red`). Bright colours are their plain ones; black,
 * white, and 256-colour or true colour picks take the text's own colour, which
 * reads on either theme, and a background has none. Every other escape code (a
 * cursor move, a title) goes.
 */
export function ansiRuns(text: string): AnsiRun[] {
  return ansiReader()(text);
}

/**
 * A reader of one stream a piece at a time, each piece as `ansiRuns` reads it,
 * a style turned on in one piece holding in the next, as in a terminal: so the
 * tail of a cut output, or a terminal's next record, keeps the colour it is in.
 */
export function ansiReader(): (text: string) => AnsiRun[] {
  let [bold, dim, italic, underline, colour] = [false, false, false, false, ""];
  return (text) => {
    const runs: { text: string; style: string }[] = [];
    const push = (part: string) => {
      const style = [bold && "a-bold", dim && "a-dim", italic && "a-italic", underline && "a-underline", colour && `a-${colour}`]
        .filter(Boolean)
        .join(" ");
      const last = runs.at(-1);
      if (last?.style === style) last.text += part;
      else if (part) runs.push({ text: part, style });
    };
    let at = 0;
    for (const code of text.matchAll(/\u001b(?:\[([0-9;?]*)([A-Za-z])|\][^\u0007\u001b]*(?:\u0007|\u001b\\)?)/g)) {
      push(text.slice(at, code.index));
      at = code.index + code[0].length;
      if (code[2] !== "m") continue;
      const params = (code[1] || "0").split(";").map(Number);
      for (let k = 0; k < params.length; k++) {
        const param = params[k]!;
        if (param === 0) [bold, dim, italic, underline, colour] = [false, false, false, false, ""];
        else if (param === 1) bold = true;
        else if (param === 2) dim = true;
        else if (param === 3) italic = true;
        else if (param === 4) underline = true;
        else if (param === 22) [bold, dim] = [false, false];
        else if (param === 23) italic = false;
        else if (param === 24) underline = false;
        else if (param === 38 || param === 48) {
          colour = param === 38 ? "" : colour;
          k += params[k + 1] === 5 ? 2 : 4;
        } else if ((param >= 30 && param <= 37) || (param >= 90 && param <= 97)) colour = ANSI_COLOURS[param % 10]!;
        else if (param === 39) colour = "";
      }
    }
    push(text.slice(at));
    return runs;
  };
}

/**
 * A file read's text and the line numbers its tool put before each line (`cat
 * -n`'s `     1\t`, or Claude's `1→`), when every line has one, each one more
 * than the last; else no numbers and the text as it is.
 */
export function lineNumbers(text: string): { readonly numbers: readonly number[] | null; readonly text: string } {
  const lines = text.split("\n");
  const numbers: number[] = [];
  for (const [k, line] of lines.entries()) {
    if (k === lines.length - 1 && !line) break;
    const number = /^ *(\d+)(?:\t|→)/.exec(line);
    if (!number || (k && Number(number[1]) !== numbers[0]! + k)) return { numbers: null, text };
    numbers.push(Number(number[1]));
    lines[k] = line.slice(number[0].length);
  }
  return numbers.length ? { numbers, text: lines.join("\n") } : { numbers: null, text };
}

/** The first line of `text`, as a reasoning block's label: its Markdown heading or emphasis marks left out. */
export function headline(text: string): string {
  const line = text.trim().split("\n", 1)[0]!;
  return line.replace(/^#+\s*/, "").replace(/^(\*\*|__)(.*)\1$/, "$2");
}

/** How a part is named above its records. */
export function partLabel(part: SessionPart): string {
  const count = `${part.records.toLocaleString("en")} ${part.records === 1 ? "record" : "records"}`;
  if (part.part === -1) return `Legacy turns, backfilled · ${count}`;
  return [`Part ${part.part}`, part.name, part.format, count].filter(Boolean).join(" · ");
}

/**
 * At most `limit` characters of `text`, and how many it left out: tool output
 * can be whole files, and the raw record has the rest.
 */
export function clipText(text: string, limit = 2000): { readonly shown: string; readonly hidden: number } {
  return text.length > limit ? { shown: text.slice(0, limit), hidden: text.length - limit } : { shown: text, hidden: 0 };
}

/**
 * The first `head` and last `tail` lines of `text`, and how many lines between
 * them it left out: a failure is usually at the end of a long output.
 */
export function clipLines(
  text: string,
  head = 20,
  tail = 40,
): { readonly head: string; readonly hidden: number; readonly tail: string } {
  const lines = text.replace(/\n$/, "").split("\n");
  const hidden = lines.length - head - tail;
  return hidden > 0
    ? { head: lines.slice(0, head).join("\n"), hidden, tail: tail ? lines.slice(-tail).join("\n") : "" }
    : { head: text, hidden: 0, tail: "" };
}

/** How `record` shows but its attachments, read from its kind and payload. */
function shapeOf(record: SessionRecord): Shape {
  const payload = record.payload ?? {};
  const field = (name: string) => stringField(payload, name);
  const extra = isFields(payload.extra) ? payload.extra : {};
  // Claude keeps a tool result's block beside the record (`_read_tool_result`
  // in `trackinizer/lib/agent/sessions/claude.py`): its text, what the model read, when
  // the record's fields spell something else or nothing, as for a failed read.
  const read = isFields(extra.$result) ? stringField(extra.$result, "text") : "";
  // The server's stub for a body moved to its sidecar (`STUB_PAYLOAD` in
  // `server/store/session_bodies.py`); `plaintext_only` reads keep only the head
  // of its text.
  if (payload.$body === "offloaded") {
    const text = record.text ?? "";
    return {
      ...output(record.kind, text),
      meta: `the first ${text.length.toLocaleString("en")} characters; the rest is offloaded`,
    };
  }
  switch (record.kind) {
    case "UserMessage": {
      const text = field("content");
      if (codexContext(text)) return harnessText("Harness", text, harnessBlocks(text)[0]!.tag);
      if (extra.isMeta !== true) return typed(text);
      // Claude marks what the harness writes on the user's turn `isMeta`: no
      // person typed it. Its first line says what it is, a skill's text or a
      // hook's condition, as a length would not.
      const line = text.split("\n", 1)[0]!;
      return harnessText("Harness", text, line.length > 120 ? `${line.slice(0, 119)}…` : line);
    }
    case "AssistantMessage":
      return { shape: "message", label: "Assistant", text: field("content"), context: null, long: false };
    case "AgentToAgentMessage":
      return said(field("sender") ? `From ${field("sender")}` : "Agent message", { text: peerPayload(field("content")), context: null });
    case "SystemMessage":
      return harnessText(field("subtype") ? `System · ${field("subtype")}` : "System", field("content"));
    case "Thinking":
      // A sealed block's ciphertext is not read (`plaintext_only`), so it has nothing to show.
      return { shape: "thinking", label: "Thinking", text: field("content") || field("summary") };
    case "ToolCall":
      return toolCall(field("name") || "?", plain(payload.arguments));
    case "ShellCommandResult":
      if (typeof payload.stdout !== "string" && typeof payload.stderr !== "string") break;
      return {
        shape: "shell",
        label: "Shell command",
        command: display(plain(payload.command)),
        stdout: (field("stdout") || field("stderr")) ? field("stdout") : read,
        stderr: field("stderr"),
        exit: typeof payload.exit_code === "number" ? payload.exit_code : null,
      };
    case "FileEditResult": {
      const edits = plain(payload.edits);
      // A failed edit that changed nothing shows why, as what the model read.
      if (!Array.isArray(edits) || (!edits.length && failedOf(payload))) break;
      return { shape: "edit", label: "File edit", source: field("path"), lines: edits.flatMap(spliceLines) };
    }
    case "FileReadResult":
    case "FileWriteResult":
      if (typeof payload.content !== "string") break;
      return { ...output(record.kind, payload.content), source: field("path") };
    case "WebFetchResult":
      if (typeof payload.content !== "string") break;
      return {
        ...output(record.kind, payload.content),
        source: field("url"),
        meta: [
          typeof payload.code === "number" ? `HTTP ${payload.code}` : "",
          typeof payload.size === "number" ? bytes(payload.size) : "",
        ]
          .filter(Boolean)
          .join(" · "),
      };
    case "WebSearchResults": {
      const results = plain(payload.content);
      if (!Array.isArray(results)) break;
      return {
        shape: "search",
        label: "Web search",
        source: field("query"),
        results: results.filter(isFields).map((result) => ({
          url: stringField(result, "url"),
          title: stringField(result, "title"),
          snippet: stringField(result, "snippet"),
        })),
      };
    }
    case "AgentStatusResult":
      // A launch still under way has no content; its text, the server's own
      // projection, is then its prompt.
      return {
        ...output(record.kind, field("content") || (record.text ?? "")),
        meta: [
          field("state"),
          field("agent_kind"),
          typeof payload.duration_sec === "number" ? `${Math.round(payload.duration_sec)} s` : "",
        ]
          .filter(Boolean)
          .join(" · "),
      };
    case "UncategorizedToolResult": {
      const { text, meta } = toolOutput(typeof payload.content === "string" ? payload.content : record.text || read);
      return { ...output(record.kind, text), label: "Tool result", meta };
    }
    case "ContextClear":
      return {
        shape: "clear",
        label: "Context cleared",
        summary: field("summary"),
        prompt: field("system_prompt"),
        continues: field("cleared_session_id"),
      };
    case "TurnContext":
      return { shape: "note", label: field("model") ? `Model: ${field("model")}` : "Turn context", meta: "" };
    case "TokenUsage":
      return { shape: "note", label: "Token usage", meta: "" };
    case "ContextState": {
      if (field("kind") !== "queued_command") {
        // State with no prose, such as codex's `world_state`, keeps what it
        // stated in `extra`: codex's under `state`, Claude's under `attachment`.
        const stated = field("content") || fieldsText(extra.state ?? extra.attachment);
        return { shape: "folded", label: "Context state", summary: field("kind") || "Context state", text: stated };
      }
      // A message sent while the agent worked, which Claude delivers as an
      // attachment: a person's, or the harness's notice that a task ended. A
      // capture that did not read the attachment's `prompt` kept it there.
      const attachment = isFields(extra.attachment) ? extra.attachment : {};
      const origin = isFields(attachment.origin) ? stringField(attachment.origin, "kind") : "";
      const text = field("content") || stringField(attachment, "prompt");
      const notice = taskNotification(text);
      if (notice) return taskNotice(notice);
      return said(origin === "human" ? "User · queued" : ["Queued", origin].filter(Boolean).join(" · "), canvasContext(text));
    }
    case "ContextCompaction": {
      // Claude states why and how in `extra`; the summary is on the ContextClear that follows.
      const directions = stringField(extra, "directions");
      return {
        shape: "note",
        label: "Context compacted",
        meta: [stringField(extra, "trigger"), directions ? `directions: ${directions}` : ""].filter(Boolean).join(" · "),
      };
    }
    case "UncategorizedRecord":
      // A legacy turn backfilled to part -1 names its old kind, `legacy/<Kind>`.
      return { ...output(record.kind, record.text || fieldsText(payload.payload)), label: field("kind") || "Uncategorized" };
  }
  return { ...output(record.kind, record.text || read), source: field("path") || field("url") || field("query") };
}

/**
 * Whether a tool's result says it failed, whatever shape it shows as: an exit
 * other than 0, or its provider's error flag, sagent's `is_error` or that of
 * Claude's block (`_read_tool_result`), which a typed result carries too.
 */
function failedOf(payload: Fields): boolean {
  const extra = isFields(payload.extra) ? payload.extra : {};
  const block = isFields(extra.$result) && isFields(extra.$result.block) ? extra.$result.block : {};
  const exit = payload.exit_code;
  return (typeof exit === "number" && exit !== 0) || extra.is_error === true || block.is_error === true;
}

/**
 * Text the harness wrote, under `label`: folded under `summary` when it runs to
 * several screens, as a system prompt or a skill does.
 */
function harnessText(label: string, text: string, summary = `${text.length.toLocaleString("en")} characters`): Shape {
  return text.length > 1000 ? { shape: "folded", label, summary, text } : { shape: "message", label, text, context: null, long: false };
}

/**
 * What came on the user's turn, not from a harness's `isMeta`: a Claude Code
 * slash command, which it writes in its own tags, as typed, and what the command
 * printed as its output, as its TUI shows them; a background task's notice
 * (`taskNotice`); else the person's text, apart from any context the canvas
 * appended.
 */
function typed(text: string): Shape {
  const command = /^<command-name>([^<]*)<\/command-name>\s*<command-message>[^<]*<\/command-message>\s*<command-args>([\s\S]*)<\/command-args>$/.exec(text.trim());
  if (command) return said("User", { text: `${command[1]} ${command[2]}`.trim(), context: null });
  const printed = /^<local-command-stdout>([\s\S]*)<\/local-command-stdout>$/.exec(text.trim());
  if (printed) return said("Command output", { text: printed[1]!, context: null });
  const notice = taskNotification(text);
  if (notice) return taskNotice(notice);
  return said("User", canvasContext(text));
}

/** A background task's notice (`taskNotification`) by its status, summary and result, not its ids and file. */
function taskNotice(notice: { readonly [field: string]: string }): Shape {
  const label = ["Task notification", notice.status].filter(Boolean).join(" · ");
  return said(label, { text: [notice.summary, notice.result].filter(Boolean).join("\n\n"), context: null });
}

/** A person's or another agent's message, under `label`, folded when long (`isLong`). */
function said(label: string, { text, context }: { readonly text: string; readonly context: CanvasContext | null }): Shape {
  return { shape: "message", label, text, context, long: isLong(text) };
}

/**
 * A tool's output as a reader reads it, and what to say of it beside its name:
 * a codex script's (its `exec` tool) with each command's output as written, its
 * status and time as the note; a tool's one JSON object as its fields (`fieldsText`);
 * anything else as it is.
 */
function toolOutput(content: string): { readonly text: string; readonly meta: string } {
  const script = /^(Script [^\n]*)\n(Wall time [^\n]*)\nOutput:\n?\n?/.exec(content);
  if (script) return { text: scriptOutput(content.slice(script[0].length)), meta: `${script[1]} · ${script[2]}` };
  const value = /^\s*\{/.test(content) ? parsed(content) : null;
  return { text: isFields(value) ? fieldsText(value) : content, meta: "" };
}

/**
 * A codex script's printout with every command's result it printed as JSON (a
 * line holding `exec_command`'s or `write_stdin`'s answer, at any depth) as the
 * command's output, as written, under `[name · exit N]`, or `[session N]` while
 * it still runs. Every other line stays as the script printed it.
 */
function scriptOutput(printed: string): string {
  return printed
    .split("\n")
    .flatMap((line) => {
      const runs = commandRuns(/^\s*[[{]/.test(line) ? parsed(line) : null);
      if (!runs.length) return [line];
      return runs.flatMap((run) => {
        const { exit_code: exit, session_id: session } = run;
        const marks = [
          stringField(run, "name"),
          typeof exit === "number" ? `exit ${exit}` : "",
          typeof session === "number" ? `session ${session}` : "",
        ].filter(Boolean);
        const said = stringField(run, "output").replace(/\n$/, "");
        return [...(marks.length ? [`[${marks.join(" · ")}]`] : []), ...(said ? [said] : [])];
      });
    })
    .join("\n");
}

/**
 * The command results in `value`, outermost first: objects with an `output` and
 * the chunk or time a command's answer carries.
 */
function commandRuns(value: unknown): Fields[] {
  if (Array.isArray(value)) return value.flatMap(commandRuns);
  if (!isFields(value)) return [];
  if (typeof value.output === "string" && ("chunk_id" in value || "wall_time_seconds" in value)) return [value];
  return Object.values(value).flatMap(commandRuns);
}

/**
 * A provider's object as one `name: value` line per field, an object or a list
 * of them indented under its name, its strings as written and anything else as
 * `display` writes it, so a message at any depth reads as prose rather than as
 * escaped JSON.
 */
function fieldsText(value: unknown): string {
  return fieldLines(plain(value), "");
}

function fieldLines(value: unknown, indent: string): string {
  const deeper = `${indent}  `;
  if (Array.isArray(value)) {
    // An item's first line takes the dash in place of its indent.
    return value
      .map((item) => `${indent}- ${nests(item) ? fieldLines(item, deeper).slice(deeper.length) : display(item)}`)
      .join("\n");
  }
  if (!isFields(value)) return `${indent}${display(value)}`;
  return Object.entries(value)
    .map(([name, item]) => `${indent}${name}:${nests(item) ? `\n${fieldLines(item, deeper)}` : ` ${display(item)}`}`)
    .join("\n");
}

/** Whether `value` holds fields, so `fieldLines` draws it under its name rather than beside it. */
function nests(value: unknown): boolean {
  return (isFields(value) && Object.keys(value).length > 0) || (Array.isArray(value) && value.some(nests));
}

/** A tool's or another record's `text`, under its kind's name. */
function output(kind: string, text: string): Shape & { readonly shape: "output" } {
  return { shape: "output", label: sentence(kind.replace(/Results?$/, "")), source: "", meta: "", text };
}

/** Where a fence opened with `fence` ends: past the line that closes it, or at the end of `text`. */
function fenceEnd(text: string, fence: string, from: number): number {
  const close = new RegExp(`^ {0,3}${fence[0]}{${fence.length},}[ \\t]*$`, "gm");
  close.lastIndex = from;
  return close.exec(text) ? close.lastIndex : text.length;
}

/**
 * Where a code span opened with `ticks` ends: past the next run of as many
 * backticks, before a blank line ends its paragraph; `from` when none closes it,
 * since the run is then text.
 */
function codeSpanEnd(text: string, ticks: string, from: number): number {
  const close = new RegExp(`(?<!\`)${ticks}(?!\`)|\\n[ \\t]*\\n`, "g");
  close.lastIndex = from;
  return close.exec(text)?.[0] === ticks ? close.lastIndex : from;
}

/** A message's attachments as `2 attachments (image/png, 2.1 KB; …)`, sized from their base64; `""` when none. */
function attachmentsChip(attachments: unknown): string {
  const each = plain(attachments);
  if (!Array.isArray(each) || each.length === 0) return "";
  const named = each.filter(isFields).map((attachment) => {
    const data = stringField(attachment, "data");
    const size = Math.floor((data.length * 3) / 4) - (data.endsWith("==") ? 2 : data.endsWith("=") ? 1 : 0);
    return `${stringField(attachment, "mime_descriptor") || "file"}, ${bytes(size)}`;
  });
  return `${named.length} ${named.length === 1 ? "attachment" : "attachments"} (${named.join("; ")})`;
}

/** `count` bytes as `512 B`, `2.1 KB` or `3.4 MB`. */
function bytes(count: number): string {
  if (count < 1024) return `${count} B`;
  return count < 1024 ** 2 ? `${(count / 1024).toFixed(1)} KB` : `${(count / 1024 ** 2).toFixed(1)} MB`;
}

/** A tool call: the argument that says what it does, then the rest. */
function toolCall(name: string, args: unknown): Shape {
  const fields = isFields(args) ? args : {};
  const primary = ["command", "cmd", "file_path", "pattern", "path", "url", "query", "description", "input"].find((key) => key in fields);
  return {
    shape: "tool",
    label: "Tool call",
    name,
    primary: primary ? display(fields[primary]) : "",
    args: Object.entries(fields)
      .filter(([key]) => key !== primary)
      .map(([key, value]) => [key, display(value)] as const),
  };
}

/**
 * One splice as unified diff lines: the provider's context before it (its own
 * `@@` header included, when it wrote one), the removed and added lines, and its
 * context after; a header naming the line when the provider gave only that.
 * Lines count from the provider's header, or from the line it named less the
 * context before it.
 */
function spliceLines(splice: unknown): DiffLine[] {
  if (!isFields(splice)) return [];
  const lead = lines(stringField(splice, "lead"));
  const { start, count } = splice;
  const named = typeof start === "number" && !lead[0]?.startsWith("@@");
  let [old, next] = named ? [start - lead.length, start - lead.length] : [Number.NaN, Number.NaN];
  const marked: (readonly [DiffLine["kind"], string])[] = [
    ...(named ? [["hunk", `@@ -${start}${typeof count === "number" ? `,${count}` : ""} @@`] as const] : []),
    ...lead.map(contextLine),
    ...lines(stringField(splice, "before")).map((line) => ["del", `-${line}`] as const),
    ...lines(stringField(splice, "after")).map((line) => ["add", `+${line}`] as const),
    ...lines(stringField(splice, "trail")).map(contextLine),
  ];
  return marked.map(([kind, text], k): DiffLine => {
    if (kind === "hunk") {
      const header = named && k === 0 ? null : /^@@ -(\d+)(?:,\d+)? (?:\+(\d+))?/.exec(text);
      if (header) [old, next] = [Number(header[1]), Number(header[2] ?? header[1])];
      return { kind, text, line: null };
    }
    const line = kind === "del" ? old : next;
    if (kind !== "add") old++;
    if (kind !== "del") next++;
    return { kind, text, line: Number.isNaN(line) ? null : line };
  });
}

function contextLine(line: string): readonly [DiffLine["kind"], string] {
  return [line.startsWith("@@") ? "hunk" : "ctx", line];
}

/** `text`'s lines, without the empty one after a final newline. */
function lines(text: string): string[] {
  return text ? text.replace(/\n$/, "").split("\n") : [];
}

/**
 * `value` as text: a string as it is, a list of words as a shell would read them
 * (a shell running one script, `bash -lc <script>`, as the script, as Codex's TUI
 * shows it; one word as it is), anything else as JSON.
 */
function display(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  if (Array.isArray(value) && value.every((word) => typeof word === "string")) {
    // A one-word argv is a whole command line, as sagent writes one.
    if (value.length === 1) return value[0]!;
    const [shell, flag, script, ...rest] = value;
    if (/(^|\/)(ba|z)?sh$/.test(shell ?? "") && (flag === "-lc" || flag === "-c") && script !== undefined && !rest.length) return script;
    return value.map(shellWord).join(" ");
  }
  return JSON.stringify(value, null, 2);
}

/** `word` quoted only when a shell would split or expand it, as Python's `shlex.quote` does. */
function shellWord(word: string): string {
  return /^[\w@%+=:,./-]+$/.test(word) ? word : `'${word.replaceAll("'", `'"'"'`)}'`;
}

/** `value` without the codec's class names: each object without its `py/object`. */
function plain(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(plain);
  if (!isFields(value)) return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => key !== "py/object")
      .map(([key, item]) => [key, plain(item)]),
  );
}

function parsed(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** The kinds a `trax run sh` capture writes, one per terminal line. */
const TERMINAL: ReadonlySet<string> = new Set(["Stdin", "Stdout", "Stderr"]);

/** What each tool result kind did (`ToolKind`). */
const TOOL_RESULTS: ReadonlyMap<string, ToolKind> = new Map([
  ["ShellCommandResult", "command"],
  ["FileReadResult", "read"],
  ["FileEditResult", "edit"],
  ["FileWriteResult", "write"],
  ["WebSearchResults", "web"],
  ["WebFetchResult", "fetch"],
  ["AgentStatusResult", "agent"],
  ["UncategorizedToolResult", "call"],
]);

/**
 * What a call does, by its tool's name in lower case without `_`: Claude's,
 * Codex's and sagent's names for the same tools. Any other is a `call`.
 */
const TOOL_NAMES: ReadonlyMap<string, ToolKind> = new Map([
  ["bash", "command"],
  ["shell", "command"],
  ["execcommand", "command"],
  ["localshell", "command"],
  ["read", "read"],
  ["readfile", "read"],
  ["edit", "edit"],
  ["multiedit", "edit"],
  ["applypatch", "edit"],
  ["write", "write"],
  ["writefile", "write"],
  ["grep", "search"],
  ["glob", "search"],
  ["searchcode", "search"],
  ["list", "list"],
  ["ls", "list"],
  ["websearch", "web"],
  ["webfetch", "fetch"],
  ["agent", "agent"],
  ["task", "agent"],
  ["spawnagent", "agent"],
]);

/** SGR's eight colours in order; black and white take the text's own colour, which reads on either theme. */
const ANSI_COLOURS = ["", "red", "green", "yellow", "blue", "magenta", "cyan", ""];

function isFields(value: unknown): value is Fields {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `ShellCommand` → `Shell command`. */
function sentence(name: string): string {
  const words = name.replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function stringField(fields: Fields, name: string): string {
  const value = fields[name];
  return typeof value === "string" ? value : "";
}
