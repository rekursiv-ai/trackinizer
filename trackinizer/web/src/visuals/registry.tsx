import { Component, Suspense, type ComponentType, type ReactNode } from "react";
import type { WorkspaceState } from "../api/workspaces";
import { lazyView } from "../router/lazy";
import { DetailView } from "../router/views";
import { useVisualMark } from "./marks";
import rendererVersions from "./renderer-versions.json";

export type RendererProps = {
  readonly instance: WorkspaceState["visuals"][number];
  readonly workspace: WorkspaceState | null;
  readonly onWorkspaceChanged: (state: WorkspaceState) => void;
  readonly focused: boolean;
  readonly children?: ReactNode;
};
type Renderer = { readonly version: number; readonly Component: ComponentType<RendererProps> };

const Chat = lazyView(() => import("./Chat"), (module) => module.Chat);
const ContextGraph = lazyView(() => import("./ContextGraph"), (module) => module.ContextGraph);
const Timeline = lazyView(() => import("./Timeline"), (module) => module.Timeline);
const Artifact = lazyView(() => import("./Artifact"), (module) => module.Artifact);

/**
 * Load every renderer's chunk, and the record view an agent's navigation opens.
 * The canvas calls it once it is up: a first render that suspends holds its
 * fallback on screen for React's 300 ms throttle (measured, 305 to 335 ms for
 * the first show of every visual type), which a loaded chunk skips. The chunks
 * stay out of the first load.
 */
export function preloadRenderers(): void {
  for (const view of [Chat, ContextGraph, Timeline, Artifact, DetailView]) void view.preload().catch(() => {});
}

/**
 * Load the renderer a new canvas shows first, Chat. The shell awaits it with the
 * canvas's own chunk, so the canvas's first render holds no lazy renderer that
 * suspends: a suspended one shows its fallback for React's 300 ms throttle, which
 * made a cold first load of a page 250 to 300 ms slower inside the canvas.
 */
export function preloadFirstRenderers(): Promise<void> {
  return Chat.preload().catch(() => {});
}

/** Frontend counterparts of the backend's inert visual descriptions. */
export const RENDERERS: Readonly<Record<string, Renderer>> = {
  "trax.browse": { version: rendererVersions["trax.browse"], Component: ({ children }) => <>{children}</> },
  "trax.chat": { version: rendererVersions["trax.chat"], Component: Chat },
  "trax.subgraph": { version: rendererVersions["trax.subgraph"], Component: ContextGraph },
  "trax.timeline": { version: rendererVersions["trax.timeline"], Component: Timeline },
  "trax.artifact": { version: rendererVersions["trax.artifact"], Component: Artifact },
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
        <PaintMark instance={instance} workspace={props.workspace} />
      </Suspense>
    </VisualErrorBoundary>
  );
}

/**
 * Marks when the pane's content painted. It sits in the pane's Suspense
 * boundary, so a lazy renderer's mark waits for the renderer, not its fallback.
 */
function PaintMark({ instance, workspace }: Pick<RendererProps, "instance" | "workspace">) {
  useVisualMark(instance, workspace, "paint");
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
