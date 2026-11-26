/** Normalized RGBA color in [0, 1] for WebGL shaders. */
export type RGBA = readonly [number, number, number, number];

const CACHE = new Map<string, RGBA>();
let probeCtx: CanvasRenderingContext2D | null | undefined;

function getProbeCtx(): CanvasRenderingContext2D | null {
  if (probeCtx !== undefined) return probeCtx;
  if (typeof document === "undefined") {
    probeCtx = null;
    return null;
  }
  const canvas = document.createElement("canvas");
  canvas.width = 1;
  canvas.height = 1;
  probeCtx = canvas.getContext("2d", { willReadFrequently: true });
  return probeCtx;
}

/**
 * Parse any CSS color string (`#rgb`, `#rrggbb`, `#rrggbbaa`, `rgb(...)`,
 * `rgba(...)`, or any CSS color via a 1x1 canvas probe) into `[r, g, b, a]` in
 * `[0, 1]`. Results are cached by string.
 */
export function parseCssColor(color: string, alpha = 1): RGBA {
  let base = CACHE.get(color);
  if (!base) {
    base = parseDirect(color) ?? parseViaCanvas(color) ?? [0.5, 0.5, 0.5, 1];
    CACHE.set(color, base);
  }
  if (alpha === 1) return base;
  return [base[0], base[1], base[2], base[3] * alpha];
}

function parseDirect(raw: string): RGBA | null {
  const s = raw.trim();
  if (s.startsWith("#")) {
    const hex = s.slice(1);
    if (hex.length === 3 && /^[\da-f]{3}$/i.test(hex)) {
      const r = Number.parseInt((hex[0] ?? "0") + (hex[0] ?? "0"), 16) / 255;
      const g = Number.parseInt((hex[1] ?? "0") + (hex[1] ?? "0"), 16) / 255;
      const b = Number.parseInt((hex[2] ?? "0") + (hex[2] ?? "0"), 16) / 255;
      return [r, g, b, 1];
    }
    if ((hex.length === 6 || hex.length === 8) && /^[\da-f]+$/i.test(hex)) {
      const r = Number.parseInt(hex.slice(0, 2), 16) / 255;
      const g = Number.parseInt(hex.slice(2, 4), 16) / 255;
      const b = Number.parseInt(hex.slice(4, 6), 16) / 255;
      const a = hex.length === 8 ? Number.parseInt(hex.slice(6, 8), 16) / 255 : 1;
      return [r, g, b, a];
    }
  }
  const m = s.match(
    /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+%?))?\s*\)$/i,
  );
  if (m) {
    const r = Number(m[1]) / 255;
    const g = Number(m[2]) / 255;
    const b = Number(m[3]) / 255;
    let a = 1;
    if (m[4] !== undefined) {
      a = m[4].endsWith("%") ? Number.parseFloat(m[4]) / 100 : Number(m[4]);
    }
    if ([r, g, b, a].every(Number.isFinite)) return [r, g, b, a];
  }
  return null;
}

function parseViaCanvas(color: string): RGBA | null {
  const ctx = getProbeCtx();
  if (!ctx) return null;
  ctx.fillStyle = "#000000";
  ctx.fillStyle = color;
  const probeA = ctx.fillStyle;
  ctx.fillStyle = "#ffffff";
  ctx.fillStyle = color;
  const probeB = ctx.fillStyle;
  if (probeA !== probeB) return null;
  ctx.clearRect(0, 0, 1, 1);
  ctx.fillRect(0, 0, 1, 1);
  const data = ctx.getImageData(0, 0, 1, 1).data;
  return [
    (data[0] ?? 0) / 255,
    (data[1] ?? 0) / 255,
    (data[2] ?? 0) / 255,
    (data[3] ?? 255) / 255,
  ];
}
