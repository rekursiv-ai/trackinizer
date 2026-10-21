import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import type { ChatHead } from "../api/chats";
import { stubClipboard } from "../debug/testing";
import { ToastProvider } from "../ui/toast";
import { ShareChat } from "./ChatSharing";

const SESSION = "5a0c6f9e-2b1d-4c3e-9f80-1d2e3f405162";

afterEach(cleanup);

/** A head its starter reads, with `audience` as given. */
function head(audience: string): ChatHead {
  return {
    conversation_id: "0f1e2d3c-4b5a-4968-8776-655443322110", session_id: SESSION, title: "a chat",
    account: "me@example.com", live: true, forks: 0, forked_from: null, forks_on_typing: false,
    audience, may_hide: true, shared_with: [], may_share: true, may_delete: true,
  };
}

/** Render Share for `chat` and open its panel. */
function openShare(chat: ChatHead): HTMLElement {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ToastProvider><ShareChat head={chat} /></ToastProvider>
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Share" }));
  return screen.getByRole("dialog", { name: "Share chat" });
}

test("Share's Copy link copies the app's link to the chat's session page, without this page's query or route", async () => {
  history.replaceState(null, "", "/app/?debug=1#/graph");
  const copied = stubClipboard();
  const panel = openShare(head("open"));
  // A click alone: user-event would put its own clipboard in place of the stub.
  fireEvent.click(panel.querySelector("button[aria-label='Copy link to this chat']")!);
  await waitFor(() => expect(copied).toEqual([`${location.origin}/app/#/lookup/${SESSION}`]));
  expect(await screen.findByText("Copied the link to this chat")).toBeTruthy();
});

test("Share says who the link opens for, by the chat's audience", () => {
  expect(openShare(head("open")).textContent).toContain("The link opens this chat for you, the people you share it with, and whoever this server lets in.");
  cleanup();
  expect(openShare(head("private")).textContent).toContain("The link opens this chat for you and the people you share it with.");
  cleanup();
  expect(openShare(head("staff")).textContent).toContain("The link opens this chat for you and the people you share it with who hold staff.");
});
