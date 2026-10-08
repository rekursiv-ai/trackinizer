import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type KeyboardEvent, memo, useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { getChat, listChats } from "../api/chats";
import { sendWorkspaceMessage, type WorkspacePartner } from "../api/workspaces";
import { MetaContext } from "../app/boot";
import { Composer } from "../composer/Composer";
import { detailQueries } from "../detail/queries";
import { relativeTime, useMinuteClock } from "../detail/time";
import { useLiveDetail } from "../live";
import { Markdown } from "../markdown/Markdown";
import { formatRoute, parseHash } from "../router/route";
import { useHash, validHash, visited } from "../router/trail";
import { HelperCommands } from "../settings/ChatPartner";
import { appendLines, type ChatLines, chatKey, lastSeq, readLines } from "./chatCache";
import { useChatFeed } from "./chatFeed";
import { type Line, type PendingLine, transcript } from "./chatLines";
import type { RendererProps } from "./registry";
import { useWorkspaceActions } from "./workspaceActions";

/** The most characters one message may have, as the server holds it. */
const MAX_TEXT = 16_384;

/** The most earlier pages one message carries. */
const MAX_TRAIL = 8;

/**
 * Talk with the canvas's partner: the default assistant, or a live
 * session of the user's own. The panel is the same for every partner, which
 * differs only in its name. A conversation's lines are one cache entry (the
 * thread read, then every pushed line and receipt); what the user just sent
 * shows at once, pending. The open conversation lives in the shell's `ChatFeed`,
 * so Chat mounting again keeps it.
 */
