import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Diff, Output } from "../detail/transcript/Code";
import { recordView, type RecordView, type SearchResult } from "../detail/transcript/records";
import { type ToolSummary, toolSummary } from "../detail/transcript/steps";
import { momentOf, type ToolExchange } from "./chatLines";
import "../detail/transcript/transcript.css";

/**
 * A tool call between Chat's lines, as the CLI's own row: ⏺, the tool and what it acted
 * on, then what came back under a └. The argv keeps the grey tier and the payload is white,
 * so the machine's answer never reads as the line that asked for it.
 *
 * The row is one line tall at rest, and each of its two cuts carries its own ▸ beside it: the
 * command's gives back the whole command, the payload's every line the call returned. Cutting
 * either away was the failure — a real command runs past the column's 477 px and wrapped it into
 * 2 to 7 rows, and this store's 11 multi-line payloads run from 2 to 49 lines — so a row left
 * alone buried the answer that asked for it. Nothing is hidden behind a guess about length: the
 * command's ▸ appears only when the row itself measures narrower than the command is.
 *
 * The name is the one the record holds (`Bash`, `Read`, `ListJobs`), not a rewording of
 * it. The right margin, held on the row's own line after the argv so the two never part, says the
 * one thing a row still owes: how long the call took, from its own stamp and the result's, so it is absent when
 * either has no time or the two name one moment; how it failed (`exit 2`); or that no
 * result has answered it yet (`running`).
 * What shows when the row is opened is the transcript's own clip — its first 20 and last 40
 * lines — and the row asks for the rest; a web search instead opens into its source list.
 */
