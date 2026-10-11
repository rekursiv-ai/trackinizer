import { useChatStyle } from "../chatStyle";
import "./chatTerminal.css";

/**
 * The shell prompt before a user's line in the Terminal chat style,
 * `handle@host:[station]$ `: the local part of the author's email, the first
 * label of the host the page is served from (the whole address of an IPv4 one),
 * and the station's name where a shell shows its git branch. Nothing in the
 * Default style.
 */
export function ChatPrompt({ author, station }: { readonly author: string; readonly station: string }) {
  if (useChatStyle() !== "terminal") return null;
  return <span className="chat-prompt"><strong>{author.split("@")[0]}@{shortHost(location.hostname)}</strong>:[<strong className="chat-prompt-station">{station}</strong>]$ </span>;
}

/** The host's first label (`alpha` for `alpha.example.com`), or a whole IPv4 address. */
function shortHost(hostname: string): string {
  return /^\d+(\.\d+){3}$/.test(hostname) ? hostname : hostname.split(".")[0]!;
}
