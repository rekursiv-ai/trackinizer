import { createContext, Fragment, type ReactNode, use, useLayoutEffect, useRef, useState } from "react";
import type { SessionRecord } from "../../api/sessions";
import { Markdown } from "../../markdown/Markdown";
import { safeUrl } from "../../markdown/safeUrl";
import { formatRoute } from "../../router/route";
import { Icon } from "../../ui/icons";
import { Ansi, AnsiLines, Code, Diff, Output } from "./Code";
import {
  ansiReader,
  type CanvasContext,
  clipLines,
  harnessBlocks,
  headline,
  lineNumbers,
  type RecordView,
  type SearchResult,
  terminalLines,
} from "./records";
import { type ToolSummary, toolSummary } from "./steps";
import "./transcript.css";

/**
 * What the reader did to a part's steps: each one opened or closed, by its id,
 * and the one whose line had the focus as it went. A regroup draws a step
 * afresh, in or out of a group, so this outlives it (`Records`). The console,
 * which never regroups, has none, and each step keeps its own.
 */
export type StepMemory = { readonly open: Map<string, boolean>; focused: string };

export const StepMemoryContext = createContext<StepMemory | null>(null);

/**
 * What one record shows below its header line (`recordView`), for a transcript or
 * the console: its body, then its attachments as a chip, whatever its shape. A
 * tool call or result is a step (`ToolStep`), whose chip shows once it is open.
 */
export function RecordBody({ view, kinds }: { view: RecordView; kinds: readonly string[] }) {
  return (
    <>
      <ShapeBody view={view} kinds={kinds} />
      {view.attachments && !view.tool ? <span className="tr-chip">{view.attachments}</span> : null}
    </>
  );
}

/**
 * A tool step as one line that opens in place, as the Claude and Codex CLIs and
 * apps show one (`toolSummary`): what it did, to what, and how it went. Closed, a
 * command's or other tool's output shows its first three lines under it, and an
 * edit its first three diff lines; a failure starts open. Open, it shows the
 * call's arguments, its command or code in its language, and the result in full.
 * `aside` ends its line (a time, a model); `foot` ends what it shows open.
 */
export function ToolStep({
  id = "",
  call,
  result,
  kinds,
  aside = null,
  foot = null,
}: {
  id?: string;
  call: RecordView | null;
  result: RecordView | null;
  kinds: readonly string[];
  aside?: ReactNode;
  foot?: ReactNode;
}) {
  const summary = toolSummary(call, result);
  const attachments = result?.attachments || call?.attachments;
  return (
    <Step
      id={id}
      name={summary.verb}
      line={<Target summary={summary} />}
      outcome={summary.outcome}
      failed={summary.failed}
      aside={aside}
      startOpen={summary.failed}
      preview={result ? preview(result, summary) : null}
    >
      {() => (
        <>
          {call?.shape === "tool" ? <Arguments call={call} summary={summary} result={result} /> : null}
          {result ? <ResultBody result={result} summary={summary} shown={Boolean(call)} kinds={kinds} /> : null}
          {attachments ? <span className="tr-chip">{attachments}</span> : null}
          {foot}
        </>
      )}
    </Step>
  );
}

/**
 * Reasoning as one line, its first, as the CLIs fold it; open, it renders as
 * Markdown: its text, or its summary when the text is sealed (`recordView`).
 * Reasoning with neither is not drawn (`unreadable`).
 */
export function Reasoning({
  id = "",
  text,
  kinds,
  name = "",
  aside = null,
  foot = null,
}: {
  id?: string;
  text: string;
  kinds: readonly string[];
  name?: string;
  aside?: ReactNode;
  foot?: ReactNode;
}) {
  return (
    <Step id={id} name={name} line={<span className="tr-think-line">{headline(text)}</span>} aside={aside}>
      {() => (
        <>
          <Markdown source={text} kinds={kinds} className="md turn-body tr-think" images={false} />
          {foot}
        </>
      )}
    </Step>
  );
}

