import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type KeyboardEvent, memo, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { newUuid } from "../api/idempotency";
import { type ChatFork, conversationOf, getChatHead, listChats, SCIENCE_CHAT_LABEL, sendChatLine } from "../api/chats";
import type { WorkspacePartner } from "../api/workspaces";
import { MetaContext, useProfile, useWriteMode } from "../app/boot";
import { Composer } from "../composer/Composer";
import { detailQueries } from "../detail/queries";
import { relativeTime, useMinuteClock } from "../detail/time";
import { useLiveDetail } from "../live";
import { Markdown } from "../markdown/Markdown";
import { formatRoute, parseHash } from "../router/route";
import { useHash, validHash, visited } from "../router/trail";
import { HelperCommands } from "../settings/ChatPartner";
import { type ChatPart, type Line, type LineAt, linesThrough, PENDING, type PendingLine, readTranscript, sentBefore, withPending } from "./chatLines";
import { chatRecordsKey, OPENING, readChatParts, useLiveChat } from "./chatRecords";
import { useChatFeed } from "./chatFeed";
import { useAnswerHighlights } from "./chatHighlights";
import type { RendererProps } from "./registry";
import { useWorkspaceActions } from "./workspaceActions";

/** The most characters one message may have, as the server holds it. */
const MAX_TEXT = 16_384;

/** The most earlier pages one message carries. */
const MAX_TRAIL = 8;

/** The most characters of a line the fork banner quotes. */
const QUOTED = 80;

/** What the composer says about a chat's reach; it is public and permanent. */
export const PUBLIC_NOTICE = "Chats are public to every user and cannot be deleted. Do not type secrets here.";

/**
 * Talk with the canvas's partner in a science chat: a session the assistant
 * opens, shared with everyone signed in like a Slack thread. Its lines are the
 * session's records (read as the Console reads them, and kept current by the
 * canvas stream's changed ids); a line posted here shows at once, pending, until
 * its record arrives. A new conversation has an id at once and a session when
 * the assistant opens it. The open conversation lives in the shell's `ChatFeed`,
 * so Chat mounting again keeps it.
 *
 * Typing in a chat joins it, unless the chat was started outside the user's
 * organisation: then what they type starts a fork of their own, from its latest
 * line, and they land in it. "Fork from here" on any stored line does the same
 * from that line, in any chat; the next message sent starts the fork.
 */
