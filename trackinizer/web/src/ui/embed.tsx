// A PAGE THE ANSWER'S AUTHOR WROTE, HOSTED INSIDE THE ANSWER.
//
// Every other node in this catalog is a shape this repo draws from numbers the author
// supplies. This one is the opposite: the author published a program it wrote — SVG, a
// canvas, a three.js scene loaded at a pinned version — and the app gives it a frame.
//
// The split is the one every vendor prompt that got this right lands on: the host owns
// the physics of the view (the WebGL or 2D context, resize, the sandbox, the content
// policy that names which CDNs may be imported at all), and the author owns what exists.
// A node whose fields ARE the geometry inverts that, and then two different models
// handed the same fields draw the app's geometry twice — which is how unrelated answers
// start looking like the same page.
//
// Nothing here grants anything. `../visuals/Artifact` is the frame the record's own
// detail pane already uses, so the CSP it is served (`wire/artifact_html.py`) is the
// same one: `allow-scripts`, `default-src none`, and a pinned CDN set for scripts.
import { HtmlArtifact } from "../visuals/HtmlArtifact";

/** One hosted page, with the address it was published at printed beside it. */
export function EmbedView({ id, title, shows, height }: {
  readonly id: string;
  readonly title: string;
  readonly shows: string;
  readonly height?: number;
}) {
  if (id.trim() === "") return <p className="ui-note">This embed names no published page, so there is nothing to host.</p>;
  return (
    <div className="ui-embed" style={{ height: `${height ?? 640}px` }}>
      <p className="ui-embed-bar">
        <b>{title}</b>
        <span>{shows}</span>
        <a href={`/api/artifacts/${encodeURIComponent(id)}/html`} target="_blank" rel="noreferrer">open</a>
      </p>
      <div className="ui-embed-body">
        <HtmlArtifact id={id} title={title} />
      </div>
    </div>
  );
}
