import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { preferences, receivePreferences } from "../preferences";
import { ChatPrompt } from "./ChatPrompt";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  style("default");
});

/** Show Chat in `chat_style`, as the user's preferences say: a choice would be sent, and its answer outlive the test. */
function style(chat_style: "default" | "terminal") {
  receivePreferences({ ...preferences(), chat_style });
}

test("the Default style draws no prompt", () => {
  const { container } = render(<ChatPrompt author="ada@example.com" station="work" />);
  expect(container.textContent).toBe("");
});

test("the Terminal style draws the author's handle at the host's first label, then a dollar", () => {
  style("terminal");
  const { container } = render(<ChatPrompt author="ada@example.com" station="work" />);
  // jsdom serves the page from localhost.
  expect(container.textContent).toBe("ada@localhost:[work]$ ");
  expect(container.querySelector("strong")?.textContent).toBe("ada@localhost");
  expect(container.querySelector(".chat-prompt-station")?.textContent).toBe("work");
});

test("a prompt appears and goes as the style changes", () => {
  const { container } = render(<ChatPrompt author="grace@example.com" station="work" />);
  act(() => style("terminal"));
  expect(container.textContent).toBe("grace@localhost:[work]$ ");
  act(() => style("default"));
  expect(container.textContent).toBe("");
});

test.each([["alpha.example.com", "alpha"], ["tracker.example.org", "tracker"], ["127.0.0.1", "127.0.0.1"]])(
  "the host %s is %s in the prompt", (hostname, shown) => {
    vi.stubGlobal("location", { hostname });
    style("terminal");
    const { container } = render(<ChatPrompt author="ada@example.com" station="work" />);
    expect(container.textContent).toBe(`ada@${shown}:[work]$ `);
  });
