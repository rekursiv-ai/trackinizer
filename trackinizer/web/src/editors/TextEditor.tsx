import { type FocusEvent, type KeyboardEvent, type MouseEvent, type ReactNode, useRef, useState } from "react";
import type { Detail } from "../api/detail";
import { editableFields } from "../api/fields";
import { useWriteMode } from "../app/boot";
import { keyCaps, useCommands } from "../commands/registry";
import type { Field } from "../detail/fields";
import { Icon } from "../ui/icons";
import { useCopy } from "../ui/toast";
import { fieldEdit, lastChangeOf, sameValue, showValue } from "../writes/edits";
import { useWrite } from "../writes/useWrite";
import { unsettled, WriteStatus } from "../writes/WriteStatus";
import { type Draft, useDraft, useDraftKeeper } from "./drafts";
import { inputText, parseInput } from "./values";
import "./editors.css";

/**
 * A text field, drawn by `children`, that opens as a Markdown editor on a
 * double-click or its Edit button: description, done-when, outcome, abstract.
 * A single click is left to select text and follow links. With `json`, the
 * field is an object edited as JSON, such as an Experiment's config.
 *
 * The draft is the editor's own, so re-renders and live refetches keep it
 * (COLD-02). When the stored value changes under it, the editor says who
 * changed it and offers both versions; a save checks again that nobody did.
 * A reason is optional. E opens the description; ⌘↵ or clicking away saves,
 * Escape cancels.
 * While a save is under way, or a failed one shows Retry and Discard, the editor
 * takes no input and cannot be cancelled (see `unsettled`):
 * the save goes through, and its toast offers Undo.
 *
 * The description, edited or only read, has a Copy button over its top right
 * corner, which copies its Markdown as stored.
 */
export function TextEditor({
  detail,
  field,
  json = false,
  children,
}: {
  detail: Detail;
  field: Field;
  json?: boolean;
  children: ReactNode;
}) {
  const mode = useWriteMode();
  const [draft, setDraft] = useDraft(detail.self.id, field.name);
  const editable = mode !== "hidden" && field.route !== undefined;
  const open = () => {
    if (mode === "enabled" && !draft) setDraft({ text: inputText(field.value, json ? "json" : "text"), base: field.value });
  };
  useCommands(editable && field.name === "description" ? [{ id: "detail.edit.description", title: "Edit description", keys: ["e"], run: open }] : []);
  const copy = field.name === "description" && typeof field.value === "string" && field.value ? <CopyText text={field.value} what="description" /> : null;
  if (!editable) {
    return copy ? (
      <div className="ed-text off">
        {children}
        {copy}
      </div>
    ) : (
      children
    );
  }
  if (draft) {
    return <OpenText detail={detail} field={field} json={json} draft={draft} setDraft={setDraft} />;
  }
  // A double-click on the text opens it, unless it lands on a link or a button.
  const onDoubleClick = (event: MouseEvent) => {
    if (!(event.target as Element).closest("a, button, summary")) open();
  };
  return (
    <div className={mode === "enabled" ? "ed-text" : "ed-text off"} onDoubleClick={onDoubleClick}>
      {children}
      {copy}
      <button
        type="button"
        className="btn ghost ed-open"
        onClick={open}
        disabled={mode === "disabled"}
        aria-label={`Edit ${field.look.label}`}
        title={field.name === "description" ? "Edit (E), or double-click the text" : "Edit, or double-click the text"}
      >
        <Icon name="edit" size={13} />
        Edit
      </button>
    </div>
  );
}

/** A button over a field's top right corner that copies `text`, the `what`'s as stored. */
function CopyText({ text, what }: { text: string; what: "title" | "description" }) {
  const copy = useCopy();
  return (
    <button
      type="button"
      className="icon-btn ed-copy"
      title={`Copy the ${what}`}
      aria-label={`Copy the ${what}`}
      onClick={() => void copy(text, `Copied the ${what}`)}
    >
      <Icon name="copy" size={13} />
    </button>
  );
}

