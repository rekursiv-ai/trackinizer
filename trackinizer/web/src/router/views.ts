// The views a first load does not need, each a chunk of its own (React's
// `lazy`, https://react.dev/reference/react/lazy): the detail and its Markdown,
// Activity, the create form, settings, admin, search results and the graph load
// when first shown, or as the entry starts (`preloadView`) for the view a link
// opens.
import { lazyView } from "./lazy";
import { parseHash, type Route } from "./route";

export const DetailView = lazyView(() => import("../detail"), (module) => module.DetailView);
export const ActivityView = lazyView(() => import("../activity"), (module) => module.ActivityView);
export const ConsoleView = lazyView(() => import("../console"), (module) => module.ConsoleView);
export const CreateView = lazyView(() => import("../create"), (module) => module.CreateView);
export const SettingsView = lazyView(() => import("../settings"), (module) => module.SettingsView);
export const AdminView = lazyView(() => import("../admin"), (module) => module.AdminView);
export const SearchView = lazyView(() => import("../search"), (module) => module.SearchView);
export const GraphView = lazyView(() => import("../graph"), (module) => module.GraphView);
/**
 * Load the view `hash` opens, so that its chunk downloads beside the boot reads
 * rather than after them. Settles once the chunk has loaded or failed; a load
 * that fails is reported when the view renders.
 */
export async function preloadView(hash: string): Promise<void> {
  // The server's kinds come with boot, after this; any part of the hash, its
  // query's values too (a graph's focus), may be one, which is enough to tell
  // which view it opens.
  const route = parseHash(hash, hash.split(/[/?&=]/));
  await VIEWS[route.name]?.preload().catch(() => {});
}

const VIEWS: { readonly [name in Route["name"]]?: { readonly preload: () => Promise<void> } } = {
  ref: DetailView,
  lookup: DetailView,
  activity: ActivityView,
  console: ConsoleView,
  new: CreateView,
  settings: SettingsView,
  admin: AdminView,
  search: SearchView,
  graph: GraphView,
};
