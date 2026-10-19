// The back of the Chat column: a terminal on one of the user's machines, with a bar saying how to start a
// session there, and the help behind it (the menu's "Terminal help" reopens it). Chat's header, passed in, tops this side too: it flips back.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, Suspense, useState } from "react";
import { confirmTerminalMachine, listTerminalMachines, type TerminalMachine } from "../api/terminals";
import type { WorkspaceState } from "../api/workspaces";
import { listWorkstreams } from "../api/workstreams";
import { lazyView } from "../router/lazy";
import { Command, setupCommand } from "../settings/ChatPartner";
import { useBarDismissed } from "./terminalFlip";
import "./Terminal.css";

// xterm is 89 KB gzip: it loads with the first flip, not with the app.
const TerminalView = lazyView(() => import("./Terminal"), (module) => module.TerminalView);

/** The command that enrolls the machine the user is on. */
const CONNECT = "trax machine connect";

/** The help's sections: what each is for, and the commands it gives, in the order a user needs them. */
function TerminalHelp({ open }: { readonly open: boolean }) {
  return <details className="chat-terminal-help" open={open}>
    <summary>More help</summary>
    <section aria-label="Connect this machine">
      <h4>Connect this machine</h4>
      <p className="muted">Keep <code>{CONNECT}</code> running, in tmux or as a user service. First time on a computer? Install trax and point it at this server, then connect.</p>
      <Command command={setupCommand()} />
      <p className="muted">Replace <code>&lt;TOKEN&gt;</code> with a token from <a href="#/settings">Settings → API tokens</a>.</p>
    </section>
    <section aria-label="Over ssh">
      <h4>Over ssh</h4>
      <Command command="ssh HOST" />
      <p className="muted">The station travels with you, so <code>trax run claude</code> there is filed into this station. Trax must be installed and set up on that host as above.</p>
    </section>
    <section aria-label="Detached agents">
      <h4>Detached agents</h4>
      <Command command="trax run --detach --name NAME claude" />
      <p className="muted">Use <code>codex</code> for Codex. A detached run survives closing this tab and the terminal's 60-minute idle close.</p>
      <Command command="trax run ls" />
      <Command command="trax run attach NAME" />
      <p className="muted">Drives it from this terminal; Ctrl-\ detaches again.</p>
      <Command command="trax run log NAME --follow" />
      <Command command="trax run stop NAME" />
    </section>
    <section aria-label="Reconnecting">
      <h4>Reconnecting</h4>
      <p className="muted">A reload re-attaches this view's shells. A shell left alone for 60 minutes is closed, so long work belongs in tmux or a detached run.</p>
    </section>
    <section aria-label="Typing feels slow?">
      <h4>Typing feels slow?</h4>
      <p className="muted">Let the terminal talk to your browser directly: on the machine, add the direct-link library to trax, then restart <code>{CONNECT}</code>.</p>
      <Command command="uv tool install --with aiortc trackinizer" />
      <p className="muted">Installed trax from a local checkout? Use that checkout's path in place of <code>trackinizer</code>. A "Direct link" badge on the terminal shows it worked; if the link drops, the terminal carries on through the server.</p>
    </section>
  </details>;
}

/** What this side tags its terminals with, so a reload re-attaches this side to its own. */
const SURFACE = "chat";

type Props = {
  readonly workspace: WorkspaceState;
  readonly shown: boolean;
  /** Chat's header, in its flipped form. */
  readonly header: ReactNode;
};

