import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { VisualPane } from "./registry";

// Each test waits for the failed import inside act: outside it, React keeps a
// suspended tile's fallback on screen for 300 ms before the error replaces it.
vi.mock("./ChatConnect", () => { throw new Error("Chat chunk unavailable"); });
vi.mock("./Timeline", () => { throw new Error("Timeline chunk unavailable"); });

afterEach(() => { cleanup(); });

test("a failed lazy Chat chunk stays inside its visual tile", async () => {
  const shared = { workspace: null, focused: false, onWorkspaceChanged: vi.fn() };
  render(<>
    <VisualPane {...shared} instance={{ id: "browse", type: "trax.browse", version: 1,
      placement: "main", record_id: null, params: {} }}><button>Browse records</button></VisualPane>
    <VisualPane {...shared} instance={{ id: "chat", type: "trax.chat", version: 1,
      placement: "side", record_id: null, params: {} }} />
  </>);
  await act(() => vi.dynamicImportSettled());
  expect(screen.getByRole("alert")).toHaveProperty("textContent", "Could not load trax.chat. Reload to try again.");
  expect(screen.getByRole("button", { name: "Browse records" })).toBeTruthy();
});

test("timeline is a separate lazy renderer whose import failure stays in its tile", async () => {
  const shared = { workspace: null, focused: false, onWorkspaceChanged: vi.fn() };
  render(<>
    <VisualPane {...shared} instance={{ id: "browse", type: "trax.browse", version: 1,
      placement: "main", record_id: null, params: {} }}><button>Browse records</button></VisualPane>
    <VisualPane {...shared} instance={{ id: "timeline", type: "trax.timeline", version: 1,
      placement: "side", record_id: "record-id", params: {} }} />
  </>);
  await act(() => vi.dynamicImportSettled());
  expect(screen.getByRole("alert")).toHaveProperty("textContent", "Could not load trax.timeline. Reload to try again.");
  expect(screen.getByRole("button", { name: "Browse records" })).toBeTruthy();
});
