// THE EQUATION NODE. A formula as its source states it, rendered by KaTeX.
//
// KaTeX is loaded on first render, not on first paint: a pane with no formula on it
// must not pay for the library, and the first-load budget (scripts/bundle-size.ts,
// 250 KB gzip) is what enforces that. Its stylesheet comes with it, in the same
// dynamic import, so the CSS never reaches a page that draws no formula either.
import { useEffect, useRef, useState } from "react";
import "./nodes.css";

/** A formula, as the paper or the record prints it. */
export type Equation = {
  /** The formula in TeX, exactly as the source writes it: `\sigma(\sum_i v_i)`. */
  readonly tex: string;
  /** Display sets it on its own line, which is the only block form; inline rides the
   *  sentence around it. */
  readonly display?: boolean;
};

/**
 * One formula.
 *
 * A formula that does not parse prints as its own TeX in machine ink rather than
 * KaTeX's red error text, because the source is the record and a colour on a word is
 * a thing this surface does not draw. Nothing is ever silently dropped.
 */
export function EquationView({ tex, display = true }: Equation) {
  const host = useRef<HTMLSpanElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    void (async () => {
      const [katex] = await Promise.all([import("katex"), import("katex/dist/katex.min.css")]);
      if (!live || !host.current) return;
      try {
        katex.default.render(tex, host.current, {
          displayMode: display,
          // The source is the record: a malformed formula raises here and is printed
          // as its own text below, rather than becoming KaTeX's own error markup.
          throwOnError: true,
          strict: false,
          output: "html",
        });
        setFailed(false);
      } catch {
        setFailed(true);
      }
    })();
    return () => { live = false; };
  }, [tex, display]);

  return (
    <div className="ui-eq">
      {/* Hidden from the tree when it failed, so a reader never hears half a formula. */}
      <span ref={host} hidden={failed} />
      {failed ? <code className="katex-error">{tex}</code> : null}
    </div>
  );
}