/**
 * One line that opens in place on what `children` draws, drawn only while open:
 * `name`, then `line`, then `outcome`, red when `failed`, then `aside`. Closed,
 * `preview` shows under it, given a function that opens it and moves the focus
 * to the line, since the preview's button goes as it opens.
 *
 * It is open as the reader last chose, or else as `startOpen` says now, so a
 * failure that arrives after it drew opens it. Given an `id` under a
 * `StepMemory`, the choice, and the focus on its line, outlive it, for when a
 * regroup draws it afresh.
 */
export function Step({
  id = "",
  name,
  line = null,
  outcome = "",
  failed = false,
  aside = null,
  startOpen = false,
  preview = null,
  children,
}: {
  id?: string;
  name: string;
  line?: ReactNode;
  outcome?: string;
  failed?: boolean;
  aside?: ReactNode;
  startOpen?: boolean;
  preview?: ((open: () => void) => ReactNode) | null;
  children: () => ReactNode;
}) {
  const memory = id ? use(StepMemoryContext) : null;
  const [chosen, setChosen] = useState(() => memory?.open.get(id));
  const open = chosen ?? startOpen;
  const choose = (next: boolean) => {
    setChosen(next);
    memory?.open.set(id, next);
  };
  const root = useRef<HTMLDivElement>(null);
  const summary = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    if (memory?.focused === id) {
      memory.focused = "";
      summary.current?.focus();
    }
    // React runs this before it takes the step's elements out, so the focus is
    // still in them, and a group's steps run it after the group, so the step
    // the focus is in has the last word.
    return () => {
      if (memory && root.current?.contains(document.activeElement)) memory.focused = id;
    };
  }, [memory, id]);
  return (
    <div className={failed ? "tr-step failed" : "tr-step"} ref={root}>
      {/* React's own changes, as `startOpen` changes, toggle it too: only one it did not make is the reader's. */}
      <details
        open={open}
        onToggle={(event) => {
          if (event.currentTarget.open !== open) choose(event.currentTarget.open);
        }}
      >
        <summary className="tool-row" ref={summary}>
          <Icon name={open ? "chevD" : "chevR"} size={12} />
          {name ? <span className="name">{name}</span> : null}
          {line}
          {outcome ? <span className="tr-outcome">{outcome}</span> : null}
          {aside}
        </summary>
        {open ? <div className="tr-step-body">{children()}</div> : null}
      </details>
      {!open && preview
        ? preview(() => {
            choose(true);
            summary.current?.focus();
          })
        : null}
    </div>
  );
}

/** A record's body, drawn by its shape. */
function ShapeBody({ view, kinds }: { view: RecordView; kinds: readonly string[] }) {
  switch (view.shape) {
    case "message": {
      const body = (
        <>
          {harnessBlocks(view.text).map((block, k) =>
            block.tag ? (
              <Fold key={k} summary={block.tag} text={block.text} />
            ) : (
              <Markdown key={k} source={block.text} kinds={kinds} className="md turn-body" images={false} breaks />
            ),
          )}
          {view.context ? <CanvasNote context={view.context} /> : null}
        </>
      );
      return view.long ? <Clamp>{body}</Clamp> : body;
    }
    case "thinking":
      return <Reasoning text={view.text} kinds={kinds} />;
    case "tool":
      return <ToolStep call={view} result={null} kinds={kinds} />;
    case "shell":
    case "edit":
    case "search":
      return <ToolStep call={null} result={view} kinds={kinds} />;
    case "clear":
      return (
        <>
          {view.continues ? (
            <p className="tr-meta">
              Continues session <span className="mono">{view.continues}</span>
            </p>
          ) : null}
          {view.summary ? <Markdown source={view.summary} kinds={kinds} className="md turn-body" images={false} /> : null}
          {view.prompt ? (
            <Fold summary={`System prompt · ${view.prompt.length.toLocaleString("en")} characters`} text={view.prompt} />
          ) : null}
        </>
      );
    case "folded":
      return <Fold summary={view.summary} text={view.text} />;
    case "output":
      if (view.tool) return <ToolStep call={null} result={view} kinds={kinds} />;
      return view.text ? <Output text={view.text} failed={view.failed} kinds={kinds} /> : null;
    case "note":
      return null;
  }
}

