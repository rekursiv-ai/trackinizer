/**
 * The hash of `href` when it is a page of this app, else null.
 *
 * A page of this app has this page's origin and path (`/app/`) and a `#/` route:
 * `https://<this host>/app/#/ref/Issue/9` is `#/ref/Issue/9`. A relative `#/…`
 * is one too. `/api/…` URLs, other hosts and other paths are not.
 */
export function appHash(href: string, page: Pick<Location, "href"> = location): string | null {
  try {
    const own = new URL(page.href);
    const url = new URL(href, own);
    return url.origin === own.origin && url.pathname === own.pathname && url.hash.startsWith("#/") ? url.hash : null;
  } catch {
    return null;
  }
}

/**
 * The attributes of a link to `href`: a page of this app is the hash alone, which
 * moves this tab as a ref does and leaves Back and Ctrl-click to the browser;
 * anything else opens in a new tab.
 */
export function linkProps(href: string, page: Pick<Location, "href"> = location): { href: string; target?: "_blank"; rel?: string } {
  const hash = appHash(href, page);
  return hash === null ? { href, target: "_blank", rel: "noopener noreferrer" } : { href: hash };
}
