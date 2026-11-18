import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { stubFetch } from "../api/testing";
import { preferences, receivePreferences } from "../preferences";
import { readHighlightMentions, useHighlightMentions } from "./highlightMentions";

function Switch() {
  const [on, set] = useHighlightMentions();
  return <button type="button" data-testid="on" onClick={() => set(!on)}>{String(on)}</button>;
}

const shown = () => screen.getByTestId("on").textContent;

afterEach(() => {
  cleanup();
  receivePreferences({ ...preferences(), highlight_mentions: true });
});

test("on until the user's preferences say otherwise", () => {
  expect(readHighlightMentions()).toBe(true);
  render(<Switch />);
  expect(shown()).toBe("true");
  act(() => receivePreferences({ ...preferences(), highlight_mentions: false }));
  expect(shown()).toBe("false");
  expect(readHighlightMentions()).toBe(false);
});

test("turning it off shows at once and is sent to the user's preferences", async () => {
  const sent = stubFetch(() => Response.json({ ...preferences(), highlight_mentions: false }));
  render(<Switch />);
  act(() => screen.getByTestId("on").click());
  expect(shown()).toBe("false");
  await vi.waitFor(() => expect(sent.map(({ method, path, body }) => [method, path, body])).toEqual([["PATCH", "/api/me/preferences", { highlight_mentions: false }]]));
});