export function ChatToolCard({ step, kinds }: { readonly step: ToolExchange; readonly kinds: readonly string[] }) {
  const call = recordView(step.call);
  const result = step.result === null ? null : recordView(step.result);
  const summary = toolSummary(call, result);
  const failed = result?.failed ?? false;
  const argv = argvOf(call, result);
  const gutter = gutterOf(step, result, summary);
  const [open, setOpen] = useState(false);
  const [argvOpen, setArgvOpen] = useState(false);
  // Whether the row's own box is narrower than the command it prints: measured, never guessed at
  // a length, so a short command is never offered a ▸ that reveals nothing.
  const [clipped, setClipped] = useState(false);
  const arg = useRef<HTMLElement>(null);
  const measure = useCallback(() => {
    const box = arg.current;
    if (box) setClipped(box.scrollWidth > box.clientWidth + 1);
  }, []);
  useEffect(() => {
    measure();
    const box = arg.current;
    if (!box || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    return () => observer.disconnect();
  }, [measure, argv]);
  const lines = result ? payloadLines(result) + (result.attachments ? 1 : 0) : 0;
  const noun = result?.shape === "search" ? "sources" : "lines";
  return <article className="chat-line chat-line-tool" data-state={result === null ? "running" : failed ? "failed" : "done"}>
    <div className={`chat-run${open ? " open" : ""}${argvOpen ? " argv-open" : ""}`}>
      <b className="verb">{call.shape === "tool" ? call.name || "Tool" : summary.verb}</b>
      {" "}
      {argv ? <i className="arg" ref={arg}>{argv}</i> : null}
      {argv && (clipped || argvOpen)
        ? <Fold open={argvOpen} onToggle={() => setArgvOpen((current) => !current)}
          closed="the whole command" again="the command on one line again" />
        : null}
      {gutter ? <span className={`t${failed ? " failed" : result === null ? " quiet" : ""}`}>{gutter}</span> : null}
      {result ? <Payload result={result} language={summary.language} kinds={kinds} lines={lines} noun={noun} open={open}
        onToggle={() => setOpen((current) => !current)} /> : null}
    </div>
  </article>;
}

/** What the call acted on, whole: what the result says it read, else the argument that says what the call does. */
function argvOf(call: RecordView, result: RecordView | null): string {
  const where = result?.shape === "shell" ? result.command : result && "source" in result ? result.source : "";
  return where || (call.shape === "tool" ? call.primary : "");
}

/** The right margin: how it failed, or that it is still out, else how long it took and what it changed or found. */
function gutterOf(step: ToolExchange, result: RecordView | null, summary: ToolSummary): string {
  if (result === null) return "running";
  if (result.failed) return result.shape === "shell" && result.exit ? `exit ${result.exit}` : "failed";
  return elapsedOf(step) || summary.outcome;
}

/** What the call took, as its own stamp and the result's say: `1.62 s`, or nothing when either has no time or the two are the same moment. */
function elapsedOf({ call, result }: ToolExchange): string {
  const from = momentOf(call);
  const to = result === null ? null : momentOf(result);
  return from === null || to === null || to <= from ? "" : `${((to - from) / 1000).toFixed(2)} s`;
}

/** What came back, under the └, and what came with it. */
function Payload({ result, language, kinds, lines, noun, open, onToggle }: { readonly result: RecordView, readonly language: string, readonly kinds: readonly string[],
  readonly lines: number, readonly noun: string, readonly open: boolean, readonly onToggle: () => void }) {
  const body = payloadOf(result, language, kinds, open);
  const attachments = result.attachments ? <div className="quiet">{result.attachments}</div> : null;
  if (!body && !attachments) return null;
  return <div className={`out${open ? " open" : ""}`}>
    <span className="branch">└</span>
    <div className="lvl">{body}{attachments}</div>
    {lines > 1 && <Fold open={open} onToggle={onToggle}
      closed={`all ${lines.toLocaleString("en")} ${noun} this returned`}
      again={noun === "sources" ? "the source chips again" : "only the first line again"} />}
  </div>;
}

/**
 * One cut, one ▸, each beside the thing it gives back: the command's sits at the cut's right
 * edge on the row, the payload's at its own row's right edge. A press opens only what its own
 * glyph promised, so no control has to say two things at once.
 */
function Fold({ open, closed, again, onToggle }: { readonly open: boolean; readonly closed: string; readonly again: string; readonly onToggle: () => void }) {
  const say = open ? again : closed;
  return <button type="button" className="fold" aria-expanded={open} aria-label={`Show ${say}`}
    title={`Show ${say}`} onClick={onToggle}>{open ? "▾" : "▸"}</button>;
}

/** How many lines the record holds to show: the count the row's ▸ promises, off the payload itself. */
function payloadLines(result: RecordView): number {
  switch (result.shape) {
    case "shell":
      return count(result.stdout) + count(result.stderr);
    case "output":
      return count(result.text);
    case "edit":
      return result.lines.length;
    case "search":
      return result.results.length;
    default:
      return 0;
  }
}

function count(text: string): number {
  return text ? text.replace(/\n$/, "").split("\n").length : 0;
}

function payloadOf(result: RecordView, language: string, kinds: readonly string[], open: boolean): ReactNode | null {
  switch (result.shape) {
    case "shell":
      return (
        <>
          {result.stdout ? <Output text={result.stdout} failed={false} kinds={kinds} /> : null}
          {result.stderr ? <Output text={result.stderr} failed kinds={kinds} /> : null}
        </>
      );
    case "output":
      return result.text ? <Output text={result.text} failed={result.failed} kinds={kinds} /> : null;
    case "edit":
      return result.lines.length ? <Diff lines={result.lines} language={language} /> : null;
    case "search":
      // A search's hits are a source list, not a paragraph: the row of numbered domains is what
      // a reader scans to decide which one to open, so it stays closed at one line and the
      // titles and snippets are what its ▸ gives back.
      return result.results.length ? <Sources results={result.results} open={open} /> : null;
    default:
      return null;
  }
}

/**
 * The hits of a web search. Closed, one line of numbered chips — the marker a cited `[1]` in
 * the answer's prose answers to, in the same order. Open, each chip's title and what the
 * search said of it.
 *
 * The address comes out of a stored record, so it is only a link when its own scheme is one
 * that fetches; anything else prints as text rather than being clickable.
 */
function Sources({ results, open }: { readonly results: readonly SearchResult[]; readonly open: boolean }) {
  return <ol className="chat-sources">
    {results.map((hit, i) => {
      const href = linkable(hit.url);
      return <li key={`${hit.url}:${i}`}>
        {href
          ? <a className="src" href={href} target="_blank" rel="noreferrer noopener" title={hit.title || hit.url}>
            <span className="n" aria-hidden="true">{i + 1}</span>{domainOf(hit.url)}
          </a>
          : <span className="src plain" title={hit.url}><span className="n" aria-hidden="true">{i + 1}</span>{domainOf(hit.url) || hit.url}</span>}
        {open && (hit.title || hit.snippet)
          ? <p className="src-what">{hit.title}{hit.snippet ? <span className="src-said"> — {hit.snippet}</span> : null}</p>
          : null}
      </li>;
    })}
  </ol>;
}

/** The host a reader recognises: no scheme, no `www.`, no path. */
function domainOf(url: string): string {
  return url.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "").replace(/^www\./i, "").split("/")[0];
}

/** The address, only when its scheme fetches. A stored record is not a trusted author of `href`. */
function linkable(url: string): string {
  return /^https?:\/\//i.test(url) ? url : "";
}
