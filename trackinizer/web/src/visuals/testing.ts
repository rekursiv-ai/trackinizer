// Test helpers for the canvas's visuals; only tests import this file.
import { vi } from "vitest";
import type { WorkspaceActions } from "./workspaceActions";

/**
 * A canvas's operations for a view under test: idle, `operate` a mock, and no
 * visual types offered; `fields` override any of them.
 */
export function canvasActions(fields: Partial<WorkspaceActions> = {}): WorkspaceActions {
  return {
    busy: false,
    writeError: null,
    operate: vi.fn(),
    visualTypes: new Set(),
    ...fields,
  };
}
