// THE DRAWING NODE. Geometry the agent authored, not geometry this app pre-shaped.
//
// Every other visual node here is a template: `scene` offers three fixed shapes
// (a tube-and-marker path, a height-field mesh, a point cloud) and the agent picks one
// and fills it with numbers. That is why a dark-matter drift, an orbital precession and
// an attention-sink surface all came out looking alike — the shape was decided in THIS
// repository, by hand, before the question was ever asked. A per-answer visual cannot
// come out of a fixed vocabulary of three.
//
// So this node holds no shapes at all. The agent writes the SVG: the marks, their
// coordinates, their order, the labels pinned to them. What this file owns is only what
// the agent must not be trusted with — the ink, and the proof that nothing executes.
//
// SVG, not a <script> or a canvas program, for one reason: it is inert. Markup cannot
// run, cannot fetch, cannot reach the page around it, so an answer that draws is not an
// answer that executes. Every element and attribute is checked against an allow-list
// below and anything outside it is dropped, named, and reported rather than rendered —
// a drawing that tried to smuggle a script is a finding about the answer, not a surface
// to paint.
import { useEffect, useMemo, useRef } from "react";
import "./drawing.css";

/** Elements an answer may draw with: shapes, text, grouping, and the defs a gradient-free
 *  marker needs. No `script`, no `foreignObject`, no `image`, no `use` (which can pull
 *  a node from elsewhere in the document), no `animate*` that drives an attribute on
 *  something outside itself. */
const ELEMENTS: ReadonlySet<string> = new Set([
  "svg", "g", "defs", "marker", "path", "line", "polyline", "polygon", "rect",
  "circle", "ellipse", "text", "tspan", "title", "desc", "clipPath",
  "animate", "animateTransform", "animateMotion", "mpath", "set",
]);

/** Attributes an answer may set. Presentation attributes are allowed because the law
 *  check below reads them; `style` is not, because a style string is a second syntax to
 *  audit and everything it can say is sayable here. Event handlers (`on*`) and `href`
 *  are absent by construction: neither appears in this list. */
const ATTRIBUTES: ReadonlySet<string> = new Set([
  "viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r",
  "rx", "ry", "d", "points", "transform", "fill", "stroke", "stroke-width",
  "stroke-dasharray", "stroke-dashoffset", "stroke-linecap", "stroke-linejoin",
  "opacity", "fill-opacity", "stroke-opacity", "font-size", "font-weight",
  "font-family", "text-anchor", "dominant-baseline", "dx", "dy", "id", "class",
  "clip-path", "marker-end", "marker-start", "orient", "refX", "refY",
  "markerWidth", "markerHeight", "markerUnits", "preserveAspectRatio",
  "attributeName", "from", "to", "values", "dur", "begin", "end", "repeatCount",
  "keyTimes", "keySplines", "calcMode", "type", "rotate", "path", "additive",
  "accumulate", "fill-rule", "pathLength",
]);

/** The allow-list as the parser reports names: SVG's own attribute names are camelCase, and a
 *  comparison that lowercases one side only is a comparison that drops them. */
const ALLOWED_LOWER: ReadonlySet<string> = new Set([...ATTRIBUTES].map((name) => name.toLowerCase()));

/** The same for the element names: an XML parser keeps `animateTransform` camelCased, so the
 *  set has to be read lower-against-lower or the timing nodes are dropped with the names they
 *  need — which leaves a drawing that renders perfectly and never moves. */
const ALLOWED_ELEMENTS: ReadonlySet<string> = new Set([...ELEMENTS].map((name) => name.toLowerCase()));

/** A visual the agent drew itself. */
export type Drawing = {
  /** The SVG body — the agent's own marks, without the outer `<svg>` wrapper, which
   *  this node supplies so the viewBox and sizing stay the app's. A body may move: the
   *  SMIL timing elements are allowed, and this node injects the body by a route that
   *  registers them, so a sweep or a value advancing over time is something to compose. */
  readonly body: string;
  /** The coordinate space the body draws in: `0 0 w h`. The agent picks it to suit the
   *  geometry rather than being handed a fixed stage. */
  readonly viewBox: string;
  /** What it shows, for a reader given it by a screen reader. A drawing with no
   *  description is refused: an unlabelled custom visual is the one thing a reader
   *  cannot fall back to prose for. */
  readonly label: string;
  readonly height?: number;
};

type Checked = {
  readonly safe: string | null;
  readonly dropped: readonly string[];
  readonly frozen: readonly string[];
};

/** Nothing is reported as frozen any more, and the measurement that emptied this set is
 *  worth keeping: Chrome once refused to let SMIL drive the SVG2 geometry *properties*, so
 *  `cx`, `r`, `x` and `y` sat at their `baseVal` with the clock advancing and the mark never
 *  moved. It has since implemented them. Re-measured in the Chromium this app is screenshotted
 *  in, with the timeline running and `animVal` sampled 600 ms apart: `cx` 199.98 → 33.97,
 *  `cy` 94.99 → 25.82, `r` 14.50 → 4.81, `x` 194.98 → 70.48, `y` 113.00 → 82.56. All five
 *  move. The stale list was worse than no list, because it printed `moves nothing` under
 *  four techniques that work — a caption that tells a reader a live figure is dead, and tells
 *  an author to go and rewrite a drawing that was already running.
 *
 *  If a future engine freezes one again, measure it and put it back; do not restore the set
 *  from memory of this comment. */
const FROZEN: ReadonlySet<string> = new Set();
const TIMING = new Set(["animate", "set"]);

