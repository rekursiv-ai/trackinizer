import { Component, lazy, Suspense, type ComponentType, type ReactNode } from "react";
import type { WorkspaceState } from "../api/workspaces";
import rendererVersions from "./renderer-versions.json";

export type RendererProps = {
  readonly instance: WorkspaceState["visuals"][number];
  readonly workspace: WorkspaceState | null;
  readonly onWorkspaceChanged: (state: WorkspaceState) => void;
  readonly focused: boolean;
  readonly children?: ReactNode;
};
type Renderer = { readonly version: number; readonly Component: ComponentType<RendererProps> };

const ChatConnect = lazy(() => import("./ChatConnect").then((module) => ({ default: module.ChatConnect })));
const Subgraph = lazy(() => import("./Subgraph").then((module) => ({ default: module.Subgraph })));
const Timeline = lazy(() => import("./Timeline").then((module) => ({ default: module.Timeline })));
const Artifact = lazy(() => import("./Artifact").then((module) => ({ default: module.Artifact })));

/** Frontend counterparts of the backend's inert visual descriptions. */
export const RENDERERS: Readonly<Record<string, Renderer>> = {
  "trax.browse": { version: rendererVersions["trax.browse"], Component: ({ children }) => <>{children}</> },
  "trax.chat": { version: rendererVersions["trax.chat"], Component: ChatConnect },
  "trax.subgraph": { version: rendererVersions["trax.subgraph"], Component: Subgraph },
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
      </Suspense>
    </VisualErrorBoundary>
  );
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
