import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import { chatRefusalMessage } from "../api/chatRefusals";
import { reattachChat } from "../api/chats";
import { ApiError } from "../api/client";
import { newUuid } from "../api/idempotency";

/** What the person is told when a reattach fails with no reason from the server. */
const FAILED = "Could not reattach this chat. Press the button again to retry.";

/**
 * Reattach a chat's run: ask the server to reopen it headless, so Chat can answer it again,
 * after someone took it over in a terminal. `reattach(session)` is true once the server did;
 * otherwise `report` is told why. A press after a lost answer or a server fault goes under the
 * same idempotency key, so the run opens once; one the server refused is a new request next time.
 * `busy` is the session being reattached now.
 */
export function useReattach(report: (message: string) => void): { readonly reattach: (session: string) => Promise<boolean>; readonly busy: string | null } {
  const queryClient = useQueryClient();
  const keys = useRef(new Map<string, string>());
  const [busy, setBusy] = useState<string | null>(null);
  const reattach = useCallback(async (session: string): Promise<boolean> => {
    const key = keys.current.get(session) ?? newUuid();
    keys.current.set(session, key);
    setBusy(session);
    try {
      await reattachChat(session, key);
      keys.current.delete(session);
      // The chat's partner, the canvas list and the console's agents all say whether it is taken over.
      for (const queryKey of [["canvas"], ["canvases"], ["console"], ["chat", "head"]]) void queryClient.invalidateQueries({ queryKey });
      return true;
    } catch (error) {
      // A refusal would be refused again under its key; only a send that got no answer is repeated.
      if (error instanceof ApiError && error.status > 0 && error.status < 500) keys.current.delete(session);
      report(chatRefusalMessage(error, FAILED));
      return false;
    } finally {
      setBusy(null);
    }
  }, [queryClient, report]);
  return { reattach, busy };
}
