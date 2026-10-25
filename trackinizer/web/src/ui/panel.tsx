import { type ReactNode, type RefObject, useLayoutEffect, useRef, useSyncExternalStore } from "react";
import { type Command, keyCaps } from "../commands/registry";
import { Icon } from "./icons";
import "./panel.css";

/** A side panel that collapses, as its view declares it. */
export type PanelSpec = {
  /** Its name in storage (`trackinizer.v2.panel.<id>`) and in its command's id. */
  readonly id: string;
  /** What it holds, as its button says it: "Collapse <name>", "Expand <name>". */
  readonly name: string;
  /** The side of its view it stands on, which its button's icon points to. */
  readonly side: "left" | "right";
  /** The keys that collapse and expand it while its view mounts `panelCommand`. */
  readonly keys?: readonly string[];
  /** Starts collapsed: a panel opened on demand. */
  readonly startsCollapsed?: boolean;
};

/** A panel, whether it is collapsed now, and what collapses or expands it. */
export type Panel = PanelSpec & {
  readonly collapsed: boolean;
  readonly setCollapsed: (collapsed: boolean) => void;
  readonly toggle: () => void;
  /** The `PanelToggle` mounted now, in its bar or its strip: one at a time. */
  readonly button: RefObject<HTMLButtonElement | null>;
  /** The panel changed with the focus in what `button` controls: the button that takes its place takes the focus. */
  readonly refocus: RefObject<boolean>;
};

/**
 * The panel `spec` declares, collapsed or not. Whether it is collapsed is kept
 * for this tab in `sessionStorage`, as the graph keeps whether its key shows: a
 * reload or Back finds the panel as it was left, and a new tab starts each panel
 * as it starts. Every view holding the panel sees a change at once, as the graph
 * frames itself again when the app's sidebar collapses.
 */
export function usePanel(spec: PanelSpec): Panel {
  const key = `trackinizer.v2.panel.${spec.id}`;
  const now = () => kept(key) ?? spec.startsCollapsed ?? false;
  const collapsed = useSyncExternalStore(subscribe, now);
  const button = useRef<HTMLButtonElement>(null);
  const refocus = useRef(false);
  const setCollapsed = (next: boolean) => {
    // Whatever changes it, its button or its key alike: what the button controls
    // (the panel, or the strip in its place) hides or goes, and a focus in it
    // would be left there, or on the page.
    const region = document.getElementById(button.current?.getAttribute("aria-controls") ?? "");
    refocus.current = next !== now() && (region?.contains(document.activeElement) ?? false);
    keep(key, next);
  };
  return { ...spec, collapsed, button, refocus, setCollapsed, toggle: () => setCollapsed(!now()) };
}

/** The command that collapses or expands `panel`, under its keys and the palette's Panels, for its view to mount while it shows. */
export function panelCommand(panel: Panel): Command {
  return { id: `panel.${panel.id}`, title: action(panel), keys: panel.keys, section: "Panels", run: panel.toggle };
}

/**
 * The button that collapses or expands `panel`, the element `controls` names:
 * the panel's bar holds it, or the strip a collapsed panel leaves.
 */
export function PanelToggle({ panel, controls }: { panel: Panel; controls: string }) {
  const key = panel.keys?.[0];
  const { button, refocus, collapsed } = panel;
  useLayoutEffect(() => {
    if (!refocus.current) return;
    refocus.current = false;
    button.current?.focus();
  }, [button, refocus, collapsed]);
  return (
    <button
      ref={button}
      type="button"
      className="icon-btn panel-toggle"
      aria-expanded={!panel.collapsed}
      aria-controls={controls}
      aria-label={action(panel)}
      title={key ? `${action(panel)} (${keyCaps(key).join(" ")})` : action(panel)}
      onClick={panel.toggle}
    >
      <Icon name={ICONS[panel.side][panel.collapsed ? "open" : "close"]} />
    </button>
  );
}

/**
 * What a panel that pops in leaves at its side of the view while collapsed: a
 * strip, the same landmark under the same `id` and `label`, holding the button
 * that expands it and `children`, a few words of what it holds, written down
 * the strip.
 */
export function PanelStrip({
  panel,
  id,
  label,
  className = "",
  children,
}: {
  panel: Panel;
  id: string;
  label: string;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <aside id={id} aria-label={label} className={`panel-strip is-${panel.side} ${className}`.trim()}>
      <PanelToggle panel={panel} controls={id} />
      {children}
    </aside>
  );
}

function action(panel: Panel): string {
  return `${panel.collapsed ? "Expand" : "Collapse"} ${panel.name}`;
}

/** Whether the panel under `key` was left collapsed; null when nothing this build can read was kept. */
function kept(key: string): boolean | null {
  if (unsaved.has(key)) return unsaved.get(key)!;
  try {
    const saved: unknown = JSON.parse(sessionStorage.getItem(key) ?? "null");
    return typeof saved === "boolean" ? saved : null;
  } catch {
    // Unreadable storage or text: the panel starts as it starts.
    return null;
  }
}

function keep(key: string, collapsed: boolean): void {
  try {
    sessionStorage.setItem(key, JSON.stringify(collapsed));
    unsaved.delete(key);
  } catch {
    // Storage off or full: the panel changes all the same, and forgets it on the next visit.
    unsaved.set(key, collapsed);
  }
  dispatchEvent(new Event(CHANGED));
}

function subscribe(onChange: () => void): () => void {
  addEventListener(CHANGED, onChange);
  return () => removeEventListener(CHANGED, onChange);
}

/** What storage refused to keep, by key, so this tab's panels change regardless. */
const unsaved = new Map<string, boolean>();

/** Fired on `window` when this tab collapses or expands a panel. */
const CHANGED = "trackinizer-panel";

/** Each side's icons: the panel with an arrow to its edge to collapse it, away from it to expand it. */
const ICONS = {
  left: { close: "panelLeftClose", open: "panelLeftOpen" },
  right: { close: "panelRightClose", open: "panelRightOpen" },
} as const;
