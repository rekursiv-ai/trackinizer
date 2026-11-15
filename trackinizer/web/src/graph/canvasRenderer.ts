// The graph's canvas, built from scratch on the design prototype's own pipeline
// (its `draw`, `step` and camera): nothing between this file and the 2d context.
// force-graph is gone; the camera eases, the edges add in grouped passes, every
// dot wears its kind's hue at once, and an island wears its name until the view
// is close enough to read the dots themselves.

import { type Box, type Island, islandsOf, placeLabels } from "./clusterLabels";
import { mix } from "./encode";
import { type Layout, layout } from "./layout";
import type { DrawLink, DrawNode } from "./model";
import type { CreateRenderer, IslandInk, Renderer } from "./renderer";
import "./tokens.css";

/** The camera and what it frames, in world units; `k` is screen pixels per unit. */
type Camera = { x: number; y: number; k: number };

export const canvasRenderer: CreateRenderer = (host, events, covered) => {
  const canvas = document.createElement("canvas");
  canvas.style.width = "100%";
  canvas.style.height = "100%";
  canvas.style.display = "block";
  host.appendChild(canvas);
  const ctx = canvas.getContext("2d")!;
  const style = getComputedStyle(host);
  const font = style.fontFamily;
  const mono = style.getPropertyValue("--mono").trim() || font;

  let nodes: DrawNode[] = [];
  let links: DrawLink[] = [];
  /** Each link's ends, once the layout has swapped the ids for nodes. */
  let ends: { link: DrawLink; a: DrawNode; b: DrawNode }[] = [];
  let camera: Camera = { x: 0, y: 0, k: 0.6 };
  let target: Camera = { x: 0, y: 0, k: 0.6 };
  /** The zoom the last frame landed at: what the kind hues are measured from. */
  let framedZoom = 0.55;
  let framed: (node: DrawNode) => boolean = EVERY;
  let frameWhenSettled = false;
  let engine: Layout | null = null;
  let settled = false;
  let grouped: ((node: DrawNode) => string | undefined) | null = null;
  let groupingSignature: string | null = null;
  let named: ((key: string) => string | undefined) | null = null;
  // The theme's own colours, re-read when it switches: `encode.ts` resolves the
  // node looks the same way, so dots and wires move together. Until `nameIslands`
  // hands the island ink over, the names borrow the paint's.
  let paint = readPaint();
  let ink: IslandInk = { title: paint.ink, count: paint.inkQuiet, outline: paint.ground };
  const themeWatch = new MutationObserver(() => {
    paint = readPaint();
  });
  themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  let islands: { key: string; box: Box }[] = [];
  /**
   * The cross-island evidence hairlines actually drawn, thinned to the design's
   * density: see `SHEEN_CAP`.
   */
  let sheen = new Set<DrawLink>();
  let hovered: DrawNode | null = null;
  let selected: DrawNode | null = null;
  /** The hovered or selected node's one-hop neighbourhood, cached per node. */
  let focusCache: { fid: DrawNode | null; set: Set<DrawNode> | null } = { fid: null, set: null };
  const focusSet = (): { lit: boolean; set: Set<DrawNode> } | null => {
    const fid = hovered ?? selected;
    if (focusCache.fid !== fid) {
      const set = new Set<DrawNode>();
      if (fid) {
        set.add(fid);
        for (const { a, b } of ends) {
          if (a === fid) set.add(b);
          else if (b === fid) set.add(a);
        }
      }
      focusCache = { fid, set: set.size > 0 ? set : null };
    }
    return focusCache.set ? { lit: true, set: focusCache.set } : null;
  };
  let dragging: { x: number; y: number } | null = null;
  let alive = true;

  const resize = () => {
    const dpr = window.devicePixelRatio || 1;
    const [w, h] = [host.clientWidth, host.clientHeight];
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr;
      canvas.height = h * dpr;
    }
    return [w, h] as const;
  };
  const observer = new ResizeObserver(() => resize());
  observer.observe(host);

  const toScreen = (wx: number, wy: number) => {
    const [w, h] = [host.clientWidth, host.clientHeight];
    return { x: (wx - camera.x) * camera.k + w / 2, y: (wy - camera.y) * camera.k + h / 2 };
  };
  const toWorld = (sx: number, sy: number) => {
    const [w, h] = [host.clientWidth, host.clientHeight];
    return { x: (sx - w / 2) / camera.k + camera.x, y: (sy - h / 2) / camera.k + camera.y };
  };

  const frame = (filter: (node: DrawNode) => boolean) => {
    const [w, h] = [host.clientWidth, host.clientHeight];
    const over = covered();
    let [left, right, top, bottom] = [Infinity, -Infinity, Infinity, -Infinity];
    let count = 0;
    for (const node of nodes) {
      if (node.hidden || !Number.isFinite(node.x) || !Number.isFinite(node.y) || !filter(node)) continue;
      count += 1;
      left = Math.min(left, node.x!);
      right = Math.max(right, node.x!);
      top = Math.min(top, node.y!);
      bottom = Math.max(bottom, node.y!);
    }
    if (left > right) return;
    // The prototype's pads: the briefing's column and a margin left, a margin
    // right, room above for the names that crown the islands and below for the
    // timeline; the graph then centres in what is left over.
    const [padLeft, padRight] = [over.left + (w > 760 ? 40 : 16), over.right + (w > 760 ? 40 : 16)];
    const [padTop, padBottom] = [130, 60];
    const usableW = Math.max(80, w - padLeft - padRight);
    const usableH = Math.max(80, h - padTop - padBottom);
    // The prototype's cap: a near-empty frame may fill the screen, but a lone
    // dot never blows up past a record's readable size.
    const k = Math.min(count < 3 ? 2.4 : FIT_MAX_ZOOM, Math.max(0.05, Math.min(usableW / (right - left + 40), usableH / (bottom - top + 40))));
    framedZoom = k;
    target = {
      k,
      x: (left + right) / 2 - (padLeft + usableW / 2 - w / 2) / k,
      y: (top + bottom) / 2 - (padTop + usableH / 2 - h / 2) / k,
    };
  };

  /** The node under a screen point, nearest first, within its disc and a hair. */
  const nodeAt = (sx: number, sy: number): DrawNode | null => {
    const at = toWorld(sx, sy);
    let best: DrawNode | null = null;
    let bestDistance = Infinity;
    for (const node of nodes) {
      if (node.hidden || !Number.isFinite(node.x)) continue;
      const distance = Math.hypot(node.x! - at.x, node.y! - at.y);
      if (distance < node.look.radius + 3 / camera.k && distance < bestDistance) {
        best = node;
        bestDistance = distance;
      }
    }
    return best;
  };
  const islandAt = (sx: number, sy: number): string | null => {
    const at = toWorld(sx, sy);
    for (const { key, box } of islands) {
      if (at.x >= box.left && at.x <= box.right && at.y >= box.top && at.y <= box.bottom) return key;
    }
    return null;
  };
  const local = (event: { clientX: number; clientY: number }) => {
    const box = canvas.getBoundingClientRect();
    return { x: event.clientX - box.left, y: event.clientY - box.top };
  };

  const onWheel = (event: WheelEvent) => {
    event.preventDefault();
    const at = local(event);
    const before = toWorld(at.x, at.y);
    // The prototype's wheel math: a ctrl wheel is a trackpad pinch, which wants
    // a gentler step than a mouse's notch; the zoom can run out to 0.05.
    const factor = Math.exp(-event.deltaY * (event.ctrlKey ? 0.01 : 0.0016));
    const k = Math.min(8, Math.max(0.05, target.k * factor));
    const [w, h] = [host.clientWidth, host.clientHeight];
    // Keep the point under the pointer put: aim the camera so it stays.
    target = { k, x: before.x - (at.x - w / 2) / k, y: before.y - (at.y - h / 2) / k };
  };
  const onDown = (event: MouseEvent) => {
    if (event.button !== 0) return;
    dragging = { x: event.clientX, y: event.clientY };
  };
  const onMove = (event: MouseEvent) => {
    if (dragging) {
      target.x -= (event.clientX - dragging.x) / camera.k;
      target.y -= (event.clientY - dragging.y) / camera.k;
      camera.x = target.x;
      camera.y = target.y;
      dragging = { x: event.clientX, y: event.clientY };
      return;
    }
    const at = local(event);
    const node = nodeAt(at.x, at.y);
    if (node !== hovered) {
      hovered = node;
      events.hover(node);
    }
    canvas.style.cursor = node || islandAt(at.x, at.y) ? "pointer" : dragging ? "grabbing" : "";
  };
  const onUp = () => {
    dragging = null;
  };
  const onClick = (event: MouseEvent) => {
    const at = local(event);
    const key = islandAt(at.x, at.y);
    if (key !== null) {
      events.island(key);
      return;
    }
    const node = nodeAt(at.x, at.y);
    selected = node;
    events.click(node);
  };
  const onDouble = (event: MouseEvent) => {
    const at = local(event);
    const node = nodeAt(at.x, at.y);
    // A dot: one step in, straight to it. Empty canvas: back to the whole
    // frame, centred neat — the zoom the wheel takes five turns to undo.
    if (node) events.doubleClick(node);
    else frame(framed);
  };
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("mousedown", onDown);
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
  canvas.addEventListener("click", onClick);
  canvas.addEventListener("dblclick", onDouble);

  /** One drawn frame: edges in grouped additive passes, then dots, then names. */
  const draw = (now: number) => {
    const [w, h] = resize();
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.fillStyle = paint.ground;
    ctx.fillRect(0, 0, w, h);
    const k = camera.k;
    // The camera: world to screen, so every width and font below is in world
    // units divided by `k` and reads the same size on screen at any zoom.
    ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * (w / 2 - camera.x * k), dpr * (h / 2 - camera.y * k));
    drawEdges(now, k);
    drawNodes(k);
    drawNodeLabels(k);
    islands = drawIslandNames(k);
  };

  const drawEdges = (now: number, k: number) => {
    ctx.globalCompositeOperation = "lighter";
    ctx.lineCap = "round";
    // The prototype's zoom ramps: far off everything quiets, close in it thickens.
    const far = Math.min(1, Math.max(0, (0.55 - k) / 0.3));
    const zb = 0.75 + 0.45 * Math.min(1, Math.max(0, (k - 0.5) / 1.2));
    const lw = Math.max(0.55, Math.min(1, 0.45 + k * 0.3)) / k;
    const focus = focusSet();
    // The prototype's quiet at the fit out, and its cull: a wire whose both ends
    // sit off-screen never crosses the view to add clutter.
    const [w, h] = [host.clientWidth, host.clientHeight];
    const [vl, vt] = [camera.x - (w / 2 + 40) / k, camera.y - (h / 2 + 40) / k];
    const [vr, vb] = [camera.x + (w / 2 + 40) / k, camera.y + (h / 2 + 40) / k];
    const inView = (node: DrawNode) => (node.x ?? 0) >= vl && (node.x ?? 0) <= vr && (node.y ?? 0) >= vt && (node.y ?? 0) <= vb;
    for (const pass of [0, 1] as const) {
      // The bright pass belongs to a quiet focus (a filter's set); a hovered or
      // selected record lights its own links as rays instead.
      if (pass === 1 && (focus === null || focus.lit)) break;
      for (const group of EDGE_GROUPS) {
        ctx.strokeStyle = group.sign
          ? (group.positive ? paint.support : paint.against)
          : (paint.edges[group.kind ?? ""] ?? paint.struct);
        ctx.lineWidth = group.sign ? lw * 1.3 : lw;
        ctx.globalAlpha =
          pass === 1
            ? Math.min(1, group.base * zb * 1.6)
            : Math.min(1, group.base * zb) * (focus ? (focus.lit ? 0.32 : 0.4) : 1) * (1 - far * 0.4);
        ctx.setLineDash(group.dash ? [3 / k, 3 / k] : NO_DASH);
        ctx.beginPath();
        let any = false;
        for (const { link, a, b } of ends) {
          if (link.hidden || group.of(link) === false) continue;
          if (!inView(a) && !inView(b)) continue;
          // Cross-island wires are not part of an island's weave; they go below.
          if (grouped !== null && grouped(a) !== grouped(b)) continue;
          if (pass === 1 && !(focus!.set.has(a) && focus!.set.has(b))) continue;
          strokeEdge(a, b, false);
          any = true;
        }
        if (any) ctx.stroke();
        ctx.setLineDash(NO_DASH);
      }
    }
    // Connections between islands: the real cross-island evidence, as fine
    // hairlines — a hundred citations read as a sheen, not as spaghetti.
    const hair = 0.8 / k;
    // The prototype's own hairline alphas, and its density fade: a hundred
    // cross-island citations read as a sheen, not as a web. `sheen` is already
    // thinned to the design's density, so its size is the `dens` the design uses.
    const dens = Math.min(1, Math.max(0.45, 160 / Math.max(1, sheen.size)));
    for (const positive of [true, false] as const) {
      ctx.beginPath();
      let any = false;
      for (const { link, a, b } of ends) {
        if (!sheen.has(link) || link.hidden) continue;
        if (((link.valence ?? 0) >= 0) !== positive) continue;
        if (!inView(a) && !inView(b)) continue;
        strokeEdge(a, b, true);
        any = true;
      }
      if (!any) continue;
      ctx.globalAlpha = (focus ? (focus.lit ? 0.3 : 0.4) : 1) * (positive ? 0.34 : 0.38) * dens * (1 - far * 0.3);
      ctx.strokeStyle = positive ? paint.support : paint.against;
      ctx.lineWidth = hair;
      ctx.stroke();
    }
    // Structure between islands draws nothing at all, as in the design: its
    // clusters are joined only by the evidence hairlines above; the plumbing
    // appears when you zoom into an island, not across the field.
    // What the focus touches — the hovered node, else the selected one — as the
    // prototype does: a black casing under a thick animated dash, each link in
    // the hue of the neighbour it reaches, so a hover fans out in many colours.
    const rays = hovered ?? selected;
    if (rays) {
      for (const { link, a, b } of ends) {
        if (link.hidden || (a !== rays && b !== rays)) continue;
        const other = a === rays ? b : a;
        const cited = link.valence !== undefined;
        const width = (cited ? 2.2 + 2.6 * Math.abs(link.valence!) : 2.4) / k;
        ctx.globalCompositeOperation = "source-over";
        ctx.globalAlpha = 0.85;
        ctx.strokeStyle = paint.ground;
        ctx.lineWidth = width + 2.4 / k;
        ctx.setLineDash(NO_DASH);
        ctx.beginPath();
        strokeEdge(a, b, false);
        ctx.stroke();
        ctx.globalCompositeOperation = "source-over";
        ctx.globalAlpha = 1;
        ctx.strokeStyle = cited ? (link.valence! < 0 ? paint.against : paint.support) : other.look.fill;
        ctx.lineWidth = width;
        ctx.setLineDash(link.kind === "favors" ? [5 / k, 3 / k] : [12 / k, 3 / k]);
        ctx.lineDashOffset = -(((now / 45) % 1000) / k);
        ctx.beginPath();
        strokeEdge(a, b, false);
        ctx.stroke();
        ctx.setLineDash(NO_DASH);
        ctx.lineDashOffset = 0;
      }
    }
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
  };

  const strokeEdge = (a: DrawNode, b: DrawNode, bridge: boolean) => {
    const bow = bridge ? 0.18 : 0.12;
    const mx = (a.x! + b.x!) / 2 - (b.y! - a.y!) * bow;
    const my = (a.y! + b.y!) / 2 + (b.x! - a.x!) * bow;
    ctx.moveTo(a.x!, a.y!);
    ctx.quadraticCurveTo(mx, my, b.x!, b.y!);
  };

  const drawNodes = (k: number) => {
    ctx.globalCompositeOperation = "source-over";
    // A dot never shrinks below a visible speck, however far out the camera is.
    const minRadius = Math.max(1.2, 1.9 - k) / k;
    const keyline = k > 0.7;
    const focus = focusSet();
    // The design's ramp, measured from the frame rather than from the world: the
    // nine kind hues are whole two turns of the wheel past the fit that holds the
    // field — a quarter of the way up at a fifth of the zoom, a half at a half,
    // full at twice — where the prototype's absolute 0.95 to 1.45 asks for five
    // or six turns out of a field fitted at .27, so the work reads as a pair of
    // tints for most of the zooming anyone actually does.
    const kindAt = Math.min(KIND_ZOOM_LATEST, Math.max(KIND_ZOOM_EARLIEST, framedZoom * 2));
    const zq = Math.round(Math.min(1, Math.max(0, (k - kindAt * 0.7) / (kindAt * 0.3))) * 4) / 4;
    for (const node of nodes) {
      if (node.hidden || !Number.isFinite(node.x)) continue;
      const { look } = node;
      // A focused neighbourhood is the answer to what was asked: its own hues,
      // at any zoom. Everything else blends from the duo tints as you close in.
      const lit = focus !== null && focus.lit && focus.set.has(node);
      const fill = lit || zq === 1 ? look.fill : zq === 0 ? look.duo : mix(look.duo, look.fill, zq);
      const dim = focus !== null && !focus.set.has(node);
      const radius = Math.max(look.radius, minRadius);
      const belief = node.kind === "Belief";
      const state = belief
        ? node.judgement === "disproven"
          ? "hollow"
          : node.judgement === "proven"
            ? "solid"
            : "faint"
        : node.status === "abandoned" || node.status === "invalid"
          ? "retired"
          : "solid";
      ctx.beginPath();
      ctx.arc(node.x!, node.y!, radius, 0, Math.PI * 2);
      if (state === "hollow") {
        // A disproven claim stands as its outline only.
        ctx.globalAlpha = dim ? 0.36 : 0.95;
        ctx.strokeStyle = fill;
        ctx.lineWidth = 1.3 / Math.max(k, 0.8);
        ctx.stroke();
        continue;
      }
      ctx.globalAlpha = dim ? 0.36 : state === "retired" ? 0.35 : state === "faint" ? 0.55 : 0.92;
      ctx.fillStyle = fill;
      ctx.fill();
      if (keyline && !dim) {
        // Close in, a hair of black between neighbours keeps a dense ball legible.
        ctx.lineWidth = 1 / k;
        ctx.strokeStyle = paint.ground;
        ctx.stroke();
      }
    }
    if (selected) ring(selected, paint.ink, 1.4, 4, k);
    if (hovered && hovered !== selected) ring(hovered, paint.inkQuiet, 1, 3, k);
    ctx.globalAlpha = 1;
  };

  const ring = (node: DrawNode, color: string, width: number, pad: number, k: number) => {
    ctx.globalAlpha = 1;
    ctx.beginPath();
    ctx.arc(node.x!, node.y!, node.look.radius + pad / k + 1, 0, Math.PI * 2);
    ctx.lineWidth = width / k;
    ctx.strokeStyle = color;
    ctx.stroke();
  };

  const drawIslandNames = (k: number): { key: string; box: Box }[] => {
    if (!grouped || !named || k >= ISLAND_NAME_ZOOM) return [];
    const titles = new Map<string, string>();
    const found = islandsOf(nodes, (node) => {
      const key = grouped!(node as DrawNode);
      if (key === undefined) return undefined;
      if (!titles.has(key)) {
        const title = named!(key);
        if (title === undefined) return undefined;
        titles.set(key, title);
      }
      return key;
    })
      .filter((island) => island.shown >= ISLAND_MIN_NODES)
      .sort((a, b) => b.shown - a.shown)
      .slice(0, ISLAND_NAME_MAX);
    if (found.length === 0) return [];
    const titlePx = 12 / k;
    const countPx = 10.5 / k;
    const countOf = (island: Island) => `${island.shown.toLocaleString("en-US")} records, ${island.beliefs.toLocaleString("en-US")} beliefs`;
    const measure = (text: string, weight: string, px: number, family: string) => {
      ctx.font = `${weight} ${px}px ${family}`;
      return ctx.measureText(text).width;
    };
    // The panels are walls: a name that would slide under the briefing's
    // column or the open Peek goes beside its island instead, as the
    // prototype's reserved side rectangle does.
    const [w, h] = [host.clientWidth, host.clientHeight];
    const over = covered();
    const [vl, vr] = [camera.x - w / 2 / k, camera.x + w / 2 / k];
    const [vt, vb] = [camera.y - h / 2 / k, camera.y + h / 2 / k];
    const taken: Box[] = [];
    if (over.left > 0) taken.push({ left: vl - 1e6, top: vt, right: vl + over.left / k, bottom: vb });
    if (over.right > 0) taken.push({ left: vr - over.right / k, top: vt, right: vr + 1e6, bottom: vb });
    const placements = placeLabels(
      found,
      (island) => Math.max(measure(titles.get(island.key)!, "500", titlePx, font), measure(countOf(island), "400", countPx, mono)),
      { height: titlePx + countPx * 1.3, gap: 16 / k, taken },
    ).filter((placement) => placement.placed);
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.lineJoin = "round";
    ctx.lineWidth = 4 / k;
    ctx.globalAlpha = Math.min(1, (ISLAND_NAME_ZOOM - k) / 0.1);
    for (const { island, x, y } of placements) {
      ctx.font = `500 ${titlePx}px ${font}`;
      ctx.strokeStyle = ink.outline;
      ctx.strokeText(titles.get(island.key)!, x, y);
      ctx.fillStyle = ink.title;
      ctx.fillText(titles.get(island.key)!, x, y);
      ctx.font = `400 ${countPx}px ${mono}`;
      ctx.strokeText(countOf(island), x, y + titlePx * 1.15);
      ctx.fillStyle = ink.count;
      ctx.fillText(countOf(island), x, y + titlePx * 1.15);
    }
    ctx.globalAlpha = 1;
    return placements.map(({ island, box }) => ({ key: island.key, box }));
  };

  /** The prototype's naming rule: a focused neighbourhood names itself and its
   *  ring; otherwise only claims (beliefs) are named, from k 0.8 up, a growing
   *  few, never overlapping — which is what kept the 100-node view readable. */
  const drawNodeLabels = (k: number) => {
    const focus = focusSet();
    if (!focus && k < 0.8) return;
    const [w, h] = [host.clientWidth, host.clientHeight];
    const [vl, vt] = [camera.x - w / 2 / k, camera.y - h / 2 / k];
    const [vr, vb] = [camera.x + w / 2 / k, camera.y + h / 2 / k];
    const inView = (node: DrawNode) => (node.x ?? 0) >= vl && (node.x ?? 0) <= vr && (node.y ?? 0) >= vt && (node.y ?? 0) <= vb;
    const pool = (
      focus
        ? [...focus.set]
        : nodes.filter((node) => node.kind === "Belief")
    )
      .filter((node) => !node.hidden && Number.isFinite(node.x) && inView(node))
      .sort((a, b) => b.authority - a.authority)
      .slice(0, focus ? 31 : Math.round(10 + (k - 0.8) * 18));
    if (focus) {
      // The focused node names itself first, as the prototype's `label(fid,true)`.
      const fid = hovered ?? selected;
      const at = fid ? pool.indexOf(fid) : -1;
      if (at > 0) pool.splice(at, 1);
      if (fid && !fid.hidden && Number.isFinite(fid.x)) pool.unshift(fid);
    }
    if (pool.length === 0) return;
    const minRadius = Math.max(1.2, 1.9 - k) / k;
    const trunc = (text: string, at: number) => (text.length > at ? `${text.slice(0, at - 1)}…` : text);
    // Screen-space collision, in world units: the panels' columns and the
    // header's sky are claimed first, and no two names may share a box.
    const over = covered();
    const placed: Box[] = [];
    if (over.left > 0) placed.push({ left: vl, top: vt, right: vl + over.left / k, bottom: vb });
    if (over.right > 0) placed.push({ left: vr - over.right / k, top: vt, right: vr, bottom: vb });
    const fits = (x: number, y: number, wide: number) => {
      const [bx, by, bw, bh] = [x - 8 / k, y - 11 / k, wide + 16 / k, 22 / k];
      for (const p of placed) {
        if (bx < p.right && bx + bw > p.left && by < p.bottom && by + bh > p.top) return false;
      }
      placed.push({ left: bx, top: by, right: bx + bw, bottom: by + bh });
      return true;
    };
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    ctx.lineWidth = 3 / k;
    for (const node of pool) {
      const strong = node === (hovered ?? selected);
      const ref = `${node.kind}#${node.seq}`;
      const title = trunc(node.title || "(untitled)", 44);
      ctx.font = `400 ${10 / k}px ${mono}`;
      const rw = ctx.measureText(ref).width;
      ctx.font = `${strong ? "500" : "400"} ${11.5 / k}px ${font}`;
      const tw = ctx.measureText(title).width;
      const x = node.x! + Math.max(node.look.radius, minRadius) + 6 / k;
      const wide = rw + tw + 8 / k;
      if (!strong && !fits(x, node.y!, wide)) continue;
      if (strong) placed.push({ left: x - 8 / k, top: node.y! - 11 / k, right: x + wide + 8 / k, bottom: node.y! + 11 / k });
      ctx.strokeStyle = paint.ground;
      ctx.font = `400 ${10 / k}px ${mono}`;
      ctx.strokeText(ref, x, node.y!);
      ctx.fillStyle = paint.inkQuiet;
      ctx.fillText(ref, x, node.y!);
      ctx.font = `${strong ? "500" : "400"} ${11.5 / k}px ${font}`;
      ctx.strokeText(title, x + rw + 6 / k, node.y!);
      ctx.fillStyle = paint.ink;
      ctx.fillText(title, x + rw + 6 / k, node.y!);
    }
  };

  const tick = () => {
    if (!alive) return;
    // The camera eases toward what it frames; the prototype's .2 a frame.
    camera.x += (target.x - camera.x) * 0.2;
    camera.y += (target.y - camera.y) * 0.2;
    camera.k += (target.k - camera.k) * 0.2;
    if (!settled && engine) {
      const steps = nodes.length > 3000 ? 3 : 5;
      let running = false;
      for (let i = 0; i < steps; i++) running = engine.tick() || running;
      if (!running) {
        settled = true;
        if (frameWhenSettled) frame(framed);
        frameWhenSettled = false;
      }
    }
    draw(performance.now());
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);

  const api: Renderer = {
    setData(nextNodes, nextLinks, frameAfter) {
      nodes = [...nextNodes] as DrawNode[];
      links = [...nextLinks] as DrawLink[];
      const byId = new Map(nodes.map((node) => [node.id, node]));
      ends = links.flatMap((link) => {
        const [a, b] = [byId.get(typeof link.source === "string" ? link.from : link.source.id), byId.get(typeof link.target === "string" ? link.to : link.target.id)];
        return a && b ? [{ link, a, b }] : [];
      });
      if (frameAfter) {
        framed = frameAfter;
        frameWhenSettled = true;
      }
      settled = false;
    },
    repaint() {
      // Looks are recomputed by the model; the next frame picks them up.
    },
    fit(filter = EVERY) {
      framed = filter;
      frame(filter);
      frameWhenSettled = !settled;
    },
    refit() {
      frame(framed);
    },
    zoomBy(factor) {
      target = { ...target, k: Math.min(8, Math.max(0.05, target.k * factor)) };
    },
    group(groupOf) {
      grouped = groupOf;
      const signature = grouped ? [...new Set(nodes.map((node) => grouped!(node) ?? ""))].sort().join("|") : "";
      if (signature === groupingSignature) return;
      groupingSignature = signature;
      sheen = new Set();
      if (grouped) {
        // Group the cross-island evidence by the pair of islands it joins, then
        // keep a strided sample of each pair so no two discs are ever joined by
        // more than `SHEEN_CAP` lines. The design's own busiest pair shares
        // seven; ours can reach eighty when a well-cited belief sits in one
        // island and its citers fill another, and eighty lines between the same
        // two balls read as a searchlight, not as a connection.
        const pairs = new Map<string, DrawLink[]>();
        for (const { link, a, b } of ends) {
          if (link.kind !== "proves" && link.kind !== "favors") continue;
          const [ga, gb] = [grouped(a), grouped(b)];
          if (ga === undefined || gb === undefined || ga === gb) continue;
          const key = ga < gb ? `${ga}\u0000${gb}` : `${gb}\u0000${ga}`;
          const row = pairs.get(key);
          if (row) row.push(link);
          else pairs.set(key, [link]);
        }
        // A record's own share, counted across the whole field: the design's
        // busiest dot sends seven hairlines and no more; ours can send forty-five,
        // all leaving the same disc, so capping the pair alone still leaves a
        // searchlight — eight rays to each of a dozen neighbours.
        const spent = new Map<string, number>();
        const used = (id: string) => spent.get(id) ?? 0;
        for (const row of [...pairs.values()].sort((x, y) => y.length - x.length)) {
          // Round-robin over the dots each line leaves from, so the handful kept
          // fans out of the island's whole rim. Taken in order, they would all
          // leave the one belief the citations pile on and draw a starburst
          // however few of them survive.
          const lanes = new Map<string, DrawLink[]>();
          for (const link of row) {
            const lane = lanes.get(link.from);
            if (lane) lane.push(link);
            else lanes.set(link.from, [link]);
          }
          const lists = [...lanes.values()];
          const kept: DrawLink[] = [];
          for (let at = 0; kept.length < SHEEN_CAP; at++) {
            let any = false;
            for (const lane of lists) {
              const link = lane[at];
              if (link === undefined) continue;
              any = true;
              if (used(link.from) >= SHEEN_NODE_CAP || used(link.to) >= SHEEN_NODE_CAP) continue;
              kept.push(link);
              spent.set(link.from, used(link.from) + 1);
              spent.set(link.to, used(link.to) + 1);
              if (kept.length === SHEEN_CAP) break;
            }
            if (!any) break;
          }
          for (const link of kept) sheen.add(link);
        }
      }
      // The springs by id: `source` and `target` are the ids until something
      // swaps in the nodes, and this renderer never does, so looking a node up
      // by them finds nothing and the layout runs with no springs at all — a
      // field of uniform discs whose wires span the ball.
      const position = new Map(nodes.map((node, at) => [node.id, at] as const));
      engine = layout({
        nodes,
        communityOf: (node) => grouped?.(node) ?? WHOLE,
        links: links.flatMap((link) => {
          const [a, b] = [position.get(link.from), position.get(link.to)];
          return a === undefined || b === undefined ? [] : [{ source: a, target: b, kind: link.kind }];
        }),
        radiusOf: (node) => node.look.radius,
      });
      settled = false;
      frameWhenSettled = true;
    },
    nameIslands(name, nextInk) {
      named = name;
      ink = nextInk;
    },
    centre(node) {
      frameWhenSettled = false;
      // The prototype's `flyTo`: one step in, at least to k 1.6.
      const k = Math.max(target.k, 1.6);
      const over = covered();
      target = { k, x: (node.x ?? 0) + (over.right - over.left) / 2 / k, y: node.y ?? 0 };
    },
    screenOf(node) {
      return toScreen(node.x ?? 0, node.y ?? 0);
    },
    select(node) {
      // A selection made without the pointer — by search, a link, a cluster's
      // name — wears the same light a click on the node gives it. The next frame
      // picks it up; `tick` never stops drawing.
      selected = node;
    },
    dispose() {
      alive = false;
      observer.disconnect();
      themeWatch.disconnect();
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("mousedown", onDown);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      canvas.removeEventListener("click", onClick);
      canvas.removeEventListener("dblclick", onDouble);
      host.replaceChildren();
    },
  };
  return api;
};

