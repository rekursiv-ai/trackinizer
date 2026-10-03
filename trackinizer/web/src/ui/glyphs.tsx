// The mock's status, judgement and priority glyphs, avatars, label chips and ref
// chips (the design mock, "glyphs"), as components. One renderer per
// concept, used by every view, so two views can never draw the same value
// differently (COLD-14).
import type { ReactNode } from "react";
import { formatRoute } from "../router/route";
import "./glyphs.css";
import { KindIcon } from "./kinds";

/** The four priority bands' names, P0 to P3. */
export const PRIORITY_NAMES = ["P0 Critical", "P1 High", "P2 Medium", "P3 Low"] as const;

/** A status (`active`, `invalid`) drawn as itself; a value the mock never drew gets a plain ring. */
export function StatusGlyph({ status, size = 14 }: { status: string; size?: number }) {
  return (
    <Glyph label={capitalize(status)} size={size}>
      {STATUS_MARKS[status] ?? PLAIN_RING}
    </Glyph>
  );
}

/** A Belief's judgement (`proven`, `undecidable`) drawn as itself. */
export function JudgementGlyph({ judgement, size = 14 }: { judgement: string; size?: number }) {
  return (
    <Glyph label={capitalize(judgement)} size={size}>
      {JUDGEMENT_MARKS[judgement] ?? PLAIN_RING}
    </Glyph>
  );
}

/**
 * A row's state: a Belief's judgement, and its status too once it is not active;
 * any other row's status.
 */
export function StateGlyphs({
  status,
  judgement,
  size = 14,
}: {
  status: string;
  judgement?: string | null;
  size?: number;
}) {
  if (!judgement) return <StatusGlyph status={status} size={size} />;
  return (
    <>
      <JudgementGlyph judgement={judgement} size={size} />
      {status === "active" ? null : <StatusGlyph status={status} size={size} />}
    </>
  );
}

/**
 * The band of an Issue priority: `priority // 10`, capped at 3, so backlog (40)
 * is Low. Lower is more urgent. No priority has no band.
 */
export function priorityBand(priority: number | null | undefined): number | null {
  return priority == null ? null : Math.min(3, Math.floor(priority / 10));
}

/** `P1 High`, or `No priority`. */
export function priorityName(priority: number | null): string {
  const band = priorityBand(priority);
  return band === null ? "No priority" : PRIORITY_NAMES[band]!;
}

/** Chevrons for how far up the queue it sits: three for Critical, a dash for Low, dots for none. */
export function PriorityGlyph({ priority, size = 14 }: { priority: number | null; size?: number }) {
  const band = priorityBand(priority);
  return (
    <Glyph label={priorityName(priority)} size={size}>
      {band === null ? (
        <g fill="var(--ink-tertiary)">
          <circle cx="3.5" cy="7" r="1" />
          <circle cx="7" cy="7" r="1" />
          <circle cx="10.5" cy="7" r="1" />
        </g>
      ) : band === 3 ? (
        <path d="M3.5 7h7" stroke="var(--ink-tertiary)" strokeWidth="1.7" strokeLinecap="round" />
      ) : (
        CHEVRON_ROWS[band]!.map((y) => (
          <path
            key={y}
            d={`M3.5 ${y + 3}L7 ${y}l3.5 3`}
            fill="none"
            stroke={band === 0 ? "var(--urgent)" : "var(--ink-muted)"}
            strokeWidth="1.7"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ))
      )}
    </Glyph>
  );
}

/**
 * An actor's initial on a colour picked from its name. Actors are free text:
 * emails or agent handles. Hidden from assistive technology, so a view that
 * shows no name beside it must label it.
 */
export function Avatar({ actor, size = 18 }: { actor: string; size?: number }) {
  return (
    <span
      className="avatar"
      style={{ width: size, height: size, fontSize: Math.round(size * 0.5), background: `hsl(${hash(actor) % 360} 45% 45%)` }}
      title={actor}
      aria-hidden="true"
    >
      {(actor[0] ?? "?").toUpperCase()}
    </span>
  );
}

