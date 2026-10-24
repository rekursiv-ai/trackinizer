import { Suspense, useState } from "react";
import { APP } from "../appName";
import { createCommands } from "../commands/create";
import { PaletteContext, usePaletteState } from "../commands/palette";
import { useCommands } from "../commands/registry";
import { settingsCommands } from "../commands/settings";
import { createKindFor, creatableKinds } from "../create/draft";
import { ListView } from "../lists";
import { PaletteView } from "../palette";
import { routeHref } from "../router/addressBar";
import { lazyView, useLoaded } from "../router/lazy";
import { type Route } from "../router/route";
import { useRouter } from "../router/router";
import { ActivityView, AdminView, ConsoleView, CreateView, DetailView, GraphView, SearchView, SettingsView } from "../router/views";
import { BarStack, OfflineBar, PausedBar } from "../ui/bars";
import { Icon } from "../ui/icons";
import { kindLook } from "../ui/kinds";
import { EmptyState, ViewHeader } from "../ui/view";
import { useMeta, useProfile, useWriteMode } from "./boot";
import { TopBar } from "../topbar/TopBar";

/**
 * The agent canvas around the views, for users who have not opted out
 * (`visual_canvas_enabled`, on by default): a chunk of its own, so that a
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

/** Settings and Admin: a sheet over the page, half its width, from its left edge. */
type SheetRoute = Extract<Route, { name: "settings" | "admin" }>;

/**
 * The signed-in app: the top bar, the bars, the current route's view, and the
 * palette and the create form, with the shell's own commands. The top bar
 * stands over everything; under it, Chat's column and the canvas around the
 * page, or the page alone with no canvas.
 */
export function Shell() {
  const { route, navigate } = useRouter();
  const { kinds } = useMeta();
  const { role, visual_canvas_enabled: visualCanvasEnabled } = useProfile();
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
  const canvasLoaded = useLoaded(Canvas, visualCanvasEnabled);

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
  ]);

  const withCanvas = visualCanvasEnabled && canvasLoaded;
  // With the canvas, the sheet opens over the canvas's page (ChatColumn); without it, over the main area.
  const sheetView = sheet ? (
    <aside className="sheet" aria-label={sheet.name === "settings" ? "Your settings" : "Admin"}>
      <Suspense fallback={<div className="view" aria-busy="true" />}>
        {sheet.name === "settings" ? <SettingsView /> : <AdminView />}
      </Suspense>
    </aside>
  ) : null;

  return (
    <PaletteContext value={palette}>
        <div className={withCanvas ? "app with-canvas" : "app"}>
          <TopBar canvases={withCanvas} />
          <main className="main">
            <BarStack>
              <OfflineBar />
              <PausedBar />
              {/* A view whose chunk is still loading (src/router/views.ts) holds an empty, busy frame. */}
              <Suspense fallback={<div className="view" aria-busy="true" />}>
                {/* The canvas holds the shell's busy frame until its chunk is in, then mounts the page once, inside it. */}
                {visualCanvasEnabled ? (
                  canvasLoaded ? (
                    <ChatColumn sheet={sheetView}><Canvas><RouteView route={view ?? background} /></Canvas></ChatColumn>
                  ) : (
                    <div className="view" aria-busy="true" />
                  )
                ) : (
                  <RouteView route={view ?? background} />
                )}
              </Suspense>
              {visualCanvasEnabled ? null : sheetView}
            </BarStack>
          </main>
        </div>
        {route.name === "new" ? (
          <Suspense>
            <CreateView kind={route.kind} onClose={() => navigate(background, { replace: true })} />
          </Suspense>
        ) : null}
        <PaletteView />
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
              <code className="mono">{route.hash}</code> is not a {APP.name} link.
            </p>
            <a className="btn" href={routeHref(HOME)}>
              Go to the graph
            </a>
          </EmptyState>
        </div>
      );
  }
}

/** The home view, which the empty hash opens too. */
const HOME: ViewRoute = { name: "graph" };
