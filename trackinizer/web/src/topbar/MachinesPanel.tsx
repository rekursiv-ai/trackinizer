// The Machines menu's body, a chunk of its own: the setup commands and the terminal
// API it reads cost first-load JavaScript that only the opened menu needs.
import { type UseQueryResult, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { listCompute } from "../api/compute";
import { confirmTerminalMachine, type TerminalMachines } from "../api/terminals";
import { APP } from "../appName";
import { formatRoute } from "../router/route";
import { Command, setupCommand } from "../settings/ChatPartner";

/** The command that enrolls the computer it runs on and keeps it connected. */
const CONNECT = "trax machine connect";

/** How each machine status reads; any other shows as the server says it. */
const STATUS: Readonly<Record<string, string>> = { online: "Online", offline: "Offline", never: "Never connected", revoked: "Revoked" };

/**
 * Your machines, how to connect one, and what this server provides. `read` is the
 * machines' read, or null where no machine connects: a server without sign-in
 * (`signedIn` false), or a viewer.
 */
export function MachinesPanel({ read, signedIn, onNavigate }: {
  readonly read: UseQueryResult<TerminalMachines> | null;
  readonly signedIn: boolean;
  readonly onNavigate: () => void;
}) {
  return <>
    <p className="topbar-pop-label">Your machines</p>
    {read
      ? <Machines read={read} />
      : <p className="machines-note">{signedIn
        ? "A viewer connects no machine: it takes the writer role."
        : "This server runs without sign-in, so no machine connects to it."}</p>}
    {read && <ConnectSteps none={read.data?.machines.length === 0} onNavigate={onNavigate} />}
    <Provided />
  </>;
}

/** The person's machines, each with its status, and Allow on one enrolled with a token. */
function Machines({ read }: { readonly read: UseQueryResult<TerminalMachines> }) {
  const queryClient = useQueryClient();
  const [allowing, setAllowing] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const allow = async (name: string) => {
    setAllowing(name);
    setProblem(null);
    try {
      await confirmTerminalMachine(name);
      // Every read of the machines, the terminal's for each station too.
      await queryClient.invalidateQueries({ queryKey: ["terminal-machines"] });
    } catch {
      setProblem(`Could not allow ${name}.`);
    } finally {
      setAllowing(null);
    }
  };
  if (!read.data) return <p className="machines-note">{read.isError ? "Could not list your machines." : "Loading…"}</p>;
  if (read.data.machines.length === 0) return <p className="machines-note">No machine connected yet.</p>;
  return <>
    <ul className="machines-list">
      {read.data.machines.map((each) => <li key={each.name} className="machines-row">
        <span className={`machines-status${each.status === "online" ? " is-online" : ""}`} aria-hidden="true" />
        <span className="machines-name">{each.name}</span>
        {each.confirmed
          ? <span className="machines-state">{STATUS[each.status] ?? each.status}</span>
          : <>
            <span className="machines-state">Waiting for you to allow it</span>
            <button type="button" className="btn" disabled={allowing !== null} onClick={() => void allow(each.name)}>Allow</button>
          </>}
      </li>)}
    </ul>
    {problem && <p className="machines-note form-err" role="alert">{problem}</p>}
  </>;
}

/** How to connect a computer, open while the person has none and folded once they have one. */
function ConnectSteps({ none, onNavigate }: { readonly none: boolean; readonly onNavigate: () => void }) {
  // Keyed on `none`, so the steps fold as the first machine arrives and the reader's own toggle holds otherwise.
  return <details key={String(none)} className="machines-connect" open={none}>
    <summary>Connect a machine</summary>
    <ol>
      <li>Make an API token with the writer role in{" "}
        <a href={formatRoute({ name: "settings" })} onClick={onNavigate}>Settings → API tokens</a>.</li>
      <li>On the computer, install trax and point it at this server, with your token for <code>&lt;TOKEN&gt;</code>:
        <Command command={setupCommand()} /></li>
      <li>Keep it connected, in tmux or as a user service:
        <Command command={CONNECT} /></li>
      <li>Allow it here once it shows up above.</li>
    </ol>
  </details>;
}

/** What this server provides, as its deployment notes it; nothing when it notes nothing. */
function Provided() {
  const notes = useQuery({ queryKey: ["compute"], queryFn: ({ signal }) => listCompute({ signal }), staleTime: Infinity });
  if (!notes.data?.length) return null;
  return <section className="machines-provided" aria-label={`Provided by ${APP.name}`}>
    <p className="topbar-pop-label">Provided by {APP.name}</p>
    <ul>
      {notes.data.map((each) => <li key={each.name}><strong>{each.name}</strong><span>{each.about}</span></li>)}
    </ul>
  </section>;
}
