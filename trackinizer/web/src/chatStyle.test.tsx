import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { stubFetch } from "./api/testing";
import { applyChatStyle, chooseChatStyle, installChatStyle, readChatStyle, useChatStyle } from "./chatStyle";
import { preferences, receivePreferences, sendWaiting } from "./preferences";

function Shown() {
  return <output data-testid="style">{useChatStyle()}</output>;
}

const attribute = () => document.documentElement.dataset.chatStyle;

afterEach(async () => {
  cleanup();
  // The page's style is the module's, which outlives a test: each starts on the default, once the
  // test's own sends are answered (one on its way would make the page ignore this).
  await sendWaiting();
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

test("the Terminal style's text takes the user's font size, wherever it is set", () => {
  installChatStyle();
  expect(document.documentElement.style.getPropertyValue("--t-font-size")).toBe("13px");
  act(() => receivePreferences({ ...preferences(), terminal_font_size: "16pt" }));
  expect(document.documentElement.style.getPropertyValue("--t-font-size")).toBe("16pt");
  act(() => receivePreferences({ ...preferences(), terminal_font_size: "13px" }));
});

test("a style the server brings, as from another surface or the agent, marks the page", () => {
  installChatStyle();
  act(() => receivePreferences({ ...preferences(), chat_style: "terminal" }));
  expect(attribute()).toBe("terminal");
  applyChatStyle("default");
  expect(attribute()).toBeUndefined();
});
