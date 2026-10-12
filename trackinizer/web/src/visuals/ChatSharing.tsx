import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "../api/client";
import { type ChatHead, deleteChat, shareChat, unshareChat } from "../api/chats";
import { useToast } from "../ui/toast";
import "../stations/stations.css";

/** What the server's refusal codes say to the person. */
const REFUSALS: { readonly [code: string]: string } = {
  chat_not_starter: "Only the person who started this chat can do that.",
  chat_has_other_posters: "Others have posted in this chat, so only an admin can delete it.",
};

/** What a toast says of a refused or failed change: the words for its code, else the server's reason when it gave one. */
function reason(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback;
  const known = error.code === null ? undefined : REFUSALS[error.code];
  return known ?? (error.status >= 400 && error.status < 500 && error.detail ? error.detail : fallback);
}

/**
 * Chat's Share control: a panel with the people the chat is shared with, each
 * with Remove, and a field to add an address. Only the head's `may_share` shows it.
 */
export function ShareChat({ head }: { readonly head: ChatHead }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);

  /** Make one change, then read the head again: it is the list. */
  async function change(act: () => Promise<void>, failure: string): Promise<boolean> {
    setBusy(true);
    try {
      await act();
      await queryClient.invalidateQueries({ queryKey: ["chat", "head", head.conversation_id] });
      return true;
    } catch (error) {
      toast(reason(error, failure), { failed: true, error });
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="station chat-head-pop">
      <button className="btn ghost" type="button" aria-haspopup="dialog" aria-expanded={open}
        title="Share this chat" onClick={() => setOpen(!open)}>Share</button>
      {open && <div className="station-menu" role="dialog" aria-label="Share chat"
        onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); setOpen(false); } }}>
        <ul className="station-list" aria-label="Shared with">
          {head.shared_with.length === 0 && <li className="muted">Shared with nobody.</li>}
          {head.shared_with.map((who) => <li key={who} className="chat-share-row">
            <span>{who}</span>
            <button className="btn ghost" type="button" disabled={busy} aria-label={`Remove ${who}`}
              onClick={() => void change(() => unshareChat(head.conversation_id, who), `Could not stop sharing with ${who}.`)}>Remove</button>
          </li>)}
        </ul>
        <form className="station-rename" onSubmit={(event) => {
          event.preventDefault();
          const who = email.trim();
          if (who) void change(() => shareChat(head.conversation_id, who), `Could not share with ${who}.`).then((landed) => landed && setEmail(""));
        }}>
          <input type="email" aria-label="Email to share with" placeholder="Email to share with" value={email}
            onChange={(event) => setEmail(event.target.value)} />
          <button className="btn" type="submit" disabled={busy || !email.trim()}>Add</button>
        </form>
      </div>}
    </div>
  );
}

/**
 * Chat's Delete control, behind a confirm. Only the head's `may_delete` shows it.
 * `onDeleted` runs once the server has removed the chat.
 */
export function DeleteChat({ head, onDeleted }: { readonly head: ChatHead; readonly onDeleted: () => void }) {
  const toast = useToast();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  async function remove() {
    setBusy(true);
    try {
      await deleteChat(head.conversation_id);
      setConfirming(false);
      onDeleted();
    } catch (error) {
      toast(reason(error, "Could not delete this chat."), { failed: true, error });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="station chat-head-pop">
      <button className="btn ghost" type="button" aria-haspopup="dialog" aria-expanded={confirming}
        title="Delete this chat" onClick={() => setConfirming(!confirming)}>Delete</button>
      {confirming && <div className="station-menu" role="alertdialog" aria-label="Delete this chat"
        onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); setConfirming(false); } }}>
        <p className="chat-note">Delete this chat? Its forks keep their copies.</p>
        <div className="station-actions">
          <button className="btn ghost" type="button" disabled={busy} onClick={() => setConfirming(false)}>Cancel</button>
          <button className="btn danger" type="button" disabled={busy} onClick={() => void remove()}>Delete chat</button>
        </div>
      </div>}
    </div>
  );
}
