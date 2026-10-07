import { QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, startTransition, useDeferredValue, useEffect, useState } from "react";
import { ApiError } from "../api/client";
import { CommandRegistry, CommandRegistryContext, Shortcuts } from "../commands/registry";
import { CopyDetails } from "../debug/CopyDetails";
import { prepareTimes } from "../detail/time";
import { log } from "../debug/log";
import { LiveProvider } from "../live";
import { RouterProvider } from "../router/router";
import { Icon, Logo } from "../ui/icons";
import { ToastProvider } from "../ui/toast";
import { EmptyState } from "../ui/view";
import { MetaContext, ProfileContext, useBoot } from "./boot";
import { CanvasStream } from "./canvasStream";
import { CrashBoundary } from "./CrashBoundary";
import { createQueryClient } from "./queryClient";
import { Session, SessionContext } from "./session";
import { Shell } from "./Shell";
import "../writes/writes.css";

/**
 * The whole app: boot, then the shell, under the crash screen.
 *
 * Its first render draws only the frame boot shows, and the app under it draws
 * in a background render React can interrupt
 * (https://react.dev/reference/react/useDeferredValue), while the time
 * formatters load the locale in a task of their own (`prepareTimes`). The
 * app's first render is the page's first use of React: with the CPU slowed 4x
 * on a Xeon it was one task of 37 to 39 ms, and the frame alone takes 14.
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
  const started = useDeferredValue(true, false);
  useEffect(() => {
    const timer = setTimeout(prepareTimes);
    return () => clearTimeout(timer);
  }, []);
  if (!started) return <Frame busy />;
  return (
    <CrashBoundary reload={reload} frame={(crash) => <Frame>{crash}</Frame>}>
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
 * The shell's first render is a background render React can interrupt
 * (https://react.dev/reference/react/startTransition): it is the first use of
 * most of the app's code, and in the task the reads landed in it was one of
 * 44 ms with the CPU slowed 4x on a Xeon.
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
  const [shown, setShown] = useState(boot.state === "ready");
  useEffect(() => {
    if (role && !shown) startTransition(() => setShown(true));
  }, [role, shown]);
  if (boot.state === "ready" && !shown) return <Frame busy />;
  if (boot.state === "ready") {
    return (
      <MetaContext value={boot.meta}>
        <ProfileContext value={boot.profile}>
          <RouterProvider kinds={boot.meta.kinds}>
            <LiveProvider canvas={boot.profile.visual_workspace_enabled}>
              <CanvasStream enabled={boot.profile.visual_workspace_enabled}>
                <Shell />
              </CanvasStream>
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
