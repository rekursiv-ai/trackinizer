import { QueryClientProvider } from "@tanstack/react-query";
import { Component, type ReactNode, useEffect, useState } from "react";
import { ApiError } from "../api/client";
import { CommandRegistry, CommandRegistryContext, Shortcuts } from "../commands/registry";
import { CopyDetails } from "../debug/CopyDetails";
import { log } from "../debug/log";
import { LiveProvider } from "../live";
import { RouterProvider } from "../router/router";
import { Icon, Logo } from "../ui/icons";
import { ToastProvider } from "../ui/toast";
import { EmptyState } from "../ui/view";
import { MetaContext, ProfileContext, useBoot } from "./boot";
import { createQueryClient } from "./queryClient";
import { Session, SessionContext } from "./session";
import { Shell } from "./Shell";
import "../writes/writes.css";

/**
 * The whole app: boot, then the shell, under the crash screen.
 *
 * `assign` loads another page, as `location.assign` does, and `reload` reloads
 * this one; tests pass fakes to see where a signed-out user is sent, and what
 * Reload does after a crash.
 */
export function App({
  assign = (url: string) => location.assign(url),
  reload = () => location.reload(),
}: {
  assign?: (url: string) => void;
  reload?: () => void;
}) {
  const [session] = useState(() => new Session(assign));
  const [queryClient] = useState(() => createQueryClient(() => session.leaveForLogin()));
  const [registry] = useState(() => new CommandRegistry());
  return (
    <CrashBoundary reload={reload}>
      <QueryClientProvider client={queryClient}>
        <SessionContext value={session}>
          <CommandRegistryContext value={registry}>
            <ToastProvider>
              <Shortcuts />
              <Boot />
            </ToastProvider>
          </CommandRegistryContext>
        </SessionContext>
      </QueryClientProvider>
    </CrashBoundary>
  );
}

/**
 * Read the server's vocabulary and the profile, then render the shell once.
 *
 * A 401 shows nothing here: the query cache has already sent the browser to the
 * login page.
 */
function Boot() {
  const boot = useBoot();
  const role = boot.state === "ready" ? boot.profile.role : null;
  useEffect(() => {
    if (role) log("info", "boot", { commit: __COMMIT__, role });
  }, [role]);
  if (boot.state === "ready") {
    return (
      <MetaContext value={boot.meta}>
        <ProfileContext value={boot.profile}>
          <RouterProvider kinds={boot.meta.kinds}>
            <LiveProvider>
              <Shell />
            </LiveProvider>
          </RouterProvider>
        </ProfileContext>
      </MetaContext>
    );
  }
  if (boot.state === "loading" || (boot.error instanceof ApiError && boot.error.status === 401)) {
    return <Frame busy />;
  }
  const message = boot.error instanceof Error ? boot.error.message : String(boot.error);
  return (
    <Frame>
      <EmptyState icon={<Icon name="x" size={24} />} title="Could not start Trackinizer">
        <p role="alert">{message}</p>
        <span className="w-actions">
          <button type="button" className="btn" onClick={boot.retry}>
            Retry
          </button>
          <CopyDetails message={`Could not start: ${message}`} error={boot.error} labelled />
        </span>
      </EmptyState>
    </Frame>
  );
}

/**
 * A render crash anywhere in the app shows what happened, with Copy details and
 * Reload, in place of a blank page. React's root logs the crash
 * (`logRenderError`, passed to `createRoot` in main.tsx).
 */
class CrashBoundary extends Component<{ reload: () => void; children: ReactNode }, Crashed> {
  state: Crashed = { crash: null };

  static getDerivedStateFromError(error: unknown) {
    return { crash: { error } };
  }

  render() {
    const { crash } = this.state;
    if (!crash) return this.props.children;
    const { error } = crash;
    const message = error instanceof Error ? error.message : String(error);
    return (
      <Frame>
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
      </Frame>
    );
  }
}

/** The app's frame before boot: the logo in the sidebar and an empty panel. */
function Frame({ busy = false, children }: { busy?: boolean; children?: ReactNode }) {
  return (
    <div className="app" aria-busy={busy}>
      <nav className="sidebar" aria-label="Sidebar">
        <div className="ws-row">
          <span className="ws-switch">
            <Logo />
            Trackinizer
          </span>
        </div>
      </nav>
      <main className="main">{children}</main>
    </div>
  );
}

/** What the app crashed on, once it has. */
type Crashed = { readonly crash: { readonly error: unknown } | null };
