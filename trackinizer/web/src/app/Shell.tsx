import { Suspense, useCallback, useState } from "react";
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
import { EmptyState, OpenDrawerContext, ViewHeader } from "../ui/view";
import { useMeta, useProfile, useWriteMode } from "./boot";
import { SIDEBAR, Sidebar } from "./Sidebar";

/**
 * The agent canvas around lists and details, for users who opted in
 * (`visual_workspace_enabled`, off by default): a chunk of its own, so that a
 * first load without it does not carry it.
 */
const Canvas = lazyView(() => import("../visuals/Canvas"), (module) => module.Canvas);

/** A route that shows a view; a new inquiry shows its form over one. */
type ViewRoute = Exclude<Route, { name: "new" }>;

/**
 * The signed-in app: the sidebar, the bars, the current route's view, and the
 * palette and the create form, with the shell's own commands. Collapsed, the
 * sidebar is a rail of its icons; narrow, it is a drawer whether collapsed or
 * not.
 */
export function Shell() {
  const { route, navigate } = useRouter();
  const { kinds } = useMeta();
  const { role, visual_workspace_enabled: visualWorkspaceEnabled } = useProfile();
  const writable = useWriteMode() === "enabled";
  const creatable = creatableKinds(kinds);
  // The view under a `#/new/<Kind>` route: the one before it, or home, the graph,
  // when the app opened on that link.
  const view = route.name === "new" ? null : route;
  const [background, setBackground] = useState<ViewRoute>(view ?? HOME);
  if (view && view !== background) setBackground(view);
  // No route opens the palette: a search link opens the search page.
  const palette = usePaletteState(null, () => {});
  const canvasLoaded = useLoaded(Canvas, visualWorkspaceEnabled);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const openDrawer = useCallback(() => setDrawerOpen(true), []);
  const closeDrawer = useCallback(() => setDrawerOpen(false), []);
  const sidebar = usePanel(SIDEBAR);

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
        <div className={`app${drawerOpen ? " drawer-open" : ""}${sidebar.collapsed ? " sidebar-collapsed" : ""}`}>
          <Sidebar onNavigate={closeDrawer} panel={sidebar} />
          <div className="drawer-scrim" onClick={closeDrawer} />
          <main className="main">
            <BarStack>
              <OfflineBar />
              <PausedBar />
              {/* A view whose chunk is still loading (src/router/views.ts) holds an empty, busy frame. */}
              <Suspense fallback={<div className="view" aria-busy="true" />}>
                {isCanvasRoute(view ?? background) && visualWorkspaceEnabled ? (
                  canvasLoaded ? (
                    <Canvas><RouteView route={view ?? background} /></Canvas>
                  ) : (
                    <div className="view" aria-busy="true" />
                  )
                ) : (
                  <RouteView route={view ?? background} />
                )}
              </Suspense>
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

function isCanvasRoute(route: ViewRoute): boolean {
  return route.name === "list" || route.name === "ref" || route.name === "lookup";
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
    case "settings":
      return <SettingsView />;
    case "admin":
      return <AdminView />;
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
