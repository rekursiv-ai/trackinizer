import { boundsOf, fitBounds, GraphCamera } from "./camera";
import { parseCssColor } from "./colors";
import {
  CLOSE_ZOOM,
  CROSS_ISLAND_STRENGTH,
  FIT_MAX_ZOOM,
  FIT_MS,
  FIT_PADDING_PX,
  LINK_STRENGTH,
  MOVE_MS,
  ZOOM_MS,
} from "./constants";
import { LabelOverlay } from "./labels";
import type { WorkerIn, WorkerOut } from "./layout.worker";
import { type DrawLink, type DrawNode, isPlaced } from "./model";
import { bindCanvasPointer } from "./pointer";
import { type CreateRenderer, forceGraphRenderer } from "./renderer";
import {
  ARROW_FRAG,
  ARROW_VERT,
  compileProgram,
  createInstancedVao,
  createStaticBuffer,
  LINK_FRAG,
  LINK_STRIDE_FLOATS,
  LINK_VERT,
  NODE_FLOATS,
  NODE_FRAG,
  NODE_VERT,
} from "./shaders";

const ARROW_PX = 4;
const ARROW_MIN_ZOOM = 0.35;
const ZERO_RGBA = [0, 0, 0, 0] as const;

/**
 * WebGL2 + Web Worker graph renderer implementing `CreateRenderer`.
 * Falls back to `forceGraphRenderer` if WebGL2 is unavailable.
 */
