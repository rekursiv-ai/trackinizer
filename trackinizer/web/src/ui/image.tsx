// THE IMAGE NODE. A real figure, shown, not edited.
//
// Not Tiptap. Rich (rich.tsx, @tiptap/react) is the editor for text a person is
// composing — loading it to display one static figure would pay for an editor to
// avoid writing one `<img>`. This is the display counterpart Rich never was.
import "./image.css";

/** A figure as its source holds it. */
export type ImageItem = {
  /** Where it was fetched, verbatim — never a guessed or constructed URL. */
  readonly src: string;
  /** What the figure shows, for the reader a screen reader gives it. */
  readonly alt: string;
  /** The source's own caption, quoted — "Figure 2: …" as the paper wrote it. */
  readonly caption?: string;
};

export function ImageView({ src, alt, caption }: ImageItem) {
  return (
    <figure className="ui-image">
      <img src={src} alt={alt} loading="lazy" />
      {caption ? <figcaption>{caption}</figcaption> : null}
    </figure>
  );
}
