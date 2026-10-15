import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { Icon } from "../ui/icons";
import { openStation, setStationMenu, stationsQuery, useStationMenu } from "./actions";
import { matching } from "./cycle";
import "./stations.css";

/**
 * The All stations button at the end of the tabs, which opens the station menu: a
 * search over the user's stations by name (Enter or a click opens one, the arrow
 * keys move the highlight, Esc closes the menu). The tabs rename and make stations.
 *
 * A press outside the menu closes it, as does the keyboard leaving it. A blur alone
 * does not: Safari focuses no button that is clicked, so a click on a station blurs
 * the search toward nothing, and closing then would lose the click.
 */
export function StationMenu({ station }: { readonly station: { readonly id: string } }) {
  const queryClient = useQueryClient();
  const open = useStationMenu();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
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
      setProblem(null);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setStationMenu(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
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
      onBlur={(event) => {
        const next = event.relatedTarget;
        if (open && next instanceof Node && !root.current?.contains(next)) setStationMenu(false);
      }}>
      <button ref={button} className="btn ghost station-all" type="button" aria-haspopup="dialog" aria-expanded={open}
        aria-label="All stations" title="All stations (g s)" onClick={() => setStationMenu(!open)}><Icon name="chevD" size={14} /></button>
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
          {problem && <p className="station-problem" role="alert">{problem}</p>}
        </div>
      )}
    </div>
  );
}
