import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { AlertDialog } from "radix-ui";
import { Icon } from "../ui/icons";
import type { CanvasTab } from "./canvasTabs";

/** What the bar does; the canvas does each. */
export type CanvasBarActions = {
  readonly back: () => void;
  readonly forward: () => void;
  readonly home: () => void;
  readonly clear: () => void;
  readonly open: (id: string) => void;
  readonly add: () => void;
  readonly close: (id: string) => void;
  readonly rename: (id: string, name: string) => void;
};

/**
 * The canvas's top row, as a browser's: Back and Forward step through what the
 * open tab showed, Home shows what the user's `home` setting names (the graph,
 * or nothing), and the tabs each hold a canvas of their own. A double click
 * names a tab in place; × closes it while another is left.
 */
export function CanvasBar({ tabs, open, back, forward, moving, home, canAdd, actions }: {
  readonly tabs: readonly CanvasTab[];
  readonly open: string;
  readonly back: boolean;
  readonly forward: boolean;
  readonly moving: boolean;
  readonly home: "graph" | "empty";
  readonly canAdd: boolean;
  readonly actions: CanvasBarActions;
}) {
  const [naming, setNaming] = useState<{ readonly id: string; readonly value: string } | null>(null);
  const strip = useRef<HTMLElement>(null);
  const adding = useRef<ReadonlySet<string> | null>(null);
  useEffect(() => {
    strip.current?.querySelector('[aria-current="true"]')?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [open, tabs.length]);
  useEffect(() => {
    if (!adding.current) return;
    const added = tabs.find((tab) => !adding.current!.has(tab.id));
    if (!added) return;
    adding.current = null;
    setNaming({ id: added.id, value: added.name });
  }, [tabs]);

  function keep() {
    if (!naming) return;
    const name = naming.value.trim();
    setNaming(null);
    if (name && name !== tabs.find((tab) => tab.id === naming.id)?.name) actions.rename(naming.id, name.slice(0, MAX_NAME));
  }

  function onNameKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Enter") { event.preventDefault(); keep(); }
    else if (event.key === "Escape") { event.stopPropagation(); setNaming(null); }
  }

  return (
    <div className="canvas-bar" role="toolbar" aria-label="Canvas">
      <button className="canvas-bar-button" type="button" aria-label="Back" title="Back to what this tab showed before"
        disabled={!back || moving} onClick={actions.back}><Icon name="arrowLeft" size={15} /></button>
      <button className="canvas-bar-button" type="button" aria-label="Forward" title="Forward again"
        disabled={!forward || moving} onClick={actions.forward}><Icon name="arrowRight" size={15} /></button>
      <button className="canvas-bar-button" type="button" aria-label="Home"
        title={home === "empty" ? "Home: an empty canvas (Settings, home)" : "Home: the graph (Settings, home)"}
        disabled={moving} onClick={actions.home}><Icon name="home" size={15} /></button>
      <AlertDialog.Root>
        <AlertDialog.Trigger asChild>
          <button className="canvas-bar-button danger canvas-clear" type="button" aria-label="Clear canvas"
            title="Clear the canvas and close its other tabs" disabled={moving}>
            <Icon name="trash" size={14} />
          </button>
        </AlertDialog.Trigger>
        <AlertDialog.Portal>
          <AlertDialog.Overlay className="modal-backdrop" />
          <AlertDialog.Content className="modal small">
            <div className="modal-b">
              <AlertDialog.Title asChild>
                <h3>Clear canvas?</h3>
              </AlertDialog.Title>
              <AlertDialog.Description>
                This removes every visual, Browse included, and closes the other
                tabs. Back restores what this tab showed.
              </AlertDialog.Description>
            </div>
            <div className="modal-f">
              <div className="spacer" />
              <AlertDialog.Cancel className="btn ghost">Cancel</AlertDialog.Cancel>
              <AlertDialog.Action className="btn danger" onClick={actions.clear}>
                Clear canvas
              </AlertDialog.Action>
            </div>
          </AlertDialog.Content>
        </AlertDialog.Portal>
      </AlertDialog.Root>
      <nav ref={strip} className="canvas-tabs" aria-label="Canvas tabs">
        {tabs.map((tab) => tab.id === naming?.id ? (
          <input key={tab.id} className="canvas-tab canvas-tab-name" aria-label="Tab name" maxLength={MAX_NAME}
            value={naming.value} autoFocus onFocus={(event) => event.currentTarget.select()}
            onChange={(event) => setNaming({ id: tab.id, value: event.target.value })} onKeyDown={onNameKey} onBlur={keep} />
        ) : (
          <span key={tab.id} className="canvas-tab" aria-current={tab.id === open ? "true" : undefined}>
            <button className="canvas-tab-open" type="button" title={`${tab.name} (double-click to rename)`}
              disabled={moving && tab.id !== open} onClick={() => { if (tab.id !== open) actions.open(tab.id); }}
              onDoubleClick={() => setNaming({ id: tab.id, value: tab.name })}>{tab.name}</button>
            {tabs.length > 1 && <button className="canvas-tab-close" type="button" aria-label={`Close ${tab.name}`}
              title="Close this tab" disabled={moving} onClick={() => actions.close(tab.id)}><Icon name="x" size={12} /></button>}
          </span>
        ))}
      </nav>
      <button className="canvas-bar-button canvas-tab-new" type="button" aria-label="New tab" title="New tab"
        disabled={moving || !canAdd} onClick={() => {
          adding.current = new Set(tabs.map((tab) => tab.id));
          actions.add();
        }}><Icon name="plus" size={15} /></button>
    </div>
  );
}

const MAX_NAME = 60;
