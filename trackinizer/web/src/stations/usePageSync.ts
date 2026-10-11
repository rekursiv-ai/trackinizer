import { useEffect, useRef } from "react";
import { setStationPage } from "../api/stations";
import { useHash, validHash } from "../router/trail";
import { setStation, useCurrentStation } from "./store";

/** The least time between two writes of a station's page. */
export const PAGE_EVERY_MS = 500;

/**
 * Write the page the tab shows to its station, so switching back finds it. A
 * write goes at once if none went in the last `PAGE_EVERY_MS`, else when that
 * time is up, with the page the tab is on then. A switch that moved the page
 * writes nothing until the address is where the switch sent it, and a page the
 * server would refuse is not written.
 */
export function usePageSync(): void {
  const { id, landing } = useCurrentStation();
  const hash = useHash();
  const sent = useRef<{ station: string | null; route: string | null; at: number }>({ station: null, route: null, at: 0 });
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  useEffect(() => {
    clearTimeout(timer.current);
    if (!id || !validHash(hash)) return;
    if (landing !== null) {
      // Not there yet, or there: the station already holds this page, so nothing is written for it.
      if (hash === landing) {
        sent.current = { station: id, route: hash, at: Date.now() };
        setStation(id, null);
      }
      return;
    }
    const last = sent.current;
    if (last.station === id && last.route === hash) return;
    const write = () => {
      sent.current = { station: id, route: hash, at: Date.now() };
      void setStationPage(id, hash).catch(() => {});
    };
    const wait = last.station === id ? last.at + PAGE_EVERY_MS - Date.now() : 0;
    if (wait <= 0) write();
    else timer.current = setTimeout(write, wait);
  }, [id, landing, hash]);
}
