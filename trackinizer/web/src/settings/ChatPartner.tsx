import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "../api/client";
import { newUuid } from "../api/idempotency";
import { applyWorkspaceOperation, createDefaultWorkspace, getWorkspace, type WorkspaceState } from "../api/workspaces";
import { acceptWorkspace } from "../app/canvasStream";
import { useOnline } from "../ui/bars";
import { Icon } from "../ui/icons";
import { ReadError, Section } from "./account";
import "./settings.css";

/**
 * Whose session the canvas's Chat talks to: the server's shared assistant, or
 * the user's own `trax helper`. The choice is stored on the user's default
 * canvas, as a `partner` operation at the canvas's revision; the canvas and its
 * Chat show it from the same cache entry the live stream keeps.
 */
export function ChatPartnerSection() {
  const queryClient = useQueryClient();
  const online = useOnline();
  const canvas = useQuery({
    queryKey: ["workspace", "default"],
    queryFn: ({ signal }) => createDefaultWorkspace({ signal }),
    staleTime: Infinity,
    retry: false,
  });
  const choose = useMutation({
    mutationFn: (choice: "shared" | "local") => applyWorkspaceOperation(
      canvas.data!.id, canvas.data!.revision, { kind: "partner", choice }, newUuid(),
    ),
    onSuccess: (state) => acceptWorkspace(queryClient, state),
    // A stale revision is the usual refusal: read the canvas again so a second try can land.
    onError: async (error) => {
      if (error instanceof ApiError && error.status === 409) acceptWorkspace(queryClient, await getWorkspace(canvas.data!.id));
    },
  });
  const state: WorkspaceState | undefined = canvas.data;
  const picked = state?.partner_choice === "local" ? "local" : "shared";
  const idle = !!state && online && !choose.isPending;
  return (
    <Section title="Chat partner">
      <ReadError read={canvas} />
      <div className="st-choices" role="radiogroup" aria-label="Chat partner">
        <label className="st-choice">
          <input type="radio" name="chat-partner" checked={picked === "shared"} disabled={!idle}
            onChange={() => choose.mutate("shared")} />
          <span>Shared assistant</span>
          <span className="st-choice-note">{state ? state.assistant ?? "not set up on this server" : "Loading…"}</span>
        </label>
        <label className="st-choice">
          <input type="radio" name="chat-partner" checked={picked === "local"} disabled={!idle}
            onChange={() => choose.mutate("local")} />
          <span>My local helper</span>
          <span className="st-choice-note">Your own Claude or Codex, run by <code>trax helper</code></span>
        </label>
      </div>
      {choose.isPending && <span className="w-status" role="status">Saving…</span>}
      {choose.isError && <p className="form-err" role="alert">Could not change the Chat partner.</p>}
      {picked === "local" && <HelperCommands tokens="below" />}
    </Section>
  );
}

/**
 * The two commands that start a local helper against this server, each with
 * Copy. The token is the user's own, from API tokens: a link to Settings in
 * Chat, the section below it in Settings.
 */
export function HelperCommands({ tokens }: { readonly tokens: "link" | "below" }) {
  const setup = `uv tool install trackinizer && trax profile url to ${location.origin} && trax profile token to <TOKEN>`;
  const commands = [setup, "trax helper claude"];
  return (
    <div className="helper-commands">
      {commands.map((command) => <Command key={command} command={command} />)}
      <p className="st-note">
        Replace <code>&lt;TOKEN&gt;</code> with a token from {tokens === "link"
          ? <a href="#/settings">Settings → API tokens</a> : "API tokens below"}.
      </p>
    </div>
  );
}

/**
 * One command and its Copy. Chat shows it where no toast is mounted, so the
 * outcome is said beside the button.
 */
function Command({ command }: { readonly command: string }) {
  const [outcome, setOutcome] = useState<"copied" | "refused" | null>(null);
  const copy = () => Promise.resolve().then(() => navigator.clipboard.writeText(command)).then(() => setOutcome("copied"), () => setOutcome("refused"));
  return <div className="helper-command">
    <code>{command}</code>
    <button type="button" className="btn ghost" onClick={() => void copy()}>
      <Icon name="copy" size={13} />
      Copy
    </button>
    {outcome && <span className="w-status" role="status">{outcome === "copied" ? "Copied" : "Could not copy"}</span>}
  </div>;
}
