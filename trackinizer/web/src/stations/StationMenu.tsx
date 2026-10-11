import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import type { WorkspaceState } from "../api/workspaces";
import { nameStation, newStation, openStation, setStationMenu, stationsQuery, useStationMenu } from "./actions";
import { matching } from "./cycle";
import "./stations.css";

/**
 * The station's name in Chat's header, which opens the station menu: a search
 * over the user's stations by name (Enter or a click opens one, the arrow keys
 * move the highlight, Esc closes the menu), Rename, and New station.
 */
export function StationMenu({ station }: { readonly station: WorkspaceState }) {
  const queryClient = useQueryClient();
  const open = useStationMenu();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [naming, setNaming] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const stations = useQuery({ ...stationsQuery, enabled: open, staleTime: 0 });
  const found = matching(stations.data ?? [], query);

  useEffect(() => {
    if (open) search.current?.focus();
    else {
      setQuery("");
      setActive(0);
      setNaming(null);
      setProblem(null);
    }
  }, [open]);

  function close() {
    setStationMenu(false);
    button.current?.focus();
  }

  async function pick(id: string) {
    try {
      await openStation(queryClient, id);
      setStationMenu(false);
    } catch {
      setProblem("Could not open that station.");
    }
  }

  async function rename() {
    if (naming === null) return;
    try {
      await nameStation(queryClient, station.id, naming);
      setNaming(null);
      setProblem(null);
      // The field that had the keyboard is gone: it goes back to the search, so Esc still closes the menu.
      search.current?.focus();
    } catch (error) {
      setProblem(error instanceof ApiError && error.status === 409 ? "That name is taken" : "Could not rename the station.");
    }
  }

  async function create() {
    try {
      await newStation(queryClient);
      setStationMenu(false);
    } catch {
      setProblem("Could not make a station.");
    }
  }

  function onSearchKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setActive((at) => Math.max(0, Math.min(found.length - 1, at + (event.key === "ArrowDown" ? 1 : -1))));
    } else if (event.key === "Enter") {
      event.preventDefault();
      const chosen = found[active];
      if (chosen) void pick(chosen.id);
    }
  }

  return (
    <div className="station" ref={root}
      onKeyDown={(event) => { if (event.key === "Escape" && open) { event.stopPropagation(); close(); } }}
      onBlur={(event) => { if (open && !root.current?.contains(event.relatedTarget)) setStationMenu(false); }}>
      <button ref={button} className="btn ghost station-name" type="button" aria-haspopup="dialog" aria-expanded={open}
        title="Stations" onClick={() => setStationMenu(!open)}>{station.name}</button>
      {open && (
        <div className="station-menu" role="dialog" aria-label="Stations">
          <input ref={search} type="search" aria-label="Search stations" placeholder="Search stations" value={query}
            onChange={(event) => { setQuery(event.target.value); setActive(0); }} onKeyDown={onSearchKey} />
          <ul className="station-list" aria-label="Stations">
            {stations.isPending && <li className="muted">Loading stations…</li>}
            {stations.isError && <li role="alert">Could not load stations.</li>}
            {stations.isSuccess && found.length === 0 && <li className="muted">No station by that name.</li>}
            {found.map((each, index) => (
              <li key={each.id}>
                <button type="button" className={`station-option${index === active ? " station-option-active" : ""}`}
                  aria-current={each.id === station.id ? "true" : undefined} onClick={() => void pick(each.id)}>{each.name}</button>
              </li>
            ))}
          </ul>
          {naming === null ? (
            <div className="station-actions">
              <button className="btn ghost" type="button" onClick={() => { setNaming(station.name); setProblem(null); }}>Rename</button>
              <button className="btn ghost" type="button" onClick={() => void create()}>New station</button>
            </div>
          ) : (
            <form className="station-rename" onSubmit={(event) => { event.preventDefault(); void rename(); }}>
              <input aria-label="Station name" maxLength={120} value={naming} autoFocus
                onChange={(event) => { setNaming(event.target.value); setProblem(null); }}
                onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); setNaming(null); setProblem(null); } }} />
              <button className="btn" type="submit" disabled={!naming.trim()}>Save</button>
            </form>
          )}
          {problem && <p className="station-problem" role="alert">{problem}</p>}
        </div>
      )}
    </div>
  );
}
