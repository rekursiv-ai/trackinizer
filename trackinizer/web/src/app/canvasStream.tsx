import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useContext, useEffect, useRef, useState } from "react";
import { createDefaultWorkspace, openWorkspaceEvents, type WorkspaceState } from "../api/workspaces";
import { recordFrame, recordNavigation } from "../debug/timings";
import { LiveContext } from "../live";
import { parseHash } from "../router/route";
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
 * revision never replaces a newer one), an agent's navigation, and what it points at
 * (the tab's highlights, `highlights.ts`). Chat has no frames of its own: a
 * conversation is a session, and a line added to it is the session's `changed` id, which
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
  }, [workspaceId, queryClient, hub, highlights]);
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
