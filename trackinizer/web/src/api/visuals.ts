import previewCatalog from "../visuals/catalog.preview.json";
import { ApiError, type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";

/** The backend's safe, data-free visual descriptor. */
export type VisualDescription = components["schemas"]["VisualDescription"];
export type VisualCatalog = components["schemas"]["VisualCatalogBody"];

/** Fetch available visuals and the default selection for a new workspace. */
export async function getVisualCatalog({ signal }: CallOptions = {}): Promise<VisualCatalog> {
  let catalog: VisualCatalog;
  try {
    catalog = await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/visuals", { signal }));
  } catch (error) {
    // The local Vite preview may point at the previous hosted server. This
    // snapshot is pinned to the Python catalog; production always requires it.
    if (import.meta.env.DEV && error instanceof ApiError && error.status === 404) {
      return previewCatalog as VisualCatalog;
    }
    throw error;
  }
  if (!catalog || typeof catalog.default_visual !== "string" || !Array.isArray(catalog.visuals)
    || !catalog.visuals.every((visual) => visual && typeof visual.type === "string"
      && Number.isInteger(visual.version) && typeof visual.title === "string")) {
    throw new Error("Invalid visual catalog");
  }
  return catalog;
}
