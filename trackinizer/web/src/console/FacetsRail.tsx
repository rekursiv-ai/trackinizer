import { useState } from "react";
import type { FeedActorFacet } from "../api/sessions";
import { useMinuteClock } from "../detail/time";
import { Icon } from "../ui/icons";
import { findAgents, type Grouping, groupAgents, isPicked, toggle } from "./facets";

/** How much of the past the live facets count: an agent is listed when it wrote within it. */
export type Active = "15m" | "1h" | "24h" | "7d";

/**
 * The choices of Active in, and how far back each reaches. There is no "all":
 * the facets count every record in their window, and the whole history took
 * 8.8 s on 9 million records, against 0.5 s for 7 days.
 */
export const ACTIVE: readonly { readonly value: Active; readonly label: string; readonly ms: number }[] = [
  { value: "15m", label: "15 m", ms: 15 * 60_000 },
  { value: "1h", label: "1 h", ms: 60 * 60_000 },
  { value: "24h", label: "24 h", ms: 24 * 60 * 60_000 },
  { value: "7d", label: "7 d", ms: 7 * 24 * 60 * 60_000 },
];

/**
 * The agent facet: what the view picks, then a way to narrow a long list before
 * ticking: find by a fragment of a name or room (or a pattern, picked in one
 * step), how far back to look when live, and groups, each picked in one step.
 * A row is a session, so a name an ended session and a live one share shows
 * twice. Working turns Quiet as the minutes pass, with or without new records.
 */
export function AgentsSection({
  agents,
  picks,
  onPicks,
  active,
  onActive,
}: {
  agents: readonly FeedActorFacet[];
  picks: readonly string[];
  onPicks: (picks: string[]) => void;
  active: Active;
  /** Null when the view is a window of history, which sets how far back on its own. */
  onActive: ((active: Active) => void) | null;
}) {
  const [query, setQuery] = useState("");
  const [by, setBy] = useState<Grouping>("none");
  const groups = groupAgents(findAgents(agents, query), by, useMinuteClock());
  return (
    <section className="console-sec" aria-label="Agents">
      <h2 className="console-h">Agents</h2>
      <Picked picks={picks} onClear={(pick) => onPicks(picks.filter((other) => other !== pick))} />
      <Find label="Find agents" placeholder="Find, or a pattern: atlas-*" query={query} onQuery={setQuery} onPattern={(pattern) => onPicks([...picks, pattern])} />
      <div className="console-facet-tools">
        {onActive ? (
          <div className="console-seg" role="group" aria-label="Active in the last">
            {ACTIVE.map(({ value, label }) => (
              <button key={value} type="button" aria-pressed={active === value} onClick={() => onActive(value)}>
                {label}
              </button>
            ))}
          </div>
        ) : null}
        <select aria-label="Group agents by" value={by} onChange={(event) => setBy(event.currentTarget.value as Grouping)}>
          <option value="none">No groups</option>
          <option value="room">By room</option>
          <option value="cli">By CLI</option>
          <option value="state">By state</option>
          <option value="family">By name family</option>
        </select>
      </div>
      <div className="console-rows">
        {groups.map((group) => {
          const names = group.agents.map(({ actor }) => actor);
          return (
            <div key={group.key} className="console-group">
              {by === "none" ? null : (
                <Row
                  label={group.pattern ?? `${group.key} (whole group)`}
                  text={group.key}
                  count={group.agents.length}
                  picks={picks}
                  names={names}
                  onChange={() => onPicks(toggle(picks, names, group.pattern))}
                  head
                />
              )}
              {group.agents.map(({ session_id, actor, count }) => (
                <Row key={session_id} label={actor} text={actor} count={count} picks={picks} names={[actor]} onChange={() => onPicks(toggle(picks, [actor], null))} />
              ))}
            </div>
          );
        })}
      </div>
    </section>
  );
}

/** A facet of names with counts, rooms or CLIs, each ticked to pick it; rooms can be found and picked by pattern. */
export function PickSection({
  label,
  items,
  picks,
  onPicks,
  find = false,
}: {
  label: string;
  items: readonly { readonly name: string; readonly count: number }[];
  picks: readonly string[];
  onPicks: (picks: string[]) => void;
  find?: boolean;
}) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const shown = needle.includes("*") ? items : items.filter(({ name }) => name.toLowerCase().includes(needle));
  return (
    <section className="console-sec" aria-label={label}>
      <h2 className="console-h">{label}</h2>
      <Picked
        picks={picks.filter((pick) => !items.some(({ name }) => name === pick))}
        onClear={(pick) => onPicks(picks.filter((other) => other !== pick))}
      />
      {find ? (
        <Find label={`Find ${label.toLowerCase()}`} placeholder="Find, or a pattern: atlas-*" query={query} onQuery={setQuery} onPattern={(pattern) => onPicks([...picks, pattern])} />
      ) : null}
      <div className="console-rows">
        {shown.map(({ name, count }) => (
          <Row key={name} label={name} text={name} count={count} picks={picks} names={[name]} onChange={() => onPicks(toggle(picks, [name], null))} />
        ))}
      </div>
    </section>
  );
}

/** What a facet picks, each with a button that clears it, so a pattern or a name no longer listed can be cleared. */
function Picked({ picks, onClear }: { picks: readonly string[]; onClear: (pick: string) => void }) {
  if (!picks.length) return null;
  return (
    <ul className="console-picked">
      {picks.map((pick) => (
        <li key={pick} className="console-chip">
          <span title={pick}>{pick}</span>
          <button type="button" aria-label={`Clear ${pick}`} onClick={() => onClear(pick)}>
            <Icon name="x" size={11} />
          </button>
        </li>
      ))}
    </ul>
  );
}

/** A find box; a query with `*` is a pattern, which a button picks, so that it takes in new names that match. */
function Find({
  label,
  placeholder,
  query,
  onQuery,
  onPattern,
}: {
  label: string;
  placeholder: string;
  query: string;
  onQuery: (query: string) => void;
  onPattern: (pattern: string) => void;
}) {
  const pattern = query.trim();
  return (
    <div className="console-find">
      <input type="search" aria-label={label} placeholder={placeholder} value={query} onChange={(event) => onQuery(event.currentTarget.value)} />
      {pattern.includes("*") ? (
        <button
          type="button"
          className="btn"
          onClick={() => {
            onPattern(pattern);
            onQuery("");
          }}
        >
          Pick {pattern}
        </button>
      ) : null}
    </div>
  );
}

/** One tick: a name, or a group's head, which is ticked when all its names are and half ticked when some are. */
function Row({
  label,
  text,
  count,
  picks,
  names,
  onChange,
  head = false,
}: {
  label: string;
  text: string;
  count: number;
  picks: readonly string[];
  names: readonly string[];
  onChange: () => void;
  head?: boolean;
}) {
  const ticked = names.filter((name) => isPicked(picks, name)).length;
  return (
    <label className={head ? "console-row console-row-head" : "console-row"}>
      <input
        type="checkbox"
        aria-label={label}
        checked={ticked > 0 && ticked === names.length}
        ref={(box) => {
          if (box) box.indeterminate = ticked > 0 && ticked < names.length;
        }}
        onChange={onChange}
      />
      <span className="console-row-name" title={text}>
        {text}
      </span>
      <span className="console-n">{count.toLocaleString("en")}</span>
    </label>
  );
}