export function Chat({ instance, workspace }: RendererProps) {
  const queryClient = useQueryClient();
  const kinds = useContext(MetaContext)?.kinds ?? [];
  const { email: me } = useProfile();
  const writing = useWriteMode() === "enabled";
  const { feed, state } = useChatFeed();
  const workspaceId = workspace?.id ?? null;
  const partner = workspace?.partner ?? null;
  const conversationId = feed.openId(workspaceId);
  const [menu, setMenu] = useState<"history" | null>(null);
  const [pending, setPending] = useState<readonly PendingLine[]>([]);
  const [sending, setSending] = useState<ReadonlySet<string>>(new Set());
  const [notice, setNotice] = useState<string | null>(null);
  // The line "Fork from here" picked: the next message starts a fork of this chat after it.
  const [forkAt, setForkAt] = useState<(LineAt & { readonly quote: string }) | null>(null);
  // When the last line was sent into the open conversation, while its session may still be on its way.
  const [sentAt, setSentAt] = useState<number | null>(null);
  // Moves when the user picks another conversation; a receipt that comes back
  // after it no longer decides which one is open.
  const epoch = useRef(0);
  const [epochState, setEpochState] = useState(0);

  // The conversation's session: absent until the assistant has opened it.
  const head = useQuery({
    queryKey: ["chat", "head", conversationId],
    queryFn: ({ signal }) => getChatHead(conversationId!, { signal }),
    enabled: conversationId !== null,
    refetchInterval: (query) => (query.state.data === null && sentAt !== null && Date.now() - sentAt < OPENING.giveUpMs ? OPENING.everyMs : false),
  });
  const session = head.data?.session_id ?? null;
  const records = useQuery({
    queryKey: chatRecordsKey(session),
    queryFn: ({ signal }) => readChatParts(session!, queryClient.getQueryData<ChatPart[]>(chatRecordsKey(session)), signal),
    enabled: session !== null,
    // The stream keeps it current (`useLiveChat`); a mount or a reconnect reads what it missed.
    structuralSharing: false,
  });
  useLiveChat(session);

  const transcript = useMemo(() => readTranscript(records.data ?? []), [records.data]);
  // What the answers cite lights up on the page as they arrive (the Settings switch turns it off).
  const answers = useMemo(() => records.data === undefined ? null
    : transcript.lines.filter((line) => line.role === "assistant").map(({ key, text }) => ({ key, text, pointed: transcript.pointed.has(key) })), [records.data, transcript]);
  useAnswerHighlights(answers, session);
  const lines = withPending(transcript, pending.filter((line) => line.conversationId === conversationId), { me });
  const loading = session !== null && records.data === undefined && !records.isError;
  const unopened = conversationId !== null && head.data === null && !head.isFetching;
  const stale = unopened && sentAt !== null && Date.now() - sentAt >= OPENING.giveUpMs;
  // The give-up time is not a render of its own: the last look may come just before it.
  const [, tick] = useState(0);
  useEffect(() => {
    if (sentAt === null) return;
    const timer = setTimeout(() => tick((n) => n + 1), Math.max(0, sentAt + OPENING.giveUpMs - Date.now()) + 1);
    return () => clearTimeout(timer);
  }, [sentAt]);

  const pickFork = useCallback((at: LineAt, quote: string) => setForkAt({ ...at, quote }), []);
  const stored = lines.filter((line) => !line.pending);
  const forks = head.data?.forks_on_typing === true;
  const latest = stored.at(-1)?.at ?? null;
  // The line a message sent now would fork after: the one picked, else the latest of a chat that cannot be joined.
  const forkPoint = forkAt ?? (forks ? latest : null);
  const fork: ChatFork | null = session && forkPoint ? { sessionId: session, part: forkPoint.part, idx: forkPoint.idx } : null;

  const last = lines.at(-1);
  const waiting = last?.pending ? (sending.has(last.key.slice(PENDING.length)) ? "Sending…" : "Waiting for the assistant…") : "";
  const busyText = waiting || (transcript.working === null || last?.pending ? "" : transcript.working ? `Working: ${transcript.working}…` : "Working…");
  // A partner that has gone away answers no more: the wait for it gives way to that.
  const statusText = busyText && partner && partner.status !== "live" ? partnerGone(partner) : busyText;

  const stage = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  // Every row that grows the log, the working line included.
  useLayoutEffect(() => {
    const element = stage.current;
    if (element && stuck.current) element.scrollTop = element.scrollHeight;
  }, [lines.length, statusText, conversationId, notice]);

  const chatInstanceId = workspace?.visuals.some((visual) => visual.id === instance.id && visual.type === "trax.chat")
    ? instance.id : null;
  const expectedRecordId = instance.record_id ?? null;
  // The target names where a draft goes, so a retry under one key only ever
  // repeats a send to the same place. `epochState` moves when the user picks
  // another conversation, not when a first line gives a new one its id.
  const target = JSON.stringify([workspaceId, partner?.session_id ?? null, chatInstanceId, expectedRecordId, epochState, fork]);
  const live = !!workspace && partner?.status === "live" && writing;
  // Whose chat this is, and so whether typing joins it or forks it, is known once its head has been read.
  const reading = conversationId !== null && head.isPending;
  const hash = useHash();
  const screen = parseHash(hash, kinds);

  function startConversation(id: string | null) {
    if (workspaceId) feed.setOpen(workspaceId, id);
    epoch.current += 1;
    setEpochState(epoch.current);
    setMenu(null);
    setNotice(null);
    setForkAt(null);
    setSentAt(null);
    stuck.current = true;
  }

  // A request from the Console or a session's page: open that science chat here. Taking it
  // ends this effect's run, so what it started is dropped only when Chat goes away.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    const request = state.request;
    if (!request || !workspaceId) return;
    feed.taken(request.n);
    void queryClient.fetchQuery(detailQueries.detail(request.sessionId)).then((detail) => {
      if (!mounted.current) return;
      const conversation = conversationOf(typeof detail.self.cli_session_id === "string" ? detail.self.cli_session_id : null);
      const labels = Array.isArray(detail.self.labels) ? detail.self.labels : [];
      if (conversation && labels.includes(SCIENCE_CHAT_LABEL)) startConversation(conversation);
      else setNotice("That session is not a science chat.");
    }, () => mounted.current && setNotice("Could not open that chat."));
    // `startConversation` is the same function every render, but for what it closes over.
  }, [state.request, workspaceId]);

  /** The server refuses a post into a chat started outside my organisation; what I typed then forks it at its latest line. */
  async function send(text: string, key: string): Promise<string> {
    try {
      return await post(text, key, forkPoint);
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 403) || fork || latest === null) throw error;
      return post(text, newUuid(), latest);
    }
  }

  async function post(text: string, key: string, after: LineAt | null): Promise<string> {
    // A fork is a new conversation, which the key names; it opens with my own lines up to the fork point.
    const forkOf: ChatFork | null = session && after ? { sessionId: session, part: after.part, idx: after.idx } : null;
    const sentTo = forkOf ? key : conversationId;
    const sentEpoch = epoch.current;
    const baseline = sentBefore(after && forkOf ? linesThrough(lines, after) : lines, { me });
    setPending((held) => [...held.filter((line) => line.key !== key), { key, text, conversationId: sentTo, baseline }]);
    setSending((held) => new Set(held).add(key));
    try {
      const { page, trail } = whereFrom(location.hash);
      const sent = await sendChatLine({ workspaceId: workspace!.id, text, chatInstanceId, expectedRecordId, conversationId: forkOf ? null : sentTo,
        ...(forkOf && { fork: forkOf }), page, trail }, key);
      setPending((held) => held.map((line) => (line.key === key ? { ...line, conversationId: sent.conversation_id } : line)));
      if (epoch.current === sentEpoch) {
        feed.setOpen(workspace!.id, sent.conversation_id);
        setForkAt(null);
        setSentAt(Date.now());
      }
      void queryClient.invalidateQueries({ queryKey: ["chats"] });
      void queryClient.invalidateQueries({ queryKey: ["chat", "head", sent.conversation_id] });
      return "";
    } catch (error) {
      setPending((held) => held.filter((line) => line.key !== key));
      throw error;
    } finally {
      setSending((held) => {
        const next = new Set(held);
        next.delete(key);
        return next;
      });
    }
  }

  const closeOnEscape = (event: KeyboardEvent) => {
    if (event.key === "Escape" && menu) {
      event.stopPropagation();
      setMenu(null);
    }
  };

  return (
    <section className="chat-panel" aria-label="Chat" onKeyDown={closeOnEscape}>
      <header className="chat-head">
        <div className="chat-partner" role="group" aria-label="Chat partner">
          {partner && <>
            <strong>{partnerName(partner)}</strong>
            <span className="chat-partner-note">{partnerNote(partner)}</span>
          </>}
        </div>
        <div className="chat-head-actions" role="toolbar" aria-label="Chat controls">
          <button className="btn ghost" type="button" aria-haspopup="menu" aria-expanded={menu === "history"}
            disabled={!workspace} onClick={() => setMenu(menu === "history" ? null : "history")}>History</button>
          {/* A new chat, the old one kept in History: a conversation is the partner's memory, resumed by opening it. */}
          <button className="btn ghost" type="button" disabled={!workspace || (conversationId === null && lines.length === 0)}
            onClick={() => startConversation(null)}>Clear chat</button>
        </div>
      </header>
      {menu === "history" && <History current={conversationId} onOpen={startConversation} />}
      {instance.record_id && <ChatRecordContext recordId={instance.record_id} />}
      {!instance.record_id && screen.name === "lookup" && <ChatScreenContext id={screen.id} label={screen.id} />}
      {!instance.record_id && screen.name === "ref" && <ChatScreenRef kind={screen.kind} seq={screen.seq} />}
      <div className="chat-lines" ref={stage} role="log" aria-label="Messages" aria-live="polite"
        onScroll={(event) => {
          const element = event.currentTarget;
          stuck.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48;
        }}>
        {notice && <p className="chat-note" role="status">{notice}</p>}
        {loading && <p className="chat-note">Loading the conversation…</p>}
        {(head.isError || records.isError) && <p role="alert" className="chat-error">Could not load the conversation.{" "}
          <button className="btn ghost" type="button" onClick={() => void (head.isError ? head.refetch() : records.refetch())}>Retry</button></p>}
        {unopened && !stale && sentAt === null && <p className="chat-note" role="status">
          This conversation has no session on this server yet. It starts when the assistant hears its first line.</p>}
        {stale && <p className="chat-error" role="alert">The assistant has not opened this chat. Your line may not have been
          delivered; send it again.</p>}
        {lines.length === 0 && !loading && !notice && !unopened && live && <p className="chat-note">
          Say something to {partner?.actor ?? "your partner"}.</p>}
        {head.data && head.data.account !== me && <p className="chat-note">Started by {head.data.account}.</p>}
        {head.data?.forked_from && <p className="chat-note">Forked from{" "}
          <button className="btn ghost" type="button" onClick={() => startConversation(head.data!.forked_from!)}>another chat</button>.</p>}
        {head.data && head.data.forks > 0 && <p className="chat-note">{forkedTimes(head.data.forks)}</p>}
        {forks && <p className="chat-note" role="status">This chat was started outside your organisation, so what you type
          starts a fork of your own from its latest line.</p>}
        {lines.map((line) => <ChatLine key={line.key} line={line} me={me} kinds={kinds} onFork={live && head.data ? pickFork : undefined} />)}
        {/* One working line, the last tool call's name, in place of a status the partner no longer sends. */}
        {statusText && <p className="chat-status" role="status">{statusText}</p>}
        {workspace && writing && !live && !loading && <ChatOff local={workspace.partner_choice === "local"} />}
      </div>
      {forkAt && <p className="chat-fork-banner" role="status">
        Your next message starts a fork of this chat after “{forkAt.quote}”.{" "}
        <button className="btn ghost" type="button" onClick={() => setForkAt(null)}>Cancel fork</button>
      </p>}
      <p className="chat-notice">{PUBLIC_NOTICE}</p>
      <Composer send={send} target={target} enabled={live && !reading} editable
        placeholder={reading ? "Loading the conversation…" : live ? `Message ${partner?.actor ?? "your partner"}…` : readOnly(partner?.status, writing)}
        check={checkText} retryable={resendable}
        failure={(error) => error instanceof ApiError && error.status >= 400 && error.status < 500 && error.detail
          ? error.detail : "Could not send this message. Retry to send the same draft safely."} />
    </section>
  );
}

