import { memo, type ReactNode } from "react";
import type { InquiryRow } from "../api/inquiries";
import { Selectable } from "../bulk/Selectable";
import { usd } from "../detail/fields";
import { dateTime } from "../detail/time";
import { formatRoute } from "../router/route";
import { Avatar, LabelChip, PriorityGlyph, StateGlyphs } from "../ui/glyphs";
import { KindIcon } from "../ui/kinds";

/**
 * One inquiry in a list, as the mock's `row`: a link to its detail.
 *
 * What it shows follows the row's own fields, not its kind's name: a row with a
 * `priority` field shows the priority glyph, one with `judgement` shows that and
 * its status once closed, and so on. Absent or empty values show nothing (COLD-01).
 *
 * With `onSelect`, it has a check for selecting it, and a shift-click selects
 * rows instead of opening one.
 */
export const Row = memo(function Row({
  row,
  mixed,
  focused,
  now,
  onFocus,
  selected = false,
  onSelect,
  context,
  note,
}: {
  row: InquiryRow;
  /** Several kinds in one list: the ref names the kind. */
  mixed: boolean;
  focused: boolean;
  now: number;
  onFocus: (id: string) => void;
  selected?: boolean;
  onSelect?: (id: string, range: boolean) => void;
  /** Where the row sits, after its title: a stream names the row's parent there. */
  context?: string;
  /** What the view says of the row besides its fields, before them: an outline's "also under". */
  note?: ReactNode;
}) {
  const link = (
    <a
      className={focused ? "row is-focused" : "row"}
      href={formatRoute({ name: "ref", kind: row.kind, seq: row.seq })}
      data-row={row.id}
      aria-current={focused ? "true" : undefined}
      onClick={(event) => {
        if (onSelect && event.shiftKey) {
          event.preventDefault();
          onSelect(row.id, true);
        } else {
          onFocus(row.id);
        }
      }}
    >
      {"priority" in row && <PriorityGlyph priority={row.priority ?? null} />}
      {row.priority != null && <span className="row-pri">{row.priority}</span>}
      <span className={mixed ? "row-ref wide" : "row-ref"}>
        {mixed ? (
          <>
            <KindIcon kind={row.kind} size={14} />
            <span className="mono-ref">
              {row.kind}#{row.seq}
            </span>
          </>
        ) : (
          `#${row.seq}`
        )}
      </span>
      <StateGlyphs status={row.status} judgement={row.judgement} />
      <span className="row-title">
        {row.title}
        {context && <span className="row-ctx"> · in {context}</span>}
      </span>
      <span className="row-meta">
        {note}
        <RowMeta row={row} now={now} />
      </span>
      {row.owner ? (
        <span className="row-owner" role="img" aria-label={row.owner}>
          <Avatar actor={row.owner} />
        </span>
      ) : (
        <span className="avatar-gap" />
      )}
      <span className="row-date" title={`Updated ${dateTime(row.modified)}`}>
        {shortAge(row.modified, now)}
      </span>
    </a>
  );
  if (!onSelect) return link;
  return (
    <Selectable id={row.id} name={`${row.kind}#${row.seq}`} selected={selected} onSelect={onSelect}>
      {link}
    </Selectable>
  );
});

/** The kind's own details, then every label. */
function RowMeta({ row, now }: { row: InquiryRow; now: number }) {
  const bits: ReactNode[] = [];
  for (const kind of row.issue_kind ?? []) {
    bits.push(<span key={`kind-${kind}`} className="kind-tag hide-sm">{kind}</span>);
  }
  const evidence = [...(row.proved_by ?? []), ...(row.favored_by ?? [])];
  if (evidence.length) {
    bits.push(
      <span key="ev" className="ev hide-sm" title="Evidence for / against">
        <span className="p">+{evidence.filter((e) => e.valence > 0).length}</span>{" "}
        <span className="n">−{evidence.filter((e) => e.valence < 0).length}</span>
      </span>,
    );
  }
  if (row.confidence != null) {
    bits.push(
      <span key="conf" className="conf" title="Author confidence (stored on the row)">
        <span className="conf-bar hide-sm">
          <i style={{ width: `${row.confidence * 100}%` }} />
        </span>
        {row.confidence.toFixed(2)}
      </span>,
    );
  }
  const paper = paperLine(row);
  if (paper) bits.push(<span key="paper" className="hide-sm">{paper}</span>);
  if (row.sha) bits.push(<span key="sha" className="mono">{row.sha.slice(0, 10)}</span>);
  if (row.url) bits.push(<span key="url" className="hide-sm">{host(row.url)}</span>);
  if (row.provider) bits.push(<span key="provider" className="kind-tag">{row.provider}</span>);
  if ("cli" in row) {
    bits.push(
      row.ended ? (
        <span key="ended">ended {shortAge(row.ended, now)}</span>
      ) : (
        <span key="live" className="live-badge">
          <span className="live-dot" />
          Live
        </span>
      ),
    );
    if (row.cli) bits.push(<span key="cli" className="kind-tag hide-sm">{row.cli}</span>);
    const cost = row.marginal_cost.agent_usd + row.marginal_cost.resource_usd;
    bits.push(<span key="cost" className="num hide-sm">{usd(cost)}</span>);
  }
  bits.push(...(row.labels ?? []).map((label) => <LabelChip key={`label-${label}`} label={label} />));
  return bits;
}

/** `Lovelace et al. · NeurIPS 2024`, from whatever of it the Paper has. */
function paperLine(row: InquiryRow): string {
  const authors = row.authors ?? [];
  const first = authors[0]?.trim().split(/\s+/).pop();
  const byline = first ? `${first}${authors.length > 1 ? " et al." : ""}` : "";
  const year = row.publish_date?.slice(0, 4) ?? "";
  const where = [row.venue, year].filter(Boolean).join(" ");
  return [byline, where].filter(Boolean).join(" · ");
}

/**
 * How long ago `iso` was, as the mock's rows write it to fit their narrow date
 * column: `now`, `5m`, `3h`, `2d`, then `Sep 12`.
 */
export function shortAge(iso: string, now: number): string {
  const seconds = (now - Date.parse(iso)) / 1000;
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h`;
  if (seconds < 7 * 86_400) return `${Math.floor(seconds / 86_400)}d`;
  return new Date(iso).toLocaleDateString("en", { month: "short", day: "numeric" });
}

function host(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}
