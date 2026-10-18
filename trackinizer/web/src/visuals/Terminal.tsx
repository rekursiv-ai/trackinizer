// The terminal view: xterm on a socket to a shell on one of the user's machines. The `trax.terminal` tile
// and the Chat column's terminal side (`ChatTerminalSide.tsx`) both render it. Nothing starts until the user
// clicks Start; a terminal already live re-attaches once, when this mounts.
import { ClipboardAddon } from "@xterm/addon-clipboard";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal as Xterm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { ApiError } from "../api/client";
import { attachTerminal, closeTerminal, connectTerminal, listTerminalMachines, openTerminal, type LinkStatus, type TerminalLink } from "../api/terminals";
import { useQuery } from "@tanstack/react-query";
import { ProfileContext } from "../app/boot";
import type { WorkspaceState } from "../api/workspaces";
import { usePreferences } from "../preferences";
import type { RendererProps } from "./registry";
import { useWorkspaceActions } from "./workspaceActions";
import { gatedClipboard } from "./terminalClipboard";
import { terminalOptions } from "./terminalTheme";
import "./Terminal.css";

/** A terminal already running: what a reload re-attaches to. */
export type LiveTerminal = { readonly terminal_id: string; readonly session_id: string };

/** A terminal to connect to: `created` when this call opened it (and so must close it if it is let go of first). */
type Opened = LiveTerminal & { readonly ticket: string; readonly created?: boolean };

type Phase = { readonly state: "idle" } | { readonly state: "starting" } | { readonly state: "failed"; readonly note: string } | LinkStatus;

const encoder = new TextEncoder();

export type TerminalViewProps = {
  /** The machine **Start terminal** opens on; none leaves the button off. */
  readonly machine: string | null;
  /** The station the terminal is opened from; its workstream receives the sessions started in it. */
  readonly workspace: string | null;
  /** A terminal already live, to re-attach to once. */
  readonly resume?: LiveTerminal | null;
  /** Told when a terminal opens, so a tile can keep its id and a reload re-attach. */
  readonly onOpened?: (opened: LiveTerminal) => void;
  /** False while the view is hidden, as under a flipped Chat: it refits when it shows again. */
  readonly shown?: boolean;
  /** Start without a click, as an agent asked: the user's open tab starts it, once for this key (the tile's id). */
  readonly autoStart?: string | null;
  /** Who asked, shown as "Opened at NAME's request". */
  readonly requestedBy?: string;
};

/** The terminals this page already started for an agent's request, by tile: a re-render never starts one again. */
const requested = new Set<string>();

/** The row Chat and the stage share, below the top bar: what a maximized terminal fills. */
function canvasRect(): CSSProperties {
  const rect = document.querySelector(".chat-row")?.getBoundingClientRect();
  return rect ? { top: rect.top, left: rect.left, width: rect.width, height: rect.height } : { top: 0, left: 0, width: "100vw", height: "100vh" };
}

