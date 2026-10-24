import { type ReactNode, useCallback, useRef } from "react";
import { keyCaps } from "../commands/registry";
import { routeHref } from "../router/addressBar";
import { type Route } from "../router/route";
import { useRouter } from "../router/router";
import { Icon } from "../ui/icons";
import { KindIcon, kindLook } from "../ui/kinds";
import { useMeta } from "../app/boot";
import { useDismiss } from "./useDismiss";
/**
 * The Go menu at the top bar's start: Graph (the home view), Console, Activity,
 * then one Browse entry per kind, the current one marked. The palette (⌘K) and
 * the `g` keys reach the same pages. The top bar holds whether it is open, so
 * New and the account menu close it as they navigate.
 */
export function GoMenu({ open, onOpen }: { readonly open: boolean; readonly onOpen: (open: boolean) => void }) {
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => onOpen(false), [onOpen]);
  useDismiss(open, close, root, button);
  const { kinds } = useMeta();
  const { route } = useRouter();
  const currentKind = route.name === "list" || route.name === "ref" ? route.kind : null;
  return (
    <div className="topbar-pop-root" ref={root}>
      <button ref={button} type="button" className="icon-btn" aria-label="Go to" title="Go to a page"
        aria-haspopup="true" aria-expanded={open} onClick={() => onOpen(!open)}>
        <Icon name="menu" />
      </button>
      {open && (
        <nav className="topbar-pop go-menu" aria-label="Go to">
          <GoItem route={{ name: "graph" }} current={route.name === "graph"} icon={<Icon name="graph" />} label="Graph" keys="g g" onGo={close} />
          <GoItem route={{ name: "console" }} current={route.name === "console"} icon={<Icon name="terminal" />} label="Console" keys="g c" onGo={close} />
          <GoItem route={{ name: "activity" }} current={route.name === "activity"} icon={<Icon name="activity" />} label="Activity" keys="g a" onGo={close} />
          <p className="topbar-pop-label">Browse</p>
          {kinds.map((kind) => (
            <GoItem key={kind} route={{ name: "list", kind }} current={kind === currentKind} icon={<KindIcon kind={kind} />}
              label={kindLook(kind).plural} onGo={close} />
          ))}
        </nav>
      )}
    </div>
  );
}

/** A page's entry: its icon and label, and the keys that go there too. */
function GoItem({ route, current, icon, label, keys, onGo }: {
  readonly route: Route;
  readonly current: boolean;
  readonly icon: ReactNode;
  readonly label: string;
  readonly keys?: string;
  readonly onGo: () => void;
}) {
  return (
    <a className={current ? "topbar-item is-current" : "topbar-item"} href={routeHref(route)}
      aria-current={current ? "page" : undefined} onClick={onGo}>
      {icon}
      <span className="label">{label}</span>
      {keys && <kbd>{keyCaps(keys).join(" ")}</kbd>}
    </a>
  );
}
