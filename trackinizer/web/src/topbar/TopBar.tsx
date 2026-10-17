import { useCallback, useEffect, useRef, useState } from "react";
import { usePalette } from "../commands/palette";
import { keyCaps } from "../commands/registry";
import { ComposeButton } from "../create/ComposeButton";
import { formatRoute } from "../router/route";
import { Icon, Logo } from "../ui/icons";
import { useWorkbenchPresent, WorkbenchSlot } from "../app/workbenchSlot";
import { AccountMenu } from "./AccountMenu";
import { GoMenu } from "./GoMenu";
import { WorkstreamBar } from "./WorkstreamBar";
import { useDismiss } from "./useDismiss";
import { useHoverOpen } from "../visuals/hoverOpen";
import "./topbar.css";

/**
 * The app's one bar, along the top: the logo (home, the graph), Go (the pages),
 * Search, New, then the user's workstreams, the Workbench's visuals while a
 * canvas is mounted, and the account at the end. It hides until the pointer
 * reaches the top edge; a click on the bar itself (not on a control) keeps it
 * open, and another lets it hide again.
 */
export function TopBar({ stations }: { readonly stations: boolean }) {
  const palette = usePalette();
  const [goOpen, setGoOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
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
    <header ref={header} className={`topbar${shown ? " topbar-auto-open" : " topbar-auto-hidden"}${pinned ? " topbar-pinned" : ""}`} aria-label="App"
      title={pinned ? "Click the bar to let it hide" : "Click the bar to keep it open"}
      onPointerMove={peek.handlers.onPointerMove} onPointerLeave={peek.handlers.onPointerLeave} onBlurCapture={peek.handlers.onBlur}
      onClick={(event) => { if (!onControl(event.target)) setPinned((value) => !value); }}>
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
      <div className="topbar-stations">{stations && <WorkstreamBar />}</div>
      <WorkbenchMenu />
      <AccountMenu onNavigate={closeGo} />
    </header>
    </>
  );
}

/** Whether a click landed on a control (or a menu or field it opened), whose click it is, not the bar's. */
function onControl(target: EventTarget): boolean {
  return target instanceof Element && target.closest("a, button, input, select, textarea, [role='menu'], [role='dialog']") !== null;
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
