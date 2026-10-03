import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, createEvent, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { getDetail } from "../api/detail";
import { getWorkspaceConnectionStatus, listConnectableSessions, sendWorkspaceMessage, type WorkspaceState } from "../api/workspaces";
import { listSessionParts, readSessionRecords } from "../api/sessions";
import { LiveContext } from "../live";
import { LiveHub } from "../live/hub";
import { ChatConnect } from "./ChatConnect";
import { WorkspaceActionsProvider } from "./workspaceActions";

vi.mock("../api/detail", () => ({
  getDetail: vi.fn().mockResolvedValue({
    self: { id: "record", kind: "Issue", seq: 42, title: "A useful issue" },
    edges: {}, backlinks: {}, changes: [],
  }),
}));

vi.mock("../api/workspaces", () => ({
  getWorkspaceConnectionStatus: vi.fn(),
  listConnectableSessions: vi.fn(),
  sendWorkspaceMessage: vi.fn(),
}));

vi.mock("../api/sessions", () => ({
  listSessionParts: vi.fn().mockResolvedValue([]),
  readSessionRecords: vi.fn(),
  readRecentSessionTurns: vi.fn().mockResolvedValue([]),
}));

const SESSION_ID = "2de97e19-2624-4e89-804e-f19e7248eec3";
const WORKSPACE_ID = "c5286865-67b6-4bd8-ab51-e06e10c326c5";
const workspace: WorkspaceState = {
  id: WORKSPACE_ID,
  revision: 3,
  connected_session_id: null,
  focused_instance: null,
  visuals: [{ id: "889ffcb2-cf44-43e7-9806-eb08428c6203", type: "trax.chat", version: 1,
    placement: "main", record_id: null, params: {} }],
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.mocked(listConnectableSessions).mockResolvedValue([]);
  vi.mocked(listSessionParts).mockResolvedValue([]);
  vi.mocked(readSessionRecords).mockReset();
  vi.useRealTimers();
  history.replaceState(null, "", "#/");
});

/**
 * The Message box, once the paired session's status has loaded and the box takes
 * input, as it must be before a person can type into it.
 */
async function liveMessageBox(): Promise<HTMLElement> {
  const message = await screen.findByRole("textbox", { name: "Message" });
  await waitFor(() => expect(message).toHaveProperty("disabled", false));
  return message;
}

function showChat(state: WorkspaceState | null, connectSession = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <WorkspaceActionsProvider value={{ busy: false, writeError: null,
      revealRecord: vi.fn(async () => true), connectSession }}>
      <ChatConnect instance={state?.visuals[0] ?? workspace.visuals[0]!} focused={false} workspace={state}
        onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);
  return connectSession;
}

test("a browser selects a live session from Chat", async () => {
  vi.mocked(listConnectableSessions).mockResolvedValue([
    { id: SESSION_ID, title: "codex session", actor: "researcher", cli: "codex" },
  ]);
  const connectSession = showChat(workspace);

  fireEvent.click(screen.getByRole("button", { name: "Connect session" }));
  fireEvent.click(await screen.findByRole("button", { name: "Connect codex session" }));
  expect(connectSession).toHaveBeenCalledWith(SESSION_ID);
});

test("a paired session omitted from the available list is not called ended", async () => {
  vi.mocked(listConnectableSessions).mockResolvedValue([]);
  vi.mocked(getWorkspaceConnectionStatus).mockResolvedValue({ status: "live", session_id: SESSION_ID, actor: "researcher", cli: "codex" });
  showChat({ ...workspace, connected_session_id: SESSION_ID });
  expect(await screen.findByText("Connected to researcher · codex")).toBeTruthy();
  expect(screen.queryByText(/Session ended/)).toBeNull();
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("disabled", false);
});

test("Chat identifies the record selected as its context", async () => {
  showChat({
    ...workspace,
    visuals: [{ ...workspace.visuals[0]!, record_id: "record" }],
  });

  const context = await screen.findByRole("link", { name: "Issue#42 A useful issue" });
  expect(context.getAttribute("href")).toBe("#/lookup/record");
});

