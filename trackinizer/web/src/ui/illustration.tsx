// THE ILLUSTRATION NODE. A rendered video, never a measurement.
//
// `scene` draws the numbers; this draws a mood a real number cannot carry — what an
// ignition looks like, not what temperature it reaches. A generator imagines this from
// a prompt, so nothing in it is checkable against a row the way every other node is,
// and that gap is named on the surface itself, not left for a reader to assume away.
import "./illustration.css";

/** A video a generator rendered from a prompt. Composed only when a video-generation
 *  tool is actually connected and actually returned a `src` — never a guessed or
 *  future URL, the same rule `image` holds for a fetched figure. */
export type IllustrationItem = {
  /** Where it was rendered, verbatim. */
  readonly src: string;
  /** The prompt exactly as sent, printed in full — the honest gap between what was
   *  asked for and what a reader sees is theirs to judge, not smoothed over. */
  readonly prompt: string;
  /** The real paper or figure this dramatizes. Never implies the video measures it. */
  readonly inspiredBy: string;
  readonly alt: string;
};

export function IllustrationView({ src, prompt, inspiredBy, alt }: IllustrationItem) {
  return (
    <figure className="ui-illustration">
      <video src={src} aria-label={alt} autoPlay muted loop playsInline />
      <figcaption>
        <strong>illustrative — not measured</strong>
        <span>{`dramatizes ${inspiredBy}`}</span>
        <span>{`prompt: “${prompt}”`}</span>
      </figcaption>
    </figure>
  );
}
