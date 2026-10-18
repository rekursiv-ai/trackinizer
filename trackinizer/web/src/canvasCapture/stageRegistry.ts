import { createContext } from "react";

/** What the open canvas offers to be pictured: its stage, red boxes and tiles in it. */
export type CaptureSource = { readonly stage: HTMLElement };

/**
 * The canvas that is on screen, if one is. The stream that hears an agent's request
 * for a picture sits above the canvas (Settings and Admin are outside it), so the
 * canvas registers itself here and the stream asks.
 */
export class CaptureRegistry {
  #source: CaptureSource | null = null;

  /** Offer `source`, or withdraw the offer with `null`. */
  readonly set = (source: CaptureSource | null): void => {
    this.#source = source;
  };

  readonly get = (): CaptureSource | null => this.#source;
}

/** The stream's registry; null outside it, where nothing can be pictured. */
export const CaptureContext = createContext<CaptureRegistry | null>(null);
