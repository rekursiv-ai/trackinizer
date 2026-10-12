import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { stubFetch } from "./api/testing";
import { applyChatStyle, chooseChatStyle, installChatStyle, readChatStyle, useChatStyle } from "./chatStyle";
import { preferences, receivePreferences } from "./preferences";

function Shown() {
  return <output data-testid="style">{useChatStyle()}</output>;
}

const attribute = () => document.documentElement.dataset.chatStyle;

afterEach(() => {
  cleanup();
  // The page's style is the module's, which outlives a test: each starts on the default.
  receivePreferences({ ...preferences(), chat_style: "default" });
});

test("the style is Default until the user's preferences say otherwise", () => {
  expect(readChatStyle()).toBe("default");
  receivePreferences({ ...preferences(), chat_style: "terminal" });
  expect(readChatStyle()).toBe("terminal");
});

test("terminal marks the page, shows wherever it is read, and is sent to the user's preferences", async () => {
  const sent = stubFetch(() => Response.json({ ...preferences(), chat_style: "terminal" }));
  installChatStyle();
  render(<Shown />);
  expect(screen.getByTestId("style").textContent).toBe("default");
  act(() => chooseChatStyle("terminal"));
  expect(readChatStyle()).toBe("terminal");
  expect(attribute()).toBe("terminal");
  expect(screen.getByTestId("style").textContent).toBe("terminal");
  await vi.waitFor(() => expect(sent.map(({ method, path, body }) => [method, path, body])).toEqual([["PATCH", "/api/me/preferences", { chat_style: "terminal" }]]));
  expect(localStorage.length).toBe(0);
});

test("a style the server brings, as from another surface or the agent, marks the page", () => {
  installChatStyle();
  act(() => receivePreferences({ ...preferences(), chat_style: "terminal" }));
  expect(attribute()).toBe("terminal");
  applyChatStyle("default");
  expect(attribute()).toBeUndefined();
});
