import { Icon } from "../ui/icons";

/** What the bar does; the canvas does each. */
export type CanvasBarActions = {
  readonly back: () => void;
  readonly forward: () => void;
  readonly home: () => void;
};

/**
 * The canvas's top row, as a browser's: Back and Forward step through what the
 * open tab showed, Home shows what the user's `home` setting names (the graph,
 * or nothing), and the station and tab on show stand beside them. The tabs
 * themselves stand in the app's top bar, in the station's group. Everything
 * waits while a step is on its way (`moving`).
 */
export function CanvasBar({ station, tab, back, forward, moving, home, actions }: {
  readonly station: string;
  readonly tab: string;
  readonly back: boolean;
  readonly forward: boolean;
  readonly moving: boolean;
  readonly home: "graph" | "empty";
  readonly actions: CanvasBarActions;
}) {
  return (
    <div className="canvas-bar" role="toolbar" aria-label="Canvas">
      <button className="canvas-bar-button" type="button" aria-label="Back" title="Back to what this tab showed before"
        disabled={!back || moving} onClick={actions.back}><Icon name="arrowLeft" size={15} /></button>
      <button className="canvas-bar-button" type="button" aria-label="Forward" title="Forward again"
        disabled={!forward || moving} onClick={actions.forward}><Icon name="arrowRight" size={15} /></button>
      <button className="canvas-bar-button" type="button" aria-label="Home"
        title={home === "empty" ? "Home: an empty canvas (Settings, home)" : "Home: the graph (Settings, home)"}
        disabled={moving} onClick={actions.home}><Icon name="home" size={15} /></button>
      <div className="canvas-crumbs" role="group" aria-label="On show">
        <span className="canvas-crumb-station">{station}</span>
        <span aria-hidden="true">/</span>
        <span className="canvas-crumb-tab">{tab}</span>
      </div>
    </div>
  );
}