/** The page a message is sent from and the up to 8 pages before it, oldest first. */
function whereFrom(hash: string): { readonly page: string | null; readonly trail: readonly string[] } {
  const page = validHash(hash) ? hash : null;
  const before = visited();
  return { page, trail: (before.at(-1) === page ? before.slice(0, -1) : before).slice(-MAX_TRAIL) };
}

/** A send that got no answer or a server fault may be sent again; a refusal would be refused again. */
function resendable(error: Error): boolean {
  return !(error instanceof ApiError) || error.status === 0 || error.status >= 500;
}

function checkText(text: string): string {
  if (!text.trim()) return "Write a message first.";
  return text.length > MAX_TEXT ? `A message holds at most ${MAX_TEXT.toLocaleString("en")} characters; this one has ${text.length.toLocaleString("en")}.` : "";
}

/** The name a partner goes by in the header: a local helper is the user's own, named once it runs. */
function partnerName(partner: WorkspacePartner): string {
  if (partner.kind === "shared") return partner.actor ?? "assistant";
  return partner.actor ? `your local helper (${partner.actor})` : "your local helper";
}

function partnerNote(partner: WorkspacePartner): string {
  const state = partner.status === "live" ? "" : ` · ${partner.status}`;
  return `${partner.cli ?? "agent"}${state}`;
}

