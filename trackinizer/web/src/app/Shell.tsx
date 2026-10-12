import { Suspense, useCallback, useRef, useState } from "react";
import { createCommands } from "../commands/create";
import { PaletteContext, usePaletteState } from "../commands/palette";
import { useCommands } from "../commands/registry";
import { settingsCommands } from "../commands/settings";
import { createKindFor, creatableKinds } from "../create/draft";
import { ListView } from "../lists";
import { PaletteView } from "../palette";
import { lazyView, useLoaded } from "../router/lazy";
import { formatRoute, type Route } from "../router/route";
import { useRouter } from "../router/router";
import { ActivityView, AdminView, ConsoleView, CreateView, DetailView, GraphView, SearchView, SettingsView } from "../router/views";
import { BarStack, OfflineBar, PausedBar } from "../ui/bars";
import { Icon } from "../ui/icons";
import { kindLook } from "../ui/kinds";
import { panelCommand, usePanel } from "../ui/panel";
import { useChatColumn } from "../visuals/chatColumn";
import { useHoverOpen } from "../visuals/hoverOpen";
import { EmptyState, OpenDrawerContext, ViewHeader } from "../ui/view";
import { useMeta, useProfile, useWriteMode } from "./boot";
import { SIDEBAR, Sidebar } from "./Sidebar";

/**
 * The agent canvas around the views, for users who have not opted out
 * (`visual_workspace_enabled`, on by default): a chunk of its own, so that a
 * first load without it does not carry it, loaded only for a user who has it on,
 * with Chat's column and the registry of the renderers it shows.
 */
const Canvas = lazyView(
  async () => {
    // Chat's column loads in parallel with the canvas chunk and with it, so the
    // column's first render suspends on nothing.
    const [module] = await Promise.all([import("../visuals/Canvas"), import("../visuals/registry"), ChatColumn.preload()]);
    return module;
  },
  (module) => module.Canvas,
);

/** Chat, a column beside the canvas rather than a visual in it. */
const ChatColumn = lazyView(() => import("../visuals/ChatColumn"), (module) => module.ChatColumn);

/** A route that shows a page; a new inquiry's form, Settings and Admin show over one. */
type ViewRoute = Exclude<Route, { name: "new" | "settings" | "admin" }>;

/** Settings and Admin: a sheet over the page, half its width, from the sidebar's side. */
type SheetRoute = Extract<Route, { name: "settings" | "admin" }>;

/**
 * The signed-in app: the sidebar, the bars, the current route's view, and the
 * palette and the create form, with the shell's own commands. The sidebar
 * stands opposite Chat, a rail of its icons that opens over the workbench while
 * hovered, or pinned open; narrow, it is a drawer whether collapsed or not.
 */
