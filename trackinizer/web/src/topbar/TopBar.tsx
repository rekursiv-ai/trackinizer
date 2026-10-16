import { useCallback, useRef, useState } from "react";
import { usePalette } from "../commands/palette";
import { keyCaps } from "../commands/registry";
import { ComposeButton } from "../create/ComposeButton";
import { formatRoute } from "../router/route";
import { Icon, Logo } from "../ui/icons";
import { useWorkbenchPresent, WorkbenchSlot } from "../app/workbenchSlot";
import { AccountMenu } from "./AccountMenu";
import { GoMenu } from "./GoMenu";
import { StationGroups } from "./StationGroups";
import { useDismiss } from "./useDismiss";
import "./topbar.css";

/**
 * The app's one bar, along the top: the logo (home, the graph), Go (the pages),
 * Search, New, then the user's stations as tab groups, each holding its canvas
 * tabs (`stations`, with the canvas only), the Workbench's visuals while a
 * canvas is mounted, and the account at the end.
 */
export function TopBar({ stations }: { readonly stations: boolean }) {
  const palette = usePalette();
  const [goOpen, setGoOpen] = useState(false);
  const closeGo = useCallback(() => setGoOpen(false), []);
  return (
    <header className="topbar" aria-label="App">
      <a className="topbar-logo" href={formatRoute({ name: "graph" })} aria-label="Trackinizer, the graph" title="Trackinizer">
        <Logo />
      </a>
      <GoMenu open={goOpen} onOpen={setGoOpen} />
      <button type="button" className="icon-btn" onClick={() => palette.show()} aria-label="Search"
        title={`Search and commands (${keyCaps("$mod+k").join(" ")})`}>
        <Icon name="search" />
      </button>
      <ComposeButton onNavigate={closeGo} />
      <span className="topbar-rule" aria-hidden="true" />
      <div className="topbar-stations">{stations && <StationGroups />}</div>
      <WorkbenchMenu />
      <AccountMenu onNavigate={closeGo} />
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
