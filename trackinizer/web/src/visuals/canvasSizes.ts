/** How large a floating window is, in pixels. */
export type Size = { readonly width: number; readonly height: number };

/**
 * The sizes the user gave the canvas's parts in this browser: how wide the
 * column at each side of the page is, how large each floating window is, by
 * visual type, and each docked tile's share of the strip it stands in, by
 * visual type (1 is an even share). A part never resized has none.
 */
export type CanvasSizes = {
  readonly left?: number;
  readonly side?: number;
  readonly floating: { readonly [type: string]: Size };
  readonly share: { readonly [type: string]: number };
};

/** The narrowest a column goes: Chat's header and box still fit. */
export const MIN_COLUMN = 260;

/** The smallest a floating window goes. */
export const MIN_FLOAT: Size = { width: 280, height: 220 };

/** The share of the stage a column may take at most, so the page keeps the rest. */
const MAX_SHARE = 0.7;

const KEY = "trackinizer.v2.canvas.sizes";

/** `width` held between the narrowest column and the most a stage `stageWidth` wide gives one. */
export function heldWidth(width: number, stageWidth: number): number {
  return Math.round(Math.max(MIN_COLUMN, Math.min(width, stageWidth * MAX_SHARE)));
}

/** The sizes this browser keeps; none where storage is off or holds something else. */
export function readCanvasSizes(): CanvasSizes {
  try {
    const kept: unknown = JSON.parse(localStorage.getItem(KEY) ?? "{}");
    const { left, side, floating, share } = record(kept);
    return {
      ...(positive(left) ? { left } : {}),
      ...(positive(side) ? { side } : {}),
      floating: Object.fromEntries(Object.entries(record(floating)).flatMap(([type, size]) => {
        const { width, height } = record(size);
        return positive(width) && positive(height) ? [[type, { width, height }]] : [];
      })),
      share: Object.fromEntries(Object.entries(record(share)).filter((entry): entry is [string, number] => positive(entry[1]))),
    };
  } catch {
    return { floating: {}, share: {} };
  }
}

export function writeCanvasSizes(sizes: CanvasSizes): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(sizes));
  } catch {
    // Storage is off or full: the canvas still holds the sizes until the page goes.
  }
}

function record(value: unknown): { readonly [name: string]: unknown } {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as { [name: string]: unknown } : {};
}

function positive(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}