export const webglRenderer: CreateRenderer = (host, events, covered) => {
  const glCanvas = document.createElement("canvas");
  const gl = glCanvas.getContext("webgl2", {
    antialias: true,
    alpha: true,
    premultipliedAlpha: false,
  });
  if (!gl) {
    return forceGraphRenderer(host, events, covered);
  }

  const labelCanvas = document.createElement("canvas");
  glCanvas.style.cssText =
    "position:absolute;inset:0;width:100%;height:100%;display:block;";
  labelCanvas.style.cssText =
    "position:absolute;inset:0;width:100%;height:100%;display:block;pointer-events:none;";
  host.replaceChildren(glCanvas, labelCanvas);

  const font = getComputedStyle(host).fontFamily || "sans-serif";
  const labels = new LabelOverlay(labelCanvas, font);
  const camera = new GraphCamera();

  const nodeProg = compileProgram(gl, NODE_VERT, NODE_FRAG);
  const linkProg = compileProgram(gl, LINK_VERT, LINK_FRAG);
  const arrowProg = compileProgram(gl, ARROW_VERT, ARROW_FRAG);

  const nodeQuadBuf = createStaticBuffer(
    gl,
    new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
  );
  const linkQuadBuf = createStaticBuffer(
    gl,
    new Float32Array([0, -0.5, 1, -0.5, 0, 0.5, 0, 0.5, 1, -0.5, 1, 0.5]),
  );
  const arrowTriBuf = createStaticBuffer(
    gl,
    new Float32Array([0, 0, -1, 0.5, -1, -0.5]),
  );
  const nodeInstBuf = gl.createBuffer();
  const linkInstBuf = gl.createBuffer();
  if (!nodeInstBuf || !linkInstBuf)
    throw new Error("Failed to create instance buffers");

  const nodeVao = createInstancedVao(gl, nodeQuadBuf, nodeInstBuf, NODE_FLOATS, [
    [1, 2, 0],
    [2, 1, 2],
    [3, 4, 3],
    [4, 4, 7],
    [5, 4, 11],
    [6, 4, 15],
    [7, 4, 19],
  ]);
  const linkVao = createInstancedVao(gl, linkQuadBuf, linkInstBuf, LINK_STRIDE_FLOATS, [
    [1, 2, 0],
    [2, 2, 2],
    [3, 1, 4],
    [4, 1, 5],
    [5, 1, 6],
    [6, 4, 7],
  ]);
  const arrowVao = createInstancedVao(
    gl,
    arrowTriBuf,
    linkInstBuf,
    LINK_STRIDE_FLOATS,
    [
      [1, 2, 0],
      [2, 2, 2],
      [3, 1, 4],
      [4, 4, 7],
    ],
  );

  const uNodeCam = gl.getUniformLocation(nodeProg, "u_cam");
  const uNodeRes = gl.getUniformLocation(nodeProg, "u_res");
  const uLinkCam = gl.getUniformLocation(linkProg, "u_cam");
  const uLinkRes = gl.getUniformLocation(linkProg, "u_res");
  const uLinkArrow = gl.getUniformLocation(linkProg, "u_arrowLen");
  const uArrowCam = gl.getUniformLocation(arrowProg, "u_cam");
  const uArrowRes = gl.getUniformLocation(arrowProg, "u_res");
  const uArrowLen = gl.getUniformLocation(arrowProg, "u_arrowLen");

  let nodes: DrawNode[] = [];
  let links: DrawLink[] = [];
  let nodeById = new Map<string, DrawNode>();
  let indexById = new Map<string, number>();
  let labelled: DrawNode[] = [];
  let groupFn: ((node: DrawNode) => string | undefined) | null = null;

  let nodeData = new Float32Array(0);
  let linkData = new Float32Array(0);
  let visibleNodeCount = 0;
  let visibleLinkCount = 0;
  let buffersDirty = true;

  let framed: (node: DrawNode) => boolean = () => true;
  let frameWhenSettled = false;
  let settling = false;
  let workerGen = 0;
  let rafId = 0;
  let disposed = false;
  let dpr = Math.min(window.devicePixelRatio || 1, 2);

  const fitHost = () => {
    const w = host.clientWidth;
    const h = host.clientHeight;
    if (w <= 0 || h <= 0) return;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    const pw = Math.round(w * dpr);
    const ph = Math.round(h * dpr);
    if (glCanvas.width !== pw || glCanvas.height !== ph) {
      glCanvas.width = pw;
      glCanvas.height = ph;
      labelCanvas.width = pw;
      labelCanvas.height = ph;
    }
    camera.resize(w, h);
    gl.viewport(0, 0, pw, ph);
  };

  const frame = (filter: (node: DrawNode) => boolean) => {
    fitHost();
    const bounds = boundsOf(nodes, filter);
    if (!bounds) return;
    const target = fitBounds(
      bounds,
      camera.width,
      camera.height,
      covered().right,
      FIT_MAX_ZOOM,
      FIT_PADDING_PX,
    );
    camera.animateTo(target.x, target.y, target.zoom, FIT_MS);
    scheduleDraw();
  };

  const relabel = () => {
    labelled = nodes
      .filter((node) => node.look.label !== null)
      .sort((a, b) => b.degree - a.degree);
  };

  const writeRgba = (
    out: Float32Array,
    offset: number,
    c: readonly [number, number, number, number],
  ) => {
    out[offset] = c[0];
    out[offset + 1] = c[1];
    out[offset + 2] = c[2];
    out[offset + 3] = c[3];
  };

  const uploadBuffers = () => {
    if (nodeData.length < nodes.length * NODE_FLOATS) {
      nodeData = new Float32Array(Math.max(64, nodes.length) * NODE_FLOATS);
    }
    if (linkData.length < links.length * LINK_STRIDE_FLOATS) {
      linkData = new Float32Array(Math.max(64, links.length) * LINK_STRIDE_FLOATS);
    }

    let nCount = 0;
    for (const node of nodes) {
      if (node.hidden || !isPlaced(node)) continue;
      const base = nCount * NODE_FLOATS;
      const { look } = node;
      nodeData[base] = node.x;
      nodeData[base + 1] = node.y;
      nodeData[base + 2] = look.radius;
      writeRgba(
        nodeData,
        base + 3,
        look.fillAlpha > 0 ? parseCssColor(look.fill, look.fillAlpha) : ZERO_RGBA,
      );
      writeRgba(
        nodeData,
        base + 7,
        look.wash ? parseCssColor(look.wash, look.washAlpha) : ZERO_RGBA,
      );
      writeRgba(nodeData, base + 11, parseCssColor(look.ring, look.ringAlpha));
      writeRgba(nodeData, base + 15, look.dot ? parseCssColor(look.dot, 1) : ZERO_RGBA);
      writeRgba(
        nodeData,
        base + 19,
        look.halo ? parseCssColor(look.halo, 1) : ZERO_RGBA,
      );
      nCount += 1;
    }
    visibleNodeCount = nCount;

    let lCount = 0;
    for (const link of links) {
      if (link.hidden) continue;
      const src =
        typeof link.source === "string" ? nodeById.get(link.source) : link.source;
      const dst =
        typeof link.target === "string" ? nodeById.get(link.target) : link.target;
      if (!src || !dst || !isPlaced(src) || !isPlaced(dst)) continue;
      const base = lCount * LINK_STRIDE_FLOATS;
      linkData[base] = src.x;
      linkData[base + 1] = src.y;
      linkData[base + 2] = dst.x;
      linkData[base + 3] = dst.y;
      linkData[base + 4] = dst.look.radius;
      linkData[base + 5] = link.look.width;
      linkData[base + 6] = link.look.dash ? (link.look.dash[0] ?? 4) : 0;
      const col = parseCssColor(link.look.color, 1);
      const hubAlpha =
        link.valence === undefined && link.look.width > 2.5 && col[3] > 0.3 ? 0.5 : 1;
      writeRgba(linkData, base + 7, [col[0], col[1], col[2], col[3] * hubAlpha]);
      lCount += 1;
    }
    visibleLinkCount = lCount;

    gl.bindBuffer(gl.ARRAY_BUFFER, nodeInstBuf);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      nodeData.subarray(0, visibleNodeCount * NODE_FLOATS),
      gl.DYNAMIC_DRAW,
    );
    gl.bindBuffer(gl.ARRAY_BUFFER, linkInstBuf);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      linkData.subarray(0, visibleLinkCount * LINK_STRIDE_FLOATS),
      gl.DYNAMIC_DRAW,
    );
    buffersDirty = false;
  };

  const renderFrame = () => {
    rafId = 0;
    if (disposed) return;
    fitHost();
    const stillAnimating = camera.step();
    if (buffersDirty) uploadBuffers();

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    const arrowLen = camera.zoom >= ARROW_MIN_ZOOM ? ARROW_PX / camera.zoom : 0;
    if (visibleLinkCount > 0) {
      bindProgram(gl, linkProg);
      gl.uniform3f(uLinkCam, camera.x, camera.y, camera.zoom);
      gl.uniform2f(uLinkRes, camera.width, camera.height);
      gl.uniform1f(uLinkArrow, arrowLen);
      gl.bindVertexArray(linkVao);
      gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, visibleLinkCount);

      if (arrowLen > 0) {
        bindProgram(gl, arrowProg);
        gl.uniform3f(uArrowCam, camera.x, camera.y, camera.zoom);
        gl.uniform2f(uArrowRes, camera.width, camera.height);
        gl.uniform1f(uArrowLen, arrowLen);
        gl.bindVertexArray(arrowVao);
        gl.drawArraysInstanced(gl.TRIANGLES, 0, 3, visibleLinkCount);
      }
    }

    if (visibleNodeCount > 0) {
      bindProgram(gl, nodeProg);
      gl.uniform3f(uNodeCam, camera.x, camera.y, camera.zoom);
      gl.uniform2f(uNodeRes, camera.width, camera.height);
      gl.bindVertexArray(nodeVao);
      gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, visibleNodeCount);
    }

    gl.bindVertexArray(null);
    labels.draw(labelled, camera, dpr);
    if (stillAnimating) scheduleDraw();
  };

  const scheduleDraw = () => {
    if (!rafId && !disposed) rafId = requestAnimationFrame(renderFrame);
  };

  const worker = new Worker(new URL("./layout.worker.ts", import.meta.url), {
    type: "module",
  });
  worker.onmessage = (event: MessageEvent<WorkerOut>) => {
    if (disposed || event.data.gen !== workerGen) return;
    const { x, y, settled } = event.data;
    const len = Math.min(nodes.length, x.length);
    for (let i = 0; i < len; i++) {
      const node = nodes[i];
      if (!node) continue;
      node.x = x[i];
      node.y = y[i];
    }
    buffersDirty = true;
    scheduleDraw();
    if (settled) {
      settling = false;
      if (frameWhenSettled) frame(framed);
      frameWhenSettled = false;
    }
  };

  const encodeGroups = () => {
    const groupId = new Int32Array(nodes.length).fill(-1);
    const ids = new Map<string, number>();
    if (groupFn) {
      for (let i = 0; i < nodes.length; i++) {
        const node = nodes[i];
        if (!node) continue;
        const g = groupFn(node);
        if (g === undefined) continue;
        let idx = ids.get(g);
        if (idx === undefined) {
          idx = ids.size;
          ids.set(g, idx);
        }
        groupId[i] = idx;
      }
    }
    const linkStrength = new Float32Array(links.length);
    for (let i = 0; i < links.length; i++) {
      const link = links[i];
      if (!link) continue;
      const si = indexById.get(link.from) ?? -1;
      const ti = indexById.get(link.to) ?? -1;
      linkStrength[i] =
        !groupFn || (si >= 0 && ti >= 0 && groupId[si] === groupId[ti])
          ? LINK_STRENGTH
          : CROSS_ISLAND_STRENGTH;
    }
    return { groupId, linkStrength };
  };

  const unbindPointer = bindCanvasPointer(
    glCanvas,
    camera,
    () => nodes,
    events,
    scheduleDraw,
  );
  const resize = new ResizeObserver(() => {
    fitHost();
    scheduleDraw();
  });
  resize.observe(host);
  fitHost();

  return {
    setData(nextNodes, nextLinks, frameAfter) {
      nodes = [...nextNodes];
      links = [...nextLinks];
      nodeById = new Map(nodes.map((n) => [n.id, n]));
      indexById = new Map(nodes.map((n, i) => [n.id, i]));
      for (const link of links) {
        link.source = nodeById.get(link.from) ?? link.from;
        link.target = nodeById.get(link.to) ?? link.to;
      }
      if (frameAfter) {
        framed = frameAfter;
        frameWhenSettled = true;
      }
      settling = true;
      relabel();
      buffersDirty = true;
      scheduleDraw();

      const x = new Float32Array(nodes.length);
      const y = new Float32Array(nodes.length);
      const vx = new Float32Array(nodes.length);
      const vy = new Float32Array(nodes.length);
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i];
        x[i] = n?.x ?? Number.NaN;
        y[i] = n?.y ?? Number.NaN;
        vx[i] = n?.vx ?? 0;
        vy[i] = n?.vy ?? 0;
      }
      const linkFrom = new Int32Array(links.length);
      const linkTo = new Int32Array(links.length);
      for (let i = 0; i < links.length; i++) {
        const l = links[i];
        linkFrom[i] = l ? (indexById.get(l.from) ?? 0) : 0;
        linkTo[i] = l ? (indexById.get(l.to) ?? 0) : 0;
      }
      const { groupId, linkStrength } = encodeGroups();
      workerGen += 1;
      const msg: WorkerIn = {
        type: "set",
        gen: workerGen,
        count: nodes.length,
        x,
        y,
        vx,
        vy,
        linkFrom,
        linkTo,
        linkStrength,
        groupId,
        reheat: true,
      };
      worker.postMessage(msg, {
        transfer: [
          x.buffer,
          y.buffer,
          vx.buffer,
          vy.buffer,
          linkFrom.buffer,
          linkTo.buffer,
          linkStrength.buffer,
          groupId.buffer,
        ],
      });
    },
    repaint() {
      relabel();
      buffersDirty = true;
      scheduleDraw();
    },
    fit(filter = () => true) {
      framed = filter;
      frame(filter);
      frameWhenSettled = settling;
    },
    refit() {
      frame(framed);
    },
    zoomBy(factor) {
      camera.animateTo(camera.x, camera.y, camera.zoom * factor, ZOOM_MS);
      scheduleDraw();
    },
    group(nextGroupOf) {
      groupFn = nextGroupOf;
      const { groupId, linkStrength } = encodeGroups();
      frameWhenSettled = true;
      settling = true;
      workerGen += 1;
      const msg: WorkerIn = {
        type: "group",
        gen: workerGen,
        groupId,
        linkStrength,
        reheat: true,
      };
      worker.postMessage(msg, {
        transfer: [groupId.buffer, linkStrength.buffer],
      });
    },
    centre(node) {
      frameWhenSettled = false;
      fitHost();
      const over = covered();
      const zoom = Math.max(camera.zoom, CLOSE_ZOOM);
      camera.animateTo(
        (node.x ?? 0) + over.right / 2 / zoom,
        node.y ?? 0,
        zoom,
        MOVE_MS,
      );
      scheduleDraw();
    },
    screenOf(node) {
      return camera.graphToScreen(node.x ?? 0, node.y ?? 0);
    },
    dispose() {
      disposed = true;
      if (rafId) cancelAnimationFrame(rafId);
      worker.terminate();
      resize.disconnect();
      unbindPointer();
      gl.deleteProgram(nodeProg);
      gl.deleteProgram(linkProg);
      gl.deleteProgram(arrowProg);
      gl.deleteBuffer(nodeQuadBuf);
      gl.deleteBuffer(linkQuadBuf);
      gl.deleteBuffer(arrowTriBuf);
      gl.deleteBuffer(nodeInstBuf);
      gl.deleteBuffer(linkInstBuf);
      gl.deleteVertexArray(nodeVao);
      gl.deleteVertexArray(linkVao);
      gl.deleteVertexArray(arrowVao);
      host.replaceChildren();
    },
  };
};

function bindProgram(gl: WebGL2RenderingContext, prog: WebGLProgram): void {
  // biome-ignore lint/correctness/useHookAtTopLevel: WebGL2RenderingContext.useProgram is not a React hook
  gl.useProgram(prog);
}
