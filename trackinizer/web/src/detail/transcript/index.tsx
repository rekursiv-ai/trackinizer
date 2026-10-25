import {
  type DefaultError,
  type InfiniteData,
  infiniteQueryOptions,
  type QueryKey,
  queryOptions,
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { memo, type ReactNode, use, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import type { DetailRow } from "../../api/detail";
import { listSessionParts, readSessionRecords, sendSessionMessage, type SessionPart, type SessionRecord } from "../../api/sessions";
import { useMeta, useWriteMode } from "../../app/boot";
import { Composer } from "../../composer/Composer";
import { ReadFailure } from "../../ui/failure";
import { type DiffMode, DiffModeToggle, FileDiff, useDiffMode } from "./SplitDiff.tsx";
import { Refresh } from "../Refresh";
import { dateTime } from "../time";
import { placeholderHeight, useDrawnFrom } from "./drawing";
import { Reasoning, RecordBody, Step, type StepMemory, StepMemoryContext, Terminal, ToolStep } from "./RecordBody";
import { markBookkeeping, partLabel, type RecordView, recordView, type TranscriptRow, transcriptRows, unreadable } from "./records";
import { groupRows, groupSummary, toolSummary } from "./steps";
import "./transcript.css";

/** Records per request. The old UI read one page of 200 and stopped there (R44). */
const RECORDS_PAGE = 200;

/**
 * The synthetic part the capture writes its net worktree diff to -- the same
 * name the runner's `--diff` gives it. It is lifted out of the transcript into its
 * own Changes panel and left out of the transcript's record total.
 */
const WORKTREE_DIFF_PART = "worktree-diff";

/** Records a part reads at once: its newest on open, then as many earlier ones on each Load earlier. */
const BATCH_RECORDS = 1000;

/** A run of a part's records: the `count` after idx `after`. */
type RecordsRange = { readonly after: number; readonly count: number };

/** The transcript's reads, so the stream layer and Refresh can find them by key. */
export const transcriptQueries = {
  parts: (sessionId: string) =>
    queryOptions({
      queryKey: ["session", sessionId, "parts"],
      queryFn: ({ signal }) => listSessionParts(sessionId, { signal }),
      // Fresh objects from every read, changed or not: a part whose last read came
      // up short reads on at the next listing (`Part`), and a listing kept as the
      // same objects would not say one came.
      structuralSharing: false,
    }),
  /**
   * One part's records, a range of idx at a time: its newest `BATCH_RECORDS`
   * first, then earlier ranges before them, and later ones after them up to the
   * part's listed count. Idx is dense in a part, so the count bounds it, and a
   * read at the listed count is the end, not a sign of more. A later range starts
   * after the last record read, wherever the one before was asked to end: the
   * server lists a part's count before it writes the records
   * (`append_session_records_route`), so a read between the two comes back short.
   */
  records: (sessionId: string, { part, records }: SessionPart) =>
    // Its data named in full, since TanStack's default leaves the ranges `unknown`.
    infiniteQueryOptions<SessionRecord[], DefaultError, InfiniteData<SessionRecord[], RecordsRange>, QueryKey, RecordsRange>({
      queryKey: ["session", sessionId, "records", part],
      queryFn: ({ pageParam, signal }) => readRange(sessionId, part, pageParam, signal),
      initialPageParam: { after: Math.max(-1, records - BATCH_RECORDS - 1), count: Math.min(records, BATCH_RECORDS) },
      getPreviousPageParam: (_first: SessionRecord[], _pages: SessionRecord[][], { after }: RecordsRange): RecordsRange | undefined => {
        const start = Math.max(-1, after - BATCH_RECORDS);
        return after < 0 ? undefined : { after: start, count: after - start };
      },
      getNextPageParam: (last: SessionRecord[], _pages: SessionRecord[][], { after }: RecordsRange): RecordsRange | undefined => {
        const end = last.at(-1)?.idx ?? after;
        return end < records - 1 ? { after: end, count: Math.min(records - 1 - end, BATCH_RECORDS) } : undefined;
      },
    }),
};

/**
 * An AgentSession's transcript: every part the listing names, part -1
 * included, each showing its newest records, with earlier ones on request and
 * each record's raw JSON a click away, and a box at its foot for messaging the
 * session while it runs. Nothing for other kinds. When the session's id comes
 * through the stream, the listing is read again, and each part reads only what
 * was appended to it (`src/live/detail.ts`).
 */
export function Transcript({ row }: { row: DetailRow }) {
  return row.kind === "AgentSession" ? <SessionTranscript row={row} /> : null;
}

function SessionTranscript({ row }: { row: DetailRow }) {
  const { id } = row;
  const parts = useQuery(transcriptQueries.parts(id));
  const heading = useId();
  // The worktree diff is its own panel, not a transcript part, and is left out
  // of the record total.
  const diffPart = parts.data?.find((part) => part.name === WORKTREE_DIFF_PART) ?? null;
  const shown = parts.data?.filter((part) => part.name !== WORKTREE_DIFF_PART) ?? [];
  const total = shown.reduce((sum, part) => sum + part.records, 0);
  return (
    <>
      {diffPart ? <Changes sessionId={id} part={diffPart} /> : null}
      <section className="sec" aria-labelledby={heading} data-section="transcript">
        <div className="sec-h">
          <h2 id={heading}>
            Transcript{" "}
            {parts.data ? (
              <span className="count">
                {total.toLocaleString("en")} {total === 1 ? "record" : "records"}
              </span>
            ) : null}
          </h2>
          <span className="spacer" />
          <Refresh queryKey={["session", id]} title="Read every loaded record again; new ones arrive on their own" />
        </div>
        {!parts.data ? (
          parts.isError ? (
            <ReadFailure error={parts.error} retry={() => void parts.refetch()} />
          ) : (
            <p className="unset">Loading the transcript…</p>
          )
        ) : shown.length === 0 ? (
          <p className="unset">No records captured yet.</p>
        ) : (
          shown.map((part) => <Part key={part.part} sessionId={id} part={part} named={shown.length > 1} />)
        )}
        <SessionComposer row={row} />
      </section>
    </>
  );
}

/**
 * The net `git diff` of the files this session edited (the runner's `--diff`), each
 * file drawn by the shared split/unified diff view, the one trax.files and
 * campaign lineage draw with. It reads a key OUTSIDE the `records` slot so the
 * live layer refetches it on every wake (`keyedBy` in `src/live/detail.ts`):
 * the diff part is overwritten whole each turn, which the parts listing's
 * record count cannot reveal.
 */
function Changes({ sessionId, part }: { sessionId: string; part: SessionPart }) {
  const heading = useId();
  const [mode, setMode] = useDiffMode();
  const query = useQuery({
    queryKey: ["session", sessionId, "worktree-diff"],
    queryFn: ({ signal }) =>
      readSessionRecords(sessionId, { part: part.part, afterIdx: -1, limit: Math.max(part.records, 1) }, { signal }),
  });
  const records = query.data ?? [];
  return (
    <section className="sec" aria-labelledby={heading} data-section="changes">
      <div className="sec-h">
        <h2 id={heading}>
          Changes{" "}
          {query.data ? (
            <span className="count">
              {records.length.toLocaleString("en")} {records.length === 1 ? "file" : "files"}
            </span>
          ) : null}
        </h2>
        {records.length > 0 ? <DiffModeToggle mode={mode} onChange={setMode} /> : null}
        <span className="spacer" />
        <Refresh queryKey={["session", sessionId, "worktree-diff"]} title="Read the diff again; it refreshes on its own as the session edits files" />
      </div>
      {!query.data ? (
        query.isError ? (
          <ReadFailure error={query.error} retry={() => void query.refetch()} />
        ) : (
          <p className="unset">Loading changes…</p>
        )
      ) : records.length === 0 ? (
        <p className="unset">No file changes yet.</p>
      ) : (
        <div className="sd-patch" role="group" aria-label="Changed files">
          {records.map((record) => (
            <ChangedFile key={record.idx} record={record} mode={mode} />
          ))}
        </div>
      )}
    </section>
  );
}

/**
 * One file of the worktree diff: its path, then its hunks drawn by the shared
 * `FileDiff`. A record with no hunk is a note the capture left where a diff
 * would be (a binary or over-size file, or a count of the rest); its message
 * shows alone, under its path when it names one. A file's diff opens at its
 * `@@` hunk and a note never does, which tells the two apart; the aggregate
 * count's pseudo-path opens with "(", so it names no file.
 */
function ChangedFile({ record, mode }: { record: SessionRecord; mode: DiffMode }) {
  const view = recordView(record);
  if (view.shape !== "edit") return null;
  const body = view.lines.map((line) => line.text);
  const path = view.source;
  const diffing = view.lines[0]?.kind === "hunk";
  return (
    <section className="sd-file" aria-label={path || "Note"} data-path={path}>
      {path && !path.startsWith("(") ? <h4 className="sd-path">{path}</h4> : null}
      {diffing ? <FileDiff file={{ path, head: [], body }} mode={mode} /> : <pre className="sd-note">{body.join("\n")}</pre>}
    </section>
  );
}

/**
 * A writer's box for messaging the session (`POST /api/sessions/{id}/inbound`):
 * its run types each message into the session's terminal, from the
 * signed-in user. None once the session has ended, nor for a viewer, whose sends
 * the server refuses. It never reads the session's queue (`GET .../inbound`):
 * that read takes the agent's messages, and passes for a run polling.
 */
function SessionComposer({ row }: { row: DetailRow }) {
  const mode = useWriteMode();
  if (mode === "hidden") return null;
  if (row.ended) {
    return <p className="unset tr-composer">The session has ended, so it takes no messages.</p>;
  }
  const owner = typeof row.owner === "string" && row.owner ? row.owner : "the session";
  return (
    <div className="tr-composer">
      <Composer
        send={async (text, key) => {
          await sendSessionMessage(row.id, text, key);
          return `Queued for ${owner}`;
        }}
        target={row.id}
        enabled={mode === "enabled"}
        placeholder="Message the agent: its run types it into the session's terminal"
        failure={(error) =>
          error instanceof ApiError && error.status === 409
            ? `Not connected: no run is polling this session (${error.detail}).`
            : `Could not send: ${error.message}. Retry sends the same message once.`
        }
      />
    </div>
  );
}

/**
 * One part's records: its newest on open, and Load earlier for the ones before
 * them. When its listed count grows, it reads the records after the last it
 * read; when the count drops below them, the capture restarted, and it reads its
 * newest afresh. A read that came back short of its range reads on from there at
 * the next listing, count grown or not, and not before: at once, it would ask
 * again and again until the server wrote the records.
 *
 * A read that fails shows here, with Retry for that read. A refresh that fails
 * keeps the records, and the section's header says so (`Refresh`).
 */
function Part({ sessionId, part, named }: { sessionId: string; part: SessionPart; named: boolean }) {
  const queryClient = useQueryClient();
  const query = useInfiniteQuery(transcriptQueries.records(sessionId, part));
  const { data, hasNextPage, hasPreviousPage, isFetching, isError, fetchNextPage, fetchPreviousPage } = query;
  const records = useMemo(() => data?.pages.flat() ?? [], [data]);
  const restarted = part.records <= (records.at(-1)?.idx ?? -1);
  useEffect(() => {
    if (restarted) void queryClient.resetQueries({ queryKey: ["session", sessionId, "records", part.part], exact: true });
  }, [restarted, queryClient, sessionId, part.part]);
  const range = data?.pageParams.at(-1);
  const short = range !== undefined && (data?.pages.at(-1)?.at(-1)?.idx ?? range.after) < range.after + range.count;
  // The listing the last read was made under: a short read waits for another.
  const listed = useRef(part);
  // Keyed on the data too: a read that answers at once can land without a render
  // that shows it fetching, and nothing else would change to ask for the next.
  useEffect(() => {
    if (!hasNextPage || isFetching || isError || (short && listed.current === part)) return;
    listed.current = part;
    void fetchNextPage();
  }, [hasNextPage, data, isFetching, isError, fetchNextPage, short, part]);
  const { kinds } = useMeta();
  return (
    <div className="tr-part" data-part={part.part}>
      {named || part.part === -1 ? <h3 className="tr-part-h">{partLabel(part)}</h3> : null}
      {query.isFetchPreviousPageError ? (
        <ReadFailure error={query.error} retry={() => void fetchPreviousPage()} />
      ) : data && hasPreviousPage ? (
        <p className="tr-more">
          Showing {records.length.toLocaleString("en")} of {part.records.toLocaleString("en")} records.
          <button type="button" className="btn" disabled={isFetching} onClick={() => void fetchPreviousPage()}>
            {query.isFetchingPreviousPage ? "Loading…" : "Load earlier"}
          </button>
        </p>
      ) : null}
      {data ? <Records records={records} kinds={kinds} /> : null}
      {!data && query.isError ? (
        <ReadFailure error={query.error} retry={() => void query.refetch()} />
      ) : query.isFetchNextPageError ? (
        <ReadFailure error={query.error} retry={() => void fetchNextPage()} />
      ) : query.isPending ? (
        <p className="unset" role="status">
          Loading records…
        </p>
      ) : records.length === 0 ? (
        <p className="unset">No records in this part.</p>
      ) : null}
    </div>
  );
}

/**
 * A part's loaded records as lines (`transcriptRows`), each run of tool steps
 * folded into one (`groupRows`), the newest drawn first (`useDrawnFrom`), and its
 * bookkeeping (`markBookkeeping`) only after its toggle; a record with nothing
 * to read (`unreadable`) neither drawn nor counted. What the reader did to its
 * steps outlives each (`StepMemory`). Mounted only while the part has data, so a
 * restarted part draws afresh.
 */
function Records({ records: loaded, kinds }: { records: readonly SessionRecord[]; kinds: readonly string[] }) {
  const records = useMemo(() => loaded.filter((record) => !unreadable(record)), [loaded]);
  const [memory] = useState<StepMemory>(() => ({ open: new Map(), focused: "" }));
  const [bookkeepingShown, setBookkeepingShown] = useState(false);
  const bookkeeping = useMemo(() => markBookkeeping(records), [records]);
  const bookkeepingCount = bookkeeping.filter(Boolean).length;
  const from = useDrawnFrom(records[0]?.idx ?? 0, records.at(-1)?.idx ?? -1);
  const items = useMemo(
    () => groupRows(transcriptRows(records.filter((record, k) => record.idx >= from && (bookkeepingShown || !bookkeeping[k])))),
    [records, from, bookkeepingShown, bookkeeping],
  );
  return (
    <StepMemoryContext value={memory}>
      {bookkeepingCount ? (
        <button type="button" className="btn ghost tr-noise" onClick={() => setBookkeepingShown((shown) => !shown)}>
          {bookkeepingShown ? "Hide" : "Show"} {bookkeepingCount.toLocaleString("en")} bookkeeping{" "}
          {bookkeepingCount === 1 ? "record" : "records"}
        </button>
      ) : null}
      <ol className="transcript">
        {items.map((item) =>
          "group" in item ? (
            <StepGroup key={`g${item.group[0]!.record.idx}`} rows={item.group} kinds={kinds} />
          ) : (
            <Line key={item.record.idx} row={item} kinds={kinds} />
          ),
        )}
      </ol>
    </StepMemoryContext>
  );
}

/** A range's records, `RECORDS_PAGE` a request, all its requests at once. */
async function readRange(sessionId: string, part: number, { after, count }: RecordsRange, signal: AbortSignal): Promise<SessionRecord[]> {
  const starts = Array.from({ length: Math.ceil(count / RECORDS_PAGE) }, (_, k) => after + k * RECORDS_PAGE);
  const pages = await Promise.all(
    starts.map((start) =>
      readSessionRecords(sessionId, { part, afterIdx: start, limit: Math.min(RECORDS_PAGE, after + count - start) }, { signal }),
    ),
  );
  return pages.flat();
}

/**
 * A run of tool steps (`groupRows`) as one line that counts what they did
 * (`groupSummary`) and how many failed, its time the first step's; open, each
 * step on its line, and the reasoning between them. Closed by default, as the
 * Claude app keeps a turn's tool calls; its steps draw only once it is open. A
 * group drawn about a step the reader opened, or whose line had the focus,
 * starts open, so a regroup neither closes that step nor drops the focus.
 */
function StepGroup({ rows, kinds }: { rows: readonly TranscriptRow[]; kinds: readonly string[] }) {
  const memory = use(StepMemoryContext);
  const ids = rows.map((row) => String(row.record.idx));
  const [holds, setHolds] = useState(() => ids.some((id) => memory?.open.get(id)));
  // Once, as the group is drawn: the line that had the focus says so only as it
  // goes (`Step`), after this drew, and before the page paints.
  useLayoutEffect(() => {
    if (memory && ids.includes(memory.focused)) setHolds(true);
  }, []);
  const steps = rows
    .filter((row) => row.record.kind !== "Thinking")
    .map(({ record, result }) => {
      const view = recordView(record);
      return view.shape === "tool" ? toolSummary(view, result && recordView(result)) : toolSummary(null, view);
    });
  const failed = steps.filter((step) => step.failed).length;
  const [first] = rows;
  return (
    <li className="tr-group" data-group={first!.record.idx}>
      <Step
        id={`g${first!.record.idx}`}
        name={groupSummary(steps)}
        outcome={failed ? `${failed.toLocaleString("en")} failed` : ""}
        failed={failed > 0}
        aside={<Stamp record={first!.record} showModel={first!.showModel} showTime={first!.showTime} />}
        startOpen={holds}
      >
        {() => (
          <ol className="transcript">
            {rows.map((row) => (
              <Line key={row.record.idx} row={row} kinds={kinds} />
            ))}
          </ol>
        )}
      </Step>
    </li>
  );
}

/**
 * One line: a record, a tool step (a call with the result that answers it, or a
 * result alone) or reasoning as one line that opens in place, or a run of
 * terminal records. Memoized by its row's parts, since a part re-renders as each
 * later page arrives.
 */
const Line = memo(
  function Line({ row: { record, result, stream, showModel, showTime }, kinds }: { row: TranscriptRow; kinds: readonly string[] }) {
    const view = recordView(record);
    const stamp = <Stamp record={record} showModel={showModel} showTime={showTime} />;
    if (!stream && view.tool) {
      const answer = result && recordView(result);
      return (
        <li className="turn turn-tool" data-idx={record.idx} data-result={result?.idx}>
          <ToolStep
            id={String(record.idx)}
            call={view.shape === "tool" ? view : null}
            result={view.shape === "tool" ? answer : view}
            kinds={kinds}
            aside={stamp}
            foot={<Raw records={result ? [record, result] : [record]} />}
          />
        </li>
      );
    }
    if (!stream && view.shape === "thinking") {
      return (
        <li className="turn turn-thinking" data-idx={record.idx}>
          <Reasoning id={String(record.idx)} text={view.text} kinds={kinds} name="Thinking" aside={stamp} foot={<Raw records={[record]} />} />
        </li>
      );
    }
    const placeholder = !stream && view.shape === "message" ? `auto ${placeholderHeight(view.text, view.long)}px` : undefined;
    return (
      <li className={`turn turn-${stream ? "terminal" : view.shape}`} data-idx={record.idx} style={{ containIntrinsicSize: placeholder }}>
        <Block record={record} view={view} stream={stream} stamp={stamp} kinds={kinds} />
      </li>
    );
  },
  (before, after) =>
    before.kinds === after.kinds &&
    before.row.record === after.row.record &&
    before.row.result === after.row.result &&
    before.row.stream?.length === after.row.stream?.length &&
    before.row.showModel === after.row.showModel &&
    before.row.showTime === after.row.showTime,
);

/**
 * One record, or run of terminal records: what it is, where it acted, its model
 * and time when they changed (`stamp`), its body, and its raw JSON on request.
 */
function Block({
  record,
  view,
  stream,
  stamp,
  kinds,
}: {
  record: SessionRecord;
  view: RecordView;
  stream: readonly SessionRecord[] | null;
  stamp: ReactNode;
  kinds: readonly string[];
}) {
  const [raw, setRaw] = useState(false);
  return (
    <>
      <div className="turn-h">
        <b>{stream ? "Terminal" : view.label}</b>
        {"source" in view && view.source ? <span className="mono turn-src">{view.source}</span> : null}
        {"meta" in view && view.meta ? <span className="turn-meta">{view.meta}</span> : null}
        {stamp}
        <span className="spacer" />
        <span className="turn-idx">#{record.idx}</span>
        <button type="button" className="turn-raw" aria-expanded={raw} onClick={() => setRaw((open) => !open)}>
          Raw
        </button>
      </div>
      {stream ? <Terminal records={stream} /> : <RecordBody view={view} kinds={kinds} />}
      {raw ? <pre className="turn-json">{JSON.stringify(stream ?? record, null, 2)}</pre> : null}
    </>
  );
}

/** A line's model and time, each when it changed from the line before. */
function Stamp({ record, showModel, showTime }: { record: SessionRecord; showModel: boolean; showTime: boolean }) {
  return (
    <>
      {showModel ? <span className="turn-model">{record.model}</span> : null}
      {showTime && record.timestamp ? (
        <time dateTime={record.timestamp} title={record.timestamp}>
          {dateTime(record.timestamp)}
        </time>
      ) : null}
    </>
  );
}

/** The foot of an open step: its records' idx, and their raw JSON on request. */
function Raw({ records }: { records: readonly SessionRecord[] }) {
  const [raw, setRaw] = useState(false);
  return (
    <>
      <div className="tr-foot">
        {records.map((record) => (
          <span key={record.idx} className="turn-idx">
            #{record.idx}
          </span>
        ))}
        <button type="button" className="turn-raw" aria-expanded={raw} onClick={() => setRaw((open) => !open)}>
          Raw
        </button>
      </div>
      {raw ? <pre className="turn-json">{JSON.stringify(records, null, 2)}</pre> : null}
    </>
  );
}
