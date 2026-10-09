import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import type { FeedEvent } from "../api/sessions";
import { ToastProvider } from "../ui/toast";
import { ConsoleLine, ToChips } from ".";
import { appendEvents } from "./feed";

afterEach(() => {
  cleanup();
});

const LONG = "lead_nightly-regression-sweep-across-all-shards";
const ROOM = "nightly-regression-sweep-across-all-shards";

/** One record of agent `actor` in `rooms`, as the feed sends it. */
function event(actor: string, rooms: string[], kind: string, message: { [field: string]: unknown }): FeedEvent {
  return {
    session_id: "00000000-0000-4000-8000-0000000000c1",
    actor,
    rooms,
    cli: "codex",
    part: 0,
    seq: 1,
    kind,
    created: "2026-10-01T10:00:01Z",
    model: "m",
    message: { "py/object": `trackinizer.lib.agent.types.sessions.${kind}`, ...message },
    text: "",
  };
}

/** `event` drawn as one console line, its agent's click told to `onMention`; a code block's or a path's Copy says how it went in a toast. */
function line(
  one: FeedEvent,
  onMention: ((event: FeedEvent) => void) | null = null,
  onContinue: ((sessionId: string) => void) | null = null,
) {
  return render(
    <ToastProvider>
      <ConsoleLine held={appendEvents([], [one])[0]!} kinds={["Issue"]} onMention={onMention} onContinue={onContinue} />
    </ToastProvider>,
  );
}

test("a line's agent and rooms are cut to an ellipsis, each whole on hover and to a screen reader", () => {
  line(event(LONG, [ROOM, "ops"], "AssistantMessage", { content: "On it." }));
  for (const name of [LONG, `${ROOM}, ops`]) {
    const shown = screen.getByTitle(name);
    expect(shown.className).toContain("console-name");
    expect(shown.textContent).toBe(name);
  }
  // The agent under its time; its rooms and what the line is on the line below.
  const [first, second] = [...document.querySelectorAll(".turn-h")].map((row) => row.textContent!);
  expect([first!.endsWith(`:01${LONG}`), second]).toEqual([true, `[${ROOM}, ops]Assistant`]);
});

test("a line's agent is a button that asks for a message to it, named for it", () => {
  const asked: FeedEvent[] = [];
  const one = event(LONG, [ROOM], "AssistantMessage", { content: "On it." });
  line(one, (event) => asked.push(event));
  const agent = screen.getByLabelText(`Message ${LONG}`);
  expect([agent.tagName, agent.getAttribute("type"), agent.title, agent.textContent]).toEqual(["BUTTON", "button", LONG, LONG]);
  agent.click();
  expect(asked.map(({ actor }) => actor)).toEqual([LONG]);
});

test("a science chat's session offers Continue in Chat, which opens that session in Chat", () => {
  const continued: string[] = [];
  const one = event("chat-3d0e9f1a1b2c", [], "AssistantMessage", { content: "An answer." });
  line(one, null, (sessionId) => continued.push(sessionId));
  const button = screen.getByRole("button", { name: "Continue in Chat" });
  button.click();
  expect(continued).toEqual([one.session_id]);
});

test("no other session offers Continue in Chat, nor does a console outside a canvas", () => {
  line(event("tiles-a", [], "AssistantMessage", { content: "On it." }), null, () => {});
  line(event("chat-3d0e9f1a1b2", [], "AssistantMessage", { content: "A near miss." }), null, () => {});
  line(event("scout", [], "AssistantMessage", { content: "The assistant itself." }), null, () => {});
  expect(screen.queryByRole("button", { name: "Continue in Chat" })).toBeNull();
  cleanup();
  line(event("chat-3d0e9f1a1b2c", [], "AssistantMessage", { content: "An answer." }));
  expect(screen.queryByRole("button", { name: "Continue in Chat" })).toBeNull();
});

test("a To chip's name is cut to an ellipsis, whole on hover and in its button's name", () => {
  render(<ToChips to={[{ actor: LONG, room: ROOM }]} onLeaveOut={() => {}} />);
  const chip = screen.getByTitle(`@${LONG}:${ROOM}`);
  expect(chip.className).toContain("console-name");
  expect(chip.textContent).toBe(`@${LONG}:${ROOM}`);
  expect(screen.getByRole("button", { name: `Leave out @${LONG}:${ROOM}` }).tagName).toBe("BUTTON");
});

test("a long message shows its first lines until Show all, the agent's own too, as a person's does in a transcript", () => {
  line(event("tiles-a", [], "AssistantMessage", { content: "A line.\n".repeat(30) }));
  expect(screen.getByRole("button", { name: "Show all" }).getAttribute("aria-expanded")).toBe("false");
  cleanup();
  line(event("tiles-a", [], "AssistantMessage", { content: "Short." }));
  expect(screen.queryByRole("button", { name: "Show all" })).toBeNull();
});

test("a message keeps its lines, as the agent's chat wrote them", () => {
  line(event("Ada", [], "UserMessage", { content: "Ada: New in the thread:\n@helperbot: Launched.\n@helperbot: Done." }));
  expect(document.querySelectorAll(".turn-body br")).toHaveLength(2);
});
