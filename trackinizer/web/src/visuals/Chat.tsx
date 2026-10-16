import { useQuery, useQueryClient } from "@tanstack/react-query";
import { memo, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { newUuid } from "../api/idempotency";
import { CHAT_DELETED, CHAT_FORK_CLOSED, CHAT_FORK_WIDER, type ChatFork, conversationOf, getChatHead, SCIENCE_CHAT_LABEL, sendChatLine } from "../api/chats";
import type { OrgMode } from "../api/me";
import { getWorkspace } from "../api/workspaces";
import type { WorkspacePartner, WorkspaceState } from "../api/workspaces";
import { MetaContext, useProfile, useWriteMode } from "../app/boot";
import { keyCaps } from "../commands/registry";
import { Composer } from "../composer/Composer";
import { detailQueries } from "../detail/queries";
import { useLiveDetail } from "../live";
import { Markdown } from "../markdown/Markdown";
import { parseHash } from "../router/route";
import { useHash, validHash, visited } from "../router/trail";
import { HelperCommands } from "../settings/ChatPartner";
import { stationForChat, type StationSummary } from "../api/stations";
import { closeStation, STATIONS_KEY } from "../stations/actions";
import { showStation } from "../stations/useStation";
import { Icon } from "../ui/icons";
import { type ChatPart, type Line, type LineAt, linesThrough, PENDING, type PendingLine, readTranscript, sentBefore, withPending } from "./chatLines";
import { chatRecordsKey, OPENING, readChatParts, useLiveChat } from "./chatRecords";
import { useChatFeed } from "./chatFeed";
import { FOLLOW_UP_ASK, followUpSuggested } from "./chatFollowUp";
import { flipChatSide, setChatHidden, useChatColumn } from "./chatColumnState";
import { useAnswerHighlights } from "./chatHighlights";
import { useChatPartial } from "./chatPartial";
import { ChatPrompt } from "./ChatPrompt";
import { DeleteChat, PrivacySwitch, ShareChat } from "./ChatSharing";

/** The most characters one message may have, as the server holds it. */
const MAX_TEXT = 16_384;

/** The most earlier pages one message carries. */
const MAX_TRAIL = 8;

/** The most characters of a line the fork banner quotes. */
const QUOTED = 80;

/** What Chat says when the server refuses a fork, by the refusal's code. */
const FORK_REFUSED: ReadonlyMap<string, string> = new Map([
  [CHAT_FORK_CLOSED, "This chat was hidden or made staff only, so it can no longer be forked."],
  [CHAT_FORK_WIDER, "This chat is shared with you, not with your organisation, which would read a fork of it; post in it instead."],
]);

/**
 * What the composer says about who reads a chat, the same for every reader: by
 * its audience (`open`, as the server's org mode lets people in; `private`, once
 * its starter hid it; `staff`, once the assistant read a staff session into it)
 * and the org mode, which a server older than it does not name.
 */
export function chatNotice(audience: string, orgMode: OrgMode | undefined): string {
  return `${chatReach(audience, orgMode)} Do not type secrets here.`;
}

/** Who reads an open chat, by the server's org mode. */
const READERS: { readonly [mode in OrgMode | "unnamed"]: string } = {
  single: "everyone signed in to this server",
  domain: "its starter, the people they share it with, and the starter's organisation",
  individual: "its starter and the people they share it with",
  unnamed: "its starter, the people they share it with, and their organisation where this server lets it in",
};

function chatReach(audience: string, orgMode: OrgMode | undefined): string {
  if (audience === "private") return `Private: only ${READERS.individual} read it.`;
  const readers = READERS[orgMode ?? "unnamed"];
  return audience === "staff" ? `Staff only: of ${readers}, only those who hold staff read it.` : `Read by ${readers}.`;
}

/**
 * Talk with the workspace's partner in a science chat: a session the assistant
 * opens, read by whom the server's org mode lets in, as a Slack channel is by its
 * workspace. Its lines are the session's records (read as the Console reads
 * them, and kept current by the canvas stream's changed ids); a line posted here
 * shows at once, pending, until its record arrives. A new conversation has an id
 * at once and a session when the assistant opens it. The open conversation lives
 * in the shell's `ChatFeed`, so Chat mounting again keeps it.
 *
 * Whoever reads a chat joins it by typing in it. Anyone else, whose head says
 * `forks_on_typing`, reads none of it: the server refuses its records and their
 * lines, so Chat says why and takes no line. "Fork from here" on any stored
 * answer of an open chat starts a fork of it from that answer; the next message
 * sent starts it. A private or staff-only chat cannot be forked, since the copy
 * would not keep who may read it. The starter makes a chat private or public (`PrivacySwitch`).
 */
export function Chat({ workspace }: { readonly workspace: WorkspaceState | null }) {
  const queryClient = useQueryClient();
  const kinds = useContext(MetaContext)?.kinds ?? [];
  const { email: me, org_mode: orgMode } = useProfile();
  const writing = useWriteMode() === "enabled";
  const { feed, state } = useChatFeed();
  const { side, hidden } = useChatColumn();
  const workspaceId = workspace?.id ?? null;
  const partner = workspace?.partner ?? null;
  // The station's chat: bound by its first line, and the same however Chat is mounted again.
  const conversationId = workspace?.conversation_id ?? null;
  const [pending, setPending] = useState<readonly PendingLine[]>([]);
  const [sending, setSending] = useState<ReadonlySet<string>>(new Set());
  const [notice, setNotice] = useState<string | null>(null);
  // The line "Fork from here" picked: the next message starts a fork of this chat after it.
  const [forkAt, setForkAt] = useState<(LineAt & { readonly quote: string }) | null>(null);
  // When the last line was sent into the open conversation, while its session may still be on its way.
  const [sentAt, setSentAt] = useState<number | null>(null);

  // The conversation's session: absent until the assistant has opened it.
  const head = useQuery({
    queryKey: ["chat", "head", conversationId],
    queryFn: ({ signal }) => getChatHead(conversationId!, { signal }),
    enabled: conversationId !== null,
    refetchInterval: (query) => (query.state.data === null && sentAt !== null && Date.now() - sentAt < OPENING.giveUpMs ? OPENING.everyMs : false),
  });
  const session = head.data?.session_id ?? null;
  // I do not read the chat: the server refuses its records and my lines alike.
  const outsider = head.data?.forks_on_typing === true;
  const readable = outsider ? null : session;
  const records = useQuery({
    queryKey: chatRecordsKey(session),
    queryFn: ({ signal }) => readChatParts(session!, queryClient.getQueryData<ChatPart[]>(chatRecordsKey(session)), signal),
    enabled: readable !== null,
    // The stream keeps it current (`useLiveChat`); a mount or a reconnect reads what it missed.
    structuralSharing: false,
  });
  useLiveChat(session);
  // Lines read before the chat closed to me (made staff only, or unshared) go with it.
  const parts = outsider ? undefined : records.data;

  const transcript = useMemo(() => readTranscript(parts ?? []), [parts]);
  // What the answers cite lights up on the page as they arrive (the Settings switch turns it off).
  const answers = useMemo(() => parts === undefined ? null
    : transcript.lines.filter((line) => line.role === "assistant").map(({ key, text }) => ({ key, text, pointed: transcript.pointed.has(key) })), [parts, transcript]);
  useAnswerHighlights(answers, session);
  const lines = withPending(transcript, pending.filter((line) => line.conversationId === conversationId), { me });
  const loading = readable !== null && records.data === undefined && !records.isError;
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
  // "Launch the follow-up" on a recap sends the canned ask as any typed line would, through the latest
  // `send` (a fresh closure each render); a ref keeps the handler's identity stable, and a guard drops a
  // second press while the first is on its way.
  const sendRef = useRef<(text: string, key: string) => Promise<string>>(() => Promise.resolve(""));
  const launching = useRef(false);
  const launchFollowUp = useCallback(async () => {
    if (launching.current) return;
    launching.current = true;
    try { await sendRef.current(FOLLOW_UP_ASK, newUuid()); } finally { launching.current = false; }
  }, []);
  // The line a message sent now would fork after: the one "Fork from here" picked.
  const fork: ChatFork | null = session && forkAt ? { sessionId: session, part: forkAt.part, idx: forkAt.idx } : null;

  const last = lines.at(-1);
  const sendingLast = last?.pending === true && sending.has(last.key.slice(PENDING.length));
  const waiting = last?.pending ? (sendingLast ? "Sending…" : "Waiting for the assistant…") : "";
  const busyText = waiting || (transcript.working === null || last?.pending ? "" : transcript.working ? `Working: ${transcript.working}…` : "Working…");
  // A partner that has gone away answers no more: the wait for it gives way to that.
  const statusText = busyText && partner && partner.status !== "live" ? partnerGone(partner) : busyText;
  // The reply being written, read while an answer is owed in a chat I read from a live partner, stands
  // in for the wait for it; "Sending…" says more. The stored answer takes its place in one render.
  const owed = (last?.pending === true || transcript.working !== null) && partner?.status === "live";
  const bubble = useChatPartial(readable, { active: owed, answers: answers?.length ?? 0 });
  const reply = sendingLast ? null : bubble;

  const stage = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  // Every row that grows the log, the working line and the reply being written included.
  useLayoutEffect(() => {
    const element = stage.current;
    if (element && stuck.current) element.scrollTop = element.scrollHeight;
  }, [lines.length, statusText, reply?.text, conversationId, notice]);

  // The target names where a draft goes, so a retry under one key only ever
  // repeats a send to the same place.
  const target = JSON.stringify([workspaceId, partner?.session_id ?? null, fork]);
  const live = !!workspace && partner?.status === "live" && writing;
  // Who reads this chat, and so whether I may read it and type in it, is known once its head has been read.
  const reading = conversationId !== null && head.isPending;
  // Only an open chat forks: a copy would not keep a hidden or staff-only chat's readers.
  const forkable = live && head.data?.audience === "open";
  const hash = useHash();
  const screen = parseHash(hash, kinds);

  // Another station is another conversation: what belonged to this one's pass with it. A
  // fork's own station keeps the wait for its session, which its first line began.
  const forkLanding = useRef<string | null>(null);
  const shownStation = useRef(workspaceId);
  useEffect(() => {
    if (shownStation.current === workspaceId) return;
    shownStation.current = workspaceId;
    setNotice(null);
    setForkAt(null);
    setSentAt(forkLanding.current === workspaceId ? Date.now() : null);
    forkLanding.current = null;
    stuck.current = true;
  }, [workspaceId]);

  /** Switch to the user's station that holds `conversation`, which the server makes if there is none. */
  async function openConversation(conversation: string) {
    try {
      showStation(queryClient, await stationForChat(conversation));
    } catch (error) {
      if (mounted.current) setNotice(error instanceof ApiError && error.code === CHAT_DELETED ? error.detail : "Could not open that chat.");
    }
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
      if (conversation && labels.includes(SCIENCE_CHAT_LABEL)) void openConversation(conversation);
      else setNotice("That session is not a science chat.");
    }, () => mounted.current && setNotice("Could not open that chat."));
    // `openConversation` is the same function every render, but for what it closes over.
  }, [state.request, workspaceId]);

  /** The chat is gone: the station holds no conversation, and the stream says so to every tab. */
  function chatDeleted(conversation: string) {
    queryClient.setQueryData<WorkspaceState>(["workspace", workspaceId], (held) => held && { ...held, conversation_id: null });
    queryClient.setQueryData<StationSummary[]>(STATIONS_KEY, (held) =>
      held?.map((each) => (each.conversation_id === conversation ? { ...each, conversation_id: null } : each)));
    queryClient.removeQueries({ queryKey: ["chat", "head", conversation] });
    void queryClient.invalidateQueries({ queryKey: ["stations"] });
  }

  // A station that loses its conversation (deleted here or, by the stream, in another tab) shows an
  // empty chat: what belonged to the conversation goes with it.
  const heldConversation = useRef(conversationId);
  useEffect(() => {
    const lost = heldConversation.current;
    heldConversation.current = conversationId;
    if (lost === null || conversationId !== null) return;
    setPending((held) => held.filter((line) => line.conversationId !== lost));
    setForkAt(null);
    setSentAt(null);
  }, [conversationId]);

  // A chat deleted under this tab is let go, wherever the tab learns of it: its head read says 410.
  const goneHead = head.error instanceof ApiError && head.error.code === CHAT_DELETED;
  useEffect(() => {
    if (goneHead && conversationId) chatDeleted(conversationId);
    // `chatDeleted` is the same function every render, but for what it closes over.
  }, [goneHead, conversationId]);

  /**
   * A post into a deleted chat (410) lets it go and starts a new chat with the line. Any other refusal is
   * shown: one into a chat I do not read never becomes a fork, since a fork copies lines I may not read.
   * A fork refused (`FORK_REFUSED`), because the chat was hidden or made staff only since its line was
   * picked or because my organisation would read the fork, drops that line, keeps the draft, says why,
   * and reads the head again. The composer drops its failure along with the fork's target, so the reason
   * is a notice of Chat's own.
   */
  async function send(text: string, key: string): Promise<string> {
    try {
      return await post(text, key, forkAt);
    } catch (error) {
      const refused = error instanceof ApiError ? FORK_REFUSED.get(error.code ?? "") : undefined;
      if (refused) {
        setForkAt(null);
        setNotice(refused);
        void queryClient.invalidateQueries({ queryKey: ["chat", "head", conversationId] });
      }
      if (!(error instanceof ApiError && error.code === CHAT_DELETED && conversationId)) throw error;
      chatDeleted(conversationId);
      return post(text, newUuid(), null, true);
    }
  }

  async function post(text: string, key: string, after: LineAt | null, fresh = false): Promise<string> {
    // A fork is a new conversation, which the key names; it opens with my own lines up to the fork point.
    const forkOf: ChatFork | null = session && after ? { sessionId: session, part: after.part, idx: after.idx } : null;
    const joined = fresh ? null : conversationId;
    const sentTo = forkOf ? key : joined;
    const baseline = sentBefore(fresh ? [] : after && forkOf ? linesThrough(lines, after) : lines, { me });
    setPending((held) => [...held.filter((line) => line.key !== key), { key, text, conversationId: sentTo, baseline }]);
    setSending((held) => new Set(held).add(key));
    try {
      const { page, trail } = whereFrom(location.hash);
      const sent = await sendChatLine({ workspaceId: workspace!.id, text, conversationId: forkOf ? null : sentTo,
        ...(forkOf && { fork: forkOf }), page, trail }, key);
      setPending((held) => held.map((line) => (line.key === key ? { ...line, conversationId: sent.conversation_id } : line)));
      if (sent.workspace_id === workspace!.id) {
        // The first line binds the conversation to the station; the station's state says so from here.
        queryClient.setQueryData<WorkspaceState>(["workspace", sent.workspace_id], (held) => held && { ...held, conversation_id: sent.conversation_id });
        setForkAt(null);
        setSentAt(Date.now());
      } else {
        // A fork is a station of its own, which this tab now shows, on the page it is on.
        forkLanding.current = sent.workspace_id;
        showStation(queryClient, await getWorkspace(sent.workspace_id), window.location.hash);
      }
      void queryClient.invalidateQueries({ queryKey: ["stations"] });
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

  sendRef.current = send;
  // The last line, when it is a stored recap and posting is open, may carry a "keep going" button.
  const followUpKey = live && last && !last.pending && last.role === "assistant" ? last.key : null;

  return (
    <section className="chat-panel" aria-label="Chat">
      <header className="chat-head">
        {head.data && head.data.audience !== "open" && !(head.data.may_hide && head.data.audience === "private")
          && <ChatBadge audience={head.data.audience} />}
        <div className="chat-partner" role="group" aria-label="Chat partner">
          {partner && <>
            <strong>{partnerName(partner)}</strong>
            <span className="chat-partner-note">{partnerNote(partner)}</span>
          </>}
          {/* The station's tabs stand in the top bar now; its name stays here, so Chat says whose it is. */}
          {workspace && <span className="chat-station" title={workspace.name}>{workspace.name}</span>}
        </div>
        <div className="chat-head-actions" role="toolbar" aria-label="Chat controls">
          {head.data?.may_hide && <PrivacySwitch head={head.data} />}
          {head.data?.may_share && <ShareChat head={head.data} />}
          {head.data?.may_delete && <DeleteChat head={head.data} onDeleted={() => {
            chatDeleted(head.data!.conversation_id);
            // Deleting a chat closes its station tab, as closing any tab does, moving to the one beside it.
            if (workspaceId) void closeStation(queryClient, workspaceId);
          }} />}
          {/* Hidden, Chat is open over the page by hovering its strip; pinning stands it beside the page again. */}
          {hidden
            ? <button className="btn ghost" type="button" title="Pin Chat beside the page"
                aria-label="Pin Chat beside the page" onClick={() => setChatHidden(false)}>Pin</button>
            : <button className="btn ghost" type="button" title={`Hide Chat (${keyCaps("$mod+\\").join(" ")})`}
                aria-label="Hide Chat" onClick={() => setChatHidden(true)}>
                <Icon name={side === "left" ? "panelLeftClose" : "panelRightClose"} size={14} />
              </button>}
          <button className="btn ghost" type="button" title={`Move Chat to the ${side === "left" ? "right" : "left"}`}
            aria-label={`Move Chat to the ${side === "left" ? "right" : "left"}`} onClick={flipChatSide}>{side === "left" ? "▷" : "◁"}</button>
        </div>
      </header>
      {screen.name === "lookup" && <ChatScreenContext id={screen.id} label={screen.id} />}
      {screen.name === "ref" && <ChatScreenRef kind={screen.kind} seq={screen.seq} />}
      <div className="chat-lines" ref={stage} role="log" aria-label="Messages" aria-live="polite"
        onScroll={(event) => {
          const element = event.currentTarget;
          stuck.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48;
        }}>
        {notice && <p className="chat-note" role="status">{notice}</p>}
        {loading && <p className="chat-note">Loading the conversation…</p>}
        {((head.isError && !goneHead) || records.isError) && <p role="alert" className="chat-error">Could not load the conversation.{" "}
          <button className="btn ghost" type="button" onClick={() => void (head.isError ? head.refetch() : records.refetch())}>Retry</button></p>}
        {unopened && !stale && sentAt === null && <p className="chat-note" role="status">
          This conversation has no session on this server yet. It starts when the assistant hears its first line.</p>}
        {stale && <p className="chat-error" role="alert">The assistant has not opened this chat. Your line may not have been
          delivered; send it again.</p>}
        {lines.length === 0 && !loading && !notice && !unopened && live && <p className="chat-note">
          Say something to {partner?.actor ?? "your partner"}.</p>}
        {head.data && head.data.account !== me && <p className="chat-note">Started by {head.data.account}.</p>}
        {head.data?.forked_from && <p className="chat-note">Forked from{" "}
          <button className="btn ghost" type="button" onClick={() => void openConversation(head.data!.forked_from!)}>another chat</button>.</p>}
        {head.data && head.data.forks > 0 && <p className="chat-note">{forkedTimes(head.data.forks)}</p>}
        {outsider && <p className="chat-note" role="status">{outsiderNote(head.data!.audience)}</p>}
        {lines.map((line) => <ChatLine key={line.key} line={line} me={me} station={workspace?.name ?? ""} kinds={kinds}
          onFork={forkable ? pickFork : undefined} onFollowUp={line.key === followUpKey ? launchFollowUp : undefined} />)}
        {/* One working line, the last tool call's name, in place of a status the partner no longer sends.
            The reply being written takes its place, drawn as the answer will be; busy, so the log
            announces the answer once, when it is stored, and not each word on its way. */}
        {reply
          ? <article className="chat-line chat-line-assistant chat-line-partial" aria-busy="true">
              <Markdown source={reply.text} kinds={kinds} className="md chat-line-body" images={false} />
            </article>
          : statusText && <p className="chat-status" role="status">{statusText}</p>}
        {workspace && writing && !live && !loading && <ChatOff local={workspace.partner_choice === "local"} />}
      </div>
      {forkAt && <p className="chat-fork-banner" role="status">
        Your next message starts a fork of this chat after “{forkAt.quote}”.{" "}
        <button className="btn ghost" type="button" onClick={() => setForkAt(null)}>Cancel fork</button>
      </p>}
      <p className="chat-notice">{chatNotice(head.data?.audience ?? "open", orgMode)}</p>
      <Composer send={send} target={target} enabled={live && !reading && !loading && !outsider} editable
        placeholder={reading ? "Loading the conversation…" : outsider ? closedTo(head.data!.audience)
          : live ? `Ask ${partner?.actor ?? "your partner"}…` : readOnly(partner?.status, writing)}
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

/** Why a chat I do not read is closed to me: staff only and I hold no staff, or not shared with me. */
function outsiderNote(audience: string): string {
  if (audience === "staff") return "This chat is staff only and you hold no staff, so you can neither read it nor post in it.";
  const kept = audience === "private" ? "private and " : "";
  return `This chat is ${kept}not shared with you, so you can neither read it nor post in it. Ask its starter to share it.`;
}

/** The box's placeholder in a chat I do not read. */
function closedTo(audience: string): string {
  return audience === "staff" ? "This chat is staff only" : "This chat is not shared with you";
}

/**
 * A chat that is not open to whom the org mode lets in says so in its header:
 * made private by its starter, or staff only. The starter's own Public/Private
 * switch says it for them.
 */
function ChatBadge({ audience }: { readonly audience: string }) {
  const staff = audience === "staff";
  return <span className="chat-badge" title={staff
    ? "The assistant read a staff session into this chat, so only its readers who hold staff read it"
    : "Its starter made it private: only they and the people they share it with read it"}>{staff ? "Staff only" : "Private"}</span>;
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
const ChatLine = memo(function ChatLine({ line, me, station, kinds, onFork, onFollowUp }: {
  readonly line: Line;
  readonly me: string;
  /** The station's name, which the Terminal style's prompt shows. */
  readonly station: string;
  readonly kinds: readonly string[];
  /** Pick this stored answer as where a fork starts; absent where a fork cannot be started. */
  readonly onFork?: (at: LineAt, quote: string) => void;
  /** Launch the follow-up a recap proposes; absent where none is offered on this line. */
  readonly onFollowUp?: () => void;
}) {
  const other = line.role === "user" && line.author !== null && line.author !== me;
  const at = line.at;
  // Hovering a line shows when it was sent, in the reader's own time zone.
  const when = line.sentAt ? sentWhen(line.sentAt) : undefined;
  if (line.role === "relay") {
    // What the person told agents from the Console: "you → lead-2: text", neither a message to the partner nor an answer.
    const who = line.author === null || line.author === me ? "you" : line.author.split("@")[0];
    return <article className="chat-line chat-line-relay" title={when}><p className="chat-line-body">{who} {line.text}</p></article>;
  }
  return <article className={`chat-line chat-line-${line.role}${other ? " chat-line-other" : ""}${line.pending ? " chat-line-pending" : ""}`} title={when}>
    {other && <span className="chat-line-author">{line.author}</span>}
    {line.role === "assistant"
      ? <Markdown source={line.text} kinds={kinds} className="md chat-line-body" images={false} />
      : <p className="chat-line-body"><ChatPrompt author={line.author ?? me} station={station} />{line.text}</p>}
    {onFollowUp && line.role === "assistant" && followUpSuggested(line.text)
      && <button className="btn chat-line-followup" type="button" onClick={onFollowUp}>Launch the follow-up</button>}
    {onFork && at && line.role === "assistant" && <button className="btn ghost chat-line-fork" type="button" onClick={() => onFork(at, quoted(line.text))}>Fork from here</button>}
  </article>;
});

/** When a line was sent, in the reader's own time zone, for the hover title; "" for a time that will not parse. */
function sentWhen(iso: string): string {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? "" : `Sent ${at.toLocaleString()}`;
}

/** The start of a line, on one line, for the banner that says where a fork starts. */
function quoted(text: string): string {
  const flat = text.split(/\s+/).join(" ").trim();
  return flat.length > QUOTED ? `${flat.slice(0, QUOTED - 1)}…` : flat;
}

function forkedTimes(count: number): string {
  return count === 1 ? "Forked once." : `Forked ${count} times.`;
}

/** The record the page names by `Kind#seq`, once its id is found. */
function ChatScreenRef({ kind, seq }: { readonly kind: string; readonly seq: number }) {
  const id = useQuery(detailQueries.ref(kind, seq));
  return <ChatScreenContext id={id.data ?? null} label={`${kind}#${seq}`} />;
}

/** What is on screen: the record the page shows, as it changes. */
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
