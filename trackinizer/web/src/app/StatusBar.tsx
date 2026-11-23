import { runStateOf } from "../api/runState";
import { APP } from "../appName";
import { useRouter } from "../router/router";
import { useStation } from "../stations/useStation";
import "./chrome.css";

/** What the page under the rail is called, in the one word the rail uses for it. */
const PLACES: Record<string, string> = {
  graph: "Graph", console: "Runs", blocks: "Evidence", activity: "Activity",
  list: "List", ref: "Record", lookup: "Record", search: "Search", settings: "Settings", admin: "Admin", new: "New",
};

/**
 * The line under everything: what you are in, what page the rail is lit on, and what the
 * chat's run says it is doing. It reads off the same records the rest of the app draws, so
 * it can only say what is actually held; nothing here is a status the web invents.
 */
export function StatusBar() {
  const { route } = useRouter();
  const { data: station } = useStation();
  const partner = station?.partner ?? null;
  const state = runStateOf(partner);
  const off = !partner || partner.status !== "live";
  return (
    <footer className="statusbar" aria-label="Workspace status">
      {/* The chat's name while there is one, and the app's own until the read lands: a name
          invented here would read as a workspace that exists. */}
      <span className="statusbar-name">{station?.name ?? APP.name}</span>
      <span className="statusbar-rule" aria-hidden="true" />
      <span>{PLACES[route.name] ?? "Page"}</span>
      <span className="right">{off || !state ? "No run attached" : `Run ${state.replace(/_/g, " ")}`}</span>
    </footer>
  );
}
