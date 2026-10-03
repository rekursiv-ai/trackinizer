import { useEffect, useMemo, useState } from "react";
import type { Meta } from "../app/boot";
import { type Palette, readPalette } from "./encode";
import "./tokens.css";

/**
 * The palette the current theme's tokens give, read again whenever the theme
 * changes: the graph's and the detail's graph preview's, which draw alike.
 */
export function useThemePalette(meta: Meta): Palette {
  const read = useMemo(() => {
    const vocabulary = { kinds: meta.kinds, edgeKinds: Object.keys(meta.edges), statuses: meta.enums.status ?? [] };
    return () => readPalette(document.documentElement, vocabulary);
  }, [meta]);
  const [palette, setPalette] = useState(read);
  useEffect(() => {
    // `applyTheme` (src/theme.ts) switches the theme by this attribute alone.
    const observer = new MutationObserver(() => setPalette(read()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, [read]);
  return palette;
}
