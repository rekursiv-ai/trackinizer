import { Component, Suspense, type ComponentType, type ReactNode } from "react";
import type { CanvasState } from "../api/canvases";
import { lazyView, type LazyView } from "../router/lazy";
import { DetailView } from "../router/views";
import { useVisualMark } from "./marks";
import rendererVersions from "./renderer-versions.json";

export type RendererProps = {
  readonly instance: CanvasState["visuals"][number];
  readonly canvas: CanvasState | null;
  readonly onCanvasChanged: (state: CanvasState) => void;
  readonly focused: boolean;
  readonly children?: ReactNode;
};
type Renderer = { readonly version: number; readonly Component: ComponentType<RendererProps> };

const ContextGraph = lazyView(() => import("./ContextGraph"), (module) => module.ContextGraph);
const Timeline = lazyView(() => import("./Timeline"), (module) => module.Timeline);
const Artifact = lazyView(() => import("./Artifact"), (module) => module.Artifact);
const Summary = lazyView(() => import("./Summary"), (module) => module.Summary);
const Files = lazyView(() => import("./Files"), (module) => module.Files);
const Terminal = lazyView(() => import("./Terminal"), (module) => module.TerminalTile);
/**
 * Renderers for visuals an addon's deployment adds to the catalog. They stay out
 * of renderer-versions.json, which mirrors the default catalog every server has.
 */
const ADDON_RENDERERS: Readonly<Record<string, Renderer>> = {
};

/** The addon renderers' chunks; an assistant shows a campaign as it launches it, so its first show must not wait. */
const ADDON_VIEWS: readonly LazyView<RendererProps>[] = [
];

/**
 * Load every renderer's chunk, and the record view an agent's navigation opens.
 * The canvas calls it once it is up: a first render that suspends holds its
 * fallback on screen for React's 300 ms throttle (measured, 305 to 335 ms for
 * the first show of every visual type), which a loaded chunk skips. The chunks
 * stay out of the first load.
 */
export function preloadRenderers(): void {
  for (const view of [ContextGraph, Timeline, Artifact, Summary, Files, DetailView, ...ADDON_VIEWS]) void view.preload().catch(() => {});
}

/** Frontend counterparts of the backend's inert visual descriptions. */
export const RENDERERS: Readonly<Record<string, Renderer>> = {
  ...ADDON_RENDERERS,
  "trax.browse": { version: rendererVersions["trax.browse"], Component: ({ children }) => <>{children}</> },
  "trax.subgraph": { version: rendererVersions["trax.subgraph"], Component: ContextGraph },
  "trax.timeline": { version: rendererVersions["trax.timeline"], Component: Timeline },
  "trax.artifact": { version: rendererVersions["trax.artifact"], Component: Artifact },
  "trax.summary": { version: rendererVersions["trax.summary"], Component: Summary },
  "trax.files": { version: rendererVersions["trax.files"], Component: Files },
  "trax.terminal": { version: rendererVersions["trax.terminal"], Component: Terminal },
} satisfies Record<keyof typeof rendererVersions, Renderer>;

/** A missing or newer renderer fails within its tile, never the entire canvas. */
export function VisualPane(props: RendererProps) {
  const { instance } = props;
  const renderer = RENDERERS[instance.type];
  if (!renderer || renderer.version !== instance.version) {
    return <div className="visual-unsupported" role="alert">No renderer for {instance.type} version {instance.version}.</div>;
  }
  const Component = renderer.Component;
  return (
    <VisualErrorBoundary key={`${instance.type}:${instance.version}`} visualType={instance.type}>
      <Suspense fallback={<div className="visual-loading" aria-busy="true">Loading visual…</div>}>
        <Component {...props} />
        <PaintMark instance={instance} canvas={props.canvas} />
      </Suspense>
    </VisualErrorBoundary>
  );
}

/**
 * Marks when the pane's content painted. It sits in the pane's Suspense
 * boundary, so a lazy renderer's mark waits for the renderer, not its fallback.
 */
function PaintMark({ instance, canvas }: Pick<RendererProps, "instance" | "canvas">) {
  useVisualMark(instance, canvas, "paint");
  return null;
}

class VisualErrorBoundary extends Component<{
  readonly visualType: string;
  readonly children: ReactNode;
}, { readonly failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return <div className="visual-unsupported" role="alert">Could not load {this.props.visualType}. Reload to try again.</div>;
    }
    return this.props.children;
  }
}
