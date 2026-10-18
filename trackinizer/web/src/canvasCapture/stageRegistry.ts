import { createContext } from "react";

/** A red box on the stage, as the picture of it names it. */
export type CapturedBox = { readonly id: string; readonly author: "person" | "agent" };

/** A red box with its two corners, as fractions of the stage, to be drawn on the picture. */
export type SourceBox = CapturedBox & { readonly points: readonly { readonly x: number; readonly y: number }[] };

/** What the open canvas offers to be pictured: its stage, and the red boxes now on it. */
export type CaptureSource = {
  readonly stage: HTMLElement;
  readonly boxes: () => readonly SourceBox[];
};

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
