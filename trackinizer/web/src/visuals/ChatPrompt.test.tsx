import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { chooseChatStyle } from "../chatStyle";
import { ChatPrompt } from "./ChatPrompt";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  chooseChatStyle("default");
});

test("the Default style draws no prompt", () => {
  const { container } = render(<ChatPrompt author="ada@example.com" station="work" />);
  expect(container.textContent).toBe("");
});

test("the Terminal style draws the author's handle at the host's first label, then a dollar", () => {
  chooseChatStyle("terminal");
  const { container } = render(<ChatPrompt author="ada@example.com" station="work" />);
  // jsdom serves the page from localhost.
  expect(container.textContent).toBe("ada@localhost:[work]$ ");
  expect(container.querySelector("strong")?.textContent).toBe("ada@localhost");
  expect(container.querySelector(".chat-prompt-station")?.textContent).toBe("work");
});

test("a prompt appears and goes as the style is chosen", () => {
  const { container } = render(<ChatPrompt author="grace@example.com" station="work" />);
  act(() => chooseChatStyle("terminal"));
  expect(container.textContent).toBe("grace@localhost:[work]$ ");
  act(() => chooseChatStyle("default"));
  expect(container.textContent).toBe("");
});

test.each([["alpha.example.com", "alpha"], ["tracker.example.org", "tracker"], ["127.0.0.1", "127.0.0.1"]])(
  "the host %s is %s in the prompt", (hostname, shown) => {
    vi.stubGlobal("location", { hostname });
    chooseChatStyle("terminal");
    const { container } = render(<ChatPrompt author="ada@example.com" station="work" />);
    expect(container.textContent).toBe(`ada@${shown}:[work]$ `);
  });
