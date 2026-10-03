import type { ComponentProps } from "react";
import type { ExtraProps } from "react-markdown";
import { Icon } from "../ui/icons";
import { KindIcon } from "../ui/kinds";

/**
 * One link. A ref gets its kind's icon; an external link opens in a new tab.
 *
 * A target `safeUrl` refused arrives as `""`, and renders as its text: an
 * `<a href="">` would load the app's own page and drop the current route.
 */
export function Link({ href, className, title, children }: ComponentProps<"a"> & ExtraProps) {
  if (!href) return <span className="md-dead-link">{children}</span>;
  if (className === "ref") {
    const kind = /^#\/ref\/([^/]+)\//.exec(href)?.[1];
    return (
      <a className="ref" href={href} title={title}>
        {kind ? <KindIcon kind={decodeURIComponent(kind)} size={12} /> : <Icon name="link" size={12} />}
        {children}
      </a>
    );
  }
  if (href.startsWith("#/")) {
    return (
      <a href={href} title={title}>
        {children}
      </a>
    );
  }
  return (
    <a href={href} title={title} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}