export function Shell() {
  const { route, navigate } = useRouter();
  const { kinds } = useMeta();
  const { role, visual_workspace_enabled: visualWorkspaceEnabled } = useProfile();
  const writable = useWriteMode() === "enabled";
  const creatable = creatableKinds(kinds);
  // The page under a `#/new/<Kind>` route or a sheet: the one before it, or home,
  // the graph, when the app opened on that link.
  const sheet = isSheet(route) ? route : null;
  const view = route.name === "new" || isSheet(route) ? null : route;
  const [background, setBackground] = useState<ViewRoute>(view ?? HOME);
  if (view && view !== background) setBackground(view);
  // No route opens the palette: a search link opens the search page.
  const palette = usePaletteState(null, () => {});
  const canvasLoaded = useLoaded(Canvas, visualWorkspaceEnabled);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const openDrawer = useCallback(() => setDrawerOpen(true), []);
  const closeDrawer = useCallback(() => setDrawerOpen(false), []);
  // The sidebar stands opposite Chat, so one flip of Chat's side moves both.
  const sidebar = usePanel({ ...SIDEBAR, side: useChatColumn().side === "left" ? "right" : "left" });
  // A rail opens over the workbench while the pointer rests on it or the keyboard is in it.
  const dock = useRef<HTMLDivElement>(null);
  const peek = useHoverOpen(dock);
  const peeking = sidebar.collapsed && peek.open;
  const navigated = () => {
    closeDrawer();
    peek.set(false);
  };
  const openWorkbench = () => {
    peek.set(true);
    requestAnimationFrame(() => dock.current?.querySelector<HTMLElement>(".workbench-slot input:not(:disabled), .workbench-slot button:not(:disabled)")?.focus());
  };

  useCommands([
    {
      id: "palette.toggle",
      title: "Search and commands",
      keys: ["$mod+k", "$mod+p"],
      global: true,
      run: palette.toggle,
    },
    {
      id: "go.activity",
      title: "Go to Activity",
      keys: ["g a"],
      section: "Navigate",
      run: () => navigate({ name: "activity" }),
    },
    {
      id: "go.console",
      title: "Go to Console",
      keys: ["g c"],
      section: "Navigate",
      run: () => navigate({ name: "console" }),
    },
    {
      id: "go.graph",
      title: "Go to Graph",
      keys: ["g g"],
      section: "Navigate",
      run: () => navigate({ name: "graph" }),
    },
    ...kinds.map((kind) => ({
      id: `go.list.${kind}`,
      title: `Go to ${kindLook(kind).plural}`,
      section: "Navigate",
      run: () => navigate({ name: "list", kind }),
    })),
    ...settingsCommands(role, (name) => navigate({ name })),
    ...(writable
      ? createCommands(creatable, createKindFor(background, creatable), (kind) => navigate({ name: "new", kind }))
      : []),
    panelCommand(sidebar),
  ]);

  return (
    <PaletteContext value={palette}>
      <OpenDrawerContext value={openDrawer}>
        <div className={`app${drawerOpen ? " drawer-open" : ""}${sidebar.collapsed ? " sidebar-collapsed" : ""}${peeking ? " sidebar-peek" : ""}${sidebar.side === "right" ? " sidebar-right" : ""}`}>
          <div ref={dock} className="sidebar-dock" {...peek.handlers}>
            <Sidebar onNavigate={navigated} onWorkbench={openWorkbench} panel={sidebar} />
          </div>
          <div className="drawer-scrim" onClick={closeDrawer} />
          <main className="main">
            <BarStack>
              <OfflineBar />
              <PausedBar />
              {/* A view whose chunk is still loading (src/router/views.ts) holds an empty, busy frame. */}
              <Suspense fallback={<div className="view" aria-busy="true" />}>
                {/* The canvas holds the shell's busy frame until its chunk is in, then mounts the page once, inside it. */}
                {visualWorkspaceEnabled ? (
                  canvasLoaded ? (
                    <ChatColumn><Canvas><RouteView route={view ?? background} /></Canvas></ChatColumn>
                  ) : (
                    <div className="view" aria-busy="true" />
                  )
                ) : (
                  <RouteView route={view ?? background} />
                )}
              </Suspense>
              {sheet ? (
                <aside className="sheet" aria-label={sheet.name === "settings" ? "Your settings" : "Admin"}>
                  <Suspense fallback={<div className="view" aria-busy="true" />}>
                    {sheet.name === "settings" ? <SettingsView /> : <AdminView />}
                  </Suspense>
                </aside>
              ) : null}
            </BarStack>
          </main>
        </div>
        {route.name === "new" ? (
          <Suspense>
            <CreateView kind={route.kind} onClose={() => navigate(background, { replace: true })} />
          </Suspense>
        ) : null}
        <PaletteView />
      </OpenDrawerContext>
    </PaletteContext>
  );
}

/** Whether `route` opens a sheet over the page rather than a page. */
function isSheet(route: Route): route is SheetRoute {
  return route.name === "settings" || route.name === "admin";
}

function RouteView({ route }: { route: ViewRoute }) {
  switch (route.name) {
    case "list":
      return <ListView kind={route.kind} />;
    case "ref":
      return <DetailView target={{ kind: route.kind, seq: route.seq }} />;
    case "lookup":
      return <DetailView target={{ id: route.id }} />;
    case "activity":
      return <ActivityView />;
    case "console":
      return <ConsoleView />;
    case "search":
      return <SearchView q={route.q} />;
    case "graph":
      return <GraphView />;
    case "notFound":
      return (
        <div className="view">
          <ViewHeader icon={<Icon name="x" />} title="Not found" />
          <EmptyState icon={<Icon name="x" size={24} />} title="Nothing lives at this address">
            <p>
              <code className="mono">{route.hash}</code> is not a Trackinizer link.
            </p>
            <a className="btn" href={formatRoute(HOME)}>
              Go to the graph
            </a>
          </EmptyState>
        </div>
      );
  }
}

/** The home view, which the empty hash opens too. */
const HOME: ViewRoute = { name: "graph" };
