import { APP } from "../appName";
import { topicName, useTopic } from "../graph/topic";
import type { Route } from "../router/route";
import { useRouter } from "../router/router";
import { Icon } from "../ui/icons";
import { setChatHidden, useChatColumn } from "../visuals/chatColumnState";
import "./chrome.css";

/**
 * The app's five places, in a rail down the left, and the settings under them.
 *
 * Each is a route the app already serves, or Chat's column: the rail moves the
 * page, it does not restyle it. So a place is lit when the page under it is the
 * one the address bar holds, and Chat is lit while its column is open beside
 * whatever page is.
 */
const PLACES: readonly {
  readonly name: string;
  readonly icon: any;
  readonly to: Route;
  readonly title: string;
  /** Whether this place opens on what the canvas is about, when the canvas is about something. */
  readonly onTopic?: true;
}[] = [
  { name: "Chat", icon: "chat", to: { name: "blocks" }, title: "Chat and the active canvas instrument" },
  { name: "Graph", icon: "graph", to: { name: "graph" }, onTopic: true, title: "The newest records and the wires between them" },
  { name: "Files", icon: "folder", to: { name: "list", kind: "CodeChange" }, title: "Code changes, source files, and research artifacts" },
  { name: "Runs", icon: "terminal", to: { name: "console" }, title: "The runs across sessions, and a line to message them" },
  { name: "Evidence", icon: "book", to: { name: "list", kind: "Issue" }, title: "What was recorded, and what it bears on" },
];

export function Rail() {
  const { route, navigate } = useRouter();
  const { hidden } = useChatColumn();
  const topic = useTopic();
  return (
    <nav className="rail" aria-label={`${APP.name} places`}>
      {PLACES.map((place) => {
        const isChat = place.name === "Chat";
        const lit = isChat
          ? route.name === "blocks" || (!hidden && route.name !== "graph" && route.name !== "console" && route.name !== "list")
          : matches(route, place.to);
        // Graph opens lit on what the canvas is about. A focus lights and does not
        // filter, so this takes nothing away from the bare view it replaces.
        const to = place.onTopic && topic ? { name: "graph", focus: { ref: topic, hops: 2 } } as const : place.to;
        return (
          <button key={place.name} type="button" className={lit ? "on" : undefined} aria-current={lit ? "page" : undefined}
            title={place.onTopic && topic ? `${place.title}, opened on ${topicName(topic)}` : place.title}
            onClick={() => {
              if (isChat) {
                setChatHidden(false);
                navigate({ name: "blocks" });
              } else {
                navigate(to);
              }
            }}>
            <Icon name={place.icon} size={21} />
            <span>{place.name}</span>
          </button>
        );
      })}
      <button type="button" className="rail-settings" aria-label="Settings" title="Settings"
        onClick={() => navigate({ name: "settings" })}>
        <Icon name="sliders" size={19} />
      </button>
    </nav>
  );
}

/** Whether the page `route` is the one `to` opens; a list is its own place, a record is the list it came from. */
function matches(route: Route, to: Route): boolean {
  if (route.name !== to.name) return false;
  if (route.name === "list" && to.name === "list") return route.kind === to.kind;
  return true;
}
