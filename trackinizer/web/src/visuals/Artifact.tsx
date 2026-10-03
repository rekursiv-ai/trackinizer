import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { getArtifactContentRevision } from "../api/artifacts";
import { formatRoute, UUID } from "../router/route";
import type { RendererProps } from "./registry";
import "./Artifact.css";

/** A graph row cited by an Artifact finding. */
export type ArtifactCitation = {
  readonly record_id: string;
  readonly kind: string;
  readonly seq: number;
  readonly title: string;
  readonly claim_id?: string | null;
  readonly claim_kind?: string | null;
  readonly claim_seq?: number | null;
  readonly claim_title?: string | null;
  readonly edge_kind?: string | null;
  readonly valence?: number | null;
  readonly note?: string | null;
};

/** A measured result with its evaluation denominator and split. */
export type ArtifactOutcome = {
  readonly result: string;
  readonly denominator: number;
  readonly split: string;
};

/** One bounded direction or conclusion in structured Artifact content. */
export type ArtifactSection = {
  readonly title: string;
  readonly summary: string;
  readonly details: string;
  readonly findings: readonly {
    readonly claim: string;
    readonly outcome: ArtifactOutcome;
    readonly uncertainty: string;
    readonly citations: readonly ArtifactCitation[];
  }[];
};

/** An immutable Artifact revision in either structured or custom HTML form. */
export type ArtifactContentRevision = {
  readonly revision: number;
  readonly artifact_id: string;
  readonly issue_id: string;
  readonly title: string;
  readonly summary: string;
  readonly author: string;
  readonly created_at: string;
  readonly scope: "team";
  readonly citations: readonly ArtifactCitation[];
} & (
  | { readonly format: "html"; readonly html: string }
  | { readonly format: "structured"; readonly sections: readonly ArtifactSection[] }
);

/** Render the revision selected by a canvas operation. */
export function Artifact({ instance }: RendererProps) {
  const id = instance.record_id;
  if (typeof id !== "string" || !UUID.test(id)) {
    return <p className="visual-unsupported">Open an Artifact or ask the agent to select one.</p>;
  }
  return <ArtifactPage id={id} />;
}

/** Read one exact revision; the link stays valid after later publications. */
export function ArtifactPage({ id, optional = false }: { readonly id: string; readonly optional?: boolean }) {
  const query = useQuery({
    queryKey: ["artifact-content", id],
    queryFn: ({ signal }) => getArtifactContentRevision(id, { signal }),
    staleTime: Infinity,
    retry: false,
  });
  if (query.isPending) return <div className="visual-loading" aria-busy="true">Loading Artifact…</div>;
  if (optional && query.error instanceof ApiError && query.error.status === 404) return null;
  if (query.isError) return <div className="visual-unsupported" role="alert">Could not load this Artifact revision. <button className="btn ghost" type="button" onClick={() => void query.refetch()}>Retry</button></div>;
  return <ArtifactContent artifact={query.data} />;
}

/** Render a versioned Artifact summary, evidence, or isolated custom HTML. */
export function ArtifactContent({ artifact }: { readonly artifact: ArtifactContentRevision }) {
  return <article className="artifact-content">
    <header className="artifact-header">
      <p className="artifact-eyebrow">Artifact revision {artifact.revision}</p>
      <p className="artifact-scope">Shared with team</p>
      <h2>{artifact.title}</h2>
      <p className="artifact-summary">{artifact.summary}</p>
      <p className="artifact-byline">By {artifact.author} · {artifact.created_at.slice(0, 10)}</p>
      <p className="artifact-source">
        <a href={formatRoute({ name: "lookup", id: artifact.artifact_id })}>Link to this Artifact</a>
        {artifact.format === "html" && <>{" · "}<a href={htmlPageUrl(artifact.artifact_id)} target="_blank" rel="noopener noreferrer">Open full page</a></>}
      </p>
      <p className="artifact-source">
        Source: <a href={formatRoute({ name: "lookup", id: artifact.issue_id })}>Issue</a>
        {" · "}Artifact <a href={formatRoute({ name: "lookup", id: artifact.artifact_id })}>{artifact.artifact_id}</a>
      </p>
    </header>
    {artifact.citations.length > 0 && <section className="artifact-source-evidence" aria-label="Artifact sources">
      <h3>Sources</h3>
      <ul className="artifact-citations">{artifact.citations.map((citation, index) =>
        <Citation key={citationKey(citation, index)} citation={citation} />)}</ul>
    </section>}
    {artifact.format === "html"
      ? <HtmlArtifact id={artifact.artifact_id} title={artifact.title} />
      : <StructuredArtifact sections={artifact.sections} />}
  </article>;
}

