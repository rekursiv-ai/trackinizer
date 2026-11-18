import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { AlertDialog } from "radix-ui";
import { Icon, type IconName } from "../ui/icons";
import type { Boxes } from "../canvasMarks/useBoxes";
import type { CanvasTab } from "./canvasTabs";

function tabIcon(name: string): IconName {
  const lower = name.toLowerCase();
  if (lower.includes("review") || lower.includes("evidence")) return "doc";
  if (lower.includes("graph")) return "graph";
  if (lower.includes("file")) return "folder";
  if (lower.includes("canvas")) return "layers";
  return "layers";
}

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
 * The canvas's top, as a browser's — two rows. The tabs row holds one tab per canvas,
 * and the open tab is cut into the page below it; a double click names a tab in place and
 * × closes it while another is left. At the right end the pen draws red boxes on the page
 * (Escape stops), the eraser takes every box away and Clear empties the tab.
 *
 * `back`, `forward` and `home` are still in the props the canvas passes — the actions behind
 * them live on and the palette reaches them — but no button here draws them any more.
 */
export function CanvasBar({ tabs, open, moving, canAdd, boxes, actions, onPage, showPage }: {
  readonly tabs: readonly CanvasTab[];
  readonly open: string;
  readonly back: boolean;
  readonly forward: boolean;
  readonly moving: boolean;
  readonly home: "graph" | "empty";
  readonly canAdd: boolean;
  readonly boxes: Boxes;
  readonly actions: CanvasBarActions;
  /** Whether the stage is showing the routed page rather than the visual on top of it. */
  readonly onPage?: boolean;
  /** Put the visual away to read the page, or bring it back. */
  readonly showPage?: (page: boolean) => void;
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
      {/* Row one is the tabs, cut into the page below them; row two navigates the open one. */}
      <div className="canvas-tabs-row">
        <nav ref={strip} className="canvas-tabs" aria-label="Canvas tabs">
          {tabs.map((tab) => tab.id === naming?.id ? (
            <input key={tab.id} className="canvas-tab canvas-tab-name" aria-label="Tab name" maxLength={MAX_NAME}
              value={naming.value} autoFocus onFocus={(event) => event.currentTarget.select()}
              onChange={(event) => setNaming({ id: tab.id, value: event.target.value })} onKeyDown={onNameKey} onBlur={keep} />
          ) : (
            <span key={tab.id} className="canvas-tab" aria-current={tab.id === open ? "true" : undefined}>
              {/* A tab reads as the name it holds. Two names were swapped for "Canvas" on the way
                  to the screen, so the word a person clicked was not the word the tab was called,
                  nor the one its label, its title and its rename field all said. */}
              <Icon name={tabIcon(tab.name)} size={13} className="canvas-tab-ic" />
              <button className="canvas-tab-open" type="button" aria-label={tab.name} title={`${tab.name} (double-click to rename)`}
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
        <span className="canvas-toolbar-tail">
          {/* The way back to the page, and to the rail that navigates it. A composition
              covers the stage, and the only control that moved it aside was a destructive
              Clear; Graph, Files, Runs and Evidence are routes, so with no way to put the
              visual down they changed the address and nothing on screen. This puts it
              down and picks it back up, and writes nothing to the station either way. */}
          {showPage && (
            <button className="canvas-bar-button" type="button" aria-pressed={onPage === true}
              aria-label={onPage ? "Back to the canvas" : "Show the page"}
              title={onPage ? "Back to the canvas" : "Show the page the rail navigates"}
              onClick={() => showPage(!onPage)}><Icon name={onPage ? "layers" : "doc"} size={14} /></button>
          )}
          <span className="canvas-bar-button" role="presentation" title="More options" aria-hidden="true">
            <Icon name="more" size={14} />
          </span>
          <span className="canvas-bar-button" role="presentation" title="Maximize canvas" aria-hidden="true">
            <Icon name="maximize" size={14} />
          </span>
          <span className="canvas-tools-hidden">
            <button className="canvas-bar-button" type="button" aria-label="Draw a box" aria-pressed={boxes.drawing}
              title="Draw a red box on this page; click one to remove it (Escape stops drawing)"
              disabled={boxes.page === null} onClick={boxes.toggle}><Icon name="edit" size={15} /></button>
            <button className="canvas-bar-button" type="button" aria-label="Clear all boxes" title="Clear all boxes on this page"
              disabled={boxes.shown.length === 0} onClick={boxes.clearAll}><Icon name="eraser" size={15} /></button>
            <AlertDialog.Root>
              <AlertDialog.Trigger asChild>
                <button className="canvas-bar-button danger canvas-clear" type="button" aria-label="Clear canvas"
                  title="Clear this tab's canvas" disabled={moving}>
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
                      This removes every visual on this tab, Browse included. Other tabs
                      keep what they show. Back restores what this tab showed.
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
          </span>
        </span>
      </div>
    </div>
  );
}

const MAX_NAME = 60;