/** The open editor: the draft, what the stored value did meanwhile, and the save. */
function OpenText({
  detail,
  field,
  json,
  draft,
  setDraft,
}: {
  detail: Detail;
  field: Field;
  json: boolean;
  draft: Draft;
  setDraft: (draft: Draft | null) => void;
}) {
  useDraftKeeper(detail.self.id, field.name, draft);
  const writer = useWrite();
  const offline = useWriteMode() === "disabled";
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const pending = unsettled(writer.state);
  const { label } = field.look;
  // Set once the editor is done, so a blur as it closes never saves again.
  const done = useRef(false);
  const close = () => {
    done.current = true;
    setDraft(null);
  };
  const cancel = () => {
    if (!pending) close();
  };
  const save = async () => {
    if (done.current || offline || pending) return;
    const parsed = parseInput(draft.text, json ? "json" : "text");
    if ("error" in parsed) return setError(parsed.error);
    if (sameValue(parsed.value, draft.base) && sameValue(field.value, draft.base)) return close();
    const to = parsed.value;
    const edit = fieldEdit({ id: detail.self.id, field: field.name, route: field.route!, label, from: draft.base, to, reason: reason.trim() });
    if ((await writer.run(edit)) !== null) close();
  };
  // Clicking away saves. Moving within the editor does not, nor does leaving the
  // window, which takes focus with it and gives it back on return.
  const onBlur = (event: FocusEvent<HTMLDivElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget) && document.hasFocus()) void save();
  };
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      void save();
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancel();
    }
  };
  // Focusable itself, so a click on its text, or on a button that Safari does not
  // focus, keeps focus in the editor rather than leaving it.
  return (
    <div className="md-edit" data-field={field.name} tabIndex={-1} onBlur={onBlur}>
      {field.name === "description" ? null : <span className="k">{label}</span>}
      <Changed
        detail={detail}
        field={field}
        draft={draft}
        pending={pending}
        onTheirs={() => setDraft({ text: inputText(field.value, json ? "json" : "text"), base: field.value })}
        onMine={() => setDraft({ ...draft, base: field.value })}
      />
      <textarea
        ref={focusAtEnd}
        className={json ? "mono" : undefined}
        aria-label={label}
        readOnly={pending}
        value={draft.text}
        placeholder={json ? "A JSON object" : `Add ${label.toLowerCase()}… Markdown, and Issue#123 links to other inquiries.`}
        onChange={(event) => {
          setDraft({ ...draft, text: event.target.value });
          setError(null);
        }}
        onKeyDown={onKeyDown}
      />
      {error ? (
        <span className="form-err" role="alert">
          {error}
        </span>
      ) : null}
      <div className="md-edit-foot">
        <span className="muted">
          {json ? "JSON" : "Markdown"} · <kbd>{keyCaps("$mod+Enter")[0]}</kbd> save · <kbd>Esc</kbd> cancel
        </span>
        <input
          className="field ed-why"
          aria-label="Reason"
          placeholder="Reason (optional)"
          readOnly={pending}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <WriteStatus state={writer.state} />
        <button type="button" className="btn ghost" onClick={cancel} disabled={pending}>
          Cancel
        </button>
        <button type="button" className="btn primary" onClick={() => void save()} disabled={pending || offline}>
          Save
        </button>
      </div>
    </div>
  );
}

/**
 * Say so when the stored value moved away from the one the draft began from:
 * who changed it, when the change log names them, and their version beside
 * the draft. Using theirs replaces the draft; keeping mine saves over theirs
 * without asking again. Neither changes a draft that is being saved.
 */
