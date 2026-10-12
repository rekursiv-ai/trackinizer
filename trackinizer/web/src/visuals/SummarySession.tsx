import { useState } from "react";
import { Pill } from "./SummaryPill";
import { SummaryRaw } from "./SummaryRaw";
import { anchorOf, itemRange, type Belief, type SessionDoc, type Step } from "./summaryDoc";

type Focus = { readonly start: number; readonly end: number };

const when = (iso: string) => (iso ? iso.slice(0, 16).replace("T", " ") : "");

/**
 * One session: how it went, then four columns side by side, the transcript (for a reader who may
 * read it), the steps it took, the phases they made, and the beliefs it rested on with the items
 * that bear on each. Picking a span or an item points the transcript at it.
 */
export function SummarySession({ doc, staff }: { readonly doc: SessionDoc; readonly staff: boolean }) {
  const [focus, setFocus] = useState<Focus | null>(null);
  const select = (next: Focus) => staff && setFocus(next);
  return <article className="sum-session" aria-label={doc.title}>
    <header>
      <h2>{doc.title} <Pill value={doc.outcome} /></h2>
      <p className="sum-overview">{doc.overview}</p>
      <p className="sum-facts">
        {doc.items} items · {doc.tool_calls} tool calls · {doc.errors} errors
        {doc.started && <> · started {when(doc.started)}</>}
        {doc.state === "running" && <> · still running, as of {when(doc.as_of)}</>}
      </p>
    </header>
    <div className="sum-columns">
      {staff && <section className="sum-col sum-col-raw" aria-label="Raw transcript">
        <h3>Raw</h3>
        <SummaryRaw sessionId={doc.session} doc={doc} focus={focus} />
      </section>}
      <SpanColumn name="Steps" spans={doc.steps} doc={doc} staff={staff} focus={focus} select={select} />
      <SpanColumn name="Phases" spans={doc.phases} doc={doc} staff={staff} focus={focus} select={select} />
      <section className="sum-col" aria-label="Beliefs">
        <h3>Beliefs ({doc.beliefs.length})</h3>
        <ul className="sum-list">
          {doc.beliefs.map((belief) => <BeliefItem key={belief.id} belief={belief} doc={doc} staff={staff} select={select} />)}
        </ul>
        {doc.beliefs.length === 0 && <p className="sum-empty">No belief is recorded.</p>}
      </section>
    </div>
  </article>;
}

function ItemRef({ item, doc, staff, select, label }: {
  readonly item: number; readonly doc: SessionDoc; readonly staff: boolean; readonly label?: string;
  readonly select: (focus: Focus) => void;
}) {
  const text = label ?? `#${item}`;
  // Anyone sees the number; only a reader of the transcript can open it, and the server holds that line.
  if (!staff || !anchorOf(doc, item)) return <span className="sum-ref">{text}</span>;
  return <button type="button" className="sum-ref sum-ref-open" onClick={() => select({ start: item, end: item })}>{text}</button>;
}

function SpanColumn({ name, spans, doc, staff, focus, select }: {
  readonly name: string; readonly spans: readonly Step[]; readonly doc: SessionDoc; readonly staff: boolean;
  readonly focus: Focus | null; readonly select: (focus: Focus) => void;
}) {
  return <section className="sum-col" aria-label={name}>
    <h3>{name} ({spans.length})</h3>
    <ul className="sum-list">
      {spans.map((span, i) => {
        const chosen = focus?.start === span.start && focus.end === span.end;
        const canOpen = staff && anchorOf(doc, span.start) && anchorOf(doc, span.end);
        return <li key={i} className={chosen ? "sum-chosen" : undefined}>
          <h4>{span.title}</h4>
          <p>{span.text}</p>
          {span.learned && <p className="sum-learned">Learned: {span.learned}</p>}
          {canOpen
            ? <button type="button" className="sum-ref sum-ref-open" aria-pressed={chosen}
              onClick={() => select({ start: span.start, end: span.end })}>{itemRange(span)}</button>
            : <span className="sum-ref">{itemRange(span)}</span>}
        </li>;
      })}
    </ul>
    {spans.length === 0 && <p className="sum-empty">None.</p>}
  </section>;
}

function BeliefItem({ belief, doc, staff, select }: {
  readonly belief: Belief; readonly doc: SessionDoc; readonly staff: boolean; readonly select: (focus: Focus) => void;
}) {
  return <li>
    <p><Pill value={belief.verdict} /> {belief.claim} <span className="sum-count">{Math.round(belief.confidence * 100)}%</span></p>
    {belief.evidence.length > 0 && <ul className="sum-evidence" aria-label="Evidence">
      {belief.evidence.map((item, i) => <li key={i} data-sign={item.valence > 0 ? "for" : "against"}>
        <span aria-label={item.valence > 0 ? "supports" : "refutes"}>{item.valence > 0 ? "✓" : "✗"}</span>{" "}
        <ItemRef item={item.at} doc={doc} staff={staff} select={select} />
      </li>)}
    </ul>}
  </li>;
}