test("a failed session lookup preserves its known paired state and offers retry", async () => {
  vi.mocked(getWorkspaceConnectionStatus).mockRejectedValue(new Error("network unavailable"));
  showChat({ ...workspace, connected_session_id: SESSION_ID });

  expect(await screen.findByText(`Paired session status unavailable · ${SESSION_ID}`)).toBeTruthy();
  expect(screen.getByRole("alert").textContent).toContain("Could not verify the paired session");
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "Change session" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Disconnect" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Retry session lookup" }));
  await waitFor(() => expect(getWorkspaceConnectionStatus).toHaveBeenCalledTimes(2));
});

test("session picker shows and retries a list failure while already paired", async () => {
  vi.mocked(getWorkspaceConnectionStatus).mockResolvedValue({ status: "live", session_id: SESSION_ID, actor: "researcher" });
  vi.mocked(listConnectableSessions).mockRejectedValue(new Error("network unavailable"));
  showChat({ ...workspace, connected_session_id: SESSION_ID });

  fireEvent.click(await screen.findByRole("button", { name: "Change session" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Could not list sessions.");
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(listConnectableSessions).toHaveBeenCalledTimes(2));
});

test("Chat queues a record-scoped message with a stable key for retry", async () => {
  vi.mocked(getWorkspaceConnectionStatus).mockResolvedValue({ status: "live", session_id: SESSION_ID, actor: "researcher" });
  vi.mocked(sendWorkspaceMessage).mockRejectedValueOnce(new Error("network unavailable"))
    .mockResolvedValueOnce({ session_id: SESSION_ID, queued: 2 });
  const chatWorkspace = {
    ...workspace,
    connected_session_id: SESSION_ID,
    visuals: [{ ...workspace.visuals[0]!, record_id: "record" }],
  };
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <WorkspaceActionsProvider value={{ busy: false, writeError: null,
      revealRecord: vi.fn(async () => true), connectSession: vi.fn() }}>
      <ChatConnect instance={chatWorkspace.visuals[0]!} focused={false} workspace={chatWorkspace}
        onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  const message = await screen.findByRole("textbox", { name: "Message" });
  await screen.findByText("Connected to researcher · agent");
  fireEvent.change(message, { target: { value: "Why is this issue open?" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Could not queue this message");
  fireEvent.click(screen.getByRole("button", { name: "Retry message" }));

  await waitFor(() => expect(sendWorkspaceMessage).toHaveBeenCalledTimes(2));
  const [[workspaceId, text, instanceId, expectedRecordId, key],
    [retryWorkspaceId, retryText, retryInstanceId, retryExpectedRecordId, retryKey]] =
    vi.mocked(sendWorkspaceMessage).mock.calls;
  expect([workspaceId, text, instanceId]).toEqual([WORKSPACE_ID, "Why is this issue open?", chatWorkspace.visuals[0]!.id]);
  expect(expectedRecordId).toBe("record");
  expect([retryWorkspaceId, retryText, retryInstanceId, retryExpectedRecordId, retryKey])
    .toEqual([workspaceId, text, instanceId, expectedRecordId, key]);
  expect(await screen.findByText("Queued for researcher")).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("value", "");
  const transcript = screen.getByRole("region", { name: "Recent captured turns" });
  const composer = screen.getByRole("textbox", { name: "Message" }).closest("form");
  expect(transcript.compareDocumentPosition(composer!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("Chat labels and sends an exact shared Artifact", async () => {
  const reportId = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
  vi.mocked(getWorkspaceConnectionStatus).mockResolvedValue({ status: "live", session_id: SESSION_ID, actor: "researcher" });
  vi.mocked(sendWorkspaceMessage).mockResolvedValue({ session_id: SESSION_ID, queued: 1 });
  const state = { ...workspace, connected_session_id: SESSION_ID,
    visuals: [{ ...workspace.visuals[0]!, record_id: reportId }] };
  showChat(state);

  expect(screen.getByRole("link", { name: reportId }).getAttribute("href"))
    .toBe(`#/lookup/${reportId}`);
  const message = await liveMessageBox();
  fireEvent.change(message, { target: { value: "Explain the evidence" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(sendWorkspaceMessage).toHaveBeenCalledWith(
    WORKSPACE_ID, "Explain the evidence", state.visuals[0]!.id, reportId, expect.any(String),
  ));
});

test("Enter sends Chat messages while Shift+Enter and IME Enter preserve the draft", async () => {
  vi.mocked(getWorkspaceConnectionStatus).mockResolvedValue({ status: "live", session_id: SESSION_ID });
  vi.mocked(sendWorkspaceMessage).mockResolvedValue({ session_id: SESSION_ID, queued: 1 });
  const user = userEvent.setup();
  showChat({ ...workspace, connected_session_id: SESSION_ID });

  const message = await liveMessageBox();
  await user.type(message, "First line");
  await user.keyboard("{Shift>}{Enter}{/Shift}");
  await user.type(message, "Second line");
  expect(message).toHaveProperty("value", "First line\nSecond line");

  fireEvent.keyDown(message, { key: "Enter", isComposing: true });
  expect(sendWorkspaceMessage).not.toHaveBeenCalled();
  expect(message).toHaveProperty("value", "First line\nSecond line");

  await user.keyboard("{Enter}");
  await waitFor(() => expect(sendWorkspaceMessage).toHaveBeenCalledTimes(1));
  expect(vi.mocked(sendWorkspaceMessage).mock.calls[0]?.[1]).toBe("First line\nSecond line");
});

test("Chat sends no instance id for the synthetic empty-canvas visual", async () => {
  vi.mocked(getWorkspaceConnectionStatus).mockResolvedValue({ status: "live", session_id: SESSION_ID, actor: "researcher" });
  vi.mocked(sendWorkspaceMessage).mockResolvedValue({ session_id: SESSION_ID, queued: 1 });
  const emptyWorkspace = { ...workspace, connected_session_id: SESSION_ID, visuals: [] };
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <WorkspaceActionsProvider value={{ busy: false, writeError: null,
      revealRecord: vi.fn(async () => true), connectSession: vi.fn() }}>
      <ChatConnect instance={{ ...workspace.visuals[0]!, id: "chat-disconnected" }} focused={false} workspace={emptyWorkspace}
        onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  const message = await liveMessageBox();
  fireEvent.change(message, { target: { value: "Hello" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(sendWorkspaceMessage).toHaveBeenCalledTimes(1));
  expect(vi.mocked(sendWorkspaceMessage).mock.calls[0]?.[2]).toBeNull();
  expect(vi.mocked(sendWorkspaceMessage).mock.calls[0]?.[3]).toBeNull();
});

test("editing a failed draft clears its old retry error", async () => {
  vi.mocked(getWorkspaceConnectionStatus).mockResolvedValue({ status: "live", session_id: SESSION_ID });
  vi.mocked(sendWorkspaceMessage).mockRejectedValue(new Error("network unavailable"));
  const state = { ...workspace, connected_session_id: SESSION_ID };
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <WorkspaceActionsProvider value={{ busy: false, writeError: null,
      revealRecord: vi.fn(async () => true), connectSession: vi.fn() }}>
      <ChatConnect instance={state.visuals[0]!} focused={false} workspace={state} onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  const message = await liveMessageBox();
  fireEvent.change(message, { target: { value: "First draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Could not queue this message");
  fireEvent.change(message, { target: { value: "Revised draft" } });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByRole("button", { name: "Retry message" })).toBeNull();
});

test("re-pairing clears the failed attempt key before sending the draft to a new session", async () => {
  const nextSessionId = "afd1b8d4-4fb0-46ca-b4f9-1b52e8a039f0";
  const finalSessionId = "dd8b38c4-c776-46d4-9a5b-38bd32c88876";
  vi.mocked(getWorkspaceConnectionStatus)
    .mockResolvedValueOnce({ status: "live", session_id: SESSION_ID, actor: "first agent" })
    .mockResolvedValueOnce({ status: "live", session_id: nextSessionId, actor: "second agent" })
    .mockResolvedValueOnce({ status: "live", session_id: finalSessionId, actor: "final agent" });
  vi.mocked(sendWorkspaceMessage).mockRejectedValueOnce(new Error("network unavailable"))
    .mockResolvedValueOnce({ session_id: nextSessionId, queued: 1 });
  const initial = { ...workspace, connected_session_id: SESSION_ID };
  const actions = { busy: false, writeError: null,
    revealRecord: vi.fn(async () => true), connectSession: vi.fn() };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { rerender } = render(<QueryClientProvider client={client}>
    <WorkspaceActionsProvider value={actions}>
      <ChatConnect instance={initial.visuals[0]!} focused={false} workspace={initial} onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  const message = await liveMessageBox();
  fireEvent.change(message, { target: { value: "Keep this draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Could not queue this message");
  const firstKey = vi.mocked(sendWorkspaceMessage).mock.calls[0]?.[4];
  const pairedAgain = { ...initial, connected_session_id: nextSessionId };
  rerender(<QueryClientProvider client={client}>
    <WorkspaceActionsProvider value={actions}>
      <ChatConnect instance={pairedAgain.visuals[0]!} focused={false} workspace={pairedAgain} onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  expect(await screen.findByText("Connected to second agent · agent")).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByRole("button", { name: "Retry message" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(sendWorkspaceMessage).toHaveBeenCalledTimes(2));
  expect(vi.mocked(sendWorkspaceMessage).mock.calls[1]?.[4]).not.toBe(firstKey);
  expect(vi.mocked(sendWorkspaceMessage).mock.calls[1]?.[0]).toBe(WORKSPACE_ID);
  expect(await screen.findByText("Queued for second agent")).toBeTruthy();
  const pairedOnceMore = { ...initial, connected_session_id: finalSessionId };
  rerender(<QueryClientProvider client={client}>
    <WorkspaceActionsProvider value={actions}>
      <ChatConnect instance={pairedOnceMore.visuals[0]!} focused={false} workspace={pairedOnceMore} onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);
  expect(await screen.findByText("Connected to final agent · agent")).toBeTruthy();
  expect(screen.queryByText("Queued for second agent")).toBeNull();
});

test("changing the Chat record context gives a failed draft a fresh request key", async () => {
  vi.mocked(getWorkspaceConnectionStatus).mockResolvedValue({ status: "live", session_id: SESSION_ID, actor: "researcher" });
  vi.mocked(sendWorkspaceMessage).mockRejectedValueOnce(new Error("network unavailable"))
    .mockResolvedValueOnce({ session_id: SESSION_ID, queued: 1 });
  const visual = { ...workspace.visuals[0]!, id: "persisted-chat", type: "trax.chat", record_id: "record-a" };
  const initial = { ...workspace, connected_session_id: SESSION_ID, visuals: [visual] };
  const actions = { busy: false, writeError: null,
    revealRecord: vi.fn(async () => true), connectSession: vi.fn() };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { rerender } = render(<QueryClientProvider client={client}>
    <WorkspaceActionsProvider value={actions}>
      <ChatConnect instance={visual} focused={false} workspace={initial} onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  const message = await liveMessageBox();
  fireEvent.change(message, { target: { value: "Explain this record" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Could not queue this message");
  const firstKey = vi.mocked(sendWorkspaceMessage).mock.calls[0]?.[4];
  const changedVisual = { ...visual, record_id: "record-b" };
  const changed = { ...initial, visuals: [changedVisual] };
  rerender(<QueryClientProvider client={client}>
    <WorkspaceActionsProvider value={actions}>
      <ChatConnect instance={changedVisual} focused={false} workspace={changed} onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  expect(screen.queryByRole("alert")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(sendWorkspaceMessage).toHaveBeenCalledTimes(2));
  const [, , chatInstanceId, expectedRecordId, nextKey] = vi.mocked(sendWorkspaceMessage).mock.calls[1]!;
  expect(chatInstanceId).toBe("persisted-chat");
  expect(expectedRecordId).toBe("record-b");
  expect(nextKey).not.toBe(firstKey);
});

test("Chat keeps the composer disabled for an ended pairing and still shows its transcript", async () => {
  vi.mocked(getWorkspaceConnectionStatus).mockResolvedValue({ status: "ended", session_id: SESSION_ID });
  const state = { ...workspace, connected_session_id: SESSION_ID };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <WorkspaceActionsProvider value={{ busy: false, writeError: null,
      revealRecord: vi.fn(async () => true), connectSession: vi.fn() }}>
      <ChatConnect instance={state.visuals[0]!} focused={false} workspace={state} onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  expect(await screen.findByText(/session ended/i)).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("region", { name: "Recent captured turns" })).toBeTruthy();
  expect(sendWorkspaceMessage).not.toHaveBeenCalled();
});

test("Chat only renders a transcript when direct pairing status includes a known session", async () => {
  vi.mocked(getWorkspaceConnectionStatus).mockResolvedValue({ status: "unavailable", session_id: SESSION_ID });
  const state = { ...workspace, connected_session_id: SESSION_ID };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <WorkspaceActionsProvider value={{ busy: false, writeError: null,
      revealRecord: vi.fn(async () => true), connectSession: vi.fn() }}>
      <ChatConnect instance={state.visuals[0]!} focused={false} workspace={state} onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  expect(await screen.findByText(/session unavailable/i)).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Recent captured turns" })).toBeNull();
});

test("a failed coordinated write appears in Chat", () => {
  render(<QueryClientProvider client={new QueryClient()}>
    <WorkspaceActionsProvider value={{ busy: false, writeError: "Could not update the workspace.",
      revealRecord: vi.fn(async () => true), connectSession: vi.fn() }}>
      <ChatConnect instance={workspace.visuals[0]!} focused={false} workspace={workspace}
        onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  expect(screen.getByRole("alert").textContent).toBe("Could not update the workspace.");
});

test("a busy workspace disables its record link without revealing or navigating", async () => {
  history.replaceState(null, "", "#/activity");
  const revealRecord = vi.fn(async () => true);
  render(<QueryClientProvider client={new QueryClient()}>
    <WorkspaceActionsProvider value={{ busy: true, writeError: null,
      revealRecord, connectSession: vi.fn() }}>
      <ChatConnect instance={{ ...workspace.visuals[0]!, record_id: "record" }} focused={false}
        workspace={workspace} onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  const link = await screen.findByRole("link", { name: "Issue#42 A useful issue" });
  expect(link.getAttribute("aria-disabled")).toBe("true");
  expect(link.getAttribute("tabindex")).toBe("-1");
  fireEvent.click(link);

  expect(revealRecord).not.toHaveBeenCalled();
  expect(location.hash).toBe("#/activity");
});

test.each([
  ["Ctrl-click", { ctrlKey: true }],
  ["Command-click", { metaKey: true }],
  ["middle-click", { button: 1 }],
] as const)("Chat record link preserves %s", async (_label, options) => {
  const revealRecord = vi.fn(async () => true);
  render(<QueryClientProvider client={new QueryClient()}>
    <WorkspaceActionsProvider value={{ busy: false, writeError: null,
      revealRecord, connectSession: vi.fn() }}>
      <ChatConnect instance={{ ...workspace.visuals[0]!, record_id: "record" }} focused={false}
        workspace={workspace} onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  const link = await screen.findByRole("link", { name: "Issue#42 A useful issue" });
  const event = createEvent.click(link, options);
  fireEvent(link, event);
  expect(event.defaultPrevented).toBe(false);
  expect(revealRecord).not.toHaveBeenCalled();
});

test("Chat record context refreshes through the live detail query", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, advanceTimeDelta: 2 });
  const before = {
    self: { id: "record", kind: "Issue", seq: 42, title: "Before update", status: "open",
      created: "2026-01-01T00:00:00Z", modified: "2026-01-01T00:00:00Z" },
    edges: {}, backlinks: {}, changes: [],
  };
  const after = { ...before, self: { ...before.self, title: "After update" } };
  vi.mocked(getDetail).mockResolvedValueOnce(before).mockResolvedValueOnce(after);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const hub = new LiveHub(client);
  const chatWorkspace = {
    ...workspace,
    visuals: [{ ...workspace.visuals[0]!, record_id: "record" }],
  };
  render(<QueryClientProvider client={client}>
    <LiveContext value={hub}>
      <WorkspaceActionsProvider value={{ busy: false, writeError: null,
        revealRecord: vi.fn(async () => true), connectSession: vi.fn() }}>
        <ChatConnect instance={chatWorkspace.visuals[0]!} focused={false} workspace={chatWorkspace}
          onWorkspaceChanged={vi.fn()} />
      </WorkspaceActionsProvider>
    </LiveContext>
  </QueryClientProvider>);

  expect(await screen.findByRole("link", { name: "Issue#42 Before update" })).toBeTruthy();
  hub.change("record");
  // The hub hands over the stream's ids once a second.
  await act(() => vi.advanceTimersByTimeAsync(1_000));
  await waitFor(() => expect(getDetail).toHaveBeenCalledTimes(2));

  expect(await screen.findByRole("link", { name: "Issue#42 After update" })).toBeTruthy();
  expect(getDetail).toHaveBeenCalledTimes(2);
  hub.stop();
});
