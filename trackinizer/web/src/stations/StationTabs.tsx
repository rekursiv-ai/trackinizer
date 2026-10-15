import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import type { StationSummary } from "../api/stations";
import type { WorkspaceState } from "../api/workspaces";
import { Icon } from "../ui/icons";
import { nameStation, newStation, openStation, stationsQuery } from "./actions";
import { StationMenu } from "./StationMenu";
import "./stations.css";

/** A name being edited: the station's, and what the field holds. */
type Naming = { readonly id: string; readonly value: string };

/**
 * The user's stations as tabs along Chat's top, as a browser's: one per station,
 * in the order they were made, the one on show current. A click on another tab
 * opens it; a click on the current one, or a double click on any, edits its name
 * in place (Enter or leaving the field saves, Esc keeps the name it had, a taken
 * name says so and keeps the field). + makes a fresh station and opens it, and
 * All stations opens the station menu's search.
 */
export function StationTabs({ station }: { readonly station: WorkspaceState }) {
  const queryClient = useQueryClient();
  const listed = useQuery(stationsQuery).data;
  const [naming, setNaming] = useState<Naming | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  // What the field holds as the handlers read it: a blur that comes after Enter or Esc settled the name sees null.
  const live = useRef<Naming | null>(null);
  const strip = useRef<HTMLElement>(null);
  const [refocus, setRefocus] = useState(false);
  const tabs = inOrder(listed ?? [], station);

  useEffect(() => {
    if (!refocus) return;
    strip.current?.querySelector<HTMLElement>('[aria-current="true"]')?.focus();
    setRefocus(false);
  }, [refocus]);

  // The current tab stays in sight as the strip scrolls.
  useEffect(() => {
    strip.current?.querySelector('[aria-current="true"]')?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [station.id, tabs.length]);

  function edit(next: Naming | null) {
    live.current = next;
    setNaming(next);
    setProblem(null);
  }

  function stop() {
    edit(null);
    setRefocus(true);
  }

  async function open(id: string): Promise<boolean> {
    try {
      await openStation(queryClient, id);
      setProblem(null);
      return true;
    } catch {
      setProblem("Could not open that station.");
      return false;
    }
  }

  async function save() {
    const held = live.current;
    if (!held) return;
    const name = held.value.trim();
    if (!name || name === station.name) {
      stop();
      return;
    }
    live.current = null;
    try {
      await nameStation(queryClient, held.id, name);
      stop();
    } catch (error) {
      live.current = held;
      setProblem(error instanceof ApiError && error.status === 409 ? "That name is taken" : "Could not rename the station.");
    }
  }

  async function create() {
    try {
      await newStation(queryClient);
      setProblem(null);
    } catch {
      setProblem("Could not make a station.");
    }
  }

  function onNameKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      void save();
    } else if (event.key === "Escape") {
      event.stopPropagation();
      stop();
    }
  }

  return (
    <div className="station-tabs">
      <nav ref={strip} className="station-strip" aria-label="Station tabs">
        {tabs.map((each) => each.id === naming?.id ? (
          <input key={each.id} className="station-tab station-tab-name" aria-label="Station name" maxLength={120}
            value={naming.value} autoFocus onFocus={(event) => event.currentTarget.select()}
            onChange={(event) => edit({ id: each.id, value: event.target.value })}
            onKeyDown={onNameKey} onBlur={() => void save()} />
        ) : (
          <button key={each.id} type="button" title={each.name}
            className={`station-tab${each.id === station.id ? " station-name" : ""}`}
            aria-current={each.id === station.id ? "true" : undefined}
            onClick={() => { if (each.id === station.id) edit({ id: each.id, value: each.name }); else void open(each.id); }}
            onDoubleClick={() => {
              if (each.id === station.id) return;
              void open(each.id).then((opened) => { if (opened) edit({ id: each.id, value: each.name }); });
            }}>{each.name}</button>
        ))}
      </nav>
      <button className="btn ghost station-new" type="button" aria-label="New station" title="New station"
        onClick={() => void create()}><Icon name="plus" size={14} /></button>
      <StationMenu station={station} />
      {problem && <p className="station-problem" role="alert">{problem}</p>}
    </div>
  );
}

/**
 * The tabs: every station in the order they were made (by id when made together),
 * so opening one moves none. The one on show is always among them, under the name
 * it has now, even before the list has it.
 */
function inOrder(listed: readonly StationSummary[], current: WorkspaceState): { readonly id: string; readonly name: string }[] {
  const ordered = listed.toSorted((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id))
    .map((each) => (each.id === current.id ? { id: each.id, name: current.name } : each));
  return ordered.some((each) => each.id === current.id) ? ordered : [...ordered, { id: current.id, name: current.name }];
}
