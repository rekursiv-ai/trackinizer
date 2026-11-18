import { useMemo, useState } from "react";
import { Pill } from "./SummaryPill";
import { sessionsByStart, type SessionDoc, type SessionRow } from "./summaryDoc";

const stamp = (iso: string) => (iso ? iso.slice(0, 16).replace("T", " ") : "");

/**
 * The campaign's sessions by start time, with what each came to: outcome, size, errors and
 * what it established. How the campaign was staffed (who spawned whom, what it cost) is not shown.
 */
export function SummarySessions({ rows, docs, onOpen }: {
  readonly rows: readonly SessionRow[];
  readonly docs: ReadonlyMap<string, SessionDoc>;
  readonly onOpen: (session: string) => void;
}) {
  const [filter, setFilter] = useState("");
  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return sessionsByStart(rows).filter((row) =>
      !needle || `${row.title} ${row.ref} ${docs.get(row.session)?.overview ?? ""}`.toLowerCase().includes(needle));
  }, [rows, docs, filter]);
  return <div className="sum-sessions">
    <div className="sum-toolbar">
      <input type="search" value={filter} onChange={(event) => setFilter(event.target.value)}
        placeholder="Filter by title or overview" aria-label="Filter sessions" />
      <span className="sum-count">{rows.length} {rows.length === 1 ? "session" : "sessions"}</span>
    </div>
    <table className="sum-table">
      <thead><tr>
        <th scope="col">Started</th><th scope="col">Session</th><th scope="col">Outcome</th>
        <th scope="col" className="num">Items</th><th scope="col" className="num">Errors</th>
        <th scope="col" className="num">Learnings</th>
      </tr></thead>
      <tbody>
        {shown.map((row) => {
          const overview = docs.get(row.session)?.overview;
          return <tr key={row.session}>
            <td className="sum-when">{stamp(row.started)}</td>
            <td>
              <button type="button" className="sum-link" disabled={!docs.has(row.session)} onClick={() => onOpen(row.session)}>
                {row.title}
              </button>
              {overview && <p className="sum-overview">{overview}</p>}
            </td>
            <td>{row.outcome && <Pill value={row.outcome} />}</td>
            <td className="num">{row.items}</td>
            <td className="num">{row.errors || ""}</td>
            <td className="num">{row.learnings || ""}</td>
          </tr>;
        })}
      </tbody>
    </table>
    {shown.length === 0 && <p className="sum-empty" role="status">No session matches.</p>}
  </div>;
}
