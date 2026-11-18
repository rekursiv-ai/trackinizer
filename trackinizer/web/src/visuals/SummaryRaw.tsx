import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "../api/client";
import { listSessionParts, readSessionRecords } from "../api/sessions";
import { anchorOf, type SessionDoc } from "./summaryDoc";

const WINDOW = 80;
const clip = (text: string, length: number) => (text.length > length ? `${text.slice(0, length - 1)}…` : text);
const kindOf = (kind: string) => kind.replace(/Message$/, "").replace(/Result$/, "").toLowerCase();

type Place = { readonly part: number; readonly afterIdx: number };
const before = (a: { part: number; idx: number }, b: { part: number; idx: number }) => a.part < b.part || (a.part === b.part && a.idx < b.idx);

/**
 * The session's own records, a window of them around the span the reader picked: what an item the
 * summary names says in the transcript. The transcript routes answer only a reader of the session's
 * audience, so a reader without it gets the server's refusal, shown as a note, and the records are
 * never held here.
 */
export function SummaryRaw({ sessionId, doc, focus }: {
  readonly sessionId: string;
  readonly doc: SessionDoc;
  readonly focus: { readonly start: number; readonly end: number } | null;
}) {
  const parts = useQuery({
    queryKey: ["visual", "summary", "parts", sessionId],
    queryFn: ({ signal }) => listSessionParts(sessionId, { signal }),
    retry: false,
  });
  const start = focus ? anchorOf(doc, focus.start) : undefined;
  const end = focus ? anchorOf(doc, focus.end) : undefined;
  // The window is a place chosen by the reader's moves; a new span moves it to that span.
  const [moved, setMoved] = useState<{ readonly for: string; readonly place: Place } | null>(null);
  const key = start ? `${start.part}:${start.idx}` : "";
  const first = parts.data?.[0]?.part ?? 0;
  const place: Place = moved && moved.for === key ? moved.place
    : start ? { part: start.part, afterIdx: Math.max(start.idx - 4, -1) } : { part: first, afterIdx: -1 };
  const records = useQuery({
    queryKey: ["visual", "summary", "raw", sessionId, place.part, place.afterIdx],
    queryFn: ({ signal }) => readSessionRecords(sessionId, { part: place.part, afterIdx: place.afterIdx, limit: WINDOW }, { signal }),
    enabled: parts.isSuccess,
    retry: false,
  });
  const error = parts.error ?? records.error;
  if (error instanceof ApiError && error.status === 403) {
    return <p className="sum-note" role="status">The transcript is available to staff only.</p>;
  }
  if (error) {
    return <p className="sum-note" role="alert">Could not load the transcript.
      <button type="button" className="btn ghost" onClick={() => void (parts.isError ? parts.refetch() : records.refetch())}>Retry</button></p>;
  }
  const partList = parts.data;
  const rows = records.data;
  if (!partList || !rows) return <p className="sum-note" aria-busy="true">Loading the transcript…</p>;
  const at = partList.findIndex((p) => p.part === place.part);
  const lastIdx = rows.at(-1)?.idx ?? place.afterIdx;
  const earlier = (): Place | null => {
    if (place.afterIdx >= 0) return { part: place.part, afterIdx: Math.max(place.afterIdx - WINDOW, -1) };
    const previous = partList[at - 1];
    return previous ? { part: previous.part, afterIdx: Math.max(previous.records - WINDOW - 1, -1) } : null;
  };
  const later = (): Place | null => {
    if (rows.length === WINDOW) return { part: place.part, afterIdx: lastIdx };
    const next = partList[at + 1];
    return next ? { part: next.part, afterIdx: -1 } : null;
  };
  const move = (to: Place | null) => to && setMoved({ for: key, place: to });
  const inSpan = (row: { idx: number }) => {
    if (!start) return false;
    const here = { part: place.part, idx: row.idx };
    return end ? !before(here, start) && !before(end, here) : here.idx === start.idx && here.part === start.part;
  };
  return <div className="sum-raw">
    <ol className="sum-raw-list" aria-label="Transcript records">
      {rows.map((row) => <li key={row.idx} className={inSpan(row) ? "sum-raw-in" : undefined}>
        <span className="sum-raw-idx">{row.idx}</span>
        <span className="sum-raw-kind">{kindOf(row.kind)}</span>
        <span className="sum-raw-text">{clip(row.text ?? "", 600)}</span>
      </li>)}
    </ol>
    {rows.length === 0 && <p className="sum-note" role="status">This part has no records yet.</p>}
    <div className="sum-raw-moves">
      <button type="button" className="btn ghost" disabled={!earlier()} onClick={() => move(earlier())}>Earlier</button>
      <span className="sum-count">part {place.part}</span>
      <button type="button" className="btn ghost" disabled={!later()} onClick={() => move(later())}>Later</button>
    </div>
  </div>;
}
