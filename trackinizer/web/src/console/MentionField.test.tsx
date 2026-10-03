import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { FeedActorFacet, FeedEvent } from "../api/sessions";
import { Composer } from "../composer/Composer";
import type { Context } from "./mentions";
import { MentionField } from "./MentionField";

afterEach(cleanup);

/** `actor`'s session, heard from at 10:MM, in no room. */
function agent(actor: string, last: string): FeedActorFacet {
  return { actor, session_id: "00000000-0000-4000-8000-000000000001", cli: null, rooms: [], count: 1, conversation: 1, last: `2026-10-03T10:${last}:00Z`, ended: null };
}

/** A line of `actor`'s, written at 10:MM. */
function line(actor: string, created: string): FeedEvent {
  const at = `2026-10-03T10:${created}:00Z`;
  return { session_id: "00000000-0000-4000-8000-000000000002", actor, rooms: [], part: 0, seq: 0, kind: "AssistantMessage", created: at, message: {}, text: "" };
}

/** Two agents, `b` heard from last, both with lines in the feed: `@` offers b, a, then every agent shown. */
const CONTEXT: Context = { agents: [agent("a", "01"), agent("b", "05")], lines: [line("a", "01"), line("b", "05")], shown: [], rooms: [] };

/** The console's box over `CONTEXT`, focused, sending through a mock; nothing it sends is refused. */
function show() {
  const send = vi.fn<(text: string, key: string) => Promise<string>>().mockResolvedValue("Sent");
  render(
    <QueryClientProvider client={new QueryClient()}>
      <Composer
        send={send}
        target="console"
        enabled
        placeholder="@agent message"
        failure={(error) => error.message}
        field={(props, edit) => <MentionField field={props} edit={edit} context={CONTEXT} />}
      />
    </QueryClientProvider>,
  );
  const box = screen.getByLabelText<HTMLTextAreaElement>("Message");
  box.focus();
  return { send, box, type: (text: string) => fireEvent.change(box, { target: { value: text } }) };
}

/** Each option's target, in order; none while the list is closed. */
function offered(): string[] {
  return screen.queryAllByRole("option").map((option) => option.title);
}

test("@ lists the agents by rank, and the box is a combobox naming the active option only while it does", () => {
  const { box, type } = show();
  expect(box.getAttribute("role")).toBeNull();
  type("@");
  expect(offered()).toEqual(["@b", "@a", "@*"]);
  const list = screen.getByRole("listbox", { name: "Agents" });
  expect(screen.getByRole("combobox", { name: "Message" })).toBe(box);
  expect(box.getAttribute("aria-expanded")).toBe("true");
  expect(box.getAttribute("aria-controls")).toBe(list.id);
  const [first] = screen.getAllByRole("option");
  expect(box.getAttribute("aria-activedescendant")).toBe(first!.id);
  expect(first!.getAttribute("aria-selected")).toBe("true");
  expect(screen.getByRole("option", { name: /every agent shown/ }).title).toBe("@*");
  type("@A");
  expect(offered()).toEqual(["@a"]);
  type("@a hi");
  expect(offered()).toEqual([]);
  expect(["role", "aria-expanded", "aria-controls", "aria-activedescendant"].map((name) => box.getAttribute(name))).toEqual([null, null, null, null]);
});

test("the arrows move, wrapping, and Enter takes the active one without sending; then Enter sends", async () => {
  const { send, box, type } = show();
  type("@");
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(screen.getByRole("option", { selected: true }).title).toBe("@*");
  fireEvent.keyDown(box, { key: "ArrowDown" });
  fireEvent.keyDown(box, { key: "ArrowDown" });
  expect(box.getAttribute("aria-activedescendant")).toBe(screen.getByRole("option", { selected: true }).id);
  expect(screen.getByRole("option", { selected: true }).title).toBe("@a");
  fireEvent.keyDown(box, { key: "Enter" });
  expect(box.value).toBe("@a ");
  expect(offered()).toEqual([]);
  expect(send).not.toHaveBeenCalled();
  type("@a hi");
  fireEvent.keyDown(box, { key: "Enter" });
  await screen.findByRole("status");
  expect(send).toHaveBeenCalledWith("@a hi", expect.any(String));
});

test("Tab completes the target the caret is in, in a list, and leaves the caret after it, the list closed", () => {
  const { box, type } = show();
  type("@,@b hi");
  box.setSelectionRange(1, 1);
  fireEvent.keyUp(box, { key: "ArrowLeft" });
  // A list's target cannot be every agent.
  expect(offered()).toEqual(["@b", "@a"]);
  fireEvent.keyDown(box, { key: "Tab" });
  expect(box.value).toBe("@b,@b hi");
  expect([box.selectionStart, box.selectionEnd]).toEqual([2, 2]);
  expect(offered()).toEqual([]);
  expect(document.activeElement).toBe(box);
});

test("Esc closes the list, and only a new target opens it again; Esc goes no further than the box", () => {
  const { box, type } = show();
  const page = vi.fn();
  document.addEventListener("keydown", page);
  type("@");
  fireEvent.keyDown(box, { key: "Escape" });
  expect(offered()).toEqual([]);
  expect(box.value).toBe("@");
  type("@b");
  expect(offered()).toEqual([]);
  type("@b,@");
  expect(offered()).toEqual(["@b", "@a"]);
  document.removeEventListener("keydown", page);
  expect(page).not.toHaveBeenCalled();
});

test("a click takes an option and keeps the focus in the box; the list shows only while the box has focus", () => {
  const { box, type } = show();
  type("@");
  const option = screen.getByRole("option", { name: "@a" });
  // The press keeps the focus in the box, as a menu's options do.
  expect(fireEvent.mouseDown(option)).toBe(false);
  fireEvent.click(option);
  expect(box.value).toBe("@a ");
  expect(document.activeElement).toBe(box);
  type("@");
  fireEvent.blur(box);
  expect(offered()).toEqual([]);
  fireEvent.focus(box);
  expect(offered()).toEqual(["@b", "@a", "@*"]);
});

test("with nothing to offer Enter sends as before, and while an input method composes it does neither", async () => {
  const { send, box, type } = show();
  type("@");
  fireEvent.keyDown(box, { key: "Enter", isComposing: true });
  expect(box.value).toBe("@");
  expect(offered()).toEqual(["@b", "@a", "@*"]);
  type("@zz");
  expect(offered()).toEqual([]);
  fireEvent.keyDown(box, { key: "Enter" });
  await screen.findByRole("status");
  expect(send).toHaveBeenCalledWith("@zz", expect.any(String));
});
