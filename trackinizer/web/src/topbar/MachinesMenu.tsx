import { useQuery } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import { useProfile } from "../app/boot";
import { NO_AUTH_EMAIL } from "../app/signOut";
import { chunk, lazyView, useLoaded } from "../router/lazy";
import { Icon } from "../ui/icons";
import { useDismiss } from "./useDismiss";

// The body loads with the first open, or as the pointer or the keyboard reaches the button.
const MachinesPanel = lazyView(() => import("./MachinesPanel"), (module) => module.MachinesPanel);

/**
 * The Machines menu in the top bar, where commands run: the signed-in person's
 * own machines, how to connect one, and what this server provides, as its
 * deployment notes it. A dot on the button says one of the machines is online.
 * The machines are the terminal's read, under the key it uses with no canvas,
 * so the two caches agree. They are read only while the bar is `shown`, and
 * again each minute it stays so: a page's first reads stay its own, and a
 * folded bar shows no dot to keep current. A server without sign-in, and a
 * viewer, connect no machine, so neither asks for them.
 */
export function MachinesMenu({ shown }: { readonly shown: boolean }) {
  const profile = useProfile();
  const signedIn = profile.email !== NO_AUTH_EMAIL;
  const connects = signedIn && profile.role !== "viewer";
  const [open, setOpen] = useState(false);
  const machines = useQuery({
    queryKey: ["terminal-machines", null],
    queryFn: async ({ signal }) => (await chunk(import("../api/terminals"))).listTerminalMachines(undefined, { signal }),
    enabled: connects && (shown || open), staleTime: 30_000, refetchInterval: 60_000,
  });
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, root, button);
  const online = (machines.data?.machines ?? []).filter((each) => each.status === "online").length;
  // Drawn only once its chunk is in, so the menu never holds a suspended body for React's 300 ms.
  const loaded = useLoaded(MachinesPanel, open);
  const preload = () => void MachinesPanel.preload();
  return (
    <div className="topbar-pop-root" ref={root}>
      <button ref={button} type="button" className="icon-btn machines-button" aria-label={online ? `Machines, ${online} online` : "Machines"}
        title="Machines: where commands run" aria-haspopup="true" aria-expanded={open} onPointerEnter={preload} onFocus={preload}
        onClick={() => {
          setOpen(!open);
          if (open) return;
          if (connects) void machines.refetch();
          // Opened, the keyboard starts in the menu: on its first control once one is drawn.
          requestAnimationFrame(() => {
            const menu = root.current?.querySelector<HTMLElement>(".machines-menu");
            (menu?.querySelector<HTMLElement>(":is(button, a[href], summary):not(:disabled)") ?? menu)?.focus();
          });
        }}>
        <Icon name="server" />
        {online > 0 && <span className="machines-dot" aria-hidden="true" />}
      </button>
      {open && (
        <div className="topbar-pop machines-menu" role="dialog" aria-label="Machines" tabIndex={-1}>
          {loaded
            ? <MachinesPanel read={connects ? machines : null} signedIn={signedIn} onNavigate={close} />
            : <p className="machines-note">Loading…</p>}
        </div>
      )}
    </div>
  );
}
