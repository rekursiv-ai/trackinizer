import { Pill } from "./SummaryPill";
import type { Claim, CorpusDoc, Effort, SessionDoc } from "./summaryDoc";

const sessionsOf = (members: readonly { readonly session: string }[]) => new Set(members.map((member) => member.session)).size;
const counted = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;

/**
 * What the campaign's sessions share: the efforts several phases pursued, and the claims several
 * beliefs state, each with how many sessions took part and, for a claim, whether they agreed.
 */
export function SummaryCorpus({ corpus, docs, onOpen }: {
  readonly corpus: CorpusDoc;
  readonly docs: ReadonlyMap<string, SessionDoc>;
  readonly onOpen: (session: string) => void;
}) {
  const title = (session: string) => corpus.sessions.find((row) => row.session === session)?.title ?? session;
  const link = (session: string) => <button type="button" className="sum-link" disabled={!docs.has(session)}
    onClick={() => onOpen(session)}>{title(session)}</button>;
  const claims = [...corpus.claims].sort((a, b) => b.members.length - a.members.length);
  return <div className="sum-corpus">
    <section aria-label="Efforts">
      <h3>Efforts ({corpus.efforts.length})</h3>
      {corpus.efforts.length === 0 && <p className="sum-empty">No effort is shared yet.</p>}
      {corpus.efforts.map((effort: Effort, i) => <details key={i} className="sum-group">
        <summary>
          <span className="sum-group-title">{effort.title}</span>
          <span className="sum-count">{counted(sessionsOf(effort.members), "session")} · {effort.members.length}</span>
        </summary>
        <p className="sum-group-text">{effort.text}</p>
        <ul className="sum-members">
          {effort.members.map((member, k) => <li key={k}>
            <span className="sum-member-title">{member.title}</span>
            <p>{member.text}</p>
            <span className="sum-from">— {link(member.session)}</span>
          </li>)}
        </ul>
      </details>)}
    </section>
    <section aria-label="Beliefs">
      <h3>Beliefs ({claims.length})</h3>
      {claims.length === 0 && <p className="sum-empty">No belief is recorded yet.</p>}
      {claims.map((claim: Claim, i) => <details key={i} className="sum-group">
        <summary>
          <span className="sum-group-title">{claim.claim}</span>
          <Pill value={claim.status} />
          <span className="sum-count">{counted(sessionsOf(claim.members), "session")} · {claim.members.length}</span>
        </summary>
        <ul className="sum-members">
          {claim.members.map((member, k) => <li key={k}>
            <Pill value={member.verdict} />
            <span className="sum-member-title">{member.claim}</span>
            <span className="sum-from">— {link(member.session)}</span>
          </li>)}
        </ul>
      </details>)}
    </section>
  </div>;
}
