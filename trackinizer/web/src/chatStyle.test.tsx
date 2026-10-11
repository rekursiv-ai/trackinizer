import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { applyChatStyle, chooseChatStyle, installChatStyle, readChatStyle, useChatStyle } from "./chatStyle";

function Shown() {
  return <output data-testid="style">{useChatStyle()}</output>;
}

const attribute = () => document.documentElement.dataset.chatStyle;

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  // The page's style is the module's, which outlives a test: each starts on the default.
  chooseChatStyle("default");
});

test("a missing or unknown choice is the default look", () => {
  expect(readChatStyle()).toBe("default");
  localStorage.setItem("trackinizer.chat.style", "neon");
  expect(readChatStyle()).toBe("default");
});

test("terminal is kept in this browser, marks the page, and shows wherever it is read", () => {
  render(<Shown />);
  expect(screen.getByTestId("style").textContent).toBe("default");
  act(() => chooseChatStyle("terminal"));
  expect(localStorage.getItem("trackinizer.chat.style")).toBe("terminal");
  expect(readChatStyle()).toBe("terminal");
  expect(attribute()).toBe("terminal");
  expect(screen.getByTestId("style").textContent).toBe("terminal");
  act(() => chooseChatStyle("default"));
  expect(attribute()).toBeUndefined();
  expect(screen.getByTestId("style").textContent).toBe("default");
});

test("booting applies the stored style", () => {
  localStorage.setItem("trackinizer.chat.style", "terminal");
  installChatStyle();
  expect(attribute()).toBe("terminal");
  applyChatStyle("default");
  expect(attribute()).toBeUndefined();
});

test("a style chosen while the browser refuses to store it holds", () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("Full", "QuotaExceededError");
  });
  render(<Shown />);
  act(() => chooseChatStyle("terminal"));
  expect(screen.getByTestId("style").textContent).toBe("terminal");
  expect(attribute()).toBe("terminal");
});

test("a storage that cannot be read gives the default look", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new DOMException("Denied", "SecurityError");
  });
  expect(readChatStyle()).toBe("default");
});
