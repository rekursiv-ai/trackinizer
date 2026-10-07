import { createContext, useContext } from "react";
import type { WorkspaceOperation } from "../api/workspaces";

type WorkspaceActions = {
  readonly busy: boolean;
  readonly writeError: string | null;
  /** Apply one canvas operation. Links never need it: they only set the address. */
  readonly operate: (operation: WorkspaceOperation) => void;
};

const WorkspaceActionsContext = createContext<WorkspaceActions | null>(null);

/** Provide the canvas operations that record views can invoke. */
export const WorkspaceActionsProvider = WorkspaceActionsContext;

/** Get the canvas operations shared by every visual in the workspace. */
export function useWorkspaceActions() {
  return useContext(WorkspaceActionsContext);
}
