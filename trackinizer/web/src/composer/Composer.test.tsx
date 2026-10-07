import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

test("an editable box stays open to typing while a draft sends, and what is typed meanwhile stays", async () => {
  let finish: (receipt: string) => void = () => {};
  const send = vi.fn(() => new Promise<string>((resolve) => { finish = resolve; }));
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const view = (editable: boolean) => <QueryClientProvider client={client}>
    <Composer send={send} target="t" enabled placeholder="Write" failure={() => "failed"} editable={editable} />
  </QueryClientProvider>;
  const { rerender } = render(view(false));
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  fireEvent.change(box, { target: { value: "first" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(box.disabled).toBe(true));
  finish("Sent");
  await screen.findByText("Sent");
  rerender(view(true));
  fireEvent.change(box, { target: { value: "second" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
  expect(box.disabled).toBe(false);
  fireEvent.change(box, { target: { value: "third, typed meanwhile" } });
  await act(async () => finish("Sent"));
  expect(box.value).toBe("third, typed meanwhile");
});

test("Retry shows only for a failure that a resend can mend", async () => {
  const send = vi.fn<(text: string, key: string) => Promise<string>>().mockRejectedValue(new Error("refused"));
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const view = (retryable: (error: Error) => boolean) => <QueryClientProvider client={client}>
    <Composer send={send} target="t" enabled placeholder="Write" failure={(error) => `Not sent: ${error.message}`} retryable={retryable} />
  </QueryClientProvider>;
  const { rerender } = render(view(() => false));
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "hi" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect((await screen.findByRole("alert")).textContent).toBe("Not sent: refused");
  expect(screen.queryByRole("button", { name: "Retry message" })).toBeNull();
  rerender(view(() => true));
  expect(screen.getByRole("button", { name: "Retry message" })).toBeTruthy();
});

test("a send that resolves with nothing shows no receipt, and still empties the box", async () => {
  const send = vi.fn<(text: string, key: string) => Promise<string>>().mockResolvedValue("");
  const { box } = show(send);
  fireEvent.change(box, { target: { value: "hi" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(box).toHaveProperty("value", ""));
  expect(screen.queryByRole("status")).toBeNull();
});