/** A label, with a dot coloured from its text. */
export function LabelChip({ label }: { label: string }) {
  return (
    <span className="label-chip">
      <LabelDot label={label} />
      {label}
    </span>
  );
}

/** The dot a label is drawn with, as in its chip. */
export function LabelDot({ label }: { label: string }) {
  return <i className="label-dot" style={{ background: LABEL_COLORS[hash(label) % LABEL_COLORS.length] }} />;
}

/** A link to an inquiry: its kind's icon and `Kind#seq`, with its title on hover. */
export function RefChip({ kind, seq, title }: { kind: string; seq: number; title: string }) {
  return (
    <a className="ref" href={formatRoute({ name: "ref", kind, seq })} title={title}>
      <KindIcon kind={kind} size={12} />
      {`${kind}#${seq}`}
    </a>
  );
}

/** `invalid` → `Invalid`. */
export function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function Glyph({ label, size, children }: { label: string; size: number; children: ReactNode }) {
  return (
    <svg className="ic" width={size} height={size} viewBox="0 0 14 14" role="img" aria-label={label}>
      {children}
    </svg>
  );
}

const CHECK = (
  <path
    d="M4.4 7.2l1.8 1.8 3.5-3.7"
    fill="none"
    stroke="var(--bg)"
    strokeWidth="1.6"
    strokeLinecap="round"
    strokeLinejoin="round"
  />
);
const CROSS = <path d="M5 5l4 4M9 5l-4 4" stroke="var(--bg)" strokeWidth="1.5" strokeLinecap="round" />;
const PLAIN_RING = <circle cx="7" cy="7" r="5.5" fill="none" stroke="var(--muted)" strokeWidth="1.6" />;

const STATUS_MARKS: { readonly [status: string]: ReactNode } = {
  active: (
    <>
      <circle cx="7" cy="7" r="5.5" fill="none" stroke="var(--green)" strokeWidth="1.6" />
      <circle cx="7" cy="7" r="2" fill="var(--green)" />
    </>
  ),
  complete: (
    <>
      <circle cx="7" cy="7" r="6.2" fill="var(--accent-hover)" />
      {CHECK}
    </>
  ),
  abandoned: (
    <>
      <circle cx="7" cy="7" r="6.2" fill="var(--ink-tertiary)" />
      {CROSS}
    </>
  ),
  invalid: (
    <>
      <circle cx="7" cy="7" r="5.5" fill="none" stroke="var(--red)" strokeWidth="1.6" />
      <path d="M3.3 10.7l7.4-7.4" stroke="var(--red)" strokeWidth="1.6" />
    </>
  ),
};

const JUDGEMENT_MARKS: { readonly [judgement: string]: ReactNode } = {
  proven: (
    <>
      <circle cx="7" cy="7" r="6.2" fill="var(--green)" />
      {CHECK}
    </>
  ),
  disproven: (
    <>
      <circle cx="7" cy="7" r="6.2" fill="var(--l-against)" />
      {CROSS}
    </>
  ),
  undecidable: (
    <>
      <circle cx="7" cy="7" r="5.5" fill="none" stroke="var(--violet)" strokeWidth="1.6" />
      <path d="M4.5 7h5" stroke="var(--violet)" strokeWidth="1.6" strokeLinecap="round" />
    </>
  ),
  unproven: (
    <>
      <circle cx="7" cy="7" r="5.5" fill="none" stroke="var(--amber)" strokeWidth="1.6" strokeDasharray="2.4 1.8" />
      <circle cx="7" cy="7" r="1.4" fill="var(--amber)" />
    </>
  ),
};

/** The chevrons' tops, by band. */
const CHEVRON_ROWS = [[1.5, 5, 8.5], [3.5, 7], [5.5]];
const LABEL_COLORS = ["#4ea7fc", "#26b5ce", "#4cb782", "#f2c94c", "#f2994a", "#eb5757", "#bb87fc", "#8a8f98", "#e58ad4"];

/** FNV-1a, as the mock hashes names to colours, so a name keeps its colour everywhere. */
function hash(text: string): number {
  let h = 2166136261;
  for (const char of text) {
    h ^= char.charCodeAt(0);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}
