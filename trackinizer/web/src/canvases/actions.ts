import type { QueryClient } from "@tanstack/react-query";
import { clearChat as requestClearChat, createCanvas, hideCanvas as requestHideCanvas, listCanvases, renameCanvas, type CanvasSummary } from "../api/canvasManagement";
import { getCanvas, type CanvasState } from "../api/canvases";
import { neighbourOf } from "../visuals/canvasTabs";
import { acceptCanvas, newerCanvas } from "./cache";
import { beginCanvasNavigation, currentCanvas, setCanvas } from "./store";
import { showCanvas } from "./useCanvas";

/** The query key of the user's canvas list. */
export const CANVASES_KEY = ["canvases"] as const;

/** The list workspace and chat pickers read: fetched again once it is a moment old. */
export const canvasesQuery = { queryKey: CANVASES_KEY, queryFn: ({ signal }: { signal?: AbortSignal }) => listCanvases({ signal }), staleTime: 2_000 } as const;

/** Select a canvas immediately, share its refresh with readers, and keep later selections. */
export async function openCanvas(queryClient: QueryClient, id: string): Promise<void> {
  if (currentCanvas().id === id) {
    beginCanvasNavigation();
    return;
  }
  const previous = currentCanvas();
  const page = window.location.hash;
  const queryKey = ["canvas", id];
  const cached = queryClient.getQueryData<CanvasState>(queryKey);
  const refresh = queryClient.fetchQuery({
    queryKey, staleTime: 0, retry: false,
    queryFn: async ({ signal }) => newerCanvas(queryClient.getQueryData<CanvasState>(queryKey), await getCanvas(id, { signal })),
  });
  if (cached) showCanvas(queryClient, cached);
  else setCanvas(id, page);
  const current = beginCanvasNavigation();
  try {
    const incoming = await refresh;
    if (!cached && current()) showCanvas(queryClient, newerCanvas(queryClient.getQueryData<CanvasState>(queryKey), incoming));
    void queryClient.invalidateQueries({ queryKey: CANVASES_KEY });
  } catch (error) {
    if (!cached && current()) {
      setCanvas(previous.id, page);
      window.location.hash = page;
    }
    throw error;
  }
}

/** Make a chat and switch to it. */
export async function newChat(queryClient: QueryClient): Promise<void> {
  const current = beginCanvasNavigation();
  const state = await createCanvas();
  acceptCanvas(queryClient, state);
  if (current()) showCanvas(queryClient, state);
  void queryClient.invalidateQueries({ queryKey: CANVASES_KEY });
}

/** Rename a canvas; the cache and the list follow. A name already taken rejects with the server's 409. */
export async function nameCanvas(queryClient: QueryClient, id: string, name: string): Promise<void> {
  acceptCanvas(queryClient, await renameCanvas(id, name));
  void queryClient.invalidateQueries({ queryKey: CANVASES_KEY });
}

/** Hide a canvas from the bar; when it is current, move to the next visible canvas. */
export async function hideCanvas(queryClient: QueryClient, id: string): Promise<void> {
  const onShow = currentCanvas().id === id;
  const current = onShow ? beginCanvasNavigation() : () => false;
  const visible = (queryClient.getQueryData<CanvasSummary[]>(CANVASES_KEY) ?? [])
    .toSorted((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id)).filter((each) => !each.hidden);
  const neighbour = neighbourOf(visible, id);
  const state = await requestHideCanvas(id);
  acceptCanvas(queryClient, state);
  queryClient.setQueryData<CanvasSummary[]>(CANVASES_KEY, (held) => held?.map((each) => each.id === id ? { ...each, hidden: true } : each));
  if (current() && neighbour) await openCanvas(queryClient, neighbour.id);
}

/** Clear a canvas's chat and open the new canvas returned by the server. */
export async function clearChat(queryClient: QueryClient, id: string, key: string): Promise<void> {
  const current = beginCanvasNavigation();
  const state = await requestClearChat(id, key);
  acceptCanvas(queryClient, state);
  if (current()) showCanvas(queryClient, state);
  void queryClient.invalidateQueries({ queryKey: CANVASES_KEY });
}
