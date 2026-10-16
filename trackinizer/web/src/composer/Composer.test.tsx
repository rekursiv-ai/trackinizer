import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useCompletion } from "./CompletionField";
import { Composer, type FieldProps } from "./Composer";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

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

test("history recalls messages and restores the unfinished draft without changing its entries", () => {
  const client = new QueryClient();
  const entries = ["First question", "Second question"];
  render(<QueryClientProvider client={client}>
    <Composer send={async () => ""} target="chat" enabled placeholder="Write" failure={() => "failed"}
      history={{ key: "station-a", entries }} />
  </QueryClientProvider>);
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  fireEvent.change(box, { target: { value: "Unfinished" } });
  box.setSelectionRange(0, 0);
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe("Second question");
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe("First question");
  box.setSelectionRange(box.value.length, box.value.length);
  fireEvent.keyDown(box, { key: "ArrowDown" });
  expect(box.value).toBe("Second question");
  fireEvent.keyDown(box, { key: "ArrowDown" });
  expect(box.value).toBe("Unfinished");
  expect(entries).toEqual(["First question", "Second question"]);
});

test("editing a recalled message keeps the way back to the unfinished draft", () => {
  const client = new QueryClient();
  render(<QueryClientProvider client={client}>
    <Composer send={async () => ""} target="chat" enabled placeholder="Write" failure={() => "failed"}
      history={{ key: "station-a", entries: ["First question", "Second question"] }} />
  </QueryClientProvider>);
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  fireEvent.change(box, { target: { value: "Unfinished" } });
  box.setSelectionRange(0, 0);
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe("Second question");
  fireEvent.change(box, { target: { value: "Second question!" } });
  box.setSelectionRange(box.value.length, box.value.length);
  fireEvent.keyDown(box, { key: "ArrowDown" });
  expect(box.value).toBe("Unfinished");
});

test("a send ends recall: Down after it does not bring back the old unfinished draft", async () => {
  const client = new QueryClient();
  render(<QueryClientProvider client={client}>
    <Composer send={async () => ""} target="chat" enabled placeholder="Write" failure={() => "failed"}
      history={{ key: "station-a", entries: ["Old"] }} />
  </QueryClientProvider>);
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  fireEvent.change(box, { target: { value: "Unfinished" } });
  box.setSelectionRange(0, 0);
  fireEvent.keyDown(box, { key: "ArrowUp" });
  fireEvent.change(box, { target: { value: "Old again" } });
  fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() => expect(box.value).toBe(""));
  expect(fireEvent.keyDown(box, { key: "ArrowDown" })).toBe(true);
  expect(box.value).toBe("");
});

/** Chat's `@` grammar (RecordField's `recordAt`): an `@` word that opens the text or follows whitespace. */
function atWord(value: string, caret: number) {
  const start = value.slice(0, caret).search(/(?<=^|\s)@\S*$/);
  if (start < 0) return null;
  const space = value.slice(start).search(/\s/);
  return { start, end: space < 0 ? value.length : start + space, query: value.slice(start + 1, caret) };
}

test.each([
  ["a query with no matches, no hint", "ask @zzz", undefined],
  ["a bare @ showing only its hint", "ask @", "Type to search records…"],
])("Down at the end of a recalled message in %s goes on to the unfinished draft", (_, entry, hint) => {
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  const client = new QueryClient();
  function AtField({ field, edit }: { field: FieldProps; edit: (text: string) => void }) {
    const { target, render: draw } = useCompletion({ field, edit, find: atWord });
    return draw([], "Records", target?.query ? undefined : hint);
  }
  render(<QueryClientProvider client={client}>
    <Composer send={async () => ""} target="chat" enabled placeholder="Write" failure={() => "failed"}
      field={(props, edit) => <AtField field={props} edit={edit} />}
      history={{ key: "station-a", entries: [entry] }} />
  </QueryClientProvider>);
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  box.focus();
  fireEvent.change(box, { target: { value: "Unfinished" } });
  box.setSelectionRange(0, 0);
  fireEvent.select(box);
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe(entry);
  box.setSelectionRange(entry.length, entry.length);
  fireEvent.select(box);
  if (hint) expect(screen.getByRole("status")).toHaveProperty("textContent", hint);
  else expect(screen.queryByRole("listbox")).toBeNull();
  fireEvent.keyDown(box, { key: "ArrowDown" });
  expect(box.value).toBe("Unfinished");
});

test("history leaves multiline movement, selections, modifiers and composing keys alone", () => {
  const client = new QueryClient();
  render(<QueryClientProvider client={client}>
    <Composer send={async () => ""} target="chat" enabled placeholder="Write" failure={() => "failed"}
      history={{ key: "station-a", entries: ["Old question"] }} />
  </QueryClientProvider>);
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  fireEvent.change(box, { target: { value: "First\nSecond" } });
  box.setSelectionRange(8, 8);
  expect(fireEvent.keyDown(box, { key: "ArrowUp" })).toBe(true);
  box.setSelectionRange(0, 3);
  expect(fireEvent.keyDown(box, { key: "ArrowUp" })).toBe(true);
  box.setSelectionRange(0, 0);
  for (const modifier of [{ shiftKey: true }, { altKey: true }, { ctrlKey: true }, { metaKey: true }, { isComposing: true }]) {
    expect(fireEvent.keyDown(box, { key: "ArrowUp", ...modifier })).toBe(true);
  }
  expect(box.value).toBe("First\nSecond");
});