function Changed({
  detail,
  field,
  draft,
  pending,
  onTheirs,
  onMine,
}: {
  detail: Detail;
  field: Field;
  draft: Draft;
  pending: boolean;
  onTheirs: () => void;
  onMine: () => void;
}) {
  if (sameValue(field.value, draft.base)) return null;
  const who = lastChangeOf(detail.changes, field.name)?.actor ?? "Someone";
  return (
    <div className="ed-changed" role="status">
      <span>
        <b>{who}</b> changed {field.look.label.toLowerCase()} while you were editing.
      </span>
      <details>
        <summary>Their version</summary>
        <pre>{showValue(field.value)}</pre>
      </details>
      <span className="w-actions">
        <button type="button" className="btn" onClick={onTheirs} disabled={pending}>
          Use theirs
        </button>
        <button type="button" className="btn ghost" onClick={onMine} disabled={pending}>
          Keep mine
        </button>
      </span>
    </div>
  );
}

/**
 * The title, edited in place: a click opens it, Enter or leaving it saves,
 * Escape puts it back. A title cannot be empty, so an empty one is not saved.
 * While a save is under way, or a failed one shows Retry and Discard, it takes no
 * input, and Escape waits for the save. Edited or only read, it has a Copy button
 * over its top right corner, outside the heading so the heading reads as the title.
 */
export function TitleEditor({ detail }: { detail: Detail }) {
  const mode = useWriteMode();
  const { id, title } = detail.self;
  const [draft, setDraft] = useDraft(id, "title");
  if (draft && mode !== "hidden") return <OpenTitle detail={detail} draft={draft} setDraft={setDraft} />;
  return (
    <div className="d-title-row">
      <h1 className="d-title">
        {mode === "hidden" ? (
          title
        ) : (
          <button
            type="button"
            className="d-title-btn"
            disabled={mode === "disabled"}
            title="Edit the title"
            onClick={() => setDraft({ text: title, base: title })}
          >
            {title}
          </button>
        )}
      </h1>
      <CopyText text={title} what="title" />
    </div>
  );
}

function OpenTitle({ detail, draft, setDraft }: { detail: Detail; draft: Draft; setDraft: (draft: Draft | null) => void }) {
  const { id, kind, title } = detail.self;
  useDraftKeeper(id, "title", draft);
  const writer = useWrite();
  const offline = useWriteMode() === "disabled";
  const pending = unsettled(writer.state);
  // Set once the editor is done, so a blur as it closes never saves again.
  const done = useRef(false);
  const route = editableFields(kind).title!;
  const field: Field = { name: "title", look: { label: "Title", place: "title" }, route, value: title };
  const close = () => {
    done.current = true;
    setDraft(null);
  };
  const save = async () => {
    const text = draft.text.replace(/\s+/g, " ").trim();
    if (done.current || offline || pending) return;
    if (!text || (text === draft.base && sameValue(title, draft.base))) return close();
    if ((await writer.run(fieldEdit({ id, field: "title", route, label: "Title", from: draft.base, to: text }))) !== null) close();
  };
  // Leaving the editor saves; moving within it (to the notice's buttons) does not.
  const onBlur = (event: FocusEvent<HTMLDivElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget)) void save();
  };
  return (
    <div className="d-title-edit" onBlur={onBlur}>
      <textarea
        ref={focusAtEnd}
        className="d-title d-title-in"
        aria-label="Title"
        rows={1}
        readOnly={pending}
        value={draft.text}
        onChange={(event) => setDraft({ ...draft, text: event.target.value })}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return;
          if (event.key === "Enter") {
            event.preventDefault();
            void save();
          } else if (event.key === "Escape") {
            event.preventDefault();
            if (!pending) close();
          }
        }}
      />
      <Changed
        detail={detail}
        field={field}
        draft={draft}
        pending={pending}
        onTheirs={() => setDraft({ text: title, base: title })}
        onMine={() => setDraft({ ...draft, base: title })}
      />
      <WriteStatus state={writer.state} />
    </div>
  );
}

/**
 * Focus an editor as it opens, with the caret after its text. Once only: a
 * later click must leave the caret where it lands.
 */
function focusAtEnd(textarea: HTMLTextAreaElement | null): void {
  if (!textarea) return;
  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);
}
