import { createContext, useContext, useState } from "react";

/** Whether the ⌘K palette is open, and on what query. */
export type Palette = {
  readonly open: boolean;
  /** What the palette's input starts with. */
  readonly query: string;
  show(query?: string): void;
  hide(): void;
  toggle(): void;
};

export const PaletteContext = createContext<Palette | null>(null);

/** The palette's state and controls. */
export function usePalette(): Palette {
  const palette = useContext(PaletteContext);
  if (!palette) throw new Error("usePalette needs a PaletteContext above it.");
  return palette;
}

/**
 * Hold the palette's state for the provider.
 *
 * `search` opens the palette on that query, or is `null`; hiding a palette
 * opened so calls `endSearch`. The shell passes `null`: a `#/search/<q>` link
 * opens the search page, not the palette.
 */
export function usePaletteState(search: string | null, endSearch: () => void): Palette {
  const [shown, setShown] = useState<{ query: string } | null>(null);
  const open = search !== null || shown !== null;
  const show = (query = "") => setShown({ query });
  const hide = () => {
    setShown(null);
    if (search !== null) endSearch();
  };
  return {
    open,
    query: search ?? shown?.query ?? "",
    show,
    hide,
    toggle: () => (open ? hide() : show()),
  };
}