/** A step's target on its line: a file's name, then its directory, dimmed; anything else whole. */
function Target({ summary: { tool, target } }: { summary: ToolSummary }) {
  if (!target) return null;
  const slash = target.lastIndexOf("/");
  if ((tool === "read" || tool === "edit" || tool === "write" || tool === "list") && slash > 0 && slash < target.length - 1) {
    return (
      <>
        <code className="tool-arg tool-file" title={target}>
          {target.slice(slash + 1)}
        </code>
        <span className="tool-dir">{target.slice(0, slash)}</span>
      </>
    );
  }
  return (
    <code className="tool-arg" title={target}>
      {target}
    </code>
  );
}

/**
 * A call's arguments, open: the one that says what it does (a command, a
 * pattern, a patch) in full, in its language, unless its line shows it whole,
 * and each other argument by name, one over a line as code. An edit's or a
 * write's are what its result shows, its diff or its text, so they go once a
 * result that succeeded shows them, as the CLIs show only the diff.
 */
function Arguments({ call, summary, result }: { call: RecordView & { shape: "tool" }; summary: ToolSummary; result: RecordView | null }) {
  // The line cuts its target to one line, or names the result's own (the file a command read).
  const code = long(call.primary) || call.primary !== summary.target ? call.primary : "";
  const reproduced =
    !result?.failed &&
    (result?.shape === "edit" || (result?.shape === "output" && summary.tool === "write" && call.args.some(([, value]) => value === result.text)));
  const args = reproduced ? [] : call.args;
  return (
    <>
      {code ? <Code text={code} language={call.tool === "command" ? "bash" : summary.language} /> : null}
      {args.length ? (
        <dl className="tool-args">
          {args.map(([name, value]) => (
            <Fragment key={name}>
              <dt>{name}</dt>
              <dd>{long(value) ? <Code text={value} language={summary.language} /> : value}</dd>
            </Fragment>
          ))}
        </dl>
      ) : null}
    </>
  );
}

/**
 * A tool's result, open: a command's output and errors apart, an edit's diff, a
 * file read's or write's text in its language, search results, or any other
 * output, a failure's included. A command over one line that no call shows
 * shows here, in full. Only a read's text has line numbers its tool put there.
 */
function ResultBody({ result, summary, shown, kinds }: { result: RecordView; summary: ToolSummary; shown: boolean; kinds: readonly string[] }) {
  switch (result.shape) {
    case "shell":
      return (
        <>
          {!shown && long(result.command) ? <Code text={result.command} language="bash" /> : null}
          {result.stdout ? <Output text={result.stdout} failed={false} kinds={kinds} /> : null}
          {result.stderr ? <Output text={result.stderr} failed kinds={kinds} /> : null}
        </>
      );
    case "edit":
      return result.lines.length ? <Diff lines={result.lines} language={summary.language} /> : null;
    case "search":
      return <SearchResults results={result.results} />;
    case "output": {
      if (!result.text) return null;
      if (result.failed || (summary.tool !== "read" && summary.tool !== "write")) {
        return <Output text={result.text} failed={result.failed} kinds={kinds} />;
      }
      const { numbers, text } = summary.tool === "read" ? lineNumbers(result.text) : { numbers: null, text: result.text };
      return <Code text={text} language={summary.language} numbers={numbers} />;
    }
    default:
      // A result is one of the shapes above; any other has no body of a result.
      return null;
  }
}

/**
 * What a closed step shows of its result, as the CLIs preview a command's or
 * tool's output: the first three lines of a command's, an agent's or another
 * tool's output, or of an edit's diff, and how many more open it. A read, a
 * search, a listing or a fetch shows none, as their exploring rows show none.
 */
