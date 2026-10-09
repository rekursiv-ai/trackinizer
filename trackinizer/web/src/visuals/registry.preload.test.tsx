import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { PROFILE } from "../detail/testing";

afterEach(cleanup);

const chat = { id: "chat", type: "trax.chat", version: 1, placement: "side" as const, record_id: null, params: {} };

/** The registry as a page that has not loaded any renderer sees it. */
async function fresh() {
  vi.resetModules();
  const registry = await import("./registry");
  const boot = await import("../app/boot");
  const show = () => render(
    <QueryClientProvider client={new QueryClient()}>
      <boot.ProfileContext value={PROFILE}>
        <registry.VisualPane instance={chat} workspace={null} focused={false} onWorkspaceChanged={vi.fn()} />
      </boot.ProfileContext>
    </QueryClientProvider>,
  );
  return { ...registry, show };
}

test("Chat's first render suspends on its chunk, showing a fallback that React holds for 300 ms", async () => {
  const { show } = await fresh();
  show();
  expect(screen.getByText("Loading visual…")).toBeTruthy();
  await act(() => vi.dynamicImportSettled());
});

test("with the first renderers preloaded, as the shell does with the canvas, Chat's first render shows no fallback", async () => {
  const { show, preloadFirstRenderers } = await fresh();
  await preloadFirstRenderers();
  show();
  expect(screen.queryByText("Loading visual…")).toBeNull();
  expect(screen.getByRole("region", { name: "Chat" })).toBeTruthy();
});
