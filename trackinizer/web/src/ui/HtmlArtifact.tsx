// THE ONE FRAME THAT HOSTS A PUBLISHED PAGE.
//
// Extracted from Artifact.tsx so a composition can host an authored page without
// importing that file's whole module graph — the detail pane pulls in Markdown, the
// highlighter and KaTeX, and a six-line iframe has no business carrying them. The
// frame is the shared part: same attributes, same route, one place to change.
// The page route sends the sandbox CSP itself. A real URL (not srcdoc) also lets
// the page's own history.pushState/replaceState work.
export function HtmlArtifact({ id, title }: { readonly id: string; readonly title: string }) {
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