function partnerGone(partner: WorkspacePartner): string {
  return `${partner.actor ?? "The partner"} is unavailable.`;
}

function readOnly(status: string | undefined, writing: boolean): string {
  return status === "live" && !writing ? "You can read this chat but not post in it" : "Chat is off";
}

/** What to do when no partner is live: two lines, then the commands that start a local helper. */
function ChatOff({ local }: { readonly local: boolean }) {
  return <div className="chat-off">
    <p className="chat-note">{local
      ? "Start your local helper:"
      : "Ask your admin to set up a shared Chat assistant, or set up a local one:"}</p>
    {!local && <p className="chat-note"><a href="#/settings">Use a local helper in Settings</a></p>}
    <HelperCommands tokens="link" />
  </div>;
}

/**
 * One line, by value: the transcript builds its lines afresh on every render,
 * so the panel's renders (a status, the canvas around it) leave each line's
 * Markdown alone unless its text changed. A line by someone else says who.
 */
const ChatLine = memo(function ChatLine({ line, me, kinds, onFork }: {
  readonly line: Line;
  readonly me: string;
  readonly kinds: readonly string[];
  /** Pick this stored line as where a fork starts; absent where a fork cannot be started. */
  readonly onFork?: (at: LineAt, quote: string) => void;
}) {
  const other = line.role === "user" && line.author !== null && line.author !== me;
  const at = line.at;
  return <article className={`chat-line chat-line-${line.role}${other ? " chat-line-other" : ""}${line.pending ? " chat-line-pending" : ""}`}>
    {other && <span className="chat-line-author">{line.author}</span>}
    {line.role === "assistant"
      ? <Markdown source={line.text} kinds={kinds} className="md chat-line-body" images={false} />
      : <p className="chat-line-body">{line.text}</p>}
    {onFork && at && <button className="btn ghost chat-line-fork" type="button" onClick={() => onFork(at, quoted(line.text))}>Fork from here</button>}
  </article>;
});