/** The terminal side of `workspace`'s Chat. */
export function ChatTerminalSide({ workspace, shown, header }: Props) {
  const machines = useQuery({ queryKey: ["terminal-machines", workspace.id], queryFn: ({ signal }) => listTerminalMachines(workspace.id, { signal }),
    enabled: shown, staleTime: 5_000 });
  const workstreams = useQuery({ queryKey: ["workstreams"], queryFn: ({ signal }) => listWorkstreams({}, { signal }), enabled: shown, staleTime: 2_000 });
  const [dismissed, dismiss, , helpOpen] = useBarDismissed();
  const [chosen, setChosen] = useState<string | null>(null);
  const [copied, setCopied] = useState<"copied" | "refused" | null>(null);
  const queryClient = useQueryClient();
  const [allowing, setAllowing] = useState<string | null>(null);
  const [allowProblem, setAllowProblem] = useState<string | null>(null);
  // Newest first, as the server lists them. A machine enrolled with a key is offered only once its owner allows it.
  const ready = (machines.data?.machines ?? []).filter((each) => each.terminal_ready && each.confirmed);
  const unconfirmed = (machines.data?.machines ?? []).filter((each) => !each.confirmed);
  // The newest terminal this side opened; the tile's own are not its to re-attach.
  const live = machines.data?.terminals?.filter((each) => each.surface === SURFACE).at(-1) ?? null;
  const machine = ready.find((each) => each.name === chosen) ?? ready.find((each) => each.name === live?.machine) ?? ready[0] ?? null;
  const group = workstreams.data?.find((each) => each.id === workspace.workstream_id) ?? workstreams.data?.find((each) => each.scratch);

  const allow = async (name: string) => {
    setAllowing(name);
    setAllowProblem(null);
    try {
      await confirmTerminalMachine(name);
      await queryClient.invalidateQueries({ queryKey: ["terminal-machines", workspace.id] });
    } catch {
      setAllowProblem(`Could not allow ${name}.`);
    } finally {
      setAllowing(null);
    }
  };
  const copy = () => Promise.resolve().then(() => navigator.clipboard.writeText(CONNECT)).then(() => setCopied("copied"), () => setCopied("refused"));

  return <section className="chat-terminal-side" aria-label="Terminal" hidden={!shown}>
    {header}
    {machines.data && unconfirmed.map((each) => <div key={each.name} className="chat-terminal-bar" role="group" aria-label={`Allow ${each.name}`}>
      <span>New machine <strong>{each.name}</strong> wants terminals.</span>
      <button type="button" className="btn" disabled={allowing !== null} onClick={() => void allow(each.name)}>Allow</button>
      {allowProblem && <span role="alert">{allowProblem}</span>}
    </div>)}
    {!dismissed && machines.data && <>
    <div className="chat-terminal-bar" role="note">
      {machine ? <>
        <span>Start a session here with <code>trax run claude</code> on{" "}
          {ready.length > 1
            ? <select aria-label="Machine" value={machine.name} onChange={(event) => setChosen(event.target.value)}>
              {ready.map((each: TerminalMachine) => <option key={each.name} value={each.name}>{each.name}</option>)}
            </select>
            : <strong>{machine.name}</strong>}
          , or <code>ssh &lt;host&gt;</code> first. Sessions land in <strong>{group?.title ?? "Scratch"}</strong>.</span>
      </> : unconfirmed.length > 0 ? <span>Allow a machine above to start a terminal on it.</span> : <>
        <span>Connect this laptop: <code>{CONNECT}</code></span>
        <button type="button" className="btn ghost" onClick={() => void copy()}>{copied === "copied" ? "Copied" : copied === "refused" ? "Copy refused" : "Copy"}</button>
        <span className="muted">Enrolls this machine and keeps it connected, so a terminal can open on it.</span>
      </>}
      <button type="button" className="btn ghost chat-terminal-dismiss" aria-label="Dismiss" onClick={dismiss}>×</button>
    </div>
    <TerminalHelp open={helpOpen} />
    </>}
    {machines.isError && !machines.data && <p role="alert" className="chat-error">Could not list your machines.</p>}
    {machines.data && <Suspense fallback={<div className="visual-loading" aria-busy="true">Loading terminal…</div>}>
      <TerminalView machine={machine?.name ?? null} workspace={workspace.id} surface={SURFACE} resume={live} shown={shown} />
    </Suspense>}
  </section>;
}
