import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useContext, useEffect, useRef, useState } from "react";
import { openWorkspaceEvents } from "../api/workspaces";
import { recordFrame, recordNavigation } from "../debug/timings";
import { LiveContext } from "../live";
import { receivePreferences } from "../preferences";
import { parseHash } from "../router/route";
import { setChatHidden } from "../visuals/chatColumn";
import { ChatFeed, ChatFeedContext } from "../visuals/chatFeed";
import { acceptWorkspace } from "../stations/cache";
import { useStation } from "../stations/useStation";
import { MetaContext } from "./boot";
import { HighlightContext, HighlightStore } from "./highlights";

/**
 * The canvas's one stream, owned above the routes so that it outlives the canvas
 * (Settings and Admin are outside it) and is the tab's only one: while the canvas
 * is `enabled`, the live layer takes its inquiry ids from it (`changed` frames)
 * and `/api/web/subscribe` stays shut.
 *
 * It also applies what the stream says about the canvas: the workspace (a
 * revision never replaces a newer one), an agent's navigation, and what it points at
 * (the tab's highlights, `highlights.ts`), and when it hides or shows Chat's
 * column. Chat's lines have no frames of their own: a conversation is a session, and a line added to it is the session's `changed` id, which
 * the live layer hands to the Chat that follows it. Gap recovery on an open is the
 * live layer's own.
 */
export function CanvasStream({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  const queryClient = useQueryClient();
  const hub = useContext(LiveContext);
  const kinds = useContext(MetaContext)?.kinds;
  const kindsRef = useRef(kinds ?? []);
  kindsRef.current = kinds ?? [];
  const [feed] = useState(() => new ChatFeed());
  const [highlights] = useState(() => new HighlightStore());
  const created = useStation({ enabled });
  const workspaceId = enabled ? created.data?.id : undefined;
  useEffect(() => {
    if (!workspaceId) return;
    const close = openWorkspaceEvents(workspaceId, {
      workspace: (state, t) => {
        recordFrame(state.revision, t);
        acceptWorkspace(queryClient, state);
      },
      navigate: (route, t) => {
        if (!route.startsWith("#/") || parseHash(route, kindsRef.current).name === "notFound") return;
        recordNavigation(route, t);
        if (window.location.hash !== route) window.location.hash = route;
      },
      highlight: (ids) => highlights.set(ids),
      chat: (hidden) => setChatHidden(hidden),
      preferences: (preferences) => receivePreferences(preferences),
      changed: (id) => hub?.change(id),
      open: () => hub?.open(),
      drop: () => hub?.drop(),
      refuse: () => hub?.refuse(),
    });
    // What the agent pointed at belongs to the stream that said so.
    return () => {
      close();
      highlights.set([]);
    };
  }, [workspaceId, queryClient, hub, highlights, feed]);
  return (
    <ChatFeedContext value={enabled ? feed : null}>
      <HighlightContext value={highlights}>{children}</HighlightContext>
    </ChatFeedContext>
  );
}
