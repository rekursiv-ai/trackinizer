import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useContext, useEffect, useRef, useState } from "react";
import { createDefaultWorkspace, openWorkspaceEvents, type WorkspaceState } from "../api/workspaces";
import { recordFrame, recordNavigation } from "../debug/timings";
import { LiveContext } from "../live";
import { parseHash } from "../router/route";
import { appendLines, chatKey } from "../visuals/chatCache";
import { ChatFeed, ChatFeedContext } from "../visuals/chatFeed";
import { MetaContext } from "./boot";
import { HighlightContext, HighlightStore } from "./highlights";

/**
 * The canvas's one stream, owned above the routes so that it outlives the canvas
 * (Settings and Admin are outside it) and is the tab's only one: while the canvas
 * is `enabled`, the live layer takes its inquiry ids from it (`changed` frames)
 * and `/api/web/subscribe` stays shut.
 *
 * It also applies what the stream says about the canvas: the workspace (a
 * revision never replaces a newer one), an agent's navigation, what it points at
 * (the tab's highlights, `highlights.ts`), and Chat's lines,
 * statuses and delivery. Gap recovery on an open is the live layer's own.
 */
export function CanvasStream({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  const queryClient = useQueryClient();
  const hub = useContext(LiveContext);
  const kinds = useContext(MetaContext)?.kinds;
  const kindsRef = useRef(kinds ?? []);
  kindsRef.current = kinds ?? [];
  const [feed] = useState(() => new ChatFeed());
  const [highlights] = useState(() => new HighlightStore());
  const created = useQuery({
    queryKey: ["workspace", "default"],
    queryFn: ({ signal }) => createDefaultWorkspace({ signal }),
    enabled,
    staleTime: Infinity,
  });
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
      message: (conversationId, message) => {
        appendLines(queryClient, conversationId, [message]);
        feed.messaged(conversationId);
      },
      status: (conversationId, text) => feed.setStatus(conversationId, text),
      delivered: (conversationId, seq) => feed.drained(conversationId, seq),
      deleted: (conversationId) => {
        // An open Chat leaves it by itself (the feed says it is gone); the rest is dropped now.
        queryClient.removeQueries({ queryKey: chatKey(conversationId), type: "inactive" });
        void queryClient.invalidateQueries({ queryKey: ["chats"] });
        feed.forget(conversationId, true);
      },
      changed: (id) => hub?.change(id),
      open: () => {
        hub?.open();
        feed.opened();
      },
      drop: () => hub?.drop(),
      refuse: () => hub?.refuse(),
    });
    // What the agent pointed at belongs to the stream that said so.
    return () => {
      close();
      highlights.set([]);
    };
  }, [workspaceId, queryClient, hub, feed, highlights]);
  return (
    <ChatFeedContext value={feed}>
      <HighlightContext value={highlights}>{children}</HighlightContext>
    </ChatFeedContext>
  );
}

/** Keep a slower read or replay from replacing a newer canvas revision. */
export function newerWorkspace(previous: WorkspaceState | undefined, incoming: WorkspaceState): WorkspaceState {
  if (!previous) return incoming;
  return incoming.revision >= previous.revision ? incoming : previous;
}

/** Put a canvas state in the cache under both its keys, unless the cache holds a newer one. */
export function acceptWorkspace(client: QueryClient, state: WorkspaceState): void {
  client.setQueryData<WorkspaceState>(["workspace", "default"], (previous) => newerWorkspace(previous, state));
  client.setQueryData<WorkspaceState>(["workspace", state.id], (previous) => newerWorkspace(previous, state));
}