function preview(result: RecordView, { tool }: ToolSummary): ((open: () => void) => ReactNode) | null {
  if (result.shape === "edit") {
    const hidden = result.lines.length - 3;
    return (open) => (
      <div className="tr-preview">
        <Diff lines={result.lines.slice(0, 3)} language="" />
        {hidden > 0 ? <More hidden={hidden} open={open} /> : null}
      </div>
    );
  }
  // A command's output and errors, each a line apart, as its line counts them.
  const text =
    result.shape === "shell"
      ? [result.stdout, result.stderr]
          .filter(Boolean)
          .map((stream) => stream.replace(/\n$/, ""))
          .join("\n")
      : result.shape === "output"
        ? result.text
        : "";
  if (!text || !(tool === "command" || tool === "call" || tool === "agent")) return null;
  const clip = clipLines(text, 3, 0);
  return (open) => (
    <div className="tr-preview">
      <pre className="tool-out">
        <AnsiLines text={clip.head.replace(/\n$/, "")} />
      </pre>
      {clip.hidden ? <More hidden={clip.hidden} open={open} /> : null}
    </div>
  );
}

/** The button under a preview that opens its step: `+N lines`, as the CLIs word it. */
function More({ hidden, open }: { hidden: number; open: () => void }) {
  return (
    <button type="button" className="tr-show" onClick={open}>
      +{hidden.toLocaleString("en")} {hidden === 1 ? "line" : "lines"}
    </button>
  );
}

/** A web search's results, each title linked to its page when the link is safe, over its snippet. */
function SearchResults({ results }: { results: readonly SearchResult[] }) {
  return (
    <ol className="tr-search">
      {results.map((result, k) => (
        <li key={k}>
          {safeUrl(result.url) ? (
            <a href={result.url} target="_blank" rel="noopener noreferrer">
              {result.title || result.url}
            </a>
          ) : (
            <span>{result.title || result.url}</span>
          )}
          {result.snippet ? <p>{result.snippet}</p> : null}
        </li>
      ))}
    </ol>
  );
}

/**
 * A run of terminal records as one terminal: input as prompt lines, errors
 * marked, colours kept, a colour turned on in one record holding in the next,
 * and only the first 20 and last 40 lines until asked, as `Output`.
 */
export function Terminal({ records }: { records: readonly SessionRecord[] }) {
  const [whole, setWhole] = useState(false);
  const read = ansiReader();
  const lines = terminalLines(records).map((line) => ({ ...line, runs: read(line.text) }));
  const hidden = lines.length - 60;
  const shown = whole || hidden <= 0 ? lines : [...lines.slice(0, 20), null, ...lines.slice(-40)];
  return (
    <pre className="tool-out tr-term">
      {shown.map((line, k) =>
        line === null ? (
          <button key={k} type="button" className="tr-show" onClick={() => setWhole(true)}>
            Show {hidden.toLocaleString("en")} more lines{"\n"}
          </button>
        ) : (
          <span key={k} className={`t-${line.kind}`}>
            {line.kind === "in" ? "$ " : null}
            <Ansi runs={line.runs} />
          </span>
        ),
      )}
    </pre>
  );
}

/** A long message held to its first lines, and whole on request. */
function Clamp({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <div className="tr-clamp" data-open={open}>
        {children}
      </div>
      <button type="button" className="tr-show tr-clamp-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "Show less" : "Show all"}
      </button>
    </>
  );
}

/** `text` folded under `summary`. */
function Fold({ summary, text }: { summary: string; text: string }) {
  return (
    <details className="tr-fold">
      <summary>{summary}</summary>
      <pre className="tool-out">{text}</pre>
    </details>
  );
}

/** The context the canvas sent with a message, folded, and a link to the record it was about. */
function CanvasNote({ context }: { context: CanvasContext }) {
  return (
    <details className="tr-fold">
      <summary>
        Canvas context
        {context.recordId ? (
          <>
            {" · "}
            <a href={formatRoute({ name: "lookup", id: context.recordId })}>{context.title || context.recordId}</a>
          </>
        ) : null}
      </summary>
      <pre className="tool-out">{context.text}</pre>
    </details>
  );
}

/** Whether `text` runs over one line or 120 characters, so it shows as code of its own rather than inline. */
function long(text: string): boolean {
  return text.includes("\n") || text.length > 120;
}
