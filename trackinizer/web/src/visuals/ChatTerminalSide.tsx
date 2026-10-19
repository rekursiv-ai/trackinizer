// The back of the Chat column: a terminal on one of the user's machines, with a bar saying how to start a
// session there, and the help behind it (the menu's "Terminal help" reopens it). Chat's header, passed in, tops this side too: it flips back.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, Suspense, useRef, useState } from "react";
import { closeTerminal, confirmTerminalMachine, listTerminalMachines } from "../api/terminals";
import type { WorkspaceState } from "../api/workspaces";
import { listWorkstreams } from "../api/workstreams";
import { lazyView } from "../router/lazy";
import { Command, setupCommand, TRAX_SOURCE } from "../settings/ChatPartner";
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
      <p className="muted">A reload re-attaches this view's shells. A shell with no browser attached for a minute ends, and one left alone for 60 minutes is closed, so long work belongs in tmux or a detached run.</p>
    </section>
    <section aria-label="Typing feels slow?">
      <h4>Typing feels slow?</h4>
      <p className="muted">Let the terminal talk to your browser directly: on the machine, add the direct-link library to trax, then restart <code>{CONNECT}</code>.</p>
      <Command command={`uv tool install --with aiortc ${TRAX_SOURCE}`} />
      <p className="muted">Installed trax from a local checkout? Use that checkout's path in place of <code>{TRAX_SOURCE}</code>. A "Direct link" badge on the terminal shows it worked; if the link drops, the terminal carries on through the server.</p>
    </section>
  </details>;
}

/** What this side tags its terminals with, so a reload re-attaches this side to its own. */
const SURFACE = "chat";

/** What the flipped header shows of this side: the terminal in view, the ones to switch to, and how to start or stop one. */
export type TerminalHead = {
  /** "gx10 2": the machine and which of its terminals this is; "Terminal" before one has started. */
  readonly label: string;
  readonly terminals: readonly { readonly id: string; readonly label: string }[];
  readonly selected: string | null;
  /** The ready machines a terminal can start on. */
  readonly machines: readonly string[];
  readonly select: (id: string) => void;
  readonly create: (machine: string) => void;
  /** Stops the terminal in view; none while there is not one. */
  readonly close: (() => void) | null;
};

/** The terminal in view: a key that stays while it runs, the machine, the live terminal it re-attaches to, and a start request. */
type View = { readonly key: string; readonly machine: string | null; readonly id: string | null; readonly auto: string | null };

type Props = {
  readonly workspace: WorkspaceState;
  readonly shown: boolean;
  /** Chat's header, in its flipped form, given what this side shows. */
  readonly header: (terminal: TerminalHead) => ReactNode;
};

/** The terminal side of `workspace`'s Chat. */
export function ChatTerminalSide({ workspace, shown, header }: Props) {
  const machines = useQuery({ queryKey: ["terminal-machines", workspace.id], queryFn: ({ signal }) => listTerminalMachines(workspace.id, { signal }),
    enabled: shown, staleTime: 5_000 });
  const workstreams = useQuery({ queryKey: ["workstreams"], queryFn: ({ signal }) => listWorkstreams({}, { signal }), enabled: shown, staleTime: 2_000 });
  const [dismissed, dismiss, , helpOpen] = useBarDismissed();
  const [chosen, setChosen] = useState<View | null>(null);
  const count = useRef(0);
  const [copied, setCopied] = useState<"copied" | "refused" | null>(null);
  const queryClient = useQueryClient();
  const [allowing, setAllowing] = useState<string | null>(null);
  const [allowProblem, setAllowProblem] = useState<string | null>(null);
  // Newest first, as the server lists them. A machine enrolled with a key is offered only once its owner allows it.
  const ready = (machines.data?.machines ?? []).filter((each) => each.terminal_ready && each.confirmed);
  const unconfirmed = (machines.data?.machines ?? []).filter((each) => !each.confirmed);
  // The newest terminal this side opened; the tile's own are not its to re-attach.
  const mine = machines.data?.terminals?.filter((each) => each.surface === SURFACE) ?? [];
  const live = mine.at(-1) ?? null;
  // Until the user picks one, the newest this side opened is in view.
  const view: View = chosen ?? { key: "newest", machine: live?.machine ?? null, id: live?.terminal_id ?? null, auto: null };
  const machine = ready.find((each) => each.name === view.machine) ?? ready[0] ?? null;
  const resume = view.id ? mine.find((each) => each.terminal_id === view.id) ?? { terminal_id: view.id } : null;
  const labels = mine.map((each, index) => ({ id: each.terminal_id, label: `${each.machine} ${mine.slice(0, index + 1).filter((other) => other.machine === each.machine).length}` }));
  const label = labels.find((each) => each.id === view.id)?.label ?? "Terminal";
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
  const onOpened = (opened: { readonly terminal_id: string }) => {
    setChosen({ ...view, machine: machine?.name ?? null, id: opened.terminal_id });
    void queryClient.invalidateQueries({ queryKey: ["terminal-machines", workspace.id] });
  };
  const fresh = (name: string | null): View => ({ key: `new-${count.current++}`, machine: name, id: null, auto: null });
  const close = async () => {
    if (view.id) await closeTerminal(view.id).catch(() => undefined);
    setChosen(fresh(machine?.name ?? null));
    await queryClient.invalidateQueries({ queryKey: ["terminal-machines", workspace.id] });
  };
  const head: TerminalHead = {
    label, terminals: labels, selected: view.id, machines: ready.map((each) => each.name),
    select: (id) => setChosen({ key: id, machine: mine.find((each) => each.terminal_id === id)?.machine ?? null, id, auto: null }),
    create: (name) => { const next = fresh(name); setChosen({ ...next, auto: `${workspace.id}:${next.key}` }); },
    close: view.id ? () => void close() : null,
  };
  const copy = () => Promise.resolve().then(() => navigator.clipboard.writeText(CONNECT)).then(() => setCopied("copied"), () => setCopied("refused"));

  return <section className="chat-terminal-side" aria-label="Terminal" hidden={!shown}>
    {header(head)}
    {machines.data && unconfirmed.map((each) => <div key={each.name} className="chat-terminal-bar" role="group" aria-label={`Allow ${each.name}`}>
      <span>New machine <strong>{each.name}</strong> wants terminals.</span>
      <button type="button" className="btn" disabled={allowing !== null} onClick={() => void allow(each.name)}>Allow</button>
      {allowProblem && <span role="alert">{allowProblem}</span>}
    </div>)}
    {!dismissed && machines.data && <>
    <div className="chat-terminal-bar" role="note">
      {machine ? <>
        <span>Start a session here with <code>trax run claude</code> on <strong>{machine.name}</strong>, or <code>ssh &lt;host&gt;</code> first. Sessions land in <strong>{group?.title ?? "Scratch"}</strong>.</span>
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
      <TerminalView key={view.key} machine={machine?.name ?? null} workspace={workspace.id} surface={SURFACE} resume={resume} shown={shown}
        autoStart={view.auto} onOpened={onOpened} />
    </Suspense>}
  </section>;
}
