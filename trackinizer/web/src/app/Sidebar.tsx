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
import { useWorkbenchPresent, WorkbenchSlot } from "./workbenchSlot";

/**
 * The sidebar as a panel, pinned open or collapsed by ⌘B (Ctrl+B), as VS
 * Code's primary side bar is: `[` and `]` are each view's own panels'. It
 * starts as a rail, which hovering opens over the workbench. Its side is the
 * one opposite Chat's, so the shell gives it `side`.
 */
export const SIDEBAR: PanelSpec = { id: "app.sidebar", name: "sidebar", side: "left", keys: ["$mod+b"], startsCollapsed: true };

/** The sidebar's element id, which its collapse and expand button controls. */
const SIDEBAR_ID = "app-sidebar";

/**
 * The sidebar: the logo, Search, New, Graph (the home view), Console,
 * Activity, one Browse entry per kind, the Workbench section (the canvas's
 * visuals and Artifact link, while a canvas is mounted, folded at first), and at
 * its foot Your settings, Admin (for admins only), the theme and the signed-in
 * user. Each icon stands where the rail has it, opened or not. The mock's other
 * entries (Starred, Notifications, Views, Later) belong to later phases and
 * stay out until those ship. The button beside the logo, at the sidebar's
 * outer edge, collapses it (`panel`) to a rail, as VS Code's activity bar is:
 * that button on top, then Search, New and every entry's icon, each named and
 * titled by its label, and the user's avatar. At the outer edge the button
 * stays where it is as the rail opens and folds. On a phone it is the drawer,
 * whole, collapsed or not (styles.css).
 */
export function Sidebar({ onNavigate, onWorkbench, panel }: {
  readonly onNavigate: () => void;
  /** The rail's Workbench icon was pressed: open the sidebar on that section. */
  readonly onWorkbench: () => void;
  readonly panel: Panel;
}) {
  const { kinds } = useMeta();
  const profile = useProfile();
  const { route } = useRouter();
  const palette = usePalette();
  const currentKind = route.name === "list" || route.name === "ref" ? route.kind : null;
  const compact = panel.collapsed;
  const workbench = useWorkbenchPresent();
  // Folded at first: the visual toggles are there when wanted, and the rail's icon unfolds them.
  const [workbenchOpen, setWorkbenchOpen] = useState(false);
  const toggle = <PanelToggle panel={panel} controls={SIDEBAR_ID} />;
  return (
    <nav id={SIDEBAR_ID} className="sidebar" aria-label="Sidebar">
      <div className="ws-row">
        {/* At the outer edge: placed here, not by CSS `order`, so Tab meets it where the eye does. The rail hides the logo. */}
        {panel.side === "left" ? toggle : null}
        <a className="ws-switch" href={formatRoute({ name: "graph" })} onClick={onNavigate}>
          <Logo />
          Trackinizer
        </a>
        {panel.side === "right" ? toggle : null}
      </div>
      <div className="sidebar-tools">
        <button
          type="button"
          className="sidebar-search"
          onClick={() => palette.show()}
          title={`Search and commands (${keyCaps("$mod+k").join(" ")})`}
          aria-label="Search"
        >
          <Icon name="search" />
          <span className="label">Search</span>
          <kbd>{keyCaps("$mod+k").join(" ")}</kbd>
        </button>
        <ComposeButton onNavigate={onNavigate} />
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
      {/* Its body is where the canvas draws its controls; in the rail only the icon stands, which opens the sidebar on them. */}
      <NavSection title="Workbench" hidden={!workbench} open={workbenchOpen} onOpen={setWorkbenchOpen}>
        <button
          type="button"
          className="nav-item workbench-rail"
          aria-label="Workbench"
          title="Workbench"
          onClick={() => {
            setWorkbenchOpen(true);
            onWorkbench();
          }}
        >
          <Icon name="sliders" />
        </button>
        <WorkbenchSlot />
      </NavSection>
      {/* The foot, a row each: Settings, Admin, the theme and the user, in the rail as opened. */}
      <div className="sidebar-foot">
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
        <ThemeButton />
        <div className="me-row" role="group" aria-label="Signed in" title={compact ? `${profile.email} (${profile.role})` : undefined}>
          <span className="avatar" aria-hidden="true">
            {profile.email.charAt(0).toUpperCase()}
          </span>
          <span className="me-email">{profile.email}</span>
          <span className="role-chip">{profile.role}</span>
        </div>
      </div>
    </nav>
  );
}

/**
 * Switches the theme shown, dark to light or light to dark, as a browser
 * choice; Settings keeps System as well. A row of the foot, it shows the theme
 * it switches to.
 */
function ThemeButton() {
  const { shown } = useTheme();
  const next = shown === "dark" ? "light" : "dark";
  const label = `Switch to ${next} theme`;
  return (
    <button type="button" className="nav-item theme-btn" aria-label={label} title={label} onClick={() => chooseTheme(next)}>
      <Icon name={next === "light" ? "sun" : "moon"} />
      <span className="label">{next === "light" ? "Light theme" : "Dark theme"}</span>
    </button>
  );
}

/** A titled group of entries that its title collapses and opens: open at first, unless its owner holds `open`. */
function NavSection({
  title,
  hidden = false,
  open: held,
  onOpen,
  children,
}: {
  title: string;
  hidden?: boolean;
  open?: boolean;
  onOpen?: (open: boolean) => void;
  children: ReactNode;
}) {
  const [own, setOwn] = useState(true);
  const open = held ?? own;
  const setOpen = onOpen ?? setOwn;
  return (
    <div className={open ? "nav-section" : "nav-section is-collapsed"} hidden={hidden}>
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
