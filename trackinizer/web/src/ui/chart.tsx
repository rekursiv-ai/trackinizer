// THE CHART NODE. A figure as Vega-Lite draws it, in the design's own ink.
//
// What the agent writes is data, marks and encoding — never a colour, a font or a
// grid. Those come from vegaConfig(themeInk()), resolved off the live document, so the
// same spec is right in all eight skins and in both themes. A spec that names a colour
// is rejected rather than drawn: a hue on this surface means a verdict, and a library
// that picks hues by itself cannot be saying anything.
import { useEffect, useRef, useState } from "react";
import { themeInk, vegaConfig } from "./theme";
import "./nodes.css";

/** A figure the answer draws from its own rows. */
export type Chart = {
  /** The Vega-Lite spec: `data.values` the rows, `mark` the form, `encoding` the
   *  fields each axis reads — your own field names, whatever the records call them.
   *  Marks, scales, axes, tooltips, layers and transforms are all available.
   *
   *  Three keys are refused, because the theme owns them and `ChartView` rejects a
   *  spec carrying any: `config`, `background`, `title`. Nothing else is refused —
   *  in particular a hue is not, and you never need to name one for the figure to
   *  come out in the design's ink. What the figure is of goes in `label`. */
  readonly spec: Record<string, unknown>;
  /** What the figure is of, for the reader a screen reader gives it to. */
  readonly label: string;
};

/** The keys a spec may not carry, because the theme owns them. */
const OWNED = ["config", "background", "title"] as const;

/**
 * One figure.
 *
 * The spec is merged under the theme's config, never over it, and the width follows
 * the pane: a chart that sets its own pixel width is a chart that overflows the first
 * time the window it floats in is narrowed.
 */
export function ChartView({ spec, label }: Chart) {
  const host = useRef<HTMLDivElement>(null);
  const [refused, setRefused] = useState<string | null>(null);

  useEffect(() => {
    const owned = OWNED.filter((key) => key in spec);
    if (owned.length > 0) {
      setRefused(`the theme owns ${owned.join(", ")}`);
      return;
    }
    setRefused(null);
    let live = true;
    let dispose: (() => void) | undefined;
    void (async () => {
      const embed = (await import("vega-embed")).default;
      if (!live || !host.current) return;
      const result = await embed(host.current, { ...spec, width: "container" } as never, {
        actions: false,
        renderer: "svg",
        config: vegaConfig(themeInk()) as never,
      });
      if (!live) { result.finalize(); return; }
      dispose = () => { result.finalize(); };
    })();
    return () => { live = false; dispose?.(); };
  }, [spec]);

  // A refusal is a finding about the answer, so it prints in machine ink and says
  // which key it was. Silence here would be a chart drawn in a library's own blue.
  if (refused !== null) return <p className="ui-note">{`This figure was not drawn: ${refused}.`}</p>;
  return <div className="ui-chart" ref={host} role="img" aria-label={label} />;
}
