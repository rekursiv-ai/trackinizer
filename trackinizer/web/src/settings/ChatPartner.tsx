import { useState } from "react";
import { useStation } from "../stations/useStation";
import { Icon } from "../ui/icons";
import { KeyValue, ReadError, Section } from "./account";
import "./settings.css";

/**
 * Whose session the canvas's Chat talks to: the server's shared assistant, the
 * only partner there is. The canvas and its Chat show it from the same cache
 * entry the live stream keeps.
 */
export function ChatPartnerSection() {
  const canvas = useStation();
  const state = canvas.data;
  return (
    <Section title="Chat partner">
      <ReadError read={canvas} />
      <KeyValue label="Shared assistant">{state ? state.assistant ?? "not set up on this server" : "Loading…"}</KeyValue>
    </Section>
  );
}

// The latest PyPI release lacks what the terminal side needs, so trax installs from the
// public repository's main branch until the next release.
/** Where `uv tool install` takes trax from. */
export const TRAX_SOURCE = "git+https://github.com/rekursiv-ai/trackinizer";

/** Install trax and point it at this server: the page's own origin, so every deployment shows its own. */
export function setupCommand(): string {
  return `uv tool install ${TRAX_SOURCE} && trax profile url to ${location.origin} && trax profile token to <TOKEN>`;
}

/**
 * One command and its Copy. Shown where no toast is mounted, so the outcome is
 * said beside the button.
 */
export function Command({ command }: { readonly command: string }) {
  const [outcome, setOutcome] = useState<"copied" | "refused" | null>(null);
  const copy = () => Promise.resolve().then(() => navigator.clipboard.writeText(command)).then(() => setOutcome("copied"), () => setOutcome("refused"));
  return <div className="copy-command">
    <code>{command}</code>
    <button type="button" className="btn ghost" onClick={() => void copy()}>
      <Icon name="copy" size={13} />
      Copy
    </button>
    {outcome && <span className="w-status" role="status">{outcome === "copied" ? "Copied" : "Could not copy"}</span>}
  </div>;
}
