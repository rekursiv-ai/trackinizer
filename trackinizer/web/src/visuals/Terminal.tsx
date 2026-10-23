// The terminal view: xterm on a socket to a shell on one of the user's machines, as the `trax.terminal` tile
// renders it. Nothing starts until the user clicks Start; a terminal already live re-attaches once, when this
// mounts. The tile holds several, one per tab.
import { ClipboardAddon } from "@xterm/addon-clipboard";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal as Xterm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { ApiError } from "../api/client";
import { attachTerminal, closeTerminal, connectTerminal, listTerminalMachines, openTerminal, type LinkStatus, type TerminalLink } from "../api/terminals";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ProfileContext } from "../app/boot";
import type { WorkspaceState } from "../api/workspaces";
import { usePreferences } from "../preferences";
import type { RendererProps } from "./registry";
import { takeNewTerminalRequest, useNewTerminalRequested } from "./newTerminal";
import { tabChord, type TabChord } from "./terminalKeys";
import { gatedClipboard } from "./terminalClipboard";
import { terminalOptions } from "./terminalTheme";
import { attachTypeahead, type TypeaheadLink } from "./terminalTypeaheadScreen";
import "./Terminal.css";

/** A terminal already running: what a reload re-attaches to. */
export type LiveTerminal = { readonly terminal_id: string };

/** A terminal to connect to: `created` when this call opened it (and so must close it if it is let go of first). */
type Opened = LiveTerminal & { readonly ticket: string; readonly ice_servers?: readonly string[]; readonly created?: boolean };

type Phase = { readonly state: "idle" } | { readonly state: "starting" } | { readonly state: "failed"; readonly note: string } | LinkStatus;

const encoder = new TextEncoder();

export type TerminalViewProps = {
  /** The machine **Start terminal** opens on; none leaves the button off. */
  readonly machine: string | null;
  /** The station the terminal is opened from; its workstream receives the sessions started in it. */
  readonly workspace: string | null;
  /** The view that opens it, so a reload re-attaches this view to its own terminal. */
  readonly surface?: string | null;
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
  /** Takes the tab chords (`terminalKeys.ts`); without it every chord goes to the shell. */
  readonly onChord?: (chord: TabChord) => void;
  /** Raised each time the user brings this view forward (a new tab, a switch): the screen takes the keyboard then, or once it opens. */
  readonly keyboard?: number;
};

/** The terminals this page already started for an agent's request, by tile: a re-render never starts one again. */
const requested = new Set<string>();

/** The row Chat and the stage share, below the top bar: what a maximized terminal fills. */
function canvasRect(): CSSProperties {
  const rect = document.querySelector(".chat-row")?.getBoundingClientRect();
  return rect ? { top: rect.top, left: rect.left, width: rect.width, height: rect.height } : { top: 0, left: 0, width: "100vw", height: "100vh" };
}

