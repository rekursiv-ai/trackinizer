import { toCanvas } from "html-to-image";

/**
 * The one seam to the rasterizer, because jsdom has no canvas: tests replace this
 * module, and only the live canvas runs `html-to-image`.
 */
export function rasterize(node: HTMLElement, scale: number, options: Pass = {}): Promise<HTMLCanvasElement> {
  const { leave = [], style } = options;
  return toCanvas(node, {
    pixelRatio: scale,
    skipFonts: true,
    cacheBust: false,
    filter: leave.length === 0 ? undefined : (each) => !leave.includes(each as Element),
    style,
  });
}

/** What one pass leaves out, and the style it draws the root with. */
export type Pass = {
  /** Elements, with what is inside them, left out of the picture. */
  readonly leave?: readonly Element[];
  /** Style laid over the root of the picture only. */
  readonly style?: Partial<CSSStyleDeclaration>;
};

/** Decode a data URL into something a canvas can draw. */
export function loadImage(source: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("The picture could not be decoded."));
    image.src = source;
  });
}
