import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { getWorkspaceConnectionStatus, listConnectableSessions, sendWorkspaceMessage } from "../api/workspaces";
import { Composer } from "../composer/Composer";
import { detailQueries } from "../detail/queries";
import { useLiveDetail } from "../live";
import { formatRoute } from "../router/route";
import { ChatTranscript } from "./ChatTranscript";
import type { RendererProps } from "./registry";
import { useWorkspaceActions } from "./workspaceActions";

/** Connect the canvas to a live trax run session chosen by its signed-in user. */
export function ChatConnect({ instance, workspace }: RendererProps) {
  const actions = useWorkspaceActions();
  const [pickerOpen, setPickerOpen] = useState(false);
  const sessionId = workspace?.connected_session_id;
  const sessions = useQuery({
    queryKey: ["workspace", "connectable-sessions"],
    queryFn: ({ signal }) => listConnectableSessions({ signal }),
    enabled: !!workspace && (pickerOpen || !!sessionId),
    refetchInterval: sessionId ? 5_000 : false,
    retry: false,
  });
  const chatInstanceId = workspace?.visuals.some((visual) => visual.id === instance.id && visual.type === "trax.chat")
    ? instance.id : null;
  const expectedRecordId = instance.record_id ?? null;
  const target = JSON.stringify([workspace?.id ?? null, sessionId, chatInstanceId, expectedRecordId]);
  const connection = useQuery({
    queryKey: ["workspace", workspace?.id, "connection", sessionId],
    queryFn: ({ signal }) => getWorkspaceConnectionStatus(workspace!.id, { signal }),
    enabled: !!workspace && !!sessionId,
    refetchInterval: sessionId ? 5_000 : false,
    retry: false,
  });
  const recordId = instance.record_id;
  const directSessionId = connection.data?.session_id;
  const isLive = !connection.isError && connection.data?.status === "live" && directSessionId === sessionId;
  const transcriptSessionId = directSessionId === sessionId && directSessionId
    && (connection.data?.status === "live" || connection.data?.status === "ended") ? directSessionId : null;
  // `target` names the workspace, session, chat and record, so a retry under one
  // key only ever repeats a send to the same ones.
  async function send(text: string, key: string): Promise<string> {
    await sendWorkspaceMessage(workspace!.id, text, chatInstanceId, expectedRecordId, key);
    return `Queued for ${connection.data?.actor ?? "connected agent"}`;
  }

  function changeSession(sessionId: string | null) {
    if (actions) {
      actions.connectSession(sessionId);
      setPickerOpen(false);
    }
  }

  return (
    <section className="chat-connect" aria-label="Chat">
      <div className="chat-connect-inner">
        <div className="chat-connect-mark" aria-hidden="true">✳</div>
        <h2>Work with an agent</h2>
        {recordId && <ChatRecordContext recordId={recordId} />}
        {sessionId && connection.isError ? <p className="chat-session-live">Paired session status unavailable · {sessionId}</p>
          : sessionId && connection.isPending ? <p className="chat-session-live">Checking paired session · {sessionId}</p>
            : isLive ? <p className="chat-session-live">Connected to {connection.data?.actor ?? "agent"} · {connection.data?.cli ?? "agent"}</p>
              : connection.data?.status === "live" ? <p className="chat-session-live" role="status">Could not confirm the paired session</p>
              : connection.data?.status === "ended" ? <p className="chat-session-live" role="status">Session ended · {sessionId}</p>
                : connection.data?.status === "unavailable" ? <p className="chat-session-live" role="status">Session unavailable · {sessionId}</p>
                  : <p>Connect a running trax session to explore records together.</p>}
        {sessionId && connection.isError && <p role="alert">Could not verify the paired session. <button className="btn ghost"
          type="button" onClick={() => void connection.refetch()}>Retry session lookup</button></p>}
        {actions?.writeError && <p role="alert">{actions.writeError}</p>}
        <div className="chat-connect-command"><code>trax run codex</code><span>or</span><code>trax run claude</code></div>
        <div className="chat-connect-actions">
          <button className="btn" type="button" disabled={!workspace || actions?.busy || !actions}
            onClick={() => setPickerOpen(!pickerOpen)}>{sessionId ? "Change session" : "Connect session"}</button>
          {sessionId && <button className="btn ghost" type="button" disabled={actions?.busy || !actions}
            onClick={() => changeSession(null)}>Disconnect</button>}
        </div>
        {pickerOpen && <div className="chat-session-picker" aria-label="Live sessions">
          {sessions.isPending && <p>Looking for live sessions…</p>}
          {sessions.isError && <p role="alert">Could not list sessions. <button className="btn" type="button"
            onClick={() => void sessions.refetch()}>Retry</button></p>}
          {sessions.isSuccess && sessions.data.length === 0 && <p>No live sessions found. Start one with a command above.</p>}
          {sessions.data?.map((session) => <button key={session.id} className="chat-session-option" type="button"
            disabled={actions?.busy || !actions} onClick={() => changeSession(session.id)}
            aria-label={`Connect ${session.title}`}>
            <strong>{session.title}</strong><span>{session.actor} · {session.cli ?? "agent"}</span>
          </button>)}
        </div>}
        {transcriptSessionId && <ChatTranscript sessionId={transcriptSessionId} />}
        <Composer send={send} target={target} enabled={isLive && !!workspace}
          placeholder={isLive ? "Write a message…" : "Connect a live session to start chatting"}
          failure={() => "Could not queue this message. Retry to send the same draft safely."} />
      </div>
    </section>
  );
}

function ChatRecordContext({ recordId }: { readonly recordId: string }) {
  const actions = useWorkspaceActions();
  const record = useQuery(detailQueries.detail(recordId));
  useLiveDetail(recordId);
  const href = formatRoute({ name: "lookup", id: recordId });

  return (
    <p className="chat-record-context" aria-label="Record context">
      Context: <a href={href} aria-disabled={actions?.busy ?? false}
        tabIndex={actions?.busy ? -1 : undefined}
        onClick={(event) => {
          if (!actions) return;
          if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          if (actions.busy) return;
          void actions.revealRecord(recordId).then((revealed) => {
            if (revealed) window.location.hash = href;
          });
        }}>
        {record.data ? `${record.data.self.kind}#${record.data.self.seq} ${record.data.self.title}` : recordId}
      </a>
    </p>
  );
}
