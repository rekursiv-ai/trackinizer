import { useQuery } from "@tanstack/react-query";
import { Fragment, type ReactNode, useId } from "react";
import type { Detail } from "../api/detail";
import { useMeta } from "../app/boot";
import { PropertyEditor } from "../editors/PropertyEditor";
import { safeUrl } from "../markdown/safeUrl";
import { formatRoute } from "../router/route";
import { ReadFailure } from "../ui/failure";
import { Avatar, capitalize, JudgementGlyph, LabelChip, PriorityGlyph, priorityName, StatusGlyph } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { type Field, usd } from "./fields";
import { detailQueries } from "./queries";
import { calendarDate, dateTime } from "./time";

/**
 * The properties panel: every panel field of the row's kind, set or not, each
 * value the button that edits it for a user who may write.
 *
 * Properties holds status, the Belief's judgement and the author's confidence,
 * with the server's evidence confidence under its own name beside it, priority,
 * type, owner, subscribers and labels; Details holds the rest, then the row's
 * identity.
 */
export function Properties({ detail, fields }: { detail: Detail; fields: readonly Field[] }) {
  const { edges } = useMeta();
  const row = detail.self;
  // The server derives evidence confidence from `proves` edges, so it exists
  // exactly for the kinds a `proves` edge can point at; others answer 404.
  const evidence = edges.proves?.to_kinds.includes(row.kind) ? (
    <EvidenceConfidence id={row.id} proves={detail.backlinks.proves?.length ?? 0} />
  ) : null;
  const properties = fields.filter((field) => field.look.place === "properties");
  const evidenceAfter = properties.findIndex((field) => field.name === "confidence");
  const id = useId();
  return (
    <aside className="props" aria-label="Properties">
      <section className="props-group" aria-labelledby={`${id}p`}>
        <h2 id={`${id}p`}>Properties</h2>
        <dl>
          {evidenceAfter < 0 ? evidence : null}
          {properties.map((field) => (
            <Fragment key={field.name}>
              <Prop detail={detail} field={field} />
              {field.name === "confidence" ? evidence : null}
            </Fragment>
          ))}
        </dl>
      </section>
      <section className="props-group" aria-labelledby={`${id}d`}>
        <h2 id={`${id}d`}>Details</h2>
        <dl>
          {fields
            .filter((field) => field.look.place === "details")
            .map((field) => (
              <Prop key={field.name} detail={detail} field={field} />
            ))}
          <PropRow label="ID">
            <span className="mono t select-all" title={row.id}>
              {row.id}
            </span>
          </PropRow>
          <PropRow label="Created">
            <span className="t">{dateTime(row.created)}</span>
          </PropRow>
          <PropRow label="Updated">
            <span className="t">{dateTime(row.modified)}</span>
          </PropRow>
        </dl>
      </section>
    </aside>
  );
}

function Prop({ detail, field }: { detail: Detail; field: Field }) {
  const text = typeof field.value === "string" ? field.value : null;
  const href = text && field.look.href ? safeUrl(field.look.href(text) ?? "") : "";
  return (
    <PropRow label={field.look.label} field={field.name}>
      <PropertyEditor detail={detail} field={field}>
        <FieldValue field={field} />
      </PropertyEditor>
      {href ? (
        <a className="icon-btn prop-open" href={href} target="_blank" rel="noopener noreferrer" aria-label={`Open ${field.look.label}`}>
          <Icon name="external" size={12} />
        </a>
      ) : null}
    </PropRow>
  );
}

function PropRow({ label, field, children }: { label: string; field?: string; children: ReactNode }) {
  return (
    <div className="prop" data-field={field}>
      <dt className="prop-k">{label}</dt>
      <dd className="prop-v">{children}</dd>
    </div>
  );
}

