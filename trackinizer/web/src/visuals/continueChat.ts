import { useCallback, useContext } from "react";
import { setChatHidden } from "./chatColumn";
import { ChatFeedContext } from "./chatFeed";

/**
 * How a view opens a science chat's session in Chat, or null where it cannot:
 * outside the shell's canvas, which has the feed and the Chat column beside it. It
 * leaves the session in the shell's `ChatFeed`, which Chat reads whenever it is
 * mounted, and shows Chat if it is hidden. Chat then checks the session is a
 * science chat.
 */
export function useContinueInChat(): ((sessionId: string) => void) | null {
  const feed = useContext(ChatFeedContext);
  const continueIn = useCallback((sessionId: string) => {
    feed?.continueIn(sessionId);
    setChatHidden(false);
  }, [feed]);
  return feed ? continueIn : null;
}
