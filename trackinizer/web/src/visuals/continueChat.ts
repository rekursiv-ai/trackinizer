import { useCallback, useContext } from "react";
import { ChatFeedContext } from "./chatFeed";
import { useWorkspaceActions } from "./workspaceActions";

/**
 * How a view opens a science chat's session in Chat, or null where it cannot: outside the
 * canvas, or on a deployment whose canvas offers no Chat. It leaves the session in the
 * shell's `ChatFeed`, which Chat reads whenever it is mounted, and shows Chat (or focuses
 * it, where it is already shown). Chat then checks the session is a science chat.
 */
export function useContinueInChat(): ((sessionId: string) => void) | null {
  const feed = useContext(ChatFeedContext);
  const actions = useWorkspaceActions();
  const operate = actions?.operate;
  const offered = !!feed && !!actions?.visualTypes.has("trax.chat");
  const continueIn = useCallback((sessionId: string) => {
    feed?.continueIn(sessionId);
    operate?.({ kind: "show", visual_type: "trax.chat" });
  }, [feed, operate]);
  return offered ? continueIn : null;
}