function StructuredArtifact({ sections }: { readonly sections: readonly ArtifactSection[] }) {
  return <div className="artifact-sections">
    {sections.map((section, index) => <section className="artifact-section" key={`${index}:${section.title}`}>
      <h3>{section.title}</h3>
      <p className="artifact-section-summary">{section.summary}</p>
      <details className="artifact-details">
        <summary>Section details</summary>
        <p className="artifact-details-copy">{section.details}</p>
      </details>
      {section.findings.map((finding, findingIndex) =>
        <article className="artifact-finding" key={`${findingIndex}:${finding.claim}`}>
          <h4>{finding.claim}</h4>
          <p className="artifact-outcome">
            {finding.outcome.result} · n={finding.outcome.denominator} · {finding.outcome.split}
          </p>
          {finding.citations.length > 0 && <ul className="artifact-citations">
            {finding.citations.map((citation, index) =>
              <Citation key={citationKey(citation, index)} citation={citation} />)}
          </ul>}
          <details className="artifact-details">
            <summary>Uncertainty and method</summary>
            <p className="artifact-uncertainty"><strong>Uncertainty:</strong> {finding.uncertainty}</p>
          </details>
        </article>)}
    </section>)}
  </div>;
}

function Citation({ citation }: { readonly citation: ArtifactCitation }) {
  const edge = citation.edge_kind;
  const signed = citation.valence;
  const evidenceClass = signed == null || signed === 0
    ? "artifact-evidence-neutral"
    : signed < 0 ? "artifact-evidence-negative" : "artifact-evidence-positive";
  const evidenceLabel = signed == null
    ? "citation · strength not recorded"
    : signed < 0 ? `${edge ?? "evidence"} · argues against (${signed})`
      : signed > 0 ? `${edge ?? "evidence"} · supports (${signed})`
        : `${edge ?? "evidence"} · neutral (0)`;
  return <li className="artifact-citation">
    <span className={evidenceClass}>{evidenceLabel}</span>{" · "}
    <a href={formatRoute({ name: "lookup", id: citation.record_id })}>
      {citation.kind}#{citation.seq} {citation.title}
    </a>
    {citation.claim_id && citation.claim_kind && citation.claim_seq != null && citation.claim_title
      && <> → <a href={formatRoute({ name: "lookup", id: citation.claim_id })}>
        {citation.claim_kind}#{citation.claim_seq} {citation.claim_title}
      </a></>}
    {citation.note && <span className="artifact-citation-note"> · {citation.note}</span>}
  </li>;
}

function citationKey(citation: ArtifactCitation, index: number): string {
  return `${citation.record_id}:${citation.claim_id ?? ""}:${index}`;
}

// The page route sends the sandbox CSP itself, so the frame and the full-page
// link share one policy. A real URL (not srcdoc) also lets the page's own
// history.pushState/replaceState work.
function HtmlArtifact({ id, title }: { readonly id: string; readonly title: string }) {
  return <iframe
    className="artifact-html-frame"
    title={`${title} Artifact content`}
    sandbox="allow-scripts allow-popups"
    referrerPolicy="no-referrer"
    src={htmlPageUrl(id)}
  />;
}

/** The sandboxed page route serving one HTML Artifact revision. */
function htmlPageUrl(id: string): string {
  return `/api/artifacts/${encodeURIComponent(id)}/html`;
}
