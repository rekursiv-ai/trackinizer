import { useQuery } from "@tanstack/react-query";
import { useState, type MouseEvent } from "react";
import { readRecentSessionTurns, type RecentSessionTurn } from "../api/sessions";
import { useMeta } from "../app/boot";
import { canvasContext, clipText } from "../detail/transcript/records";
import { Markdown } from "../markdown/Markdown";
import { formatRoute } from "../router/route";
import { useWorkspaceActions } from "./workspaceActions";

/** Show recent captured turns and a route to the session's complete transcript. */
export function ChatTranscript({ sessionId }: { readonly sessionId: string }) {
  const actions = useWorkspaceActions();
  const transcriptHref = formatRoute({ name: "lookup", id: sessionId });
  const records = useQuery({
    queryKey: ["chat", sessionId, "turns"],
    queryFn: ({ signal }) => readRecentSessionTurns(sessionId, { signal }),
    select: visibleTurns,
    refetchInterval: 5_000,
    retry: false,
  });
  const turns = records.data;

  function openTranscript(event: MouseEvent<HTMLAnchorElement>) {
    if (!actions || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (actions.busy) return;
    void actions.revealRecord(sessionId).then((revealed) => {
      if (revealed) window.location.hash = transcriptHref;
    });
  }

  return <section className="chat-transcript" aria-label="Recent captured turns">
    <div className="chat-transcript-head"><h3>Recent turns</h3>
      <a href={transcriptHref} aria-disabled={actions?.busy ?? false} tabIndex={actions?.busy ? -1 : undefined}
        onClick={openTranscript}>Full transcript</a>
    </div>
    {records.isPending && <p>Loading recent turns…</p>}
    {records.isError && <p role="alert">Could not load recent turns.
      <button type="button" className="btn ghost" onClick={() => void records.refetch()}>Retry</button></p>}
    {turns?.length === 0 && <p>No conversational turns yet.</p>}
    {turns?.map((turn) => <RecentTurn turn={turn} key={`${turn.part}:${turn.idx}`} />)}
  </section>;
}

type ChatTurn = { readonly part: number; readonly idx: number; readonly label: string; readonly text: string };

function RecentTurn({ turn }: { readonly turn: ChatTurn }) {
  const [expanded, setExpanded] = useState(false);
  const { kinds } = useMeta();
  const clipped = clipText(turn.text, 240);
  const text = expanded ? turn.text : clipped.shown;
  return <article className="chat-turn">
    <strong>{turn.label}</strong>{turn.label === "Assistant"
      ? <Markdown source={text} kinds={kinds} className="md chat-turn-body" images={false} />
      : <p>{text}</p>}
    {clipped.hidden > 0 && <button className="chat-turn-toggle" type="button" aria-expanded={expanded}
      onClick={() => setExpanded(!expanded)}>{expanded ? "Show less" : "Show full message"}</button>}
  </article>;
}

/** Hide pre-browser setup and the injected context carried after a user message. */
function visibleTurns(rows: RecentSessionTurn[]): ChatTurn[] {
  const turns = rows.map((row) => ({
    part: row.part,
    idx: row.idx,
    label: row.kind === "UserMessage" ? "User" : "Assistant",
    text: row.content,
  }));
  const browserStart = turns.findIndex((turn) => turn.label === "User" && canvasContext(turn.text).context !== null);
  return (browserStart >= 0 ? turns.slice(browserStart) : turns).map((turn) =>
    turn.label === "User" ? { ...turn, text: canvasContext(turn.text).text } : turn);
}