/**
 * The prototype's edge passes: structure quiet, evidence in its valence colour.
 * A structural pass is one edge kind, so its colour is a single token read and
 * the whole pass strokes in one path — which is what keeps a field of thousands
 * of wires cheap, and why the colour is named by kind here rather than carried.
 */
const EDGE_GROUPS: {
  kind?: string;
  sign: boolean;
  positive?: boolean;
  dash?: boolean;
  base: number;
  of: (link: DrawLink) => boolean;
}[] = [
  { kind: "narrows", sign: false, base: 0.3, of: (l) => l.kind === "narrows" },
  { kind: "requires", sign: false, base: 0.3, of: (l) => l.kind === "requires" },
  { kind: "produced_by", sign: false, base: 0.26, of: (l) => l.kind === "produced_by" },
  { kind: "supersedes", sign: false, base: 0.4, of: (l) => l.kind === "supersedes" },
  { kind: "cites_paper", sign: false, base: 0.18, of: (l) => l.kind === "cites_paper" },
  // Evidence carries the picture even at the fit out (the video's green and
  // orange strands): a brighter alpha than the structure's, at the same width.
  { sign: true, positive: true, base: 0.72, of: (l) => l.kind === "proves" && (l.valence ?? 0) >= 0 },
  { sign: true, positive: false, base: 0.75, of: (l) => l.kind === "proves" && (l.valence ?? 0) < 0 },
  { sign: true, positive: true, dash: true, base: 0.45, of: (l) => l.kind === "favors" && (l.valence ?? 0) >= 0 },
  { sign: true, positive: false, dash: true, base: 0.48, of: (l) => l.kind === "favors" && (l.valence ?? 0) < 0 },
];

