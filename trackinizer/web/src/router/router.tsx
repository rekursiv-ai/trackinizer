import {
  createContext,
  type ReactNode,
  useContext,
  useLayoutEffect,
  useMemo,
  useSyncExternalStore,
} from "react";
import { type Fields, log } from "../debug/log";
import { formatRoute, parseHash, type Route } from "./route";

/** The current route, and the way to change it. */
export type Router = {
  readonly route: Route;
  readonly navigate: typeof navigate;
};

const RouterContext = createContext<Router | null>(null);

/**
 * Hold the one route the app shows, parsed from the address bar.
 *
 * The route is never stored anywhere else: links and `navigate` write the URL
 * from a route, and every URL change (a link, `navigate`, Back, Forward) is
 * parsed into the route. So the URL and the view cannot disagree (COLD-03). A
 * hash that is not canonical, such as an old UI link, is replaced in place,
 * which adds no history entry.
 */
export function RouterProvider({
  kinds,
  children,
}: {
  kinds: readonly string[];
  children: ReactNode;
}) {
  const hash = useSyncExternalStore(subscribe, () => location.hash);
  const route = useMemo(() => parseHash(hash, kinds), [hash, kinds]);
  const canonical = formatRoute(route);
  useLayoutEffect(() => {
    if (canonical !== hash) navigate(route, { replace: true });
    else log("info", "navigate", routeFields(route));
  }, [canonical, hash, route]);
  const router = useMemo(() => ({ route, navigate }), [route]);
  return <RouterContext value={router}>{children}</RouterContext>;
}

/** The current route and `navigate`. */
export function useRouter(): Router {
  const router = useContext(RouterContext);
  if (!router) throw new Error("useRouter needs a RouterProvider above it.");
  return router;
}

/** Go to `route`; `replace` swaps the current history entry instead of adding one. */
function navigate(route: Route, { replace = false }: { replace?: boolean } = {}): void {
  const hash = formatRoute(route);
  if (!replace) {
    location.hash = hash;
    return;
  }
  const oldURL = location.href;
  history.replaceState(history.state, "", hash);
  // replaceState fires no event, and the router learns of every change from one.
  dispatchEvent(new HashChangeEvent("hashchange", { oldURL, newURL: location.href }));
}

function subscribe(onChange: () => void): () => void {
  addEventListener("hashchange", onChange);
  addEventListener("popstate", onChange);
  return () => {
    removeEventListener("hashchange", onChange);
    removeEventListener("popstate", onChange);
  };
}

/** Where `route` goes, for the log: its name, kind, seq and id, never search text. */
function routeFields(route: Route): Fields {
  switch (route.name) {
    case "list":
    case "new":
      return { route: route.name, kind: route.kind };
    case "ref":
      return { route: route.name, kind: route.kind, seq: route.seq };
    case "lookup":
      return { route: route.name, id: route.id };
    default:
      return { route: route.name };
  }
}
