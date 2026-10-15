import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import type { FeedActorFacet } from "../api/sessions";
import { AgentsSection, PickSection } from "./FacetsRail";

afterEach(() => {
  cleanup();
});

/**
 * One moment every session's age counts back from: read once, so two sessions the
 * same minutes old are heard from at the same instant, and their order is the
 * order given, however long the test takes between them.
 */
const NOW = Date.now();

/** Session `id`'s agent `actor`, last heard from `minutes` before `NOW`, with `count` records. */
function session(id: number, actor: string, minutes: number, count: number, ended: string | null = null): FeedActorFacet {
  const last = new Date(NOW - minutes * 60_000).toISOString();
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

test("a session the caller may hide offers Hide, a hidden one is marked and offers Unhide, and the rest offer neither", () => {
  const mine = { ...session(1, "mine", 1, 5), account: "ada@example.com", audience: "open", hideable: true };
  const hid = { ...session(2, "hid", 1, 3), account: "ada@example.com", audience: "private", hideable: true };
  const theirs = { ...session(3, "theirs", 1, 2), account: "grace@example.com", audience: "open", hideable: false };
  // Another's hidden session, which an admin lists: marked, but not theirs to unhide.
  const seen = { ...session(4, "seen", 1, 1), account: "grace@example.com", audience: "private", hideable: false };
  const hidden: FeedActorFacet[] = [];
  const props = { picks: [], onPicks: () => {}, active: "1h" as const, onActive: null };
  const { rerender } = render(<AgentsSection agents={[mine, hid, theirs, seen]} {...props} onHide={(agent) => hidden.push(agent)} />);
  const offered = () => screen.queryAllByRole("button", { name: /^(Hide|Unhide) / }).map((button) => button.getAttribute("aria-label"));
  expect(offered()).toEqual(["Hide mine", "Unhide hid"]);
  const marked = [...document.querySelectorAll(".console-row")].filter((row) => row.querySelector(".console-badge")).map((row) => row.querySelector(".console-row-name")!.textContent);
  expect(marked).toEqual(["hid", "seen"]);
  fireEvent.click(screen.getByRole("button", { name: "Hide mine" }));
  fireEvent.click(screen.getByRole("button", { name: "Unhide hid" }));
  expect(hidden.map(({ actor }) => actor)).toEqual(["mine", "hid"]);
  // With no way to hide, no row offers it.
  rerender(<AgentsSection agents={[mine, hid]} {...props} />);
  expect(offered()).toEqual([]);
});
