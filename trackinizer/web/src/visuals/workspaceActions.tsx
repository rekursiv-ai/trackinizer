import { createContext, useContext } from "react";

type WorkspaceActions = {
  readonly busy: boolean;
  readonly writeError: string | null;
  readonly revealRecord: (recordId: string) => Promise<boolean>;
  readonly connectSession: (sessionId: string | null) => void;
};

const WorkspaceActionsContext = createContext<WorkspaceActions | null>(null);

/** Provide the canvas operations that record views can invoke. */
export const WorkspaceActionsProvider = WorkspaceActionsContext;

/** Get the canvas operations shared by every visual in the workspace. */
export function useWorkspaceActions() {
  return useContext(WorkspaceActionsContext);
}
