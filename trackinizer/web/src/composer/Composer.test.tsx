import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Composer } from "./Composer";

afterEach(cleanup);

/**
 * Show a composer sending through `send`, refusing what `check` finds wrong;
 * `rerender` moves it to another target or turns it off.
 */
function show(send: (text: string, key: string) => Promise<string>, check?: (text: string) => string) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const view = (target: string, enabled: boolean) => (
    <QueryClientProvider client={client}>
      <Composer
        send={send}
        target={target}
        enabled={enabled}
        placeholder="Write to the agent"
        failure={(error) => `Not sent: ${error.message}`}
        check={check}
      />
    </QueryClientProvider>
  );
  const { rerender } = render(view("session-a", true));
  return {
    box: screen.getByRole("textbox", { name: "Message" }),
    rerender: (target: string, enabled = true) => rerender(view(target, enabled)),
  };
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

test("a failed send keeps its draft; Retry sends it under the same key, then the receipt shows and the box empties", async () => {
  const send = vi.fn<(text: string, key: string) => Promise<string>>().mockRejectedValueOnce(new Error("timed out")).mockResolvedValueOnce("Queued for codex");
  const { box } = show(send);
  fireEvent.change(box, { target: { value: "Stop and summarize." } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect((await screen.findByRole("alert")).textContent).toBe("Not sent: timed out");
  expect(box).toHaveProperty("value", "Stop and summarize.");
  fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
  expect((await screen.findByRole("status")).textContent).toBe("Queued for codex");
  expect(box).toHaveProperty("value", "");
  const [first, second] = send.mock.calls;
  expect(first![1]).toMatch(UUID);
  expect(second).toEqual(first);
});

test("an edited draft, or the same draft to another target, sends under a fresh key", async () => {
  const send = vi.fn<(text: string, key: string) => Promise<string>>().mockRejectedValue(new Error("timed out"));
  const { box, rerender } = show(send);
  fireEvent.change(box, { target: { value: "One" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await screen.findByRole("alert");
  fireEvent.change(box, { target: { value: "One, edited" } });
  expect(screen.queryByRole("alert")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await screen.findByRole("alert");
  rerender("session-b");
  expect(screen.queryByRole("alert")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await screen.findByRole("alert");
  const keys = send.mock.calls.map(([, key]) => key);
  expect(new Set(keys).size).toBe(3);
  expect(send.mock.calls.map(([text]) => text)).toEqual(["One", "One, edited", "One, edited"]);
});

test("Enter sends and Shift+Enter breaks the line; a composer turned off sends nothing", async () => {
  const send = vi.fn<(text: string, key: string) => Promise<string>>().mockResolvedValue("Queued");
  const { box, rerender } = show(send);
  fireEvent.change(box, { target: { value: "Line one" } });
  fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
  expect(send).not.toHaveBeenCalled();
  fireEvent.keyDown(box, { key: "Enter" });
  await screen.findByRole("status");
  expect(send).toHaveBeenCalledWith("Line one", expect.stringMatching(UUID));
  rerender("session-a", false);
  expect(box).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "Send message" })).toHaveProperty("disabled", true);
});

test("a draft its check refuses says why, sends nothing and offers no retry; editing it clears that", async () => {
  const send = vi.fn<(text: string, key: string) => Promise<string>>().mockResolvedValue("Queued");
  const { box } = show(send, (text) => (text.startsWith("@") ? "" : "Start with a target: @agent message"));
  fireEvent.change(box, { target: { value: "fix it" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(screen.getByRole("alert").textContent).toBe("Start with a target: @agent message");
  expect(screen.queryByRole("button", { name: "Retry message" })).toBeNull();
  expect(send).not.toHaveBeenCalled();
  expect(box).toHaveProperty("value", "fix it");
  fireEvent.change(box, { target: { value: "@codex fix it" } });
  expect(screen.queryByRole("alert")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await screen.findByRole("status");
  expect(send).toHaveBeenCalledWith("@codex fix it", expect.any(String));
});