/** A terminal on `machine`. */
export function TerminalView({ machine, workspace, resume = null, onOpened, shown = true, autoStart = null, requestedBy }: TerminalViewProps) {
  const prefs = usePreferences();
  const host = useRef<HTMLDivElement>(null);
  const live = useRef<{ term: Xterm; fit: FitAddon; link: TerminalLink | null } | null>(null);
  const [phase, setPhase] = useState<Phase>({ state: "idle" });
  const [session, setSession] = useState<LiveTerminal | null>(resume);
  const [maximized, setMaximized] = useState(false);
  // OSC 52 may write the clipboard only just after the user's own key or click here (`terminalClipboard.ts`).
  const lastInput = useRef(0);
  const [copied, setCopied] = useState(false);
  const noteCopied = useCallback(() => {
    setCopied(true);
    setTimeout(() => setCopied(false), 3_000);
  }, []);
  const [fill, setFill] = useState<CSSProperties>({});
  const fontSize = prefs.terminal_font_size;

  const release = useCallback(() => {
    live.current?.link?.dispose();
    live.current?.term.dispose();
    live.current = null;
  }, []);

  // Fit only a screen that is shown: a hidden element has no size, and a fit would report 24 x 80 as if it were real.
  const shownNow = useRef(shown);
  shownNow.current = shown;
  const fitNow = useCallback(() => {
    if (shownNow.current) live.current?.fit.fit();
  }, []);

  const begin = useCallback(async (get: (rows: number, cols: number) => Promise<Opened>) => {
    const element = host.current;
    if (!element) return;
    release();
    const term = new Xterm(terminalOptions(fontSize));
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new ClipboardAddon(undefined, gatedClipboard({ lastInput: () => lastInput.current, copied: noteCopied })));
    // Registered before the first fit, so no size the screen takes is missed; each goes to the link once there is one.
    term.onData((data) => {
      lastInput.current = Date.now();
      live.current?.link?.input(encoder.encode(data));
    });
    term.onResize(({ rows, cols }) => live.current?.link?.resize(rows, cols));
    live.current = { term, fit, link: null };
    setPhase({ state: "starting" });
    // The screen shows once this phase has rendered; opening and fitting it before would measure a hidden element.
    await new Promise((resolve) => setTimeout(resolve, 0));
    if (live.current?.term !== term) return;
    term.open(element);
    fitNow();
    try {
      const opened = await get(term.rows, term.cols);
      if (live.current?.term !== term) {
        // Let go before the terminal arrived: one this call opened would sit until the server's idle limit.
        if (opened.created) void closeTerminal(opened.terminal_id).catch(() => undefined);
        return;
      }
      setSession({ terminal_id: opened.terminal_id, session_id: opened.session_id });
      onOpened?.({ terminal_id: opened.terminal_id, session_id: opened.session_id });
      live.current.link = connectTerminal({
        id: opened.terminal_id, ticket: opened.ticket, onOutput: (bytes) => term.write(bytes), onStatus: setPhase,
        size: () => ({ rows: term.rows, cols: term.cols }),
      });
    } catch (error) {
      if (live.current?.term !== term) return;
      release();
      setPhase({ state: "failed", note: refusal(error) });
    }
  }, [fontSize, onOpened, release, fitNow]);

  const attach = (id: LiveTerminal) => begin(async () => ({ ...id, ticket: await attachTerminal(id.terminal_id) }));
  // A station holds one live terminal: a second open (another window, a reload) is answered 409 `terminal_live`
  // and attaches to the one that is live instead.
  const open = async (rows: number, cols: number): Promise<Opened> => {
    try {
      return { ...await openTerminal(machine!, workspace, rows, cols), created: true };
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 409 && error.code === "terminal_live" && workspace)) throw error;
      const { live } = await listTerminalMachines(workspace);
      if (!live) throw error;
      return { terminal_id: live.terminal_id, session_id: live.session_id, ticket: await attachTerminal(live.terminal_id) };
    }
  };
  const start = () => {
    if (machine) void begin(open);
  };

  // A terminal live when this mounts re-attaches, once; failing that, Start stays on offer. A cleanup lets go a
  // moment later, so React's replayed effects in development (unmount, mount) keep what the first mount began.
  const letGo = useRef<ReturnType<typeof setTimeout>>(undefined);
  const attached = useRef(false);
  useEffect(() => {
    clearTimeout(letGo.current);
    if (resume && !attached.current) {
      attached.current = true;
      void attach(resume);
    }
    return () => {
      letGo.current = setTimeout(release, 0);
    };
  }, []);

  // An agent's request starts the terminal as a click would, once for this tile.
  const [asked, setAsked] = useState(false);
  useEffect(() => {
    if (!autoStart || !machine || requested.has(autoStart)) return;
    requested.add(autoStart);
    setAsked(true);
    start();
  }, [autoStart, machine]);

  // The text size follows the user's preference, here and everywhere it is set.
  useEffect(() => {
    const term = live.current?.term;
    if (!term) return;
    term.options.fontSize = terminalOptions(fontSize).fontSize;
    fitNow();
  }, [fontSize]);

  // Maximized, the same screen and socket fill the canvas; either way it refits, and the new size goes out as a RESIZE.
  useLayoutEffect(() => {
    if (!maximized) return;
    const place = () => setFill(canvasRect());
    place();
    addEventListener("resize", place);
    return () => removeEventListener("resize", place);
  }, [maximized]);
  useLayoutEffect(() => {
    fitNow();
  }, [maximized, fill]);

  // Hidden, the view has no size to fit; shown again, it takes the column's.
  useEffect(() => {
    fitNow();
  }, [shown]);
  useEffect(() => {
    const element = host.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(fitNow);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const state = phase.state;
  const active = state === "starting" || state === "connecting" || state === "live" || state === "reconnecting";
  return (
    // Every key typed here is the shell's: none reaches the app's shortcuts (Ctrl+K is readline's).
    <section className={`terminal${maximized ? " terminal-maximized" : ""}`} aria-label="Terminal" style={maximized ? fill : undefined}
      onPointerDown={() => { lastInput.current = Date.now(); }} onKeyDown={(event) => event.stopPropagation()}>
      {/* No key is bound to this: Escape and every chord belong to the shell. */}
      <button type="button" className="btn ghost terminal-maximize" aria-label={maximized ? "Restore terminal" : "Maximize terminal"}
        title={maximized ? "Restore" : "Fill the canvas"} onClick={() => setMaximized(!maximized)}>{maximized ? "⤡" : "⤢"}</button>
      <div ref={host} className="terminal-screen" hidden={state === "idle" || state === "failed"} />
      {copied && <p className="terminal-note" role="status">Copied from terminal</p>}
      {asked && requestedBy && <p className="terminal-note" role="status">Opened at {requestedBy}'s request.</p>}
      {state === "reconnecting" && <p className="terminal-note" role="status">Reconnecting…</p>}
      {(state === "idle" || state === "failed") && <div className="terminal-start">
        {state === "failed" && <p role="alert">{phase.note}</p>}
        <button type="button" className="btn" disabled={!machine} onClick={start}>
          {machine ? `Start terminal on ${machine}` : "Start terminal"}
        </button>
        <p className="muted">Recorded to your sessions (private).</p>
      </div>}
      {phase.state === "taken" && <p className="terminal-note" role="status">{phase.note}{" "}
        {session && <button type="button" className="btn" onClick={() => void attach(session)}>Take over</button>}</p>}
      {(phase.state === "ended" || phase.state === "refused") && <p className="terminal-note" role="status">{phase.note}{" "}
        {session?.session_id && <a href={`#/lookup/${session.session_id}`}>Transcript</a>}{" "}
        <button type="button" className="btn" disabled={!machine || active} onClick={start}>Start new</button></p>}
    </section>
  );
}

/**
 * The `trax.terminal` tile: a machine picker and an inert Start, placed by a person or an agent. Its
 * `terminal` param, set once a terminal opens, makes a reload re-attach.
 */
export function TerminalTile({ instance, workspace }: RendererProps) {
  const actions = useWorkspaceActions();
  const viewer = useContext(ProfileContext)?.email ?? null;
  const params = instance.params ?? {};
  const given = typeof params.machine === "string" && params.machine ? params.machine : null;
  const terminal = typeof params.terminal === "string" && params.terminal ? params.terminal : null;
  const [picked, setPicked] = useState<string | null>(null);
  const machines = useQuery({ queryKey: ["terminal-machines", workspace?.id ?? null], queryFn: ({ signal }) => listTerminalMachines(workspace?.id, { signal }), staleTime: 5_000 });
  const ready = (machines.data?.machines ?? []).filter((each) => each.terminal_ready && each.confirmed);
  const machine = given ?? picked ?? ready[0]?.name ?? null;
  // An agent's `start`: only for the machine it names, one of the user's that is ready, and never once a terminal is placed.
  // It needs the `machine` param to name one: never a default, so an agent cannot pick which of the user's machines opens.
  const asked = params.start === true && !terminal && given !== null && ready.some((each) => each.name === given);
  if (!machines.data && !given) return <div className="visual-loading" aria-busy="true">Loading machines…</div>;
  return <div className="terminal-tile">
    {!given && ready.length > 1 && <select aria-label="Machine" value={machine ?? ""} onChange={(event) => setPicked(event.target.value)}>
      {ready.map((each) => <option key={each.name} value={each.name}>{each.name}</option>)}
    </select>}
    <TerminalView machine={machine} workspace={workspace?.id ?? null} resume={terminal ? { terminal_id: terminal, session_id: "" } : null}
      autoStart={asked ? instance.id : null} requestedBy={requester(instance.placed_by, viewer, workspace)}
      onOpened={(opened) => machine && actions?.operate({ kind: "show", visual_type: instance.type, record_id: null, params: { machine, terminal: opened.terminal_id } })} />
  </div>;
}

/** What a refused open tells the user. */
function refusal(error: unknown): string {
  if (!(error instanceof ApiError)) return "Could not start the terminal.";
  if (error.code === "terminal_not_enabled") return "Start the host with `trax machine connect`: that one command turns terminals on.";
  if (error.code === "terminal_limit") return "Terminal limit reached; close one.";
  if (error.code === "terminal_unconfirmed") return "Allow terminals on this machine first: open Chat's terminal side and click Allow.";
  if (error.code === "terminal_forbidden") return "Terminals are not available on a server without sign-in.";
  return error.message;
}


/**
 * Who asked for a terminal started by an agent: the user whose key placed the tile, when that is not the viewer; for
 * the viewer's own agent, the station's shared assistant by name, else "your agent". Display only.
 */
function requester(placedBy: string | null | undefined, viewer: string | null, workspace: WorkspaceState | null): string {
  if (placedBy && placedBy !== viewer) return placedBy;
  return workspace?.partner?.kind === "shared" && workspace.partner.actor ? workspace.partner.actor : "your agent";
}
