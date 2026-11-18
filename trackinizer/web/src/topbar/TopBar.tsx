import { useCallback, useRef, useState } from "react";
import { APP } from "../appName";
import { usePalette } from "../commands/palette";
import { keyCaps } from "../commands/registry";
import { ComposeButton } from "../create/ComposeButton";
import { formatRoute } from "../router/route";
import { Icon, Logo } from "../ui/icons";
import { useWorkbenchPresent, WorkbenchSlot } from "../app/workbenchSlot";
import { AccountMenu } from "./AccountMenu";
import { GoMenu } from "./GoMenu";
import { MachinesMenu } from "./MachinesMenu";
import { WorkstreamBar } from "./WorkstreamBar";
import { useDismiss } from "./useDismiss";
import "./topbar.css";

/**
 * The app's one bar, along the top: the logo (home, the graph), Go (the pages),
 * Search, New, then the user's workstreams, Machines (where commands run), the
 * Workbench's visuals while a canvas is mounted, and the account at the end.
 *
 * The bar is always there. It used to hide itself when the pointer left it, and
 * a pin in the bar turned that off — but the design pass that removed the pin
 * left the hiding behind, and the choice persisted in `localStorage`. A browser
 * that had ever unpinned it came back to a bar that hid with no control left to
 * bring it back: the page opened under an 8px handle. One bar that stays put is
 * what the rest of the layout already assumes, so there is no state to keep.
 */
export function TopBar({ stations }: { readonly stations: boolean }) {
  const palette = usePalette();
  const [goOpen, setGoOpen] = useState(false);
  const closeGo = useCallback(() => setGoOpen(false), []);
  return (
    <header className="topbar" aria-label="App">
      <span className="topbar-left">
        <a className="topbar-logo" href={formatRoute({ name: "graph" })} aria-label={`${APP.name}, the graph`} title={APP.name}>
          <Logo />
        </a>
        <span className="topbar-name">{APP.name}</span>
        <span className="topbar-slash" aria-hidden="true">/</span>
        <GoMenu open={goOpen} onOpen={setGoOpen} />
        <ComposeButton onNavigate={closeGo} />
        <div className="topbar-stations">{stations && <WorkstreamBar />}</div>
      </span>
      <button type="button" className="topbar-search" onClick={() => palette.show()} aria-label="Search"
        title={`Search and commands (${keyCaps("$mod+k").join(" ")})`}>
        <Icon name="search" size={13} />
        <span className="label">Search this research</span>
        <kbd>{keyCaps("$mod+k").join(" ")}</kbd>
      </button>
      <span className="topbar-tail">
        <MachinesMenu shown />
        <WorkbenchMenu />
        <AccountMenu onNavigate={closeGo} />
      </span>
    </header>
  );
}

/**
 * The Workbench's visuals, from a sliders button: the canvas draws its toggles
 * into the slot by a portal, so the slot stays mounted, hidden, while the menu
 * is closed. It shows only while a canvas is mounted.
 */
function WorkbenchMenu() {
  const present = useWorkbenchPresent();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, root, button);
  return (
    <div className="topbar-pop-root" ref={root} hidden={!present}>
      <button ref={button} type="button" className="icon-btn" aria-label="Workbench" title="Workbench: the canvas's visuals"
        aria-haspopup="true" aria-expanded={open} onClick={() => {
          setOpen(!open);
          // Opened, the keyboard starts on its first toggle, as it would in any menu.
          if (!open) requestAnimationFrame(() => root.current?.querySelector<HTMLElement>(".workbench-slot :is(input, button):not(:disabled)")?.focus());
        }}>
        <Icon name="sliders" />
      </button>
      <div className="topbar-pop workbench-menu" role="dialog" aria-label="Workbench" hidden={!open}>
        <p className="topbar-pop-label">Workbench</p>
        <WorkbenchSlot />
      </div>
    </div>
  );
}
