import { APP_BASE } from "../base";
import { formatRoute, type Route } from "./route";

/**
 * The address bar and the route it holds.
 *
 * A route is how the app, its server and agents name a page: `#/ref/Issue/7`.
 * The address is that route as a path under the app, `/trax/ref/Issue/7`, so a link is a
 * plain URL the server answers. An old link's own `#/...` route
 * (`/trax/#/ref/Issue/7`, `/#/ref/Issue/7`) still names its page: the router
 * moves the address onto its path, in place.
 */

/** The event every move by `goTo` fires, which Back and Forward's `popstate` does not cover. */
const ROUTE_CHANGE = "trackinizer:routechange";

/**
 * The route the address bar is on: an old link's `#/...`, else the path under
 * the app, with its query. The app's own address, `/trax/`, is the empty route,
 * as the empty hash was: the home page.
 */
export function currentRoute(): string {
  if (location.hash.startsWith("#/")) return location.hash;
  const path = location.pathname.startsWith(APP_BASE) ? location.pathname.slice(APP_BASE.length) : "";
  return path || location.search ? `#/${path}${location.search}` : "";
}

/** The address of `route`, `#/ref/Issue/7`: `/trax/ref/Issue/7`. */
export function routeUrl(route: string): string {
  return APP_BASE + route.replace(/^#\/?/, "");
}

/** The address of `route`, for a link's `href`. */
export function routeHref(route: Route): string {
  return routeUrl(formatRoute(route));
}

/**
 * Move the address bar to `route`; `replace` swaps the current history entry
 * instead of adding one. A move to the route already shown adds nothing, as
 * setting the same hash did.
 */
export function goTo(route: string, { replace = false }: { replace?: boolean } = {}): void {
  if (!replace && route === currentRoute()) return;
  const url = routeUrl(route);
  if (replace) history.replaceState(history.state, "", url);
  else history.pushState(null, "", url);
  dispatchEvent(new Event(ROUTE_CHANGE));
}

/**
 * Call `onChange` on every change of route: a move by `goTo`, Back and Forward,
 * and an old `#/...` link followed. Returns the function that stops it.
 */
export function onRouteChange(onChange: () => void): () => void {
  for (const type of [ROUTE_CHANGE, "popstate", "hashchange"]) addEventListener(type, onChange);
  return () => {
    for (const type of [ROUTE_CHANGE, "popstate", "hashchange"]) removeEventListener(type, onChange);
  };
}

/**
 * The route `href` opens in this app, or null when it is not a page of the app.
 *
 * A page of the app is a path under it (`/trax/ref/Issue/9`), on this page's
 * origin, or an old link's `#/...` route: a relative `#/ref/Issue/9`, or one
 * after the app's path or the old UI's `/`. Files of the build (`/trax/assets/...`),
 * `/api/...`, other hosts and other paths are not.
 */
export function routeOf(href: string, page: Pick<Location, "href"> = location): string | null {
  try {
    const own = new URL(page.href);
    const url = new URL(href, own);
    if (url.origin !== own.origin) return null;
    const under = url.pathname.startsWith(APP_BASE);
    if (url.hash.startsWith("#/") && (under || url.pathname === own.pathname || url.pathname === "/")) return url.hash;
    if (!under || url.hash || url.pathname.startsWith(`${APP_BASE}assets/`)) return null;
    return `#/${url.pathname.slice(APP_BASE.length)}${url.search}`;
  } catch {
    return null;
  }
}

/**
 * Keep a click on a link to a page of the app in this tab's app: the router
 * moves, and the page does not load again. A click with a modifier, on a link
 * that opens elsewhere or downloads, or one already handled, is the browser's.
 * Returns the function that stops it.
 */
export function followLinks(): () => void {
  const follow = (event: MouseEvent) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
    if (!(link instanceof HTMLAnchorElement) || (link.target && link.target !== "_self") || link.hasAttribute("download")) return;
    const route = routeOf(link.href);
    if (route === null) return;
    event.preventDefault();
    goTo(route);
  };
  document.addEventListener("click", follow);
  return () => document.removeEventListener("click", follow);
}
