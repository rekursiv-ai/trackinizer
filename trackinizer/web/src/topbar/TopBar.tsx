import { useCallback, useEffect, useRef, useState } from "react";
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
import { WorkspaceBar } from "./WorkspaceBar";
import { useDismiss } from "./useDismiss";
import { useHoverOpen } from "../visuals/hoverOpen";
import "./topbar.css";

/** The `localStorage` key of whether this browser keeps the bar open. */
const PINNED_KEY = "trackinizer.topbar.pinned";

function readPinned(): boolean {
  try {
    return localStorage.getItem(PINNED_KEY) !== "0";
  } catch {
    return true;
  }
}

function writePinned(pinned: boolean) {
  try {
    localStorage.setItem(PINNED_KEY, pinned ? "1" : "0");
  } catch {
    // Storage blocked: the choice lasts for this page only.
  }
}

/**
 * The app's one bar, along the top: the logo (home, the graph), Go (the pages),
 * Search, New, the pin, then the user's workspaces, Machines (where commands
 * run), the Workbench's visuals while a canvas is mounted, and the account at
 * the end.
 */
export function TopBar({ canvases }: { readonly canvases: boolean }) {
  const palette = usePalette();
  const [goOpen, setGoOpen] = useState(false);
  const [pinned, setPinned] = useState(readPinned);
  const header = useRef<HTMLElement>(null);
  const peek = useHoverOpen(header);
  const shown = pinned || peek.open;
  const closeGo = useCallback(() => setGoOpen(false), []);
  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (typeof window.matchMedia === "function" && window.matchMedia("(hover: none)").matches && event.target instanceof Element
        && !event.target.closest(".topbar, .topbar-edge")) peek.set(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [peek.set]);
  return (
    <>
    <button type="button" className="topbar-edge" aria-label="Open app bar" hidden={shown}
      onPointerMove={peek.handlers.onPointerMove} onPointerLeave={peek.handlers.onPointerLeave} onClick={() => peek.set(true)} />
    <header ref={header} className={`topbar${shown ? " topbar-auto-open" : " topbar-auto-hidden"}`} aria-label="App"
      onPointerMove={peek.handlers.onPointerMove} onPointerLeave={peek.handlers.onPointerLeave} onBlurCapture={peek.handlers.onBlur}>
      <a className="topbar-logo" href={formatRoute({ name: "graph" })} aria-label={`${APP.name}, the graph`} title={APP.name}>
        <Logo />
      </a>
      <GoMenu open={goOpen} onOpen={setGoOpen} />
      <button type="button" className="icon-btn" onClick={() => palette.show()} aria-label="Search"
        title={`Search and commands (${keyCaps("$mod+k").join(" ")})`}>
        <Icon name="search" />
      </button>
      <ComposeButton onNavigate={closeGo} />
      <span className="topbar-rule" aria-hidden="true" />
      <button type="button" className="icon-btn topbar-pin" aria-label={pinned ? "Unpin app bar" : "Keep app bar open"}
        title={pinned ? "Unpin app bar" : "Keep app bar open"} aria-pressed={pinned}
        onClick={() => { writePinned(!pinned); setPinned(!pinned); }}><span aria-hidden="true">📌</span></button>
      <div className="topbar-canvases">{canvases && <WorkspaceBar />}</div>
      <MachinesMenu shown={shown} />
      <WorkbenchMenu />
      <AccountMenu onNavigate={closeGo} />
    </header>
    </>
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
