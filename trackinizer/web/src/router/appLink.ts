import { routeOf, routeUrl } from "./addressBar";

/**
 * The attributes of a link to `href`: a page of this app is its route's own
 * address, which moves this tab as a ref does (`followLinks`) and leaves Back
 * and Ctrl-click to the browser; anything else opens in a new tab.
 */
export function linkProps(href: string, page: Pick<Location, "href"> = location): { href: string; target?: "_blank"; rel?: string } {
  const route = routeOf(href, page);
  return route === null ? { href, target: "_blank", rel: "noopener noreferrer" } : { href: routeUrl(route) };
}