/**
 * What the canvas paints with, from the theme's own tokens (`tokens.css`) — the
 * same ones `readPalette` gives the node looks. Read here rather than taken from
 * a look because a pass strokes one path for every wire of a kind, so it needs
 * the kind's colour once, not each link's.
 */
type Paint = {
  readonly edges: { readonly [kind: string]: string };
  readonly support: string;
  readonly against: string;
  /** A wire of a kind the theme names no colour for. */
  readonly struct: string;
  /** A record's title, and the quieter ink its ref is set in. */
  readonly ink: string;
  readonly inkQuiet: string;
  /** What the field is painted on, and what a letter or a keyline is outlined in. */
  readonly ground: string;
};

function readPaint(): Paint {
  const style = getComputedStyle(document.documentElement);
  const token = (name: string) => style.getPropertyValue(name).trim();
  const struct = token("--g-edge-produced_by") || token("--g-fallback");
  return {
    edges: Object.fromEntries(STRUCTURAL.map((kind) => [kind, token(`--g-edge-${kind}`) || struct])),
    support: token("--g-vpos"),
    against: token("--g-vneg"),
    struct,
    ink: token("--text"),
    inkQuiet: token("--g-fallback"),
    ground: token("--g-bg"),
  };
}

/**
 * The edge kinds that take a colour of their own, read off the passes rather
 * than listed again here so the two cannot drift apart.
 *
 * Five of the seven stored kinds: `proves` and `favors` are missing because they
 * are not drawn by kind at all. A citation wears its VALENCE — mint for support,
 * amber against — so its pass asks for `support`/`against` and never for an
 * edge colour. (`tokens.css` still names `--g-edge-proves`, for the detail's
 * preview, which knows a wire's kind without its sign.)
 */
const STRUCTURAL: readonly string[] = EDGE_GROUPS.flatMap((group) => group.kind ?? []);
const NO_DASH: number[] = [];
const EVERY = () => true;
const WHOLE = "whole";
const FIT_MAX_ZOOM = 2.6;
/** The design's own ramp end: however far the frame sits, the hues are whole by here. */
const KIND_ZOOM_LATEST = 1.45;
/** …and never sooner than here, so a frame fitted on a handful of records still
 *  has a zoom to make before it is in the kind hues. */
const KIND_ZOOM_EARLIEST = 0.5;
/** Below this zoom an island wears its name; above it the dots name themselves. */
const ISLAND_NAME_ZOOM = 0.8;
/** The prototype's floor: a ball of eight records still reads as a place worth naming. */
const ISLAND_MIN_NODES = 8;
const ISLAND_NAME_MAX = 16;
/** The most evidence hairlines drawn between any two islands. */
const SHEEN_CAP = 8;
/** The most drawn off any one record, over the whole field: the design's busiest sends seven. */
const SHEEN_NODE_CAP = 6;
