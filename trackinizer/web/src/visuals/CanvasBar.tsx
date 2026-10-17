import type { ReactNode } from "react";
import { AlertDialog } from "radix-ui";
import { Icon } from "../ui/icons";

/** What the bar does; the canvas does each. */
export type CanvasBarActions = {
  readonly back: () => void;
  readonly forward: () => void;
  readonly home: () => void;
  readonly clear: () => void;
};

/**
 * The canvas's top row, as a browser's: Back and Forward step through what the
 * canvas showed, Home shows what the user's `home` setting names (the graph,
 * or nothing), and Clear closes what it shows. `children` stand after them:
 * the records the canvas worked on (`WorkedStrip`).
 */
export function CanvasBar({ back, forward, moving, home, actions, children }: {
  readonly back: boolean;
  readonly forward: boolean;
  readonly moving: boolean;
  readonly home: "graph" | "empty";
  readonly actions: CanvasBarActions;
  readonly children?: ReactNode;
}) {
  return (
    <div className="canvas-bar" role="toolbar" aria-label="Canvas">
      <button className="canvas-bar-button" type="button" aria-label="Back" title="Back to what the canvas showed before"
        disabled={!back || moving} onClick={actions.back}><Icon name="arrowLeft" size={15} /></button>
      <button className="canvas-bar-button" type="button" aria-label="Forward" title="Forward again"
        disabled={!forward || moving} onClick={actions.forward}><Icon name="arrowRight" size={15} /></button>
      <button className="canvas-bar-button" type="button" aria-label="Home"
        title={home === "empty" ? "Home: an empty canvas (Settings, home)" : "Home: the graph (Settings, home)"}
        disabled={moving} onClick={actions.home}><Icon name="home" size={15} /></button>
      <AlertDialog.Root>
        <AlertDialog.Trigger asChild>
          <button className="canvas-bar-button danger canvas-clear" type="button" aria-label="Clear canvas"
            title="Close everything on the canvas" disabled={moving}>
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
                This closes every visual; the page comes back as the default view.
                What it worked on stays listed, and Back restores what it showed.
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
      {children}
    </div>
  );
}
