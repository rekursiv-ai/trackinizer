import { type ReactNode, useState } from "react";
import { usePalette } from "../commands/palette";
import { keyCaps } from "../commands/registry";
import { ComposeButton } from "../create/ComposeButton";
import { formatRoute, type Route } from "../router/route";
import { useRouter } from "../router/router";
import { chooseTheme, useTheme } from "../theme";
import { Icon, Logo } from "../ui/icons";
import { KindIcon, kindLook } from "../ui/kinds";
import { type Panel, type PanelSpec, PanelToggle } from "../ui/panel";
import { useMeta, useProfile } from "./boot";

/**
 * The sidebar as a panel, collapsed by ⌘B (Ctrl+B), as VS Code's primary side
 * bar is: `[` and `]` are each view's own panels'.
 */
export const SIDEBAR: PanelSpec = { id: "app.sidebar", name: "sidebar", side: "left", keys: ["$mod+b"] };

/** The sidebar's element id, which its collapse and expand button controls. */
const SIDEBAR_ID = "app-sidebar";

/**
 * The sidebar: the logo, search, New, Graph (the home view), Console,
 * Activity, one Browse entry per kind, Settings (with Admin for admins only),
 * and the signed-in user. The mock's other entries (Starred, Notifications,
 * Views, Later) belong to later phases and stay out until those ship. The
 * button beside New collapses it (`panel`) to a rail, as VS Code's activity bar
 * is: the button that expands it on top, then search, New and every entry's
 * icon, each named and titled by its label, and the user's avatar. On a phone
 * it is the drawer, whole, collapsed or not (styles.css).
 */
export function Sidebar({ onNavigate, panel }: { onNavigate: () => void; panel: Panel }) {
  const { kinds } = useMeta();
  const profile = useProfile();
  const { route } = useRouter();
  const palette = usePalette();
  const currentKind = route.name === "list" || route.name === "ref" ? route.kind : null;
  const compact = panel.collapsed;
  const toggle = <PanelToggle panel={panel} controls={SIDEBAR_ID} />;
  return (
    <nav id={SIDEBAR_ID} className="sidebar" aria-label="Sidebar">
      <div className="ws-row">
        {/* On top in the rail, last beside New: placed here, not by CSS `order`, so Tab meets it where the eye does. */}
        {compact ? toggle : null}
        <a className="ws-switch" href={formatRoute({ name: "graph" })} onClick={onNavigate}>
          <Logo />
          Trackinizer
        </a>
        <button
          type="button"
          className="icon-btn"
          onClick={() => palette.show()}
          title={`Search and commands (${keyCaps("$mod+k").join(" ")})`}
          aria-label="Search"
        >
          <Icon name="search" />
        </button>
        <ComposeButton onNavigate={onNavigate} />
        {compact ? null : toggle}
      </div>
      <NavItem
        route={{ name: "graph" }}
        current={route.name === "graph"}
        icon={<Icon name="graph" />}
        label="Graph"
        compact={compact}
        onNavigate={onNavigate}
      />
      <NavItem
        route={{ name: "console" }}
        current={route.name === "console"}
        icon={<Icon name="terminal" />}
        label="Console"
        compact={compact}
        onNavigate={onNavigate}
      />
      <NavItem
        route={{ name: "activity" }}
        current={route.name === "activity"}
        icon={<Icon name="activity" />}
        label="Activity"
        compact={compact}
        onNavigate={onNavigate}
      />
      <NavSection title="Browse">
        {kinds.map((kind) => (
          <NavItem
            key={kind}
            route={{ name: "list", kind }}
            current={kind === currentKind}
            icon={<KindIcon kind={kind} />}
            label={kindLook(kind).plural}
            compact={compact}
            onNavigate={onNavigate}
          />
        ))}
      </NavSection>
      <NavSection title="Settings">
        <NavItem
          route={{ name: "settings" }}
          current={route.name === "settings"}
          icon={<Icon name="gear" />}
          label="Your settings"
          compact={compact}
          onNavigate={onNavigate}
        />
        {profile.role === "admin" ? (
          <NavItem
            route={{ name: "admin" }}
            current={route.name === "admin"}
            icon={<Icon name="shield" />}
            label="Admin"
            compact={compact}
            onNavigate={onNavigate}
          />
        ) : null}
      </NavSection>
      <div className="sidebar-foot">
        <div className="foot-row">
          <div className="me-row" role="group" aria-label="Signed in" title={compact ? `${profile.email} (${profile.role})` : undefined}>
            <span className="avatar" aria-hidden="true">
              {profile.email.charAt(0).toUpperCase()}
            </span>
            <span className="me-email">{profile.email}</span>
            <span className="role-chip">{profile.role}</span>
          </div>
          <ThemeButton />
        </div>
      </div>
    </nav>
  );
}

/**
 * Switches the theme shown, dark to light or light to dark, as a browser
 * choice; Settings keeps System as well. It shows the theme it switches to.
 */
function ThemeButton() {
  const { shown } = useTheme();
  const next = shown === "dark" ? "light" : "dark";
  const label = `Switch to ${next} theme`;
  return (
    <button type="button" className="icon-btn theme-btn" aria-label={label} title={label} onClick={() => chooseTheme(next)}>
      <Icon name={next === "light" ? "sun" : "moon"} />
    </button>
  );
}

/** A titled group of entries that its title collapses and opens. */
function NavSection({ title, children }: { title: string; children: ReactNode }) {
  const [open, setOpen] = useState(true);
  return (
    <div className={open ? "nav-section" : "nav-section is-collapsed"}>
      <button type="button" className="nav-section-h" aria-expanded={open} onClick={() => setOpen(!open)}>
        {title}
        <Icon name="chevD" size={12} />
      </button>
      <div className="nav-section-body">{children}</div>
    </div>
  );
}

/** An entry: its icon and label, or in the rail (`compact`) its icon alone, named and titled by its label. */
function NavItem({
  route,
  current,
  icon,
  label,
  compact,
  onNavigate,
}: {
  route: Route;
  current: boolean;
  icon: ReactNode;
  label: string;
  compact: boolean;
  onNavigate: () => void;
}) {
  return (
    <a
      className={current ? "nav-item is-current" : "nav-item"}
      href={formatRoute(route)}
      aria-current={current ? "page" : undefined}
      aria-label={compact ? label : undefined}
      title={compact ? label : undefined}
      onClick={onNavigate}
    >
      {icon}
      <span className="label">{label}</span>
    </a>
  );
}
