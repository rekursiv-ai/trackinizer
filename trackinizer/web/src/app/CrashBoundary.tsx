import { Component, type ReactNode } from "react";
import { CopyDetails } from "../debug/CopyDetails";
import { Icon } from "../ui/icons";
import { EmptyState } from "../ui/view";

/**
 * A render crash below it shows what happened, with Copy details and Reload, in
 * place of a blank page. React's root logs the crash (`logRenderError`, passed to
 * `createRoot` in main.tsx). `frame` wraps the crash screen in the place it
 * stands: the app's own frame, or a view. A crash clears when `resetKey`
 * changes, as when a page moves on.
 */
export class CrashBoundary extends Component<{
  reload: () => void;
  children: ReactNode;
  frame?: (crash: ReactNode) => ReactNode;
  resetKey?: string;
}, { readonly crash: { readonly error: unknown } | null; readonly key: string | undefined }> {
  state = { crash: null as { readonly error: unknown } | null, key: this.props.resetKey };

  static getDerivedStateFromError(error: unknown) {
    return { crash: { error } };
  }

  static getDerivedStateFromProps(props: { resetKey?: string }, state: { key: string | undefined }) {
    return props.resetKey === state.key ? null : { crash: null, key: props.resetKey };
  }

  render() {
    const { crash } = this.state;
    if (!crash) return this.props.children;
    const { error } = crash;
    const message = error instanceof Error ? error.message : String(error);
    const screen = (
      <EmptyState icon={<Icon name="x" size={24} />} title="Trackinizer stopped on an error">
        <p role="alert">{message}</p>
        <p>Copy the details for a bug report, then reload to start again.</p>
        <span className="w-actions">
          <button type="button" className="btn" onClick={this.props.reload}>
            Reload
          </button>
          <CopyDetails message={`The page crashed: ${message}`} error={error} labelled />
        </span>
      </EmptyState>
    );
    return this.props.frame ? this.props.frame(screen) : screen;
  }
}