/**
 * Parse the body, drop anything outside the allow-lists, and report what went.
 *
 * Parsed with DOMParser in `image/svg+xml` rather than regex-scrubbed: a regex over
 * markup is a filter an attacker writes around, and the parser is the same one the
 * browser would use anyway.
 */
function check(body: string, viewBox: string): Checked {
  const dropped: string[] = [];
  const frozen: string[] = [];
  const doc = new DOMParser().parseFromString(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${viewBox}">${body}</svg>`,
    "image/svg+xml",
  );
  if (doc.querySelector("parsererror")) {
    return { safe: null, dropped: ["the markup did not parse as SVG"], frozen: [] };
  }

  const walk = (node: Element) => {
    for (const child of [...node.children]) {
      const tag = child.tagName.toLowerCase();
      if (!ALLOWED_ELEMENTS.has(tag)) {
        dropped.push(`<${tag}>`);
        child.remove();
        continue;
      }
      for (const attr of [...child.attributes]) {
        // Compared lower-against-lower: SVG's own attribute names are camelCase
        // (`attributeName`, `keyTimes`, `repeatCount`, `viewBox`), and lowercasing only
        // one side silently stripped exactly those — which removed every animation's
        // timing and left a drawing that rendered perfectly and never moved. The node's
        // own "dropped, not drawn" line is what surfaced it.
        const name = attr.name.toLowerCase();
        // Namespaced attributes (xlink:href and friends) carry their own fetch
        // semantics and never appear in the list, so they go with everything else.
        if (!ALLOWED_LOWER.has(name)) {
          dropped.push(`${tag}[${name}]`);
          child.removeAttribute(attr.name);
        }
      }
      walk(child);
      if (TIMING.has(tag)) {
        const target = child.getAttribute("attributeName");
        if (target && FROZEN.has(target.toLowerCase())) frozen.push(`${tag}[${target}]`);
      }
    }
  };
  walk(doc.documentElement);

  // Serialized back to a string, deliberately: it is re-parsed below by the HTML
  // parser, which is the only route that reliably registers <animate> with Chrome's
  // SMIL timeline. Serializing what the XML parser already vetted keeps the check
  // authoritative — nothing is trusted that the walk above did not see.
  return {
    safe: new XMLSerializer().serializeToString(doc.documentElement),
    dropped,
    frozen,
  };
}

/**
 * One agent-authored drawing.
 *
 * The ink is not the agent's: `.ui-drawing` sets `fill`/`stroke`/`color` from the
 * theme's own tokens and the body inherits them, so a drawing that names no colour is
 * right in every skin — and `currentColor` is what an agent is told to reach for. A
 * literal colour in the body still renders, because a data encoding sometimes needs
 * one, and `styles.test.ts` is where that is argued, not here.
 */
export function DrawingView({ body, viewBox, label, height }: Drawing) {
  const { safe, dropped, frozen } = useMemo(() => check(body, viewBox), [body, viewBox]);
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const div = host.current;
    if (!div || safe === null) return;
    // The whole <svg> goes in as inline HTML on a DIV, not as innerHTML on an <svg>.
    // Both were tried and measured: innerHTML on an existing <svg> parses children in
    // the HTML namespace, and importNode from the XML document produces correctly
    // namespaced SVGAnimateElements whose timeline still never activates (clock
    // advancing, every animated value frozen). Inline SVG parsed by the HTML parser is
    // namespace-aware AND registers with the SMIL timeline, which is what makes a
    // drawing's own animation actually run.
    div.innerHTML = safe;
    // The global reduced-motion rule is CSS, and CSS cannot stop a SMIL timeline: an
    // animated drawing ignores it. This is the surface that mounts the drawing, so this is
    // where a reader's request is honoured — every timeline it registers is paused.
    const calm = window.matchMedia("(prefers-reduced-motion: reduce)");
    const hold = (on: boolean) => {
      for (const svg of div.querySelectorAll("svg")) {
        if (typeof svg.pauseAnimations !== "function") continue;
        if (on) svg.pauseAnimations();
        else svg.unpauseAnimations();
      }
    };
    const onChange = () => hold(calm.matches);
    calm.addEventListener("change", onChange);
    // Only paused at mount, never restarted: a fresh drawing has no timeline to un-pause,
    // and a reader who asked for the full motion must get exactly what the agent wrote.
    if (calm.matches) hold(true);
    return () => {
      calm.removeEventListener("change", onChange);
      div.replaceChildren();
    };
  }, [safe]);

  if (label.trim() === "") return <p className="ui-note">This drawing was not shown: it carries no description.</p>;
  if (safe === null) return <p className="ui-note">{`This drawing was not shown: ${dropped[0] ?? "it was empty"}.`}</p>;
  const notes: string[] = [];
  if (dropped.length > 0) notes.push(`dropped, not drawn: ${[...new Set(dropped)].join(", ")}`);
  // Said out loud because it is otherwise invisible: the mark draws, the clock runs, and
  // nothing moves. The author sees this line; no reader needs to guess why a figure is still.
  if (frozen.length > 0) notes.push(`moves nothing: ${[...new Set(frozen)].join(", ")}`);
  return (
    <figure className="ui-drawing" style={height === undefined ? undefined : { height: `${height}px` }}>
      <div className="ui-drawing-host" ref={host} role="img" aria-label={label} />
      {/* What was removed is printed, not swallowed: an answer whose drawing was
          silently half-dropped is an answer whose author believes it shipped. */}
      {notes.length > 0 ? <figcaption>{notes.join(" · ")}</figcaption> : null}
    </figure>
  );
}