/** The start of a line, on one line, for the banner that says where a fork starts. */
function quoted(text: string): string {
  const flat = text.split(/\s+/).join(" ").trim();
  return flat.length > QUOTED ? `${flat.slice(0, QUOTED - 1)}…` : flat;
}

function forkedTimes(count: number): string {
  return count === 1 ? "Forked once." : `Forked ${count} times.`;
}

function History({ current, onOpen }: { readonly current: string | null; readonly onOpen: (id: string) => void }) {
  const now = useMinuteClock();
  const chats = useQuery({
    queryKey: ["chats"],
    queryFn: ({ signal }) => listChats({ signal }),
  });
  return <div className="chat-menu" role="menu" aria-label="History">
    {chats.isPending && <p className="chat-note">Loading history…</p>}
    {chats.isError && <p role="alert" className="chat-error">Could not load history.{" "}
      <button className="btn ghost" type="button" onClick={() => void chats.refetch()}>Retry</button></p>}
    {chats.isSuccess && chats.data.length === 0 && <p className="chat-note">No conversations yet.</p>}
    {chats.data?.map((chat) => <button key={chat.conversation_id} className="chat-menu-option" type="button" role="menuitemradio"
      aria-checked={chat.conversation_id === current} onClick={() => onOpen(chat.conversation_id)}>
      <strong>{chat.title}</strong>
      <span>{relativeTime(chat.modified, now)} · {chat.account}</span>
    </button>)}
  </div>;
}

/** The record this Chat is about, a link that only moves the page, and the way back to plain Chat. */
function ChatRecordContext({ recordId }: { readonly recordId: string }) {
  const actions = useWorkspaceActions();
  const record = useQuery(detailQueries.detail(recordId));
  useLiveDetail(recordId);
  return (
    <p className="chat-record-context" aria-label="Record context">
      Context: <a href={formatRoute({ name: "lookup", id: recordId })}>
        {record.data ? `${record.data.self.kind}#${record.data.self.seq} ${record.data.self.title}` : recordId}
      </a>
      <button className="btn ghost" type="button" disabled={!actions || actions.busy}
        onClick={() => actions?.operate({ kind: "show", visual_type: "trax.chat", record_id: null })}>
        Clear context
      </button>
    </p>
  );
}

/** The record the page names by `Kind#seq`, once its id is found. */
function ChatScreenRef({ kind, seq }: { readonly kind: string; readonly seq: number }) {
  const id = useQuery(detailQueries.ref(kind, seq));
  return <ChatScreenContext id={id.data ?? null} label={`${kind}#${seq}`} />;
}

/** What is on screen when Chat is pinned to no record: the record the page shows, as it changes. */
function ChatScreenContext({ id, label }: { readonly id: string | null; readonly label: string }) {
  return (
    <p className="chat-record-context" aria-label="Screen context">
      On screen: {id ? <ChatScreenTitle id={id} label={label} /> : label}
    </p>
  );
}

function ChatScreenTitle({ id, label }: { readonly id: string; readonly label: string }) {
  const record = useQuery(detailQueries.detail(id));
  useLiveDetail(id);
  return record.data ? `${record.data.self.kind}#${record.data.self.seq} ${record.data.self.title}` : label;
}
