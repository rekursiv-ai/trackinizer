// The back of the Chat column: a terminal on one of the user's machines, with a bar saying how to start a
// session there. Chat flips to it from its header and back from this one's, in the same place.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Suspense, useState } from "react";
import { confirmTerminalMachine, listTerminalMachines, type TerminalMachine } from "../api/terminals";
import type { WorkspaceState } from "../api/workspaces";
import { listWorkstreams } from "../api/workstreams";
import { lazyView } from "../router/lazy";
import { Icon } from "../ui/icons";
import { useBarDismissed } from "./terminalFlip";
import "./Terminal.css";

// xterm is 89 KB gzip: it loads with the first flip, not with the app.
const TerminalView = lazyView(() => import("./Terminal"), (module) => module.TerminalView);

/** The command that enrolls the machine the user is on. */
const CONNECT = "trax machine connect";

type Props = {
  readonly workspace: WorkspaceState;
  readonly shown: boolean;
  readonly onFlipBack: () => void;
};

/** The terminal side of `workspace`'s Chat. */
export function ChatTerminalSide({ workspace, shown, onFlipBack }: Props) {
  const machines = useQuery({ queryKey: ["terminal-machines", workspace.id], queryFn: ({ signal }) => listTerminalMachines(workspace.id, { signal }),
    enabled: shown, staleTime: 5_000 });
  const workstreams = useQuery({ queryKey: ["workstreams"], queryFn: ({ signal }) => listWorkstreams({}, { signal }), enabled: shown, staleTime: 2_000 });
  const [dismissed, dismiss] = useBarDismissed();
  const [chosen, setChosen] = useState<string | null>(null);
  const [copied, setCopied] = useState<"copied" | "refused" | null>(null);
  const queryClient = useQueryClient();
  const [allowing, setAllowing] = useState<string | null>(null);
  const [allowProblem, setAllowProblem] = useState<string | null>(null);
  // Newest first, as the server lists them. A machine enrolled with a key is offered only once its owner allows it.
  const ready = (machines.data?.machines ?? []).filter((each) => each.terminal_ready && each.confirmed);
  const unconfirmed = (machines.data?.machines ?? []).filter((each) => !each.confirmed);
  const live = machines.data?.live ?? null;
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
    <header className="chat-head">
      <strong>Terminal</strong>
      <div className="chat-head-actions" role="toolbar" aria-label="Terminal controls">
        <button className="btn ghost" type="button" aria-label="Back to chat" title="Flip back to chat" onClick={onFlipBack}><Icon name="chat" /></button>
      </div>
    </header>
    {machines.data && unconfirmed.map((each) => <div key={each.name} className="chat-terminal-bar" role="group" aria-label={`Allow ${each.name}`}>
      <span>New machine <strong>{each.name}</strong> wants terminals.</span>
      <button type="button" className="btn" disabled={allowing !== null} onClick={() => void allow(each.name)}>Allow</button>
      {allowProblem && <span role="alert">{allowProblem}</span>}
    </div>)}
    {!dismissed && machines.data && <div className="chat-terminal-bar" role="note">
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
    </div>}
    {machines.isError && !machines.data && <p role="alert" className="chat-error">Could not list your machines.</p>}
    {machines.data && <Suspense fallback={<div className="visual-loading" aria-busy="true">Loading terminal…</div>}>
      <TerminalView machine={machine?.name ?? null} workspace={workspace.id} resume={live} shown={shown} />
    </Suspense>}
  </section>;
}
