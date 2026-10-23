import { useQuery, useQueryClient } from "@tanstack/react-query";
import { memo, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { type Keyed, newUuid } from "../api/idempotency";
import { CHAT_DELETED, CHAT_FORK_CLOSED, CHAT_FORK_WIDER, type ChatFork, conversationOf, getChatHead, SCIENCE_CHAT_LABEL, sendChatLine } from "../api/chats";
import type { OrgMode } from "../api/me";
import { getCanvas } from "../api/canvases";
import type { CanvasPartner, CanvasState } from "../api/canvases";
import { type Provider, providerName, canvasProvider } from "../api/providers";
import { runStateOf, takenOver, waitingOf } from "../api/runState";
import { MetaContext, useProfile, useWriteMode } from "../app/boot";
import { Composer } from "../composer/Composer";
import { detailQueries } from "../detail/queries";
import { useLiveDetail } from "../live";
import { Markdown } from "../markdown/Markdown";
import { usePreferences } from "../preferences";
import { parseHash } from "../router/route";
import { useHash, validHash, visited } from "../router/trail";
import { canvasForChat, type CanvasSummary } from "../api/canvasManagement";
import { clearChat as clearCanvasChat, hideCanvas, CANVASES_KEY } from "../canvases/actions";
import { showCanvas } from "../canvases/useCanvas";
import { beginCanvasNavigation } from "../canvases/store";
import { acceptCanvas } from "../canvases/cache";
import { type ChatPart, type Goal, goalOf, type Line, type LineAt, linesThrough, openPrompts, PENDING, readTranscript, sentBefore, withPending } from "./chatLines";
import { chatRecordsKey, IDLE_CLOSE_MS, OPENING, readChatParts, useLiveChat } from "./chatRecords";
import { CloseChat } from "./ChatSharing";
import { type PendingPost, useChatFeed } from "./chatFeed";
import { FOLLOW_UP_ASK, followUpSuggested } from "./chatFollowUp";
import { useAnswerHighlights } from "./chatHighlights";
import { useChatPartial } from "./chatPartial";
import { useChatDraft } from "./chatDraft";
import { ChatToolCard } from "./ChatToolCard";
import { ChatWorkspaceHeader } from "./ChatWorkspaceHeader";
import { RecordField } from "./RecordField";
import { useReattach } from "./reattach";
import { type AnswerPrompt, type PromptAnswer, PromptCard } from "./PromptCard";

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
 * Talk with the canvas's partner in a science chat: a session the assistant
 * opens, read by whom the server's org mode lets in, as a Slack channel is by its
 * canvas. Its lines are the session's records (read as the Console reads
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
export function Chat({ canvas, onWaiting }: {
  readonly canvas: CanvasState | null;
  /** Told whether an agent waits on the person, as the header's dot says, whenever that changes. */
  readonly onWaiting?: (waiting: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const kinds = useContext(MetaContext)?.kinds ?? [];
  const { email: me, org_mode: orgMode, role } = useProfile();
  const writing = useWriteMode() === "enabled";
  const { feed, state } = useChatFeed();
  const canvasId = canvas?.id ?? null;
  const draft = useChatDraft(canvasId);
  const partner = canvas?.partner ?? null;
  // The canvas's chat: bound by its first line, and the same however Chat is mounted again.
  const conversationId = canvas?.conversation_id ?? null;
  const pending = state.pending.filter((post) => post.sender === me && post.canvasId === canvasId);
  const setPending = useCallback((change: (held: readonly PendingPost[]) => readonly PendingPost[]) => feed.updatePending(me, change), [feed, me]);
  // Retry keeps the first submission's confirmation boundary even if capture arrived after a lost receipt.
  const attempts = useRef(new Map<string, { readonly baseline: number; readonly submittedAt: number }>());
  const [sending, setSending] = useState<ReadonlySet<string>>(new Set());
  const [stopping, setStopping] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  // The line "Fork from here" picked: the next message starts a fork of this chat after it.
  const [forkAt, setForkAt] = useState<(LineAt & { readonly quote: string }) | null>(null);
  // When the last line was sent into the open conversation, while its session may still be on its way.
  const [sentAt, setSentAt] = useState<number | null>(null);
  const waitSince = sentAt ?? pending.filter((line) => line.conversationId === conversationId).at(-1)?.submittedAt ?? null;

  // The conversation's session: absent until the assistant has opened it.
  const head = useQuery({
    queryKey: ["chat", "head", conversationId],
    queryFn: ({ signal }) => getChatHead(conversationId!, { signal }),
    enabled: conversationId !== null,
    refetchInterval: (query) => (query.state.data === null && waitSince !== null && Date.now() - waitSince < OPENING.giveUpMs ? OPENING.everyMs : false),
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

  // The Settings switch for tool calls: each shows as a card between the lines, or
  // none does.
  const tools = usePreferences().chat_tool_calls;
  const transcript = useMemo(() => readTranscript(parts ?? [], { tools }), [parts, tools]);
  // What the answers cite lights up on the page as they arrive (the Settings switch turns it off).
  const answers = useMemo(() => parts === undefined ? null
    : transcript.lines.filter((line) => line.role === "assistant").map(({ key, text }) => ({ key, text, pointed: transcript.pointed.has(key) })), [parts, transcript]);
  useAnswerHighlights(answers, session);
  const lines = withPending(transcript, pending.filter((line) => line.conversationId === conversationId), { me });
  const loading = readable !== null && records.data === undefined && !records.isError;
  const unopened = conversationId !== null && head.data === null && !head.isFetching;
  const stale = unopened && waitSince !== null && Date.now() - waitSince >= OPENING.giveUpMs;
  // The give-up time is not a render of its own: the last look may come just before it.
  const [, tick] = useState(0);
  useEffect(() => {
    if (waitSince === null) return;
    const timer = setTimeout(() => tick((n) => n + 1), Math.max(0, waitSince + OPENING.giveUpMs - Date.now()) + 1);
    return () => clearTimeout(timer);
  }, [waitSince]);

  useEffect(() => {
    if (parts === undefined || conversationId === null) return;
    const stored = sentBefore(transcript.lines, { me });
    setPending((held) => {
      const remaining = held.filter((line) => line.conversationId !== conversationId || stored < line.baseline + 1);
      return remaining.length === held.length ? held : remaining;
    });
  }, [parts, transcript, conversationId, me, setPending, state.pending]);

  const pickFork = useCallback((at: LineAt, quote: string) => setForkAt({ ...at, quote }), []);
  // "Launch the follow-up" on a recap sends the canned ask as any typed line would, through the latest
  // `send` (a fresh closure each render); a ref keeps the handler's identity stable, and a guard drops a
  // second press while the first is on its way.
  const sendRef = useRef<(text: string, key: string) => Promise<string>>(() => Promise.resolve(""));
  const launching = useRef(false);
  // The key a follow-up went under and where it went, as the composer keeps a draft's: a resend reuses it only to the same target.
  const followUpKeyRef = useRef<{ readonly key: string; readonly target: string } | null>(null);
  const targetRef = useRef("");
  const launchFollowUp = useCallback(async () => {
    if (launching.current) return;
    launching.current = true;
    // A press after a lost answer resends under the same key, so the ask reaches the assistant once.
    const to = targetRef.current;
    if (followUpKeyRef.current?.target !== to) followUpKeyRef.current = { key: newUuid(), target: to };
    try {
      await sendRef.current(FOLLOW_UP_ASK, followUpKeyRef.current.key);
      followUpKeyRef.current = null;
    } catch (error) {
      if (!(error instanceof Error && resendable(error))) followUpKeyRef.current = null;
      if (mounted.current) setNotice(failureText(error));
    } finally {
      launching.current = false;
    }
  }, []);
  // A prompt's card answers through the same ref pattern, so its identity stays stable for the memoized lines.
  const answerRef = useRef<AnswerPrompt>(() => Promise.resolve(false));
  const answerStable = useCallback<AnswerPrompt>((attempt) => answerRef.current(attempt), []);
  // The line a message sent now would fork after: the one "Fork from here" picked.
  const fork: ChatFork | null = session && forkAt ? { sessionId: session, part: forkAt.part, idx: forkAt.idx } : null;

  const last = lines.at(-1);
  const sendingLast = last?.pending === true && sending.has(last.key.slice(PENDING.length));
  const unconfirmed = waitSince !== null && Date.now() - waitSince >= OPENING.giveUpMs;
  const waiting = last?.pending ? (sendingLast ? "Sending…" : unconfirmed
    ? "Delivery is unconfirmed. Check the session before resending." : "Waiting for the assistant…") : "";
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
  const target = JSON.stringify([canvasId, partner?.session_id ?? null, fork]);
  // A run taken over in a terminal takes no line from Chat until it is reattached.
  const taken = takenOver(partner);
  const live = !!canvas && partner?.status === "live" && writing && !taken;
  // Who reads this chat, and so whether I may read it and type in it, is known once its head has been read.
  const reading = conversationId !== null && head.isPending;
  // Only an open chat forks: a copy would not keep a hidden or staff-only chat's readers.
  const forkable = live && head.data?.audience === "open";
  const hash = useHash();
  const screen = parseHash(hash, kinds);

  // Another canvas is another conversation: what belonged to this one's pass with it. A
  // fork's own canvas keeps the wait for its session, which its first line began.
  const forkLanding = useRef<string | null>(null);
  const shownCanvas = useRef(canvasId);
  useEffect(() => {
    if (shownCanvas.current === canvasId) return;
    shownCanvas.current = canvasId;
    setNotice(null);
    setForkAt(null);
    setSentAt(forkLanding.current === canvasId ? Date.now() : null);
    forkLanding.current = null;
    stuck.current = true;
  }, [canvasId]);

  /** Switch to the user's canvas that holds `conversation`, which the server makes if there is none. */
  async function openConversation(conversation: string, current = beginCanvasNavigation()) {
    try {
      const state = await canvasForChat(conversation);
      acceptCanvas(queryClient, state);
      if (mounted.current && current()) showCanvas(queryClient, state);
    } catch (error) {
      if (mounted.current && current()) setNotice(error instanceof ApiError && error.code === CHAT_DELETED ? error.detail : "Could not open that chat.");
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
    if (!request || !canvasId) return;
    const current = beginCanvasNavigation();
    feed.taken(request.n);
    void queryClient.fetchQuery(detailQueries.detail(request.sessionId)).then((detail) => {
      if (!mounted.current || !current()) return;
      const conversation = conversationOf(typeof detail.self.cli_session_id === "string" ? detail.self.cli_session_id : null);
      const labels = Array.isArray(detail.self.labels) ? detail.self.labels : [];
      if (conversation && labels.includes(SCIENCE_CHAT_LABEL)) void openConversation(conversation, current);
      else setNotice("That session is not a science chat.");
    }, () => mounted.current && current() && setNotice("Could not open that chat."));
    // `openConversation` is the same function every render, but for what it closes over.
  }, [state.request, canvasId]);

  /** The chat is gone: the canvas holds no conversation, and the stream says so to every tab. */
  function chatDeleted(conversation: string) {
    queryClient.setQueryData<CanvasState>(["canvas", canvasId], (held) => held && { ...held, conversation_id: null });
    queryClient.setQueryData<CanvasSummary[]>(CANVASES_KEY, (held) =>
      held?.map((each) => (each.conversation_id === conversation ? { ...each, conversation_id: null } : each)));
    queryClient.removeQueries({ queryKey: ["chat", "head", conversation] });
    void queryClient.invalidateQueries({ queryKey: ["canvases"] });
  }

  async function hideCurrentCanvas() {
    if (!canvasId) return;
    try {
      await hideCanvas(queryClient, canvasId);
    } catch {
      setNotice("Could not hide this chat.");
    }
  }

  async function clearCurrentChat() {
    if (!canvasId) return;
    try {
      await clearCanvasChat(queryClient, canvasId, newUuid());
    } catch {
      setNotice("Could not clear this chat.");
    }
  }

  // A canvas that loses its conversation (deleted here or, by the stream, in another tab) shows an
  // empty chat: what belonged to the conversation goes with it.
  const heldConversation = useRef({ canvasId, conversationId });
  useEffect(() => {
    const lost = heldConversation.current;
    heldConversation.current = { canvasId, conversationId };
    if (lost.canvasId !== canvasId || lost.conversationId === null || conversationId !== null) return;
    setPending((held) => held.filter((line) => line.conversationId !== lost.conversationId));
    setForkAt(null);
    setSentAt(null);
  }, [canvasId, conversationId]);

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
    const attempt = attempts.current.get(key) ?? {
      baseline: sentBefore(fresh ? [] : after && forkOf ? linesThrough(lines, after) : lines, { me }), submittedAt: Date.now(),
    };
    attempts.current.set(key, attempt);
    const current = forkOf ? beginCanvasNavigation() : null;
    setPending((held) => [...held.filter((line) => line.key !== key), { key, text, canvasId: canvas!.id, conversationId: sentTo, ...attempt }]);
    setSending((held) => new Set(held).add(key));
    try {
      const { page, trail } = whereFrom(location.hash);
      const sent = await sendChatLine({ canvasId: canvas!.id, text, conversationId: forkOf ? null : sentTo,
        ...(forkOf && { fork: forkOf }), page, trail }, key);
      setPending((held) => held.map((line) => (line.key === key ? { ...line, canvasId: sent.canvas_id, conversationId: sent.conversation_id } : line)));
      if (sent.canvas_id === canvas!.id) {
        // The first line binds the conversation to the canvas; the canvas's state says so from here.
        queryClient.setQueryData<CanvasState>(["canvas", sent.canvas_id], (held) => held && { ...held, conversation_id: sent.conversation_id });
        if (mounted.current && shownCanvas.current === canvasId) {
          setForkAt(null);
          setSentAt(Date.now());
        }
      } else {
        // A fork is a canvas of its own, which this tab now shows, on the page it is on.
        const state = await getCanvas(sent.canvas_id);
        acceptCanvas(queryClient, state);
        if (mounted.current && current?.()) {
          forkLanding.current = sent.canvas_id;
          showCanvas(queryClient, state, window.location.hash);
        }
      }
      void queryClient.invalidateQueries({ queryKey: ["canvases"] });
      void queryClient.invalidateQueries({ queryKey: ["chat", "head", sent.conversation_id] });
      attempts.current.delete(key);
      return "";
    } catch (error) {
      setPending((held) => held.filter((line) => line.key !== key));
      if (!(error instanceof Error && resendable(error))) attempts.current.delete(key);
      throw error;
    } finally {
      setSending((held) => {
        const next = new Set(held);
        next.delete(key);
        return next;
      });
    }
  }

  /**
   * Answer a prompt the claude run asked: the decision goes to the chat as its `answer` line, and no line
   * of mine shows for it, since the run's resolution record is what closes the card. A refusal is shown and
   * the card keeps its answer, to resend under the same key.
   */
  async function answerPrompt(attempt: Keyed<PromptAnswer>): Promise<boolean> {
    if (!canvas || !conversationId) return false;
    try {
      await sendChatLine({ canvasId: canvas.id, text: attempt.body.text, conversationId, kind: "answer", payload: attempt.body.payload,
        ...whereFrom(location.hash) }, attempt.key);
      return true;
    } catch (error) {
      if (mounted.current) setNotice(failureText(error));
      return false;
    }
  }

  /**
   * Stop the turn the run is on: an `interrupt` post, under a key of its own each press, since a second press is
   * a second ask to stop. A refusal is shown.
   */
  async function stop() {
    if (!canvas || !conversationId || stopping) return;
    setStopping(true);
    try {
      await sendChatLine({ canvasId: canvas.id, text: "interrupt", conversationId, kind: "interrupt", page: null, trail: [] }, newUuid());
    } catch (error) {
      if (mounted.current) setNotice(failureText(error));
    } finally {
      if (mounted.current) setStopping(false);
    }
  }

  sendRef.current = send;
  answerRef.current = answerPrompt;
  targetRef.current = target;
  // The last line, when it is a stored recap and posting is open, may carry a "keep going" button.
  // The same gate as the composer's: no follow-up while the chat is read, closed to me, or a line is on its way.
  const followUpKey = live && !reading && !loading && !outsider && sending.size === 0 && last && !last.pending && last.role === "assistant" ? last.key : null;
  // A prompt's card answers where a line could be posted now: the same gate as the composer's.
  const answerable = live && !reading && !outsider;
  // A claude or codex run on a turn, an ask open or not, stops on Stop or Esc; the assistant takes no interrupt.
  const turn = runStateOf(partner);
  const stoppable = answerable && (partner?.provider === "claude" || partner?.provider === "codex")
    && (turn === "running" || turn === "requires_action");
  // Nothing in flight -- no turn, no line on its way, no card open, no reply being written -- and quiet for
  // IDLE_CLOSE_MS since the last record or the last change Chat saw: the strip offers Close in place of Stop.
  const lastRecordMs = useMemo(() => {
    let latest = 0;
    for (const { records } of parts ?? []) for (const each of records) latest = Math.max(latest, each.timestamp ? Date.parse(each.timestamp) || 0 : 0);
    return latest;
  }, [parts]);
  const inFlight = turn === "running" || turn === "requires_action" || lines.some((line) => line.pending) || sending.size > 0
    || openPrompts(transcript) > 0 || !!reply;
  const changeKey = `${turn}|${inFlight}`;
  const [changedAt, setChangedAt] = useState(0);
  const seenChange = useRef(changeKey);
  useEffect(() => {
    if (seenChange.current === changeKey) return;
    seenChange.current = changeKey;
    setChangedAt(Date.now());
  }, [changeKey]);
  const quietSince = Math.max(lastRecordMs, changedAt);
  const idleAt = quietSince > 0 && !inFlight ? quietSince + IDLE_CLOSE_MS : null;
  const closable = idleAt !== null && Date.now() >= idleAt && answerable && role !== "viewer" && head.data != null;
  // The threshold is not an event of its own: the strip must appear when it passes, not on the next record.
  useEffect(() => {
    if (idleAt === null || Date.now() >= idleAt) return;
    const timer = setTimeout(() => tick((n) => n + 1), idleAt - Date.now() + 1);
    return () => clearTimeout(timer);
  }, [idleAt]);
  // The goal the run is working toward, which the strip above the box says until it is met or cleared.
  const goal = goalOf(transcript);
  // Whether an agent waits on the person: the server says so, or a card in the log is still open and Chat can answer
  // it. The log is live, so a card shows its dot at once, and one the server released shows none. A card left open
  // in a chat with no live run, or one taken over in a terminal, is not answered from here, so it is no dot.
  const needsYou = waitingOf(partner) === true || (answerable && openPrompts(transcript) > 0);
  useEffect(() => onWaiting?.(needsYou), [needsYou, onWaiting]);
  const { reattach, busy: reattaching } = useReattach(useCallback((message: string) => {
    if (mounted.current) setNotice(message);
  }, []));

  return <section className="chat-panel" aria-label="Chat">
    <ChatWorkspaceHeader canvas={canvas} head={head.data ?? undefined} waiting={needsYou}
      onHide={hideCurrentCanvas} onClear={clearCurrentChat}
      onDeleted={(conversation) => chatDeleted(conversation)} />
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
      {lines.map((line) => <ChatLine key={line.key} line={line} me={me} kinds={kinds} assistant={providerName("assistant", canvas)}
        onFork={forkable ? pickFork : undefined} onFollowUp={line.key === followUpKey ? launchFollowUp : undefined}
        onAnswer={answerable ? answerStable : undefined} />)}
      {/* One working line, the last tool call's name, in place of a status the partner no longer sends.
          The reply being written takes its place, drawn as the answer will be; busy, so the log
          announces the answer once, when it is stored, and not each word on its way. */}
      {reply
        ? <article className="chat-line chat-line-assistant chat-line-partial" aria-busy="true">
            <Markdown source={reply.text} kinds={kinds} className="md chat-line-body" images={false} />
          </article>
        : statusText && <p className="chat-status" role="status">{statusText}</p>}
      {taken && <TakenOver onReattach={writing && session ? () => void reattach(session) : undefined} busy={reattaching !== null} />}
      {canvas && writing && !live && !loading && !taken && <ChatOff provider={canvasProvider(canvas)} />}
    </div>
    {forkAt && <p className="chat-fork-banner" role="status">
      Your next message starts a fork of this chat after “{forkAt.quote}”.{" "}
      <span className="chat-fork-runs">{forkRuns(canvas)}</span>{" "}
      <button className="btn ghost" type="button" onClick={() => setForkAt(null)}>Cancel fork</button>
    </p>}
    <p className="chat-notice">{chatNotice(head.data?.audience ?? "open", orgMode)}</p>
    {draft.problem && <p role="status" className="chat-note">{draft.problem}</p>}
    {goal && <p className="chat-note chat-goal">{goalStrip(goal)}</p>}
    {stoppable ? <div className="chat-stop">
      <button className="btn ghost" type="button" disabled={stopping} aria-label="Stop" title="Interrupt the turn (Esc)" onClick={() => void stop()}>■</button>
    </div>
      : closable && canvas && head.data && <div className="chat-stop">
        <CloseChat head={head.data} bar onClose={() => hideCanvas(queryClient, canvas.id)} />
      </div>}
    {role === "viewer" ? <p className="chat-note chat-read-only" role="note">Viewers can read this chat but not post.</p>
      : <Composer send={send} target={target} draft={draft}
      field={(props, edit) => <RecordField field={stoppable ? { ...props, onKeyDown: (event) => {
        // Esc stops the turn, as in the CLI's own terminal; the box's draft stays.
        if (event.key !== "Escape" || event.defaultPrevented || event.nativeEvent.isComposing) return props.onKeyDown(event);
        event.preventDefault();
        void stop();
      } } : props} edit={edit} />}
      history={{ key: JSON.stringify([me, canvasId, conversationId]), entries: lines
        .filter((line) => line.role === "user" && line.author === me && (!line.pending || !sending.has(line.key.slice(PENDING.length))))
        .map((line) => line.text) }}
      enabled={live && !reading && !loading && !outsider} editable pending={sending.size > 0}
      placeholder={reading ? "Loading the conversation…" : outsider ? closedTo(head.data!.audience)
        : taken ? TAKEN_OVER : live ? `Ask ${partner?.actor ?? "your partner"}…` : readOnly(partner?.status, writing)}
      check={checkText} retryable={resendable}
      failure={failureText} />}
  </section>;
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

/** Why a draft is refused before sending: one too long. The composer sends no blank draft. */
function checkText(text: string): string {
  // The server's `max_length` counts code points; `.length` would count an emoji twice.
  const length = [...text].length;
  return length > MAX_TEXT ? `A message holds at most ${MAX_TEXT.toLocaleString("en")} characters; this one has ${length.toLocaleString("en")}.` : "";
}

function partnerGone(partner: CanvasPartner): string {
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

/** What a chat says once its run was taken over in a terminal. */
const TAKEN_OVER = "Taken over in a terminal";

/**
 * A chat whose run someone attached to in its own terminal: the headless run stopped, so
 * Chat takes no line until the run is reattached, which reopens it on the same session.
 * `onReattach` is absent where the reader may not reattach.
 */
function TakenOver({ onReattach, busy }: { readonly onReattach?: () => void; readonly busy: boolean }) {
  return <div className="chat-off chat-taken-over">
    <p className="chat-note">{TAKEN_OVER}. Chat sends nothing to it until it is reattached.</p>
    {onReattach && <button className="btn" type="button" disabled={busy} onClick={onReattach}>Reattach to Chat</button>}
  </div>;
}

/** What Chat says when its provider is not live: the assistant, or a CLI no run holds. */
function ChatOff({ provider }: { readonly provider: Provider }) {
  return <div className="chat-off">
    <p className="chat-note">{offCopy(provider)}</p>
  </div>;
}

/** Why a chat's provider is off, in words for each provider. */
export function offCopy(provider: Provider): string {
  return provider === "assistant" ? "Chat is off: the assistant is not connected."
    : `Chat is off: no ${provider} run holds this chat. Pick a machine.`;
}

/**
 * One line, by value: the transcript builds its lines afresh on every render,
 * so the panel's renders (a status, the canvas around it) leave each line's
 * Markdown alone unless its text changed. A line by someone else says who, and an answer
 * names the agent that wrote it: a CLI by its name, the server's assistant by `assistant`.
 */
const ChatLine = memo(function ChatLine({ line, me, kinds, assistant, onFork, onFollowUp, onAnswer }: {
  readonly line: Line;
  readonly me: string;
  readonly kinds: readonly string[];
  readonly assistant: string;
  /** Pick this stored answer as where a fork starts; absent where a fork cannot be started. */
  readonly onFork?: (at: LineAt, quote: string) => void;
  /** Launch the follow-up a recap proposes; absent where none is offered on this line. */
  readonly onFollowUp?: () => void;
  /** Answer the prompt a claude run asked; absent where Chat cannot post an answer now. */
  readonly onAnswer?: AnswerPrompt;
}) {
  const sender = line.role === "user" || line.role === "command";
  const other = sender && line.author !== null && line.author !== me;
  const at = line.at;
  // Hovering a line shows when it was sent, in the reader's own time zone.
  const when = line.sentAt ? sentWhen(line.sentAt) : undefined;
  if (line.prompt) return <PromptCard prompt={line.prompt} kinds={kinds} onAnswer={onAnswer} assistant={assistant} attached={line.attached === true} />;
  if (line.tool) return <ChatToolCard step={line.tool} kinds={kinds} />;
  if (line.delivery) return <article className="chat-line chat-line-note" title={when}>
    {other && <span className="chat-line-author">{line.author}</span>}
    <p className="chat-line-body">{line.text}</p>
    <p className="chat-note" role="status">{line.delivery.refused
      ? `Not delivered: ${line.delivery.reason}. Reattach and send it again.`
      : `Delivery is unconfirmed: ${line.delivery.reason}. Check the session before resending.`}</p>
  </article>;
  if (line.role === "relay") {
    // What the person told agents from the Console: "you → lead-2: text", neither a message to the partner nor an answer.
    const who = line.author === null || line.author === me ? "you" : line.author.split("@")[0];
    return <article className="chat-line chat-line-relay" title={when}><p className="chat-line-body">{who} {line.text}</p></article>;
  }
  // A goal, what a CLI printed for a command, a compaction and an interrupt are notes in the log, not anyone's words.
  if (line.role === "goal" || line.role === "reply" || line.role === "compaction" || line.role === "interrupt") return <article className="chat-line chat-line-note" title={when}><p className="chat-line-body">{line.text}</p></article>;
  // A command is its sender's line, set on the sender's side in monospace.
  const side = line.role === "command" ? "user chat-line-command" : line.role;
  return <article className={`chat-line chat-line-${side}${other ? " chat-line-other" : ""}${line.pending ? " chat-line-pending" : ""}`} title={when}>
    {other && <span className="chat-line-author">{line.author}</span>}
    {line.agent && <span className="chat-line-author chat-line-agent">{line.agent === "assistant" ? assistant : line.agent}</span>}
    {line.role === "assistant"
      ? <Markdown source={line.text} kinds={kinds} className="md chat-line-body" images={false} />
      : <p className="chat-line-body">{line.text}</p>}
    {onFollowUp && line.role === "assistant" && followUpSuggested(line.text)
      && <button className="btn chat-line-followup" type="button" onClick={onFollowUp}>Launch the follow-up</button>}
    {onFork && at && line.role === "assistant" && <button className="btn ghost chat-line-fork" type="button" onClick={() => onFork(at, quoted(line.text))}>Fork from here</button>}
  </article>;
});

/** What a failed send says: a refusal's own reason, or that a retry is safe. */
function failureText(error: unknown): string {
  return error instanceof ApiError && error.status >= 400 && error.status < 500 && error.detail
    ? error.detail : "Could not send this message. Retry to send the same draft safely.";
}

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

/**
 * Where a fork will run, which is the canvas's provider: the assistant by its name; a CLI on its live run's machine
 * and folder, or with no run yet, which the person picks a machine for after the fork.
 */
function forkRuns(canvas: CanvasState | null): string {
  const provider = canvasProvider(canvas);
  const name = providerName(provider, canvas);
  if (provider === "assistant") return `The fork runs on ${name}`;
  const partner = canvas?.partner ?? null;
  if (partner?.status !== "live") return `The fork runs on ${name} (no run yet: pick a machine after)`;
  const folder = partner.cwd ? ` in ${partner.cwd}` : "";
  return `The fork runs on ${name} on ${partner.machine ?? "your machine"}${folder}`;
}

/** The goal strip above the box: what the run is working toward, its last check, and how to stop it. */
function goalStrip({ condition, lastReason }: Goal): string {
  return `Goal: ${condition}${lastReason ? ` · last check: ${lastReason}` : ""} · /goal clear stops it`;
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