test("history snapshots survive arriving entries and reset when their scope changes", () => {
  const client = new QueryClient();
  const view = (key: string, entries: readonly string[]) => <QueryClientProvider client={client}>
    <Composer send={async () => ""} target="chat" enabled placeholder="Write" failure={() => "failed"} history={{ key, entries }} />
  </QueryClientProvider>;
  const { rerender } = render(view("a", ["First", "Second"]));
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe("Second");
  rerender(view("a", ["First", "Second", "Third"]));
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe("First");
  rerender(view("b", ["Other station"]));
  box.setSelectionRange(0, 0);
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe("Other station");
});

test("leaving a history scope forgets its navigation position even when returning without typing", () => {
  const client = new QueryClient();
  const view = (key: string) => <QueryClientProvider client={client}>
    <Composer send={async () => ""} target={key} enabled placeholder="Write" failure={() => "failed"}
      history={{ key, entries: ["First", "Second"] }} />
  </QueryClientProvider>;
  const { rerender } = render(view("a"));
  const box = screen.getByRole("textbox") as HTMLTextAreaElement;
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe("Second");
  rerender(view("b"));
  rerender(view("a"));
  box.setSelectionRange(0, 0);
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe("Second");
});

test("a controlled draft clears its captured snapshot after a send, not the current station", async () => {
  let finish = (_text: string) => {};
  const send = vi.fn(() => new Promise<string>((resolve) => { finish = resolve; }));
  const clearA = vi.fn();
  const clearB = vi.fn();
  const edit = vi.fn();
  const client = new QueryClient();
  const view = (target: string, text: string, clear: () => void) => <QueryClientProvider client={client}>
    <Composer send={send} target={target} enabled editable placeholder="Write" failure={() => "failed"}
      draft={{ text, edit, clear }} />
  </QueryClientProvider>;
  const { rerender } = render(view("a", "Send A", clearA));
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  await waitFor(() => expect(send).toHaveBeenCalledOnce());
  rerender(view("b", "Keep B", clearB));
  await act(async () => finish(""));
  expect(clearA).toHaveBeenCalledOnce();
  expect(clearB).not.toHaveBeenCalled();
  expect(screen.getByRole("textbox")).toHaveProperty("value", "Keep B");
});

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

test("Alt+Enter sends nothing: it is the key that takes Chat full screen", async () => {
  const send = vi.fn<(text: string, key: string) => Promise<string>>().mockResolvedValue("Queued");
  const { box } = show(send);
  fireEvent.change(box, { target: { value: "Line one" } });
  fireEvent.keyDown(box, { key: "Enter", altKey: true });
  // A send starts on the next tick.
  await act(async () => {});
  expect(send).not.toHaveBeenCalled();
  expect(box).toHaveProperty("value", "Line one");
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

test("validation refusals do not return after editing away and back or changing targets", () => {
  const { box, rerender } = show(vi.fn(async () => ""), () => "Refused");
  fireEvent.change(box, { target: { value: "A" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(screen.getByRole("alert").textContent).toBe("Refused");
  fireEvent.change(box, { target: { value: "B" } });
  fireEvent.change(box, { target: { value: "A" } });
  expect(screen.queryByRole("alert")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  rerender("session-b");
  expect(screen.queryByRole("alert")).toBeNull();
  rerender("session-a");
  expect(screen.queryByRole("alert")).toBeNull();
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

test("editing a pending draft cannot unlock a second delivery or erase intervening edits", async () => {
  let finish = (_text: string) => {};
  const send = vi.fn(() => new Promise<string>((resolve) => { finish = resolve; }));
  const client = new QueryClient();
  render(<QueryClientProvider client={client}>
    <Composer send={send} target="a" enabled editable placeholder="Write" failure={() => "failed"} />
  </QueryClientProvider>);
  const box = screen.getByRole("textbox");
  fireEvent.change(box, { target: { value: "A" } });
  fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() => expect(send).toHaveBeenCalledOnce());
  fireEvent.change(box, { target: { value: "B" } });
  expect(screen.getByRole("button", { name: "Send message" })).toHaveProperty("disabled", true);
  fireEvent.keyDown(box, { key: "Enter" });
  await act(async () => {});
  expect(send).toHaveBeenCalledOnce();
  fireEvent.change(box, { target: { value: "A" } });
  await act(async () => finish("Sent"));
  expect(box).toHaveProperty("value", "A");
});

test("an external delivery blocks sending but preserves editable drafts", async () => {
  const send = vi.fn(async () => "");
  const client = new QueryClient();
  const view = (pending: boolean) => <QueryClientProvider client={client}>
    <Composer send={send} target="a" enabled editable pending={pending} placeholder="Write" failure={() => "failed"} />
  </QueryClientProvider>;
  const { rerender } = render(view(true));
  const box = screen.getByRole("textbox");
  expect(box).toHaveProperty("disabled", false);
  fireEvent.change(box, { target: { value: "Next question" } });
  expect(screen.getByRole("button", { name: "Send message" })).toHaveProperty("disabled", true);
  fireEvent.keyDown(box, { key: "Enter" });
  await act(async () => {});
  expect(send).not.toHaveBeenCalled();
  rerender(view(false));
  fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() => expect(send).toHaveBeenCalledOnce());
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

test("a draft sent as the box moves to another target still leaves the box, and shows its receipt under neither", async () => {
  let deliver = (_receipt: string) => {};
  const send = vi.fn((_text: string, _key: string) => new Promise<string>((resolve) => { deliver = resolve; }));
  const { box, rerender } = show(send);
  fireEvent.change(box, { target: { value: "start a fork" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(send).toHaveBeenCalledOnce());
  rerender("session-b");
  await act(async () => deliver("Sent."));
  expect(box).toHaveProperty("value", "");
  expect(screen.queryByRole("status")).toBeNull();
  rerender("session-a");
  expect(screen.queryByRole("status")).toBeNull();
});
