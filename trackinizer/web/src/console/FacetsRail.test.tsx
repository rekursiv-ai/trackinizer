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

test("a session the caller may flip has a Public/Private switch; another's private one is marked; the rest have neither", () => {
  const mine = { ...session(1, "mine", 1, 5), account: "ada@example.com", audience: "open", hideable: true };
  const hid = { ...session(2, "hid", 1, 3), account: "ada@example.com", audience: "private", hideable: true };
  const theirs = { ...session(3, "theirs", 1, 2), account: "grace@example.com", audience: "open", hideable: false };
  // Another's private session, which an admin lists: marked, but not theirs to flip.
  const seen = { ...session(4, "seen", 1, 1), account: "grace@example.com", audience: "private", hideable: false };
  const flipped: FeedActorFacet[] = [];
  const props = { picks: [], onPicks: () => {}, active: "1h" as const, onActive: null };
  const { rerender } = render(<AgentsSection agents={[mine, hid, theirs, seen]} {...props} onHide={(agent) => flipped.push(agent)} />);
  const offered = () => screen.queryAllByRole("button", { name: / is (public|private): / }).map((button) => [button.getAttribute("aria-label"), button.textContent]);
  expect(offered()).toEqual([["mine is public: make it private", "Public"], ["hid is private: make it public", "Private"]]);
  // The switch says it for the caller's own: only another's private session carries the mark.
  const marked = [...document.querySelectorAll(".console-row")].filter((row) => row.querySelector(".console-badge")).map((row) => row.querySelector(".console-row-name")!.textContent);
  expect(marked).toEqual(["seen"]);
  fireEvent.click(screen.getByRole("button", { name: "mine is public: make it private" }));
  fireEvent.click(screen.getByRole("button", { name: "hid is private: make it public" }));
  expect(flipped.map(({ actor }) => actor)).toEqual(["mine", "hid"]);
  // With no switch to offer, a private session of the caller's is marked instead.
  rerender(<AgentsSection agents={[mine, hid]} {...props} />);
  expect(offered()).toEqual([]);
  expect(screen.getByText("Private").className).toBe("console-badge");
});

test("a chat's session that waits on the person is marked, and one whose run was taken over offers Reattach to Chat", () => {
  const waiting = { ...session(1, "chat-3d0e9f1a1b2c", 1, 5), state: "requires_action" as const };
  const taken = { ...session(2, "chat-4e1fa02b2c3d", 1, 3), state: "taken_over" as const };
  const quiet = { ...session(3, "tiles-a", 1, 2), state: "running" as const };
  const props = { picks: [], onPicks: () => {}, active: "1h" as const, onActive: null };
  const reattached: FeedActorFacet[] = [];
  const { rerender } = render(<AgentsSection agents={[waiting, taken, quiet]} {...props} onReattach={(agent) => reattached.push(agent)} />);
  expect([...document.querySelectorAll(".console-row")].filter((row) => row.querySelector(".console-waiting")).map((row) => row.querySelector(".console-row-name")!.textContent))
    .toEqual(["chat-3d0e9f1a1b2c"]);
  expect(screen.getAllByRole("button", { name: /^Reattach / }).map((button) => [button.getAttribute("aria-label"), button.textContent])).toEqual([["Reattach chat-4e1fa02b2c3d to Chat", "Reattach to Chat"]]);
  fireEvent.click(screen.getByRole("button", { name: "Reattach chat-4e1fa02b2c3d to Chat" }));
  expect(reattached.map(({ actor }) => actor)).toEqual(["chat-4e1fa02b2c3d"]);
  // The mark is read-only information; the button needs a way to reattach.
  rerender(<AgentsSection agents={[waiting, taken, quiet]} {...props} />);
  expect(screen.queryByRole("button", { name: /^Reattach / })).toBeNull();
  expect(document.querySelectorAll(".console-waiting")).toHaveLength(1);
  expect(screen.getByText("Taken over in a terminal")).toBeTruthy();
});
