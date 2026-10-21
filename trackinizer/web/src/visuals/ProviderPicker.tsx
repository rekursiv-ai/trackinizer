import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { type BalanceBody, balanceQuery, creditLine, lowCredit } from "../api/billing";
import { ApiError } from "../api/client";
import { newUuid } from "../api/idempotency";
import { PROVIDERS, type Provider, type ProviderStatus, partnerLabel, profileChatProvider, providerName, providerStatuses, setChatProviderPreference, stationProvider } from "../api/providers";
import { confirmTerminalMachine, listTerminalMachines, openTerminal, type RunLaunch, type TerminalMachine } from "../api/terminals";
import { chooseProvider, getWorkspace, type WorkspaceState } from "../api/workspaces";
import { useProfile } from "../app/boot";
import { Command, setupCommand } from "../settings/ChatPartner";
import { acceptWorkspace } from "../stations/cache";
import "./ProviderPicker.css";

/** The default for new chats, as the query cache holds it for this page. */
const DEFAULT_KEY = ["chat-provider", "default"] as const;
/** How often a started run is asked whether it is live yet. */
const START_POLL_MS = 2_000;
/** How long a started run may take before Chat says it did not start. */
const START_GIVE_UP_MS = 90_000;
/** A run's terminal size before any screen has one: the size a terminal opens at. */
const START_ROWS = 24;
const START_COLS = 80;

/** The folder a machine's last run started in, as this browser kept it; "" (the home directory) when none. */
function lastFolder(machine: string): string {
  try {
    return localStorage.getItem(`chat-run-folder:${machine}`) ?? "";
  } catch {
    return "";
  }
}

/** Keep the folder a run on `machine` starts in, so the next one starts there; an empty folder forgets it. */
function keepFolder(machine: string, folder: string): void {
  try {
    if (folder) localStorage.setItem(`chat-run-folder:${machine}`, folder);
    else localStorage.removeItem(`chat-run-folder:${machine}`);
  } catch {
    // A browser that refuses storage starts the next run in the home directory.
  }
}

/** The provider new chats start on: the user's preference, as the profile gave it and as this page last set it. */
export function useChatDefault(): { readonly provider: Provider; readonly set: (provider: Provider) => Promise<void> } {
  const profile = useProfile();
  const queryClient = useQueryClient();
  // The profile holds the preference, so the page never waits for a read of it; a choice is kept as it is made.
  const { data: provider } = useQuery({ queryKey: DEFAULT_KEY, queryFn: () => profileChatProvider(profile), initialData: () => profileChatProvider(profile),
    staleTime: Infinity });
  return {
    provider,
    set: async (provider) => {
      const previous = queryClient.getQueryData<Provider>(DEFAULT_KEY);
      queryClient.setQueryData(DEFAULT_KEY, provider);
      try {
        await setChatProviderPreference(provider);
      } catch (error) {
        queryClient.setQueryData(DEFAULT_KEY, previous);
        throw error;
      }
    },
  };
}

/** The caller's credit and the server's verdict on it, read once a minute at most; null on a site with no ledger. */
export function useBalance(): BalanceBody | null {
  return useQuery(balanceQuery).data ?? null;
}

/**
 * Whether each provider can take a chat now. The assistant's availability is the server's verdict on the caller, which
 * the site's billing addon answers (a 404 when it has none), so it is read here; a CLI's is the station read's.
 */
export function useProviderStatuses(station: WorkspaceState | null): Record<Provider, ProviderStatus> {
  return providerStatuses(station, useBalance());
}

type Props = {
  /** The station whose chat's provider is shown and chosen. */
  readonly station: WorkspaceState;
};

/**
 * The provider card: the station's chat talks to one of the assistant, claude or codex, and the default for new
 * chats is starred. A CLI that no live run holds offers the caller's machines that have it installed, and starts
 * its run on the one picked.
 */
