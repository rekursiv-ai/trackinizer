import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useContext, useEffect, useRef, useState } from "react";
import { openCanvasEvents } from "../api/canvases";
import { answerRequest } from "../canvasCapture/answer";
import { CaptureContext, CaptureRegistry } from "../canvasCapture/stageRegistry";
import { recordFrame, recordNavigation } from "../debug/timings";
import { LiveContext } from "../live";
import { receivePreferences } from "../preferences";
import { parseHash } from "../router/route";
import { setChatHidden } from "../visuals/chatColumnState";
import { ChatFeed, ChatFeedContext } from "../visuals/chatFeed";
import { acceptCanvas } from "../canvases/cache";
import { useCanvas } from "../canvases/useCanvas";
import { MetaContext } from "./boot";
import { HighlightContext, HighlightStore } from "./highlights";

/**
 * The canvas's one stream, owned above the routes so that it outlives the canvas
 * (Settings and Admin are outside it) and is the tab's only one: while the canvas
 * is `enabled`, the live layer takes its inquiry ids from it (`changed` frames)
 * and `/api/web/subscribe` stays shut.
 *
 * It also applies what the stream says about the canvas: the canvas (a
 * revision never replaces a newer one), an agent's navigation, and what it points at
 * (the tab's highlights, `highlights.ts`), when it hides or shows Chat's
 * column, and when it asks for a picture of the canvas, which the open canvas
 * draws through the registry this provides (`canvasCapture/`). Chat's lines have no frames of their own: a conversation is a session, and a line added to it is the session's `changed` id, which
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
  const [captures] = useState(() => new CaptureRegistry());
  const created = useCanvas({ enabled });
  const canvasId = enabled ? created.data?.id : undefined;
  useEffect(() => {
    if (!canvasId) return;
    const close = openCanvasEvents(canvasId, {
      canvas: (state, t) => {
        recordFrame(state.revision, t);
        acceptCanvas(queryClient, state);
      },
      navigate: (route, t) => {
        if (!route.startsWith("#/") || parseHash(route, kindsRef.current).name === "notFound") return;
        recordNavigation(route, t);
        if (window.location.hash !== route) window.location.hash = route;
      },
      highlight: (ids) => highlights.set(ids),
      chat: (hidden) => setChatHidden(hidden),
      capture: (request) => void answerRequest(canvasId, request, captures, { hidden: document.hidden }),
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
  }, [canvasId, queryClient, hub, highlights, feed, captures]);
  return (
    <ChatFeedContext value={enabled ? feed : null}>
      <HighlightContext value={highlights}>
        <CaptureContext value={captures}>{children}</CaptureContext>
      </HighlightContext>
    </ChatFeedContext>
  );
}
