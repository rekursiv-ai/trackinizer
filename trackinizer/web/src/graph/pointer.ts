import type { GraphCamera } from "./camera";
import { hitTestNode } from "./hitTest";
import type { DrawNode } from "./model";
import type { RendererEvents } from "./renderer";

const CLICK_DRAG_PX = 4;

/**
 * Bind pan, wheel-zoom, hover, click, and double-click interactions on `canvas`
 * using `camera` and `hitTestNode`. Returns a cleanup function.
 */
export function bindCanvasPointer(
  canvas: HTMLCanvasElement,
  camera: GraphCamera,
  getNodes: () => readonly DrawNode[],
  events: RendererEvents,
  scheduleDraw: () => void,
): () => void {
  let pointed: DrawNode | null = null;
  let panning = false;
  let moved = false;
  let downX = 0;
  let downY = 0;
  let lastX = 0;
  let lastY = 0;

  const hitAtClient = (clientX: number, clientY: number) => {
    const rect = canvas.getBoundingClientRect();
    const g = camera.screenToGraph(clientX - rect.left, clientY - rect.top);
    return hitTestNode(getNodes(), g.x, g.y, camera.zoom);
  };

  const onPointerDown = (ev: PointerEvent) => {
    if (ev.button !== 0) return;
    panning = true;
    moved = false;
    downX = ev.clientX;
    downY = ev.clientY;
    lastX = ev.clientX;
    lastY = ev.clientY;
    canvas.setPointerCapture(ev.pointerId);
  };

  const onPointerMove = (ev: PointerEvent) => {
    if (panning) {
      const dx = ev.clientX - lastX;
      const dy = ev.clientY - lastY;
      lastX = ev.clientX;
      lastY = ev.clientY;
      if (Math.hypot(ev.clientX - downX, ev.clientY - downY) > CLICK_DRAG_PX) {
        moved = true;
      }
      if (moved) {
        camera.panByScreen(dx, dy);
        scheduleDraw();
      }
      return;
    }
    const hit = hitAtClient(ev.clientX, ev.clientY);
    if (hit !== pointed) {
      pointed = hit;
      canvas.style.cursor = hit ? "pointer" : "";
      events.hover(hit);
    }
  };

  const onPointerUp = (ev: PointerEvent) => {
    if (!panning) return;
    panning = false;
    if (canvas.hasPointerCapture(ev.pointerId)) {
      canvas.releasePointerCapture(ev.pointerId);
    }
    if (!moved && ev.button === 0) {
      events.click(hitAtClient(ev.clientX, ev.clientY));
    }
  };

  const onPointerLeave = () => {
    if (pointed !== null) {
      pointed = null;
      canvas.style.cursor = "";
      events.hover(null);
    }
  };

  const onDoubleClick = (ev: MouseEvent) => {
    const hit = hitAtClient(ev.clientX, ev.clientY);
    if (hit) events.doubleClick(hit);
  };

  const onWheel = (ev: WheelEvent) => {
    ev.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const factor = Math.exp(-ev.deltaY * 0.002);
    camera.zoomAtScreen(ev.clientX - rect.left, ev.clientY - rect.top, factor);
    scheduleDraw();
  };

  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointerleave", onPointerLeave);
  canvas.addEventListener("dblclick", onDoubleClick);
  canvas.addEventListener("wheel", onWheel, { passive: false });

  return () => {
    canvas.removeEventListener("pointerdown", onPointerDown);
    canvas.removeEventListener("pointermove", onPointerMove);
    canvas.removeEventListener("pointerup", onPointerUp);
    canvas.removeEventListener("pointerleave", onPointerLeave);
    canvas.removeEventListener("dblclick", onDoubleClick);
    canvas.removeEventListener("wheel", onWheel);
  };
}
