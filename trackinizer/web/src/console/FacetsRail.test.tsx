import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import type { FeedActorFacet } from "../api/sessions";
import { AgentsSection, PickSection } from "./FacetsRail";

afterEach(() => {
  cleanup();
});

/** Session `id`'s agent `actor`, last heard from `minutes` ago, with `count` records. */
function session(id: number, actor: string, minutes: number, count: number, ended: string | null = null): FeedActorFacet {
  const last = new Date(Date.now() - minutes * 60_000).toISOString();
  return { actor, session_id: `00000000-0000-4000-8000-${String(id).padStart(12, "0")}`, cli: "codex", rooms: [], count, conversation: 0, last, ended };
}

test("each row is a session: a name an ended session and a live one share leaves no row behind when the ended one goes", () => {
  const ended = session(1, "same", 3, 5, new Date().toISOString());
  const live = session(2, "same", 1, 7);
  const other = session(3, "other", 2, 3);
  const props = { picks: [], onPicks: () => {}, active: "1h" as const, onActive: null };
  const rows = () => [...document.querySelectorAll(".console-row")].map((row) => row.textContent);
  const { rerender } = render(<AgentsSection agents={[live, other, ended]} {...props} />);
  expect(rows()).toEqual(["same7", "other3", "same5"]);
  // The ended session falls out of the window, and the other agent is heard from since.
  rerender(<AgentsSection agents={[{ ...other, last: new Date().toISOString() }, live]} {...props} />);
  expect(rows()).toEqual(["other3", "same7"]);
});

test("a long agent or room name is cut to an ellipsis in its row and its picked chip, whole on hover and in the tick's name", () => {
  const name = "lead_nightly-regression-sweep-across-all-shards";
  const room = "nightly-regression-sweep-across-all-shards";
  render(<AgentsSection agents={[session(1, name, 1, 5)]} picks={[name]} onPicks={() => {}} active="1h" onActive={null} />);
  render(<PickSection label="Rooms" items={[{ name: room, count: 5 }]} picks={[`${room.slice(0, 12)}*`]} onPicks={() => {}} find />);
  for (const full of [name, room, `${room.slice(0, 12)}*`]) expect(screen.getAllByTitle(full).every((shown) => shown.textContent === full)).toBe(true);
  // The agent's row and its picked chip.
  expect(screen.getAllByTitle(name)).toHaveLength(2);
  expect(screen.getByRole("checkbox", { name })).toBeTruthy();
  expect(screen.getByRole("checkbox", { name: room })).toBeTruthy();
});
