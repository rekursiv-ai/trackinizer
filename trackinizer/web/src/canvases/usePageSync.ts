import { useEffect, useRef } from "react";
import { setCanvasPage } from "../api/canvasManagement";
import { SHEET, useHash, validHash } from "../router/trail";
import { setCanvas, useCurrentCanvas } from "./store";

/** The least time between two writes of a canvas's page. */
export const PAGE_EVERY_MS = 500;

/**
 * Write the page the tab shows to its canvas, so switching back finds it. A
 * write goes at once if none went in the last `PAGE_EVERY_MS`, else when that
 * time is up, with the page the tab is on then. A switch that moved the page
 * writes nothing until the address is where the switch sent it, and a page the
 * server would refuse, or a sheet over the page, is not written.
 */
export function usePageSync(): void {
  const { id, landing } = useCurrentCanvas();
  const hash = useHash();
  const sent = useRef<{ canvas: string | null; route: string | null; at: number }>({ canvas: null, route: null, at: 0 });
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  useEffect(() => {
    clearTimeout(timer.current);
    // A sheet (Settings, Admin) lies over the page: the canvas keeps the page under it.
    if (!id || !validHash(hash) || SHEET.test(hash)) return;
    if (landing !== null) {
      // Not there yet, or there: the canvas already holds this page, so nothing is written for it.
      if (hash === landing) {
        sent.current = { canvas: id, route: hash, at: Date.now() };
        setCanvas(id, null);
      }
      return;
    }
    const last = sent.current;
    if (last.canvas === id && last.route === hash) return;
    const write = () => {
      sent.current = { canvas: id, route: hash, at: Date.now() };
      void setCanvasPage(id, hash).catch(() => {});
    };
    const wait = last.canvas === id ? last.at + PAGE_EVERY_MS - Date.now() : 0;
    if (wait <= 0) write();
    else timer.current = setTimeout(write, wait);
  }, [id, landing, hash]);
}