export function Chat({ instance, workspace }: RendererProps) {
  const queryClient = useQueryClient();
  const kinds = useContext(MetaContext)?.kinds ?? [];
  const { feed, state } = useChatFeed();
  const workspaceId = workspace?.id ?? null;
  const partner = workspace?.partner ?? null;
  const conversationId = feed.openId(workspaceId);
  const [menu, setMenu] = useState<"history" | null>(null);
  const [pending, setPending] = useState<readonly PendingLine[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  // Moves when the user picks another conversation; a receipt that comes back
  // after it no longer decides which one is open.
  const epoch = useRef(0);
  const [epochState, setEpochState] = useState(0);

  const thread = useQuery({
    queryKey: chatKey(conversationId),
    queryFn: async ({ signal }) => readLines(
      await getChat(conversationId!, 0, { signal }),
      queryClient.getQueryData<ChatLines>(chatKey(conversationId)),
    ),
    enabled: conversationId !== null,
    // Lines pushed before the read are held but not read: the read still comes.
    staleTime: (query) => (query.state.data?.read ? Infinity : 0),
  });
  const loading = conversationId !== null && thread.data?.read !== true && !thread.isError;
  const lines = transcript(thread.data?.messages ?? NONE, pending.filter((line) => line.conversationId === conversationId));
  const reported = conversationId ? state.status[conversationId] : undefined;
  const drained = conversationId ? state.delivered[conversationId] ?? 0 : 0;
  const known = useRef(0);
  known.current = lastSeq(thread.data);

  // A conversation deleted elsewhere is a new chat here.
  const gone = thread.error instanceof ApiError && thread.error.status === 404;
  useEffect(() => {
    if (gone && workspaceId) feed.setOpen(workspaceId, null);
  }, [gone, workspaceId, feed]);

  // After the stream opens again, read what was stored while it was down.
  const seenOpens = useRef(state.opens);
  useEffect(() => {
    if (seenOpens.current === state.opens) return;
    seenOpens.current = state.opens;
    // A read still on its way answers for this open too: one read per open.
    if (!conversationId || thread.isFetching) return;
    const controller = new AbortController();
    getChat(conversationId, known.current, { signal: controller.signal }).then(
      (after) => appendLines(queryClient, conversationId, after.messages),
      () => {},
    );
    return () => controller.abort();
  }, [state.opens, conversationId, queryClient]);

  // A conversation deleted through the API, or by the server, is left for a new chat.
  const removed = conversationId !== null && state.deleted[conversationId] === true;
  useEffect(() => {
    if (!removed) return;
    startConversation(null);
    setNotice("This conversation was deleted.");
    // `startConversation` is the same function every render, but for what it closes over.
  }, [removed]);

  const last = lines.at(-1);
  const delivered = last?.role === "user" && last.seq !== null && last.seq <= drained;
  const working = delivered && reported === undefined;
  const receipt = last?.role !== "user" ? "" : last.seq === null ? "Sending…" : delivered ? "Delivered" : "";
  // A partner that has gone away answers no more: its status, or the wait for it, gives way to that.
  const busyText = reported || (working ? "Working…" : "");
  const statusText = busyText && partner && partner.status !== "live" ? partnerGone(partner) : busyText;

  const stage = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  // Every row that grows the log, the delivery and status rows included.
  useLayoutEffect(() => {
    const element = stage.current;
    if (element && stuck.current) element.scrollTop = element.scrollHeight;
  }, [lines.length, receipt, statusText, conversationId, notice]);

  const chatInstanceId = workspace?.visuals.some((visual) => visual.id === instance.id && visual.type === "trax.chat")
    ? instance.id : null;
  const expectedRecordId = instance.record_id ?? null;
  // The target names where a draft goes, so a retry under one key only ever
  // repeats a send to the same place. `epochState` moves when the user picks
  // another conversation, not when a first message gives a new one its id.
  const target = JSON.stringify([workspaceId, partner?.session_id ?? null, chatInstanceId, expectedRecordId, epochState]);
  const live = !!workspace && partner?.status === "live";
  const hash = useHash();
  const screen = parseHash(hash, kinds);

  function startConversation(id: string | null) {
    if (workspaceId) feed.setOpen(workspaceId, id);
    epoch.current += 1;
    setEpochState(epoch.current);
    setMenu(null);
    setNotice(null);
    stuck.current = true;
  }

  async function send(text: string, key: string): Promise<string> {
    const sentTo = conversationId;
    const sentEpoch = epoch.current;
    setPending((held) => [...held.filter((line) => line.key !== key), { key, text, conversationId: sentTo }]);
    try {
      const { page, trail } = whereFrom(location.hash);
      const sent = await sendWorkspaceMessage(workspace!.id, { text, chatInstanceId, expectedRecordId, conversationId: sentTo, page, trail }, key);
      appendLines(queryClient, sent.conversation_id, [sent.message]);
      if (epoch.current === sentEpoch) feed.setOpen(workspace!.id, sent.conversation_id);
      void queryClient.invalidateQueries({ queryKey: ["chats"] });
      return "";
    } catch (error) {
      if (error instanceof ApiError && error.status === 404 && epoch.current === sentEpoch) {
        startConversation(null);
        setNotice("That conversation no longer exists. Your message was not sent; the next one starts a new chat.");
      }
      throw error;
    } finally {
      setPending((held) => held.filter((line) => line.key !== key));
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
        <div className="chat-partner" aria-label="Chat partner">
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
        {thread.isError && !gone && <p role="alert" className="chat-error">Could not load the conversation.{" "}
          <button className="btn ghost" type="button" onClick={() => void thread.refetch()}>Retry</button></p>}
        {thread.data?.earlier && <p className="chat-note">Earlier messages not shown.</p>}
        {lines.length === 0 && !loading && !notice && live && <p className="chat-note">
          Say something to {partner?.actor ?? "your partner"}.</p>}
        {lines.map((line) => <ChatLine key={line.key} role={line.role} text={line.text} pending={line.seq === null} kinds={kinds} />)}
        {receipt && <p className="chat-receipt">{receipt}</p>}
        {/* Every partner works the same way: a neutral indicator once delivered, its own status in place of it, nothing once cleared or answered. */}
        {statusText && <p className="chat-status" role="status">{statusText}</p>}
        {workspace && !live && !loading && <ChatOff local={workspace.partner_choice === "local"} />}
      </div>
      <Composer send={send} target={target} enabled={live} editable
        placeholder={live ? `Message ${partner?.actor ?? "your partner"}…` : "Chat is off"}
        check={checkText} retryable={resendable}
        failure={(error) => error instanceof ApiError && error.status >= 400 && error.status < 500 && error.detail
          ? error.detail : "Could not send this message. Retry to send the same draft safely."} />
    </section>
  );
}

const NONE: readonly never[] = [];

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
 * so the panel's renders (a status, a receipt, the canvas around it) leave each
 * line's Markdown alone unless its text changed.
 */
const ChatLine = memo(function ChatLine({ role, text, pending, kinds }: {
  readonly role: Line["role"];
  readonly text: string;
  readonly pending: boolean;
  readonly kinds: readonly string[];
}) {
  return <article className={`chat-line chat-line-${role}${pending ? " chat-line-pending" : ""}`}>
    {role === "assistant"
      ? <Markdown source={text} kinds={kinds} className="md chat-line-body" images={false} />
      : <p className="chat-line-body">{text}</p>}
  </article>;
});

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
    {chats.data?.map((chat) => <button key={chat.id} className="chat-menu-option" type="button" role="menuitemradio"
      aria-checked={chat.id === current} onClick={() => onOpen(chat.id)}>
      <strong>{chat.title}</strong>
      <span>{relativeTime(chat.modified, now)}{chat.partner_actor ? ` · ${chat.partner_actor}` : ""}</span>
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
