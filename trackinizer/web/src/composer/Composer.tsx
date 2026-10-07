import { useMutation } from "@tanstack/react-query";
import { type ChangeEvent, type KeyboardEvent, type ReactNode, useId, useRef, useState } from "react";
import { newUuid } from "../api/idempotency";
import "./composer.css";

/** A draft as sent: its text, the idempotency key it went under, and where it went. */
type Sent = { readonly text: string; readonly key: string; readonly target: string };

/** The textarea as the composer sets it up, for a `field` that draws it. */
export type FieldProps = {
  readonly id: string;
  readonly value: string;
  readonly placeholder: string;
  readonly disabled: boolean;
  readonly rows: number;
  readonly onChange: (event: ChangeEvent<HTMLTextAreaElement>) => void;
  readonly onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
};

/**
 * A box for messages to live agent sessions, shared by an AgentSession's
 * detail, the canvas's Chat and the console.
 *
 * A draft goes under one idempotency key, which Retry sends again, so a send
 * whose answer was lost reaches the agent once; an edited draft, or the draft
 * sent to another `target`, takes a fresh key. Enter sends, and Shift+Enter (or
 * Enter while an input method composes) types a line break. A failure keeps the
 * draft and says `failure(error)`; a success empties the box and shows what
 * `send` resolved with. A draft `check` finds wrong (it names the problem, or
 * returns `""`) is not sent: the composer says why until the draft changes. Under a new `target`, the old key, failure and receipt
 * no longer apply. `field` draws the textarea from the props the composer gives
 * it, and may change the draft with `edit` as typing does (the console's adds
 * its @ suggestions); by default it is the plain textarea.
 *
 * By default the box is locked while a draft is sending. `editable` keeps it
 * open to typing (a draft typed meanwhile stays, as a send's success keeps it),
 * and Retry shows only for a failure `retryable` says a resend can mend (all of
 * them by default), since a refusal sent again is refused again. A send that
 * resolves with `""` shows no receipt, for a caller that shows its own.
 */
export function Composer({
  send,
  target,
  enabled,
  placeholder,
  failure,
  check = () => "",
  field = plainField,
  editable = false,
  retryable = () => true,
}: {
  send: (text: string, key: string) => Promise<string>;
  target: string;
  enabled: boolean;
  placeholder: string;
  failure: (error: Error) => string;
  check?: (text: string) => string;
  field?: (props: FieldProps, edit: (text: string) => void) => ReactNode;
  editable?: boolean;
  retryable?: (error: Error) => boolean;
}) {
  const id = useId();
  const [draft, setDraft] = useState("");
  const [sent, setSent] = useState<Sent | null>(null);
  const [receipt, setReceipt] = useState<{ readonly text: string; readonly target: string } | null>(null);
  const [refused, setRefused] = useState<{ readonly problem: string; readonly draft: string } | null>(null);
  const currentTarget = useRef(target);
  currentTarget.current = target;
  const sending = useMutation({
    mutationFn: (message: Sent) => send(message.text, message.key),
    onSuccess: (text, message) => {
      if (message.target !== currentTarget.current) return;
      setReceipt({ text, target: message.target });
      // A draft typed while this one was on its way stays.
      setDraft((current) => (current === message.text ? "" : current));
      setSent((current) => (current?.key === message.key ? null : current));
    },
  });
  /** Change the draft as typing does: a changed draft goes under a new key, and the receipt goes. */
  const edit = (text: string) => {
    setDraft(text);
    if (sent?.text !== text) {
      setSent(null);
      sending.reset();
    }
    setReceipt(null);
  };
  // The sent draft and the receipt name their target, so neither applies to another.
  const failed = sending.isError && sent?.text === draft && sent.target === target;
  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        if (!enabled || !draft.trim() || sending.isPending) return;
        const problem = check(draft);
        setRefused(problem ? { problem, draft } : null);
        if (problem) return;
        const message = sent?.text === draft && sent.target === target ? sent : { text: draft, key: newUuid(), target };
        setSent(message);
        setReceipt(null);
        sending.mutate(message);
      }}
    >
      <label htmlFor={id}>Message</label>
      {field(
        {
          id,
          value: draft,
          placeholder,
          disabled: !enabled || (sending.isPending && !editable),
          rows: 3,
          onChange: (event) => edit(event.currentTarget.value),
          onKeyDown: (event) => {
            if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
            event.preventDefault();
            event.currentTarget.form?.requestSubmit();
          },
        },
        edit,
      )}
      <div className="composer-actions">
        <button className="btn" type="submit" disabled={!enabled || !draft.trim() || sending.isPending}>
          Send message
        </button>
        {failed && sending.error && retryable(sending.error) ? (
          <button
            className="btn ghost"
            type="button"
            disabled={!enabled || sending.isPending}
            onClick={() => {
              if (sent) sending.mutate(sent);
            }}
          >
            Retry message
          </button>
        ) : null}
      </div>
      {failed && sending.error ? <p role="alert">{failure(sending.error)}</p> : null}
      {refused?.draft === draft ? <p role="alert">{refused.problem}</p> : null}
      {receipt?.target === target && receipt.text ? (
        <p className="composer-receipt" role="status">
          {receipt.text}
        </p>
      ) : null}
    </form>
  );
}

function plainField(props: FieldProps): ReactNode {
  return <textarea {...props} />;
}
