import { useQueryClient } from "@tanstack/react-query";
import { type RefObject, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { CHAT_STAFF_ONLY, type ChatHead, deleteChat, shareChat, unshareChat } from "../api/chats";
import { newUuid } from "../api/idempotency";
import { SESSION_NOT_OWNER, SESSION_STAFF, setSessionAudience } from "../api/sessions";
import { useToast } from "../ui/toast";
import "../stations/stations.css";

/** What the server's refusal codes say to the person. */
const REFUSALS: { readonly [code: string]: string } = {
  chat_not_starter: "Only the person who started this chat can do that.",
  chat_has_other_posters: "Others have posted in this chat, so only an admin can delete it.",
  [CHAT_STAFF_ONLY]: "This chat is staff only, so it can be shared only with someone who holds staff.",
  [SESSION_NOT_OWNER]: "Only the person who started this chat can make it public or private.",
  [SESSION_STAFF]: "This chat is staff only, so it is never made public or private.",
};

/** What a toast says of a refused or failed change: the words for its code, else the server's reason when it gave one. */
function reason(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback;
  const known = error.code === null ? undefined : REFUSALS[error.code];
  return known ?? (error.status >= 400 && error.status < 500 && error.detail ? error.detail : fallback);
}

/**
 * Close an open pop-over when a press lands outside `root`: anywhere else on the
 * page, the other header control included, so one pop-over shows at a time.
 */
function useCloseOutside(open: boolean, root: RefObject<HTMLElement | null>, setOpen: (open: boolean) => void): void {
  useEffect(() => {
    if (!open) return;
    const press = (event: PointerEvent) => {
      if (!(event.target instanceof Node) || !root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("pointerdown", press, true);
    return () => document.removeEventListener("pointerdown", press, true);
  }, [open, root, setOpen]);
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
  const root = useRef<HTMLDivElement>(null);
  useCloseOutside(open, root, setOpen);

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
    <div className="station chat-head-pop" ref={root}>
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
 * Chat's Public/Private switch, which only its starter has (the head's
 * `may_hide`): it says which the chat is, and a click flips it. Private keeps
 * the chat to its starter and the people it is shared with, whatever the
 * server's org mode lets in; Public opens it to them again. Every session of the
 * chat changes; the head read again says which it is now.
 */
export function PrivacySwitch({ head }: { readonly head: ChatHead }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const isPrivate = head.audience === "private";

  async function flip() {
    setBusy(true);
    try {
      await setSessionAudience(head.session_id, isPrivate ? "open" : "private", newUuid());
      await queryClient.invalidateQueries({ queryKey: ["chat", "head", head.conversation_id] });
    } catch (error) {
      toast(reason(error, isPrivate ? "Could not make this chat public." : "Could not make this chat private."), { failed: true, error });
    } finally {
      setBusy(false);
    }
  }

  return (
    <button className="btn ghost" type="button" disabled={busy}
      aria-label={isPrivate ? "Private chat: make it public" : "Public chat: make it private"}
      title={isPrivate
        ? "Private: only you and the people you share it with read it. Click to let whoever this server lets in read it."
        : "Public: whoever this server lets in reads it. Click to keep it to you and the people you share it with."}
      onClick={() => void flip()}>{isPrivate ? "Private" : "Public"}</button>
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
  const root = useRef<HTMLDivElement>(null);
  useCloseOutside(confirming, root, setConfirming);

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
    <div className="station chat-head-pop" ref={root}>
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