export function ProviderPicker({ station }: Props) {
  const queryClient = useQueryClient();
  const balance = useBalance();
  const statuses = providerStatuses(station, balance);
  const chatDefault = useChatDefault();
  const [busy, setBusy] = useState<Provider | null>(null);
  const [starting, setStarting] = useState<Provider | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [allowing, setAllowing] = useState<string | null>(null);
  // The folder each machine's run starts in, as typed here; a machine not typed in starts at its last folder.
  const [folders, setFolders] = useState<Readonly<Record<string, string>>>({});
  const current = stationProvider(station);
  const partner = station.partner ?? null;
  const live = partner?.status === "live";
  // The folder a run starts in changes its behavior (its CLAUDE.md, AGENTS.md), so a live run's line names it.
  const label = partnerLabel(partner, providerName(current, station));
  const liveLabel = partner?.cwd && current !== "assistant" ? `${label} · ${partner.cwd}` : label;
  // A CLI's chat with no live run needs a machine to start one on; the assistant's run is the server's.
  const cli: "claude" | "codex" | null = current === "assistant" ? null : current;
  const needsMachine = cli !== null && !live;
  // The chat's session a run re-opens: the station read names it on the CLI's partner, live or offline.
  const session = cli !== null && partner?.provider === cli ? partner.session_id ?? null : null;
  // A CLI is out while no machine of the caller's that has it is allowed terminals, so the machines are listed
  // then too: a machine just connected waits for its owner's Allow here, the one place a browser gives it.
  const machines = useQuery({ queryKey: ["terminal-machines", station.id], queryFn: ({ signal }) => listTerminalMachines(station.id, { signal }),
    enabled: needsMachine || !statuses.claude.ok || !statuses.codex.ok, staleTime: 5_000 });
  const capable = (machines.data?.machines ?? []).filter((each) => each.confirmed && each.terminal_ready && cli !== null && (each.clis ?? []).includes(cli));
  const unconfirmed = (machines.data?.machines ?? []).filter((each) => !each.confirmed);

  // A run that is live has started; until then the station is read again, as the partner is read today.
  useEffect(() => {
    if (live) setStarting(null);
  }, [live]);
  useEffect(() => {
    if (starting === null || live) return;
    const poll = setInterval(() => {
      void getWorkspace(station.id).then((state) => acceptWorkspace(queryClient, state), () => undefined);
    }, START_POLL_MS);
    const giveUp = setTimeout(() => {
      setStarting(null);
      setNotice(`${providerName(starting, station)} did not start. Try again, or pick another machine.`);
    }, START_GIVE_UP_MS);
    return () => {
      clearInterval(poll);
      clearTimeout(giveUp);
    };
  }, [starting, live, station.id, queryClient]);

  async function choose(provider: Provider) {
    if (provider === current || busy !== null) return;
    setNotice(null);
    setBusy(provider);
    try {
      acceptWorkspace(queryClient, await chooseProvider(station.id, station.revision, provider, newUuid()));
      // The choice may have opened the chat's session: the chat's head is read again, so Chat reads its lines.
      void queryClient.invalidateQueries({ queryKey: ["chat", "head", station.conversation_id] });
    } catch (error) {
      setNotice(error instanceof ApiError && error.detail ? error.detail : "Could not choose this provider.");
    } finally {
      setBusy(null);
    }
  }

  async function star(provider: Provider) {
    if (provider === chatDefault.provider) return;
    setNotice(null);
    try {
      await chatDefault.set(provider);
    } catch {
      setNotice("Could not save the default.");
    }
  }

  async function allow(name: string) {
    setNotice(null);
    setAllowing(name);
    try {
      await confirmTerminalMachine(name);
      await queryClient.invalidateQueries({ queryKey: ["terminal-machines", station.id] });
      // The station read says which providers a machine of the caller's can run: allowing one may add a CLI.
      acceptWorkspace(queryClient, await getWorkspace(station.id));
    } catch {
      setNotice(`Could not allow ${name}.`);
    } finally {
      setAllowing(null);
    }
  }

  async function start(machine: TerminalMachine, command: "claude" | "codex", cwd: string) {
    if (session === null) {
      setNotice("This chat has no session yet. Try again in a moment.");
      return;
    }
    setNotice(null);
    setStarting(command);
    const launch: RunLaunch = { command, attach: session, ...(cwd ? { cwd } : {}) };
    try {
      await openTerminal(machine.name, station.id, START_ROWS, START_COLS, null, launch);
      keepFolder(machine.name, cwd);
    } catch (error) {
      setStarting(null);
      setNotice(error instanceof ApiError && error.detail ? error.detail : `Could not start ${command} on ${machine.name}.`);
    }
  }

  return <div className="provider-picker">
    <div className="provider-card" role="radiogroup" aria-label="Chat provider">
      {PROVIDERS.map((provider) => {
        const status = statuses[provider];
        const shown = current === provider;
        return <div key={provider} className={`provider-row${status.ok ? "" : " grayed"}`}>
          <label className="provider-choice">
            <input type="radio" name="chat-provider" value={provider} checked={shown} disabled={!status.ok || busy !== null}
              onChange={() => void choose(provider)} />
            <span className="provider-name">{providerName(provider, station)}</span>
          </label>
          <span className="provider-live">
            {shown && <>
              <span className={`provider-dot${live ? " live" : ""}`} aria-hidden="true" />
              <span className="provider-live-text">{starting === provider ? "starting…" : liveLabel}</span>
            </>}
          </span>
          <button type="button" className="provider-star" aria-pressed={chatDefault.provider === provider} aria-label={`New chats start on ${providerName(provider, station)}`}
            disabled={!status.ok} onClick={() => void star(provider)}>{chatDefault.provider === provider ? "★" : "☆"}</button>
          {provider === "assistant" && balance !== null && <CreditLine balance={balance} />}
          {!status.ok && <p className="provider-reason muted">{reasonText(status)}</p>}
        </div>;
      })}
    </div>
    {unconfirmed.map((machine) => <div key={machine.name} className="provider-allow" role="group" aria-label={`Allow ${machine.name}`}>
      <span>New machine <strong>{machine.name}</strong> wants terminals.</span>
      <button type="button" className="btn" disabled={allowing !== null} onClick={() => void allow(machine.name)}>Allow</button>
    </div>)}
    {needsMachine && starting === null && <div className="provider-machines">
      {machines.isError && !machines.data && <p role="alert" className="provider-notice">Could not list your machines.</p>}
      {cli !== null && capable.map((machine) => {
        const folder = folders[machine.name] ?? lastFolder(machine.name);
        const cwd = folder.trim();
        return <div key={machine.name} className="provider-machine">
          <label className="provider-folder">
            <span>Folder on {machine.name}</span>
            <input type="text" value={folder} placeholder="Home directory" spellCheck={false}
              onChange={(event) => setFolders({ ...folders, [machine.name]: event.target.value })} />
          </label>
          <button type="button" className="btn" onClick={() => void start(machine, cli, cwd)}>
            Start {cli} on {machine.name}{cwd && ` in ${cwd}`}</button>
        </div>;
      })}
      {cli !== null && !machines.isPending && capable.length === 0 && !machines.isError && <>
        <p className="muted">Connect a machine with <code>trax machine connect</code> and install {cli} on it.</p>
        <Command command={setupCommand()} />
      </>}
    </div>}
    {notice && <p role="status" className="provider-notice">{notice}</p>}
  </div>;
}

/** What is left and what was spent, under the assistant's name, and the notice before the credit runs out. */
function CreditLine({ balance }: { readonly balance: BalanceBody }) {
  const { left, spent } = creditLine(balance);
  const low = lowCredit(balance);
  return <>
    <p className="provider-credit muted">{left} · {spent}</p>
    {low !== null && <p className="provider-low" role="status">{low} Top up in <a href="#/settings">Settings</a>.</p>}
  </>;
}

/** Why a provider is out, with the link to Settings where the reason is about the caller's credit. */
export function reasonText(status: ProviderStatus) {
  if (status.credit) return <>{status.reason} Top up in <a href="#/settings">Settings</a>.</>;
  return status.reason;
}
