import type { ComponentProps } from "react";
import type { ExtraProps } from "react-markdown";
import { routeUrl } from "../router/addressBar";
import { Icon } from "../ui/icons";
import { linkProps } from "../router/appLink";
import { KindIcon } from "../ui/kinds";

/**
 * One link. A ref gets its kind's icon; a link to another page opens in a new tab, one to this app moves this tab.
 *
 * A target `safeUrl` refused arrives as `""`, and renders as its text: an
 * `<a href="">` would load the app's own page and drop the current route.
 */
export function Link({ href, className, title, children }: ComponentProps<"a"> & ExtraProps) {
  if (!href) return <span className="md-dead-link">{children}</span>;
  if (className === "ref") {
    const kind = /^#\/ref\/([^/]+)\//.exec(href)?.[1];
    return (
      <a className="ref" href={routeUrl(href)} title={title}>
        {kind ? <KindIcon kind={decodeURIComponent(kind)} size={12} /> : <Icon name="link" size={12} />}
        {children}
      </a>
    );
  }
  return (
    <a {...linkProps(href)} title={title}>
      {children}
    </a>
  );
}