/** A terminal on `machine`. */
export function TerminalView({ machine, workspace, surface = null, resume = null, onOpened, shown = true, autoStart = null, requestedBy, onChord, keyboard = 0 }: TerminalViewProps) {
  const prefs = usePreferences();
  const host = useRef<HTMLDivElement>(null);
  const live = useRef<{ term: Xterm; fit: FitAddon; link: TerminalLink | null; typeahead: TypeaheadLink } | null>(null);
  const [phase, setPhase] = useState<Phase>({ state: "idle" });
  const [session, setSession] = useState<LiveTerminal | null>(resume);
  const [maximized, setMaximized] = useState(false);
  // Whether output goes direct, and for a few seconds after it fell back, why.
  const [direct, setDirect] = useState(false);
  const [fellBack, setFellBack] = useState<string | null>(null);
  const fellBackTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  // OSC 52 may write the clipboard only just after the user's own key or click here (`terminalClipboard.ts`).
  const lastInput = useRef(0);
  const [copied, setCopied] = useState(false);
  const noteCopied = useCallback(() => {
    setCopied(true);
    setTimeout(() => setCopied(false), 3_000);
  }, []);
  // The direct link came up or fell back to the server; a fall back is said for a few seconds, then the badge is simply gone.
  const onPath = useCallback((path: "relay" | "direct", note?: string) => {
    clearTimeout(fellBackTimer.current);
    setDirect(path === "direct");
    setFellBack(note ?? null);
    if (note) fellBackTimer.current = setTimeout(() => setFellBack(null), 15_000);
  }, []);
  const [fill, setFill] = useState<CSSProperties>({});
  const fontSize = prefs.terminal_font_size;
  const chordNow = useRef({ onChord, pass: prefs.terminal_tab_chords });
  chordNow.current = { onChord, pass: prefs.terminal_tab_chords };

  const release = useCallback(() => {
    live.current?.link?.dispose();
    live.current?.typeahead.dispose();
    live.current?.term.dispose();
    live.current = null;
    clearTimeout(fellBackTimer.current);
    setDirect(false);
    setFellBack(null);
  }, []);

  // Fit only a screen that is shown: a hidden element has no size, and a fit would report 24 x 80 as if it were real.
  const shownNow = useRef(shown);
  shownNow.current = shown;
  const fitNow = useCallback(() => {
    if (shownNow.current) live.current?.fit.fit();
  }, []);

  // The keyboard goes to a screen only when the user asked for it: a click here, or the tile bringing this view
  // forward. A shell an agent starts, or a reload re-attaches, leaves the keyboard where the user has it.
  const wantKeys = useRef(false);
  const screenOpen = useRef(false);
  const begin = useCallback(async (get: (rows: number, cols: number) => Promise<Opened>, focus: boolean) => {
    if (focus) wantKeys.current = true;
    screenOpen.current = false;
    const element = host.current;
    if (!element) return;
    release();
    const term = new Xterm(terminalOptions(fontSize));
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new ClipboardAddon(undefined, gatedClipboard({ lastInput: () => lastInput.current, copied: noteCopied })));
    // On a slow link what is typed shows at once, dim, until the shell's echo confirms it (`terminalTypeahead.ts`).
    const typeahead = attachTypeahead(term);
    // Registered before the first fit, so no size the screen takes is missed; each goes to the link once there is one.
    term.onData((data) => {
      lastInput.current = Date.now();
      const link = live.current?.link;
      if (link) typeahead.input(data);
      link?.input(encoder.encode(data));
    });
    term.onResize(({ rows, cols }) => {
      typeahead.resize();
      live.current?.link?.resize(rows, cols);
    });
    // A tab chord goes to the tile, not the shell: false keeps xterm from sending it, and the page keeps the browser from acting on it.
    term.attachCustomKeyEventHandler((event) => {
      const { onChord: take, pass } = chordNow.current;
      const chord = take ? tabChord(event, pass) : null;
      if (!chord) return true;
      if (event.type === "keydown") {
        event.preventDefault();
        take?.(chord);
      }
      return false;
    });
    live.current = { term, fit, link: null, typeahead };
    setPhase({ state: "starting" });
    // The screen shows once this phase has rendered; opening and fitting it before would measure a hidden element.
    await new Promise((resolve) => setTimeout(resolve, 0));
    if (live.current?.term !== term) return;
    term.open(element);
    screenOpen.current = true;
    fitNow();
    if (wantKeys.current && shownNow.current) {
      wantKeys.current = false;
      term.focus();
    }
    try {
      const opened = await get(term.rows, term.cols);
      if (live.current?.term !== term) {
        // Let go before the terminal arrived: one this call opened would sit until the server's idle limit.
        if (opened.created) void closeTerminal(opened.terminal_id).catch(() => undefined);
        return;
      }
      setSession({ terminal_id: opened.terminal_id });
      onOpened?.({ terminal_id: opened.terminal_id });
      live.current.link = connectTerminal({
        id: opened.terminal_id, ticket: opened.ticket, iceServers: opened.ice_servers, onOutput: typeahead.output, onEcho: typeahead.echo, onStatus: setPhase, onPath,
        size: () => ({ rows: term.rows, cols: term.cols }),
      });
    } catch (error) {
      if (live.current?.term !== term) return;
      release();
      setPhase({ state: "failed", note: refusal(error) });
    }
  }, [fontSize, onOpened, release, fitNow]);

  const attach = (id: LiveTerminal, focus: boolean) => begin(async () => ({ ...id, ...await attachTerminal(id.terminal_id) }), focus);
  const open = async (rows: number, cols: number): Promise<Opened> => ({ ...await openTerminal(machine!, workspace, rows, cols, surface), created: true });
  const start = (focus: boolean) => {
    if (machine) void begin(open, focus);
  };

  // A terminal live when this mounts re-attaches, once; failing that, Start stays on offer. A cleanup lets go a
  // moment later, so React's replayed effects in development (unmount, mount) keep what the first mount began.
  const letGo = useRef<ReturnType<typeof setTimeout>>(undefined);
  const attached = useRef(false);
  useEffect(() => {
    clearTimeout(letGo.current);
    if (resume && !attached.current) {
      attached.current = true;
      void attach(resume, false);
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
    start(false);
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
  // Brought forward by the user: an open screen takes the keyboard now, one still opening as it opens.
  useEffect(() => {
    if (keyboard === 0) return;
    if (live.current && screenOpen.current) live.current.term.focus();
    else wantKeys.current = true;
  }, [keyboard]);
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
      {direct && <span className="terminal-path" title="Typing goes straight to your machine, not through the server.">Direct link</span>}
      {fellBack && <p className="terminal-note" role="status">{fellBack}</p>}
      {copied && <p className="terminal-note" role="status">Copied from terminal</p>}
      {asked && requestedBy && <p className="terminal-note" role="status">Opened at {requestedBy}'s request.</p>}
      {state === "reconnecting" && <p className="terminal-note" role="status">Reconnecting…</p>}
      {(state === "idle" || state === "failed") && <div className="terminal-start">
        {state === "failed" && <p role="alert">{phase.note}</p>}
        <button type="button" className="btn" disabled={!machine} onClick={() => start(true)}>
          {machine ? `Start terminal on ${machine}` : "Start terminal"}
        </button>
      </div>}
      {phase.state === "taken" && <p className="terminal-note" role="status">{phase.note}{" "}
        {session && <button type="button" className="btn" onClick={() => void attach(session, true)}>Take over</button>}</p>}
      {(phase.state === "ended" || phase.state === "refused") && <p className="terminal-note" role="status">{phase.note}{" "}
        <button type="button" className="btn" disabled={!machine || active} onClick={() => start(true)}>Start new</button></p>}
    </section>
  );
}

/** One tab of the tile: a terminal that was live when the tile mounted, or one opened since. */
type Tab = { readonly key: string; readonly resume: LiveTerminal | null; readonly auto: boolean; readonly id: string | null; readonly machine: string | null };

/**
 * The `trax.terminal` tile: tabs, one shell each on its own machine, placed by a person or an agent. Every
 * terminal it opens carries the tile's id as its surface, so a reload re-attaches the tile's own tabs.
 */
export function TerminalTile({ instance, workspace }: RendererProps) {
  const viewer = useContext(ProfileContext)?.email ?? null;
  const params = instance.params ?? {};
  const given = typeof params.machine === "string" && params.machine ? params.machine : null;
  const [choosing, setChoosing] = useState(false);
  const machines = useQuery({ queryKey: ["terminal-machines", workspace?.id ?? null], queryFn: ({ signal }) => listTerminalMachines(workspace?.id, { signal }), staleTime: 5_000 });
  const ready = (machines.data?.machines ?? []).filter((each) => each.terminal_ready && each.confirmed);
  const first = given ?? ready[0]?.name ?? null;
  // An agent's `start`: only for the machine it names, one of the user's that is ready, and never once a terminal is live.
  // It needs the `machine` param to name one: never a default, so an agent cannot pick which of the user's machines opens.
  const resumed = (machines.data?.terminals ?? []).filter((each) => each.surface === instance.id);
  const asked = params.start === true && resumed.length === 0 && given !== null && ready.some((each) => each.name === given);
  const [tabs, setTabs] = useState<readonly Tab[] | null>(null);
  const [active, setActive] = useState<string | null>(null);
  // Raised per tab each time the user brings it forward, so its screen takes the keyboard (`keyboard` on the view).
  const [summon, setSummon] = useState<Readonly<Record<string, number>>>({});
  const bring = (key: string) => {
    setActive(key);
    setSummon((held) => ({ ...held, [key]: (held[key] ?? 0) + 1 }));
  };
  const count = useRef(0);
  const queryClient = useQueryClient();
  const fresh = (auto: boolean, machine: string | null): Tab => ({ key: `new-${count.current++}`, resume: null, auto, id: null, machine });
  // A tab's own machine: an agent's named one wins, else the one it was opened on.
  const machineOf = (tab: Tab) => given ?? tab.machine ?? first;
  // A new tab opens on `machine`, else the active tab's, else the first ready one.
  const add = (machine?: string) => {
    const tab = fresh(true, given ?? machine ?? (tabs?.find((each) => each.key === active) ?? null)?.machine ?? first);
    setChoosing(false);
    setTabs((held) => [...(held ?? []), tab]);
    bring(tab.key);
  };
  const requested = useNewTerminalRequested();
  // The tabs are set once, when the listing first arrives: each live terminal of this tile, else one to Start.
  useEffect(() => {
    if (tabs !== null || machines.isPending) return;
    const opening: Tab[] = resumed.map((each) => ({ key: each.terminal_id, resume: { terminal_id: each.terminal_id }, auto: false, id: each.terminal_id, machine: each.machine }));
    if (opening.length === 0) opening.push(fresh(requested, first));
    if (requested && opening.length === 1 && opening[0]!.auto) takeNewTerminalRequest();
    setTabs(opening);
    setActive(opening[0]!.key);
  }, [machines.isPending, tabs]);
  useEffect(() => {
    if (tabs !== null && requested && takeNewTerminalRequest()) add();
  }, [requested, tabs]);
  const close = (tab: Tab) => {
    if (tab.id) void closeTerminal(tab.id).catch(() => undefined);
    const rest = (tabs ?? []).filter((each) => each.key !== tab.key);
    const next = rest.length > 0 ? rest : [fresh(false, machineOf(tab))];
    setTabs(next);
    if (active === tab.key) bring(next.at(-1)!.key);
  };
  const chord = (action: TabChord) => {
    const at = (tabs ?? []).findIndex((each) => each.key === active);
    if (tabs === null || at < 0) return;
    if (action === "new") add();
    else if (action === "close") close(tabs[at]!);
    else bring(tabs[(at + (action === "next" ? 1 : tabs.length - 1)) % tabs.length]!.key);
  };
  if (tabs === null) return <div className="visual-loading" aria-busy="true">Loading machines…</div>;
  return <div className="terminal-tile">
    <div className="terminal-panes">
      <div className="terminal-tabs" role="tablist" aria-label="Terminals">
        {tabs.map((tab, index) => {
          const name = machineOf(tab) ?? "Terminal";
          const label = `${name} ${tabs.slice(0, index + 1).filter((each) => (machineOf(each) ?? "Terminal") === name).length}`;
          return <span key={tab.key} className="terminal-tab" data-active={tab.key === active}>
            <button type="button" role="tab" className="btn ghost" aria-selected={tab.key === active} onClick={() => bring(tab.key)}>{label}</button>
            <button type="button" className="btn ghost" aria-label={`Close ${label}`} onClick={() => close(tab)}>×</button>
          </span>;
        })}
        {/* With more than one machine ready, + asks which; with one there is nothing to ask. */}
        <button type="button" className="btn ghost" aria-label="New terminal" title="New terminal" aria-expanded={!given && ready.length > 1 ? choosing : undefined}
          onClick={() => !given && ready.length > 1 ? setChoosing(!choosing) : add()}>+</button>
        {choosing && ready.map((each) => <button key={each.name} type="button" className="btn ghost" onClick={() => add(each.name)}>{each.name}</button>)}
      </div>
      {tabs.map((tab) => <div key={tab.key} className="terminal-pane" hidden={tab.key !== active}>
        <TerminalView machine={machineOf(tab)} workspace={workspace?.id ?? null} surface={instance.id} resume={tab.resume} shown={tab.key === active}
          onChord={chord} keyboard={summon[tab.key] ?? 0}
          autoStart={tab.auto ? `${instance.id}:${tab.key}` : asked && tab === tabs[0] ? instance.id : null}
          requestedBy={tab.auto ? undefined : requester(instance.placed_by, viewer, workspace)}
          onOpened={(opened) => {
            setTabs((held) => held?.map((each) => each.key === tab.key ? { ...each, id: opened.terminal_id } : each) ?? null);
            void queryClient.invalidateQueries({ queryKey: ["terminal-machines", workspace?.id ?? null] });
          }} />
      </div>)}
    </div>
  </div>;
}

/** What a refused open tells the user. */
function refusal(error: unknown): string {
  if (!(error instanceof ApiError)) return "Could not start the terminal.";
  if (error.code === "terminal_not_enabled") return "Start this machine's host: it turns terminals on.";
  if (error.code === "terminal_limit") return "Too many terminals opened just now; wait a minute.";
  if (error.code === "terminal_unconfirmed") return "Allow terminals on this machine first: open Chat's provider card and click Allow.";
  if (error.code === "terminal_forbidden") return "Terminals are not available on a server without sign-in.";
  return error.message;
}


/**
 * Who asked for a terminal started by an agent: the user whose key placed the tile, when that is not the viewer; for
 * the viewer's own agent, the station's shared assistant by name, else "your agent". Display only.
 */
function requester(placedBy: string | null | undefined, viewer: string | null, workspace: WorkspaceState | null): string {
  if (placedBy && placedBy !== viewer) return placedBy;
  return workspace?.partner?.provider === "assistant" && workspace.partner.actor ? workspace.partner.actor : "your agent";
}
