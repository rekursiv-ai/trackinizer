import { Dialog } from "radix-ui";
import { type ChangeEvent, type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import type { InquiryKind } from "../api/inquiries";
import { useMeta, useProfile, useWriteMode } from "../app/boot";
import { useDraftSaver } from "../app/session";
import { keyCaps } from "../commands/registry";
import type { Field } from "../detail/fields";
import { inputKind } from "../editors/values";
import { Menu } from "../lists/Menu";
import { useRouter } from "../router/router";
import { capitalize } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { KindIcon, kindLook } from "../ui/kinds";
import { useWrite } from "../writes/useWrite";
import { WriteButton } from "../writes/WriteButton";
import { unsettled, WriteStatus } from "../writes/WriteStatus";
import { Chips } from "./Chips";
import {
  createEdit,
  creatableKinds,
  type Draft,
  emptyDraft,
  formFields,
  nextDraft,
  type Related,
  relationOptions,
  switchKind,
} from "./draft";
import { Relations } from "./Relations";
// The form's inputs and its dialog are the editors' and the write layer's.
import "../editors/editors.css";
import "../writes/writes.css";
import "./create.css";

/**
 * The create form, `#/new/<Kind>`, over the view it was opened from: the mock's
 * "New issue" dialog, for every kind but AgentSession.
 *
 * It holds the title and description, the kind's own fields, relations, and the
 * chips, and sends them as one request (`createEdit`). Create opens the new
 * inquiry; with Create more on, the form stays open for the next one, keeping
 * the kind and the chips. The kind menu switches kinds and keeps what both share.
 * ⌘↵ creates; Escape and Cancel call `onClose`, keeping a started draft for the
 * next open of the kind. A viewer is told creating needs a writer; offline,
 * Create is off, while the draft can still be written.
 * While the create is sent, the form takes no input; closed meanwhile, it
 * opens nothing once the create lands, whose toast still says so.
 */
export function CreateView({ kind, onClose }: { kind: string; onClose: () => void }) {
  const { kinds } = useMeta();
  const mode = useWriteMode();
  const creatable = creatableKinds(kinds);
  const shown = creatable.find((candidate) => candidate === kind);
  if (mode !== "hidden" && shown) return <CreateForm kind={shown} creatable={creatable} onClose={onClose} />;
  return (
    <Refusal
      title={`New ${kindLook(kind).one}`}
      onClose={onClose}
      why={
        mode === "hidden"
          ? "Your role can read but not create. Creating needs the writer role."
          : `${kindLook(kind).plural} are recorded by trax run, not created here.`
      }
    />
  );
}

function CreateForm({ kind, creatable, onClose }: { kind: InquiryKind; creatable: readonly InquiryKind[]; onClose: () => void }) {
  const { edges, fieldOwners } = useMeta();
  const { email } = useProfile();
  const { navigate } = useRouter();
  const writer = useWrite();
  const writable = useWriteMode() === "enabled";
  const [draft, setDraft] = useState(() => savedDraft(email, kind) ?? emptyDraft(kind));
  // It is open now, and saved again if the session ends while it is.
  useEffect(() => forgetSavedDraft(email), [email]);
  // The kind menu changes the route; the draft follows it.
  if (draft.kind !== kind) setDraft(switchKind(draft, kind, edges));
  const [more, setMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const titleInput = useRef<HTMLInputElement>(null);
  const opener = useRef<Element | null>(null);
  useDraftSaver(() => saveDraft(email, draft));
  const fields = useMemo(() => formFields(kind, fieldOwners), [kind, fieldOwners]);
  const options = useMemo(() => relationOptions(kind, edges), [kind, edges]);
  const pending = unsettled(writer.state);
  const one = kindLook(kind).one;
  const update = (change: Partial<Draft>) => {
    setDraft({ ...draft, ...change });
    setError(null);
  };
  // Closed with Escape, Cancel or a click outside, a started draft is kept for
  // the next open of this kind, as the session's end keeps it. One whose create
  // is being sent is not: it is about to land.
  const close = () => {
    if (!pending && started(draft)) saveDraft(email, draft);
    onClose();
  };

  const create = async () => {
    if (pending || !writable || !draft.title.trim()) return;
    const made = createEdit(draft, fields);
    if ("error" in made) return setError(made.error);
    const id = await writer.run(made.edit);
    if (id === null) return;
    if (!more) return navigate({ name: "lookup", id }, { replace: true });
    setDraft((current) => nextDraft(current));
    titleInput.current?.focus();
  };
  const onKeyDown = (event: KeyboardEvent<HTMLFormElement>) => {
    // Keys typed in a chip's menu reach here too, through React's tree, from its portal.
    if (event.key !== "Enter" || event.nativeEvent.isComposing || !event.currentTarget.contains(event.target as Node)) return;
    if (event.metaKey || event.ctrlKey) {
      event.preventDefault();
      void create();
    } else if (event.target instanceof HTMLInputElement) {
      // Enter in a one-line field would submit the form; only ⌘↵ and the button create.
      event.preventDefault();
    }
  };

  return (
    <Dialog.Root open onOpenChange={(open) => open || close()}>
      <Dialog.Portal>
        <Dialog.Overlay className="modal-backdrop" />
        <Dialog.Content
          className="modal cr"
          aria-describedby={undefined}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            opener.current = document.activeElement;
            titleInput.current?.focus();
          }}
          onCloseAutoFocus={(event) => {
            // Opened by the C shortcut, the form has no button to give focus back
            // to (the page body had it), or the row that had it was drawn anew
            // meanwhile: the view's current row takes focus, not the page body.
            const back =
              opener.current instanceof HTMLElement && opener.current !== document.body && opener.current.isConnected
                ? opener.current
                : document.querySelector<HTMLElement>('.main [aria-current="true"]');
            if (!back) return;
            event.preventDefault();
            back.focus();
          }}
        >
          <form
            onSubmit={(event) => {
              // The exact priority's own form submits through here too, from its portal.
              if (event.target !== event.currentTarget) return;
              event.preventDefault();
              void create();
            }}
            onKeyDown={onKeyDown}
          >
            <div className="modal-h">
              <Menu
                label="Kind…"
                trigger={
                  <button type="button" className="chip-btn" aria-label={`Kind: ${kindLook(kind).plural}`} disabled={pending}>
                    <KindIcon kind={kind} size={14} />
                    {kindLook(kind).plural}
                    <Icon name="chevD" size={12} />
                  </button>
                }
                options={creatable.map((other) => ({
                  value: other,
                  label: kindLook(other).plural,
                  icon: <KindIcon kind={other} size={14} />,
                  checked: other === kind,
                }))}
                onPick={(other) => navigate({ name: "new", kind: other }, { replace: true })}
              />
              <Icon name="chevR" size={12} />
              <Dialog.Title asChild>
                <span>New {one}</span>
              </Dialog.Title>
              <div className="spacer" />
              <Dialog.Close className="icon-btn" aria-label="Close">
                <Icon name="x" size={16} />
              </Dialog.Close>
            </div>
            <div className="modal-b">
              <input
                ref={titleInput}
                className="t-in"
                aria-label="Title"
                placeholder={`${capitalize(one)} title`}
                autoComplete="off"
                readOnly={pending}
                value={draft.title}
                onChange={(event) => update({ title: event.target.value })}
              />
              <textarea
                className="d-in"
                aria-label="Description"
                placeholder="Add description… Markdown and Issue#123 links work here."
                readOnly={pending}
                value={draft.description}
                onChange={(event) => update({ description: event.target.value })}
              />
              {fields.length ? (
                <div className="cf-grid">
                  {fields.map((field) => (
                    <FormField
                      key={field.name}
                      field={field}
                      pending={pending}
                      text={draft.fields[field.name] ?? ""}
                      onText={(text) => update({ fields: { ...draft.fields, [field.name]: text } })}
                    />
                  ))}
                </div>
              ) : null}
              <Relations
                options={options}
                relations={draft.relations}
                disabled={pending}
                onChange={(relations) => update({ relations })}
              />
              <Chips
                chips={draft.chips}
                disabled={pending}
                onChange={(field, value) => update({ chips: { ...draft.chips, [field]: value } })}
              />
              {error ? (
                <div className="form-err" role="alert">
                  {error.split("\n").map((line) => (
                    <span key={line}>{line}</span>
                  ))}
                </div>
              ) : null}
              <WriteStatus state={writer.state} />
            </div>
            <div className="modal-f">
              <button type="button" className="toggle" aria-pressed={more} onClick={() => setMore(!more)}>
                <i />
                Create more
              </button>
              <div className="spacer" />
              <Dialog.Close className="btn ghost">
                Cancel
              </Dialog.Close>
              <WriteButton
                type="submit"
                className="btn primary"
                pending={pending}
                disabled={!writable || !draft.title.trim()}
                title={writable ? undefined : "You are offline"}
              >
                Create {one} <kbd>{keyCaps("$mod+Enter")[0]}</kbd>
              </WriteButton>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/**
 * One of the kind's own fields: a menu of its values when the server lists them,
 * a text box for long text or JSON, and otherwise the input its value type calls
 * for. A list takes its items comma-separated. Nothing changes while `pending`.
 */
function FormField({ field, pending, text, onText }: { field: Field; pending: boolean; text: string; onText: (text: string) => void }) {
  const { enums } = useMeta();
  const route = field.route!;
  const kind = inputKind(route, field.look);
  const values = enums[field.name];
  const list = route.value.type === "array";
  const wide = field.look.place === "text" || (kind === "json" && !list);
  const input = {
    value: text,
    onChange: (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => onText(event.target.value),
  };
  return (
    <label className={wide ? "wide" : undefined}>
      {field.look.label}
      {values ? (
        <select {...input} className="field" disabled={pending}>
          <option value="">—</option>
          {values.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      ) : wide ? (
        <textarea
          {...input}
          className={kind === "json" ? "field mono" : "field"}
          readOnly={pending}
          rows={3}
          placeholder={kind === "json" ? "A JSON object" : "Markdown"}
        />
      ) : (
        <input
          {...input}
          className={field.look.mono ? "field mono" : "field"}
          type={list ? "text" : INPUT_TYPES[kind]}
          step={kind === "integer" ? 1 : kind === "number" ? "any" : undefined}
          placeholder={list ? "Comma-separated" : undefined}
          autoComplete="off"
          readOnly={pending}
        />
      )}
    </label>
  );
}

/** A route the form cannot serve: a viewer's, or a kind nobody creates here. */
function Refusal({ title, why, onClose }: { title: string; why: string; onClose: () => void }) {
  return (
    <Dialog.Root open onOpenChange={(open) => open || onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="modal-backdrop" />
        <Dialog.Content className="modal small">
          <div className="modal-b">
            <Dialog.Title asChild>
              <h3>{title}</h3>
            </Dialog.Title>
            <Dialog.Description asChild>
              <p>{why}</p>
            </Dialog.Description>
          </div>
          <div className="modal-f">
            <div className="spacer" />
            <Dialog.Close className="btn">Close</Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/**
 * Keep the open draft in this browser, for the form to reopen with: when the
 * session ends (after sign-in) or the form closes with a draft started.
 */
function saveDraft(email: string, draft: Draft): void {
  try {
    localStorage.setItem(draftKey(email), JSON.stringify(draft));
  } catch {
    // Storage off: the draft cannot be kept.
  }
}

/** Whether the user has started `draft`: any text, or a relation. Chips alone are its defaults. */
function started(draft: Draft): boolean {
  return (
    draft.title.trim() !== "" ||
    draft.description.trim() !== "" ||
    draft.relations.length > 0 ||
    Object.values(draft.fields).some((text) => text.trim() !== "")
  );
}

/**
 * The draft saved when the session ended or the form closed, if it was of
 * `kind`: the next open of that kind's form reopens it, once. A draft of another
 * kind was for another form, and one in another shape was saved by another
 * build, before a deploy: neither reopens.
 */
function savedDraft(email: string, kind: InquiryKind): Draft | null {
  let saved: unknown;
  try {
    saved = JSON.parse(localStorage.getItem(draftKey(email)) ?? "null");
  } catch {
    // Storage off, or text that is not JSON: there is nothing to reopen.
    return null;
  }
  return isDraft(saved) && saved.kind === kind ? saved : null;
}

/** Whether `value` has the shape `saveDraft` writes, down to what the form reads of each relation. */
function isDraft(value: unknown): value is Draft {
  const draft = (value ?? {}) as { readonly [part in keyof Draft]?: unknown };
  const relations = Array.isArray(draft.relations) ? (draft.relations as readonly Partial<Related>[]) : null;
  return (
    typeof draft.title === "string" &&
    typeof draft.description === "string" &&
    isRecord(draft.fields) &&
    Object.values(draft.fields).every((text) => typeof text === "string") &&
    isRecord(draft.chips) &&
    relations !== null &&
    relations.every(
      ({ option, target }) =>
        typeof option?.key === "string" &&
        typeof option.label === "string" &&
        Array.isArray(option.targetKinds) &&
        typeof target?.id === "string" &&
        typeof target.kind === "string" &&
        typeof target.seq === "number",
    )
  );
}

function isRecord(value: unknown): value is { readonly [key: string]: unknown } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function forgetSavedDraft(email: string): void {
  try {
    localStorage.removeItem(draftKey(email));
  } catch {
    // Storage off: nothing was kept to reopen twice.
  }
}

/** One key per user, so a draft never opens for someone else signing in on this browser. */
function draftKey(email: string): string {
  return `trackinizer.v2.create.${email}`;
}

const INPUT_TYPES = { text: "text", integer: "number", number: "number", day: "date", datetime: "datetime-local", json: "text" } as const;
