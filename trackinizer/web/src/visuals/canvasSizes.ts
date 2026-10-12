import type { TileMemory } from "../state/value";
import type { Layout } from "./stationLayout";

/** How large a floating window is, in pixels. */
export type Size = { readonly width: number; readonly height: number };

/**
 * The sizes the canvas's parts take on the stage now, in pixels, fitted from
 * the station's layout: how wide the column at each side of the page is, how
 * large each floating window is, by visual type, and each docked tile's share
 * of the strip it stands in, by visual type (1 is an even share). A part the
 * layout holds nothing for has none.
 */
export type CanvasSizes = {
  readonly left?: number;
  readonly side?: number;
  readonly floating: { readonly [type: string]: Size };
  readonly share: { readonly [type: string]: number };
};

/** The narrowest a visual's column goes. */
export const MIN_COLUMN = 260;

/** The narrowest the Chat column goes: its header and box still fit. */
export const MIN_CHAT = 280;

/** The smallest a floating window goes. */
export const MIN_FLOAT: Size = { width: 280, height: 220 };

/** The share of the stage a column may take at most, so the page keeps the rest. */
const MAX_SHARE = 0.7;

/** `width` held between `least` and the most a stage `stageWidth` wide gives one. */
export function heldWidth(width: number, stageWidth: number, least = MIN_COLUMN): number {
  return Math.round(Math.max(least, Math.min(width, stageWidth * MAX_SHARE)));
}

/** `width` as a stage `stageWidth` wide gives it; a stage not yet measured (0) holds only the least. */
export function fitWidth(width: number, stageWidth: number, least = MIN_COLUMN): number {
  return stageWidth > 0 ? heldWidth(width, stageWidth, least) : Math.round(Math.max(least, width));
}

/** `pixels` of a stage `extent` long, as the fraction the layout stores; null on a stage not yet measured. */
export function toFraction(pixels: number, extent: number): number | null {
  return extent > 0 ? Math.round((pixels / extent) * 10_000) / 10_000 : null;
}

/** The pixels a stored `fraction` of a stage `extent` long takes. */
export function toPixels(fraction: number, extent: number): number {
  return Math.round(fraction * extent);
}

/**
 * The canvas's sizes on a stage `stage` large, from the station's `layout`:
 * columns held between the least and most the stage gives one, and windows held
 * between the smallest window and the stage. Nothing is written back: a stage
 * too narrow for what is stored shows the fitted sizes, and the layout keeps
 * what the user set until they change it there.
 */
export function fitSizes(layout: Layout, stage: Size): CanvasSizes {
  const column = (fraction: number | undefined) => (fraction === undefined || stage.width === 0 ? undefined : heldWidth(toPixels(fraction, stage.width), stage.width));
  const floating = stage.width === 0 || stage.height === 0 ? [] : Object.entries(layout.floating ?? {}).map(([type, size]) => [type, {
    width: Math.min(stage.width, Math.max(MIN_FLOAT.width, toPixels(size.width, stage.width))),
    height: Math.min(stage.height, Math.max(MIN_FLOAT.height, toPixels(size.height, stage.height))),
  }]);
  const left = column(layout.columns?.left);
  const side = column(layout.columns?.side);
  return {
    ...(left === undefined ? {} : { left }),
    ...(side === undefined ? {} : { side }),
    floating: Object.fromEntries(floating),
    share: layout.share ?? {},
  };
}

const KEY = "trackinizer.v2.canvas.sizes";

/** What this browser kept in `trackinizer.v2.canvas.sizes` before the station held its layout, in pixels. */
export type LegacySizes = CanvasSizes & { readonly chat?: number };

/** The sizes an older build kept in this browser; none where storage is off or holds something else. */
export function readLegacySizes(): LegacySizes {
  try {
    const kept: unknown = JSON.parse(localStorage.getItem(KEY) ?? "{}");
    const { left, side, chat, floating, share } = record(kept);
    return {
      ...(positive(left) ? { left } : {}),
      ...(positive(side) ? { side } : {}),
      ...(positive(chat) ? { chat } : {}),
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

/**
 * Forget what an older build kept, once the station holds it: `part` is the
 * canvas's sizes or Chat's width, and the other stays until its own migration.
 * The key goes when nothing is left in it.
 */
export function dropLegacySizes(part: "canvas" | "chat"): void {
  try {
    const kept = record(JSON.parse(localStorage.getItem(KEY) ?? "{}"));
    const left = part === "chat"
      ? Object.fromEntries(Object.entries(kept).filter(([name]) => name !== "chat"))
      : Object.fromEntries(Object.entries(kept).filter(([name]) => name === "chat"));
    if (Object.keys(left).length === 0) localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, JSON.stringify(left));
  } catch {
    // Storage is off, or holds something else: there is nothing to forget.
  }
}

/**
 * What an older build kept in this browser for the canvas, as the layout key
 * set it would have been: pixels become fractions of a stage `stage` large.
 * Nothing for a stage not measured, and no key for a part never set.
 */
export function legacyLayout(sizes: LegacySizes, tiles: { readonly [type: string]: TileMemory }, stage: Size): Layout {
  const wide = (pixels: number) => toFraction(pixels, stage.width) ?? 0;
  const high = (pixels: number) => toFraction(pixels, stage.height) ?? 0;
  const columns = { ...(sizes.left ? { left: wide(sizes.left) } : {}), ...(sizes.side ? { side: wide(sizes.side) } : {}) };
  const floating = Object.entries(sizes.floating).map(([type, size]) => [type, { width: wide(size.width), height: high(size.height) }]);
  const kept = Object.entries(tiles).map(([type, tile]) => [type, {
    collapsed: tile.collapsed,
    ...(tile.place ? { place: { left: wide(tile.place.left), top: high(tile.place.top) } } : {}),
  }]);
  return {
    ...(Object.keys(columns).length ? { columns } : {}),
    ...(floating.length ? { floating: Object.fromEntries(floating) } : {}),
    ...(kept.length ? { tiles: Object.fromEntries(kept) } : {}),
    ...(Object.keys(sizes.share).length ? { share: sizes.share } : {}),
  };
}

function record(value: unknown): { readonly [name: string]: unknown } {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as { [name: string]: unknown } : {};
}

function positive(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}