/** One field's value, drawn by what it is; an unset field says so. */
function FieldValue({ field }: { field: Field }) {
  const { name, look, route, value } = field;
  if (value === undefined) return <span className="unset">{look.unset ?? "—"}</span>;
  if (name === "status") return <Named glyph={<StatusGlyph status={String(value)} />} text={capitalize(String(value))} />;
  if (name === "judgement") {
    return <Named glyph={<JudgementGlyph judgement={String(value)} />} text={capitalize(String(value))} />;
  }
  if (name === "priority" && typeof value === "number") {
    return (
      <>
        <Named glyph={<PriorityGlyph priority={value} />} text={priorityName(value)} />
        <span className="num muted prop-num">{value}</span>
      </>
    );
  }
  if (name === "owner") return <Named glyph={<Avatar actor={String(value)} />} text={String(value)} />;
  // The server stores an unrecorded cost as 0, so 0 is none recorded, not "$0.00".
  if (look.format === "usd" && typeof value === "number") {
    return value ? <span className="num">{usd(value)}</span> : <span className="unset">None recorded</span>;
  }
  if (look.format === "fraction" && typeof value === "number") return <Fraction value={value} />;
  if (Array.isArray(value)) return <List name={name} items={value.map(String)} uuids={route?.value.items?.format === "uuid"} />;
  if (look.format === "day") return <span className="t">{calendarDate(String(value))}</span>;
  if (route?.value.format === "date-time" || look.format === "date") return <span className="t">{dateTime(String(value))}</span>;
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return (
    <span className={look.mono ? "t mono" : "t"} title={text}>
      {text}
    </span>
  );
}

/**
 * A list field's items, in the row's order. Keyed by place as well as value: a
 * list may repeat a value (a byline names an author twice, and the server keeps
 * its order and repeats), and two items under one key show stale.
 */
function List({ name, items, uuids }: { name: string; items: readonly string[]; uuids: boolean }) {
  const key = (index: number, item: string) => `${index}:${item}`;
  if (name === "labels") return items.map((label, index) => <LabelChip key={key(index, label)} label={label} />);
  if (name === "subscribers") {
    return items.map((actor, index) => (
      <Named key={key(index, actor)} glyph={<Avatar actor={actor} size={16} />} text={actor} />
    ));
  }
  if (uuids) {
    return items.map((id, index) => (
      <a key={key(index, id)} className="ref" href={formatRoute({ name: "lookup", id })} title={id}>
        <Icon name="link" size={12} />
        {id.slice(0, 8)}
      </a>
    ));
  }
  return items.map((item, index) => (
    <span key={key(index, item)} className="kind-tag">
      {item}
    </span>
  ));
}

function Named({ glyph, text }: { glyph: ReactNode; text: string }) {
  return (
    <span className="named">
      {glyph}
      <span className="t">{text}</span>
    </span>
  );
}

/** A probability as a bar and two decimals. */
function Fraction({ value }: { value: number }) {
  return (
    <span className="conf">
      <span className="conf-bar" aria-hidden="true">
        <i style={{ width: `${Math.min(1, Math.max(0, value)) * 100}%` }} />
      </span>
      <span className="num">{value.toFixed(2)}</span>
    </span>
  );
}

/**
 * The server's evidence confidence, named apart from the author's own. With no
 * `proves` edge in, it is the neutral 0.50, and says so.
 */
function EvidenceConfidence({ id, proves }: { id: string; proves: number }) {
  const query = useQuery(detailQueries.confidence(id));
  const retry = () => void query.refetch();
  return (
    <PropRow label="Evidence" field="evidence_confidence">
      {query.data !== undefined ? (
        <span
          className="evidence"
          title="Derived by the server from currently-true proves edges; favors never count. Not the author's confidence."
        >
          <Fraction value={query.data} />
          {proves ? null : <span className="unset">no proves yet</span>}
          {query.isError && !query.isFetching ? (
            // The value stays; the message would widen the row, so it is the button's title.
            <span role="alert">
              <span className="sr-only">Could not refresh: {query.error.message}</span>
              <button type="button" className="btn ghost prop-retry" title={`Could not refresh: ${query.error.message}`} onClick={retry}>
                Retry
              </button>
            </span>
          ) : null}
        </span>
      ) : query.isError ? (
        <ReadFailure error={query.error} retry={retry} />
      ) : (
        <span className="unset">Loading…</span>
      )}
    </PropRow>
  );
}
