import { useQueryClient } from "@tanstack/react-query";
import { Popover } from "radix-ui";
import { type ChangeEvent, type ReactElement, type ReactNode, useEffect, useRef, useState } from "react";
import type { Detail } from "../api/detail";
import { useMeta, useProfile, useWriteMode } from "../app/boot";
import type { Field } from "../detail/fields";
import { detailQueries } from "../detail/queries";
import { Menu, type MenuOption } from "../lists/Menu";
import { PeoplePicker } from "../people/Picker";
import { Avatar, capitalize, JudgementGlyph, LabelDot, StatusGlyph } from "../ui/glyphs";
import { type Edit, fieldEdit, listEdit, sameValue } from "../writes/edits";
import { useWrite } from "../writes/useWrite";
import { WriteButton } from "../writes/WriteButton";
import { unsettled, WriteStatus } from "../writes/WriteStatus";
import { type Draft, useDraft, useDraftKeeper } from "./drafts";
import { PriorityPicker, priorityText } from "./Priority";
import { type ReasonAsk, ReasonDialog } from "./Reason";
import {
  asList,
  BYLINES,
  type InputKind,
  inputKind,
  inputText,
  loadedValues,
  parseInput,
  type Toggle,
  withToggles,
} from "./values";
import "./editors.css";

/**
 * One property's value, drawn by `children`, as the button that edits it: a
 * menu for a closed set or a person, a multi-select for a list, a small form for
 * any other value. A viewer sees the value alone, as does a field with no edit
 * route; offline, the button is off.
 *
 * Status, owner and judgement write by compare-and-set; abandoning,
 * invalidating and any judgement change first ask for a reason, and a form
 * offers an optional one. Lists change one element per `PATCH`, never by a
 * `PUT` of the whole list. Every write is guarded against the value the user
 * saw when they began it, not one a later render shows.
 */
export function PropertyEditor({ detail, field, children }: { detail: Detail; field: Field; children: ReactNode }) {
  const { enums } = useMeta();
  if (useWriteMode() === "hidden" || !field.route) return children;
  const props = { detail, field, children };
  switch (field.name) {
    case "status":
      return <StatusEditor {...props} />;
    case "judgement":
      return <JudgementEditor {...props} />;
    case "priority":
      return <PriorityEditor {...props} />;
    case "owner":
      return <OwnerEditor {...props} />;
    case "account":
      return <AccountEditor {...props} />;
  }
  if (field.route.value.type === "array") return <ListEditor {...props} />;
  if (enums[field.name]) return <EnumEditor {...props} options={enums[field.name]!} />;
  return <FormEditor {...props} />;
}

type EditorProps = { detail: Detail; field: Field; children: ReactNode };

/** The inquiry as a link names it, for dialog titles: `Issue#12`. */
function refOf(detail: Detail): string {
  return `${detail.self.kind}#${detail.self.seq}`;
}

function StatusEditor({ detail, field, children }: EditorProps) {
  const { enums } = useMeta();
  const status = String(field.value);
  return (
    <ChoiceEditor
      detail={detail}
      field={field}
      label="Change status…"
      show={(value) => capitalize(String(value))}
      options={(enums.status ?? []).map((value) => ({
        value,
        label: capitalize(value),
        icon: <StatusGlyph status={value} />,
        checked: value === status,
      }))}
      ask={(to) =>
        to === "abandoned" || to === "invalid"
          ? {
              title: `${to === "invalid" ? "Invalidate" : "Abandon"} ${refOf(detail)}`,
              body:
                to === "invalid"
                  ? "Invalid means the inquiry was wrong from the start. The reason is kept in activity."
                  : "Abandoned keeps the history but takes it off active lists.",
              confirm: to === "invalid" ? "Invalidate" : "Abandon",
              danger: to === "invalid",
            }
          : null
      }
    >
      {children}
    </ChoiceEditor>
  );
}

function JudgementEditor({ detail, field, children }: EditorProps) {
  const { enums } = useMeta();
  return (
    <ChoiceEditor
      detail={detail}
      field={field}
      label="Set judgement…"
      show={(value) => capitalize(String(value))}
      options={(enums.judgement ?? []).map((value) => ({
        value,
        label: capitalize(value),
        icon: <JudgementGlyph judgement={value} />,
        checked: value === field.value,
      }))}
      ask={(to) => ({
        title: to ? `Mark ${refOf(detail)} ${capitalize(to)}` : `Clear the judgement of ${refOf(detail)}`,
        body: "Judgement is author-owned. Say what settled it; the reason shows in activity.",
        confirm: to ? `Mark ${capitalize(to)}` : "Clear",
      })}
    >
      {children}
    </ChoiceEditor>
  );
}

/**
 * The owner: a person or an agent, from the people picker, or none. Compare-and-set
 * guards it against the owner shown when the pick was made, dialog or not.
 */
function OwnerEditor({ detail, field, children }: EditorProps) {
  const { save, button, status } = useFieldSave(detail, field);
  const current = typeof field.value === "string" ? field.value : undefined;
  return (
    <>
      <PeoplePicker
        field="owner"
        trigger={button(children)}
        current={current === undefined ? [] : [current]}
        checked={(actor) => actor === (current ?? null)}
        none={current === undefined ? undefined : "No owner"}
        onPick={(to) => {
          if (!sameValue(to, field.value)) save(to);
        }}
      />
      {status}
    </>
  );
}

/**
 * The account a row is attributed to: an active user, which the server checks.
 * No route lists users to anyone but an admin, so it offers me, the value set,
 * and anything typed.
 */
function AccountEditor({ detail, field, children }: EditorProps) {
  const { email } = useProfile();
  const current = typeof field.value === "string" ? field.value : undefined;
  return (
    <ChoiceEditor
      detail={detail}
      field={field}
      label="Attribute to an active user…"
      create={(typed) => `Attribute to “${typed}”`}
      options={[...new Set([email, ...(current ? [current] : [])])].map((actor) => ({
        value: actor,
        label: actor,
        icon: <Avatar actor={actor} size={16} />,
        hint: actor === email ? "me" : undefined,
        checked: actor === current,
      }))}
    >
      {children}
    </ChoiceEditor>
  );
}

function EnumEditor({ detail, field, options, children }: EditorProps & { options: readonly string[] }) {
  return (
    <ChoiceEditor
      detail={detail}
      field={field}
      label={`${field.look.label}…`}
      options={options.map((value) => ({ value, label: value, checked: value === field.value }))}
    >
      {children}
    </ChoiceEditor>
  );
}

/**
 * A menu that sets one value. `ask` names the changes that first ask for a
 * reason; `create` offers what was typed as a value too. A field that can be
 * cleared, and is set, offers that last.
 */
function ChoiceEditor({
  detail,
  field,
  label,
  options,
  create,
  ask,
  show,
  children,
}: EditorProps & {
  label: string;
  options: readonly MenuOption[];
  create?: (typed: string) => string;
  ask?: (to: string | null) => ReasonAsk | null;
  show?: (value: unknown) => string;
}) {
  const { save, button, status, returnFocus } = useFieldSave(detail, field, show);
  const [asking, setAsking] = useState<{ readonly to: string | null; readonly from: unknown; readonly ask: ReasonAsk } | null>(null);
  const clear: MenuOption[] =
    field.route?.delete && field.value !== undefined ? [{ value: CLEAR, label: field.look.unset ?? `No ${field.look.label.toLowerCase()}` }] : [];
  const pick = (picked: string) => {
    const to = picked === CLEAR ? null : picked;
    if (sameValue(to, field.value)) return;
    const question = ask?.(to);
    if (question) setAsking({ to, from: field.value, ask: question });
    else save(to);
  };
  return (
    <>
      <Menu label={label} trigger={button(children)} options={[...options, ...clear]} onPick={pick} create={create} />
      {status}
      <ReasonDialog
        ask={asking?.ask ?? null}
        returnFocus={returnFocus}
        onCancel={() => setAsking(null)}
        onConfirm={(reason) => {
          setAsking(null);
          save(asking!.to, reason, asking!.from);
        }}
      />
    </>
  );
}

function PriorityEditor({ detail, field, children }: EditorProps) {
  const { save, button, status } = useFieldSave(detail, field, priorityText);
  return (
    <>
      <PriorityPicker
        value={typeof field.value === "number" ? field.value : undefined}
        trigger={button(children)}
        onPick={(to) => {
          if (!sameValue(to, field.value)) save(to);
        }}
      />
      {status}
    </>
  );
}

/**
 * The writes of one field's control: `save` sends a new value (unset clears
 * it) over `from`, the value the edit began on, by default the one shown now;
 * `button` is the control that opens its editor, off while offline and opening
 * nothing while a write is `pending` (sent, or failed and showing Retry); and
 * `status` the write's state beside it.
 */
function useFieldSave(detail: Detail, field: Field, show?: (value: unknown) => string) {
  const writer = useWrite();
  const offline = useWriteMode() === "disabled";
  const pending = unsettled(writer.state);
  const returnFocus = useRef<HTMLButtonElement>(null);
  const save = (to: unknown, reason?: string, from: unknown = field.value) =>
    writer.run(
      fieldEdit({
        id: detail.self.id,
        field: field.name,
        route: field.route!,
        label: field.look.label,
        from,
        to,
        reason,
        show,
      }),
    );
  const button = (children: ReactNode): ReactElement => (
    <WriteButton
      ref={returnFocus}
      className="prop-btn"
      pending={pending}
      disabled={offline}
      title={`Change ${field.look.label.toLowerCase()}`}
    >
      {children}
    </WriteButton>
  );
  return { save, button, pending, status: <WriteStatus state={writer.state} />, returnFocus };
}

/**
 * A list field as a multi-select: each tick adds or removes one element with
 * `PATCH`, the last one too, so edits by others to other elements all land. An
 * Issue's last type is the exception, cleared with a checked `DELETE`
 * (`listEdit`). A byline (`BYLINES`) reads as the server keeps it: a removed
 * author drops once.
 *
 * Ticks made while one is saving wait their turn, and the menu shows them as
 * already made; each is its own request with its own key.
 */
function ListEditor({ detail, field, children }: EditorProps) {
  const queryClient = useQueryClient();
  const { enums } = useMeta();
  const writer = useWrite();
  const offline = useWriteMode() === "disabled";
  const [queued, setQueued] = useState<readonly Queued[]>([]);
  const running = useRef(false);
  const { name, route, look } = field;
  // A tick leaves the queue once it lands, when the cache already holds the
  // refetch that shows it, a moment before that refetch reaches `detail`. Read
  // from the cache, the list never shows a landed tick twice (an author twice),
  // nor flickers back to the list before it.
  const stored = (queryClient.getQueryData(detailQueries.detail(detail.self.id).queryKey) ?? detail).self[name];
  const shown = withToggles(asList(stored), queued, BYLINES.has(name));
  const run = writer.run;
  useEffect(() => {
    const next = queued[0];
    if (!next || running.current) return;
    running.current = true;
    const settle = () => {
      running.current = false;
      setQueued((items) => items.filter((item) => item !== next));
    };
    run(next.edit).then(settle, (error: unknown) => {
      settle();
      throw error;
    });
  }, [queued, run]);

  const people = name === "subscribers";
  const closed = enums[name];
  const offered = new Set([...(closed ?? []), ...shown]);
  // The people picker offers loaded people itself, and people added by hand.
  if (!closed && !people) loadedValues(queryClient, name).forEach((value) => offered.add(value));
  // An element stays on offer once removed, so a second tick can put it back.
  const [seen, setSeen] = useState<ReadonlySet<string>>(offered);
  if ([...offered].some((value) => !seen.has(value))) setSeen(new Set([...seen, ...offered]));
  const choices = closed ? [...new Set([...closed, ...seen, ...offered])] : [...new Set([...seen, ...offered])].sort((a, b) => a.localeCompare(b));

  const toggle = (value: string) => {
    const op = shown.includes(value) ? "sub" : "add";
    // No toast, and so no Undo: the menu stays open and a second tick undoes a
    // tick. A toast is also a Radix layer, and one that opened over the menu
    // would take the menu's Escape.
    const edit = { ...listEdit({ id: detail.self.id, field: name, route: route!, label: look.label, op, value, from: shown }), done: undefined };
    setQueued((waiting) => [...waiting, { op, value, edit }]);
  };
  const trigger = (
    <button type="button" className="prop-btn" disabled={offline} title={`Change ${look.label.toLowerCase()}`}>
      {children}
    </button>
  );
  return (
    <>
      {people ? (
        <PeoplePicker
          field="subscribers"
          trigger={trigger}
          current={choices}
          checked={(actor) => actor !== null && shown.includes(actor)}
          onPick={(actor) => toggle(actor!)}
        />
      ) : (
        <Menu
          label={`${look.label}…`}
          multi
          trigger={trigger}
          options={choices.map((value) => ({
            value,
            label: value,
            icon: name === "labels" ? <LabelDot label={value} /> : undefined,
            checked: shown.includes(value),
          }))}
          create={closed ? undefined : (typed) => (name === "labels" ? `Create label “${typed}”` : `Add “${typed}”`)}
          onPick={toggle}
        />
      )}
      <WriteStatus state={writer.state} />
    </>
  );
}

/**
 * Any other field: a small form with the input its value type calls for (text,
 * a number, a date, JSON). Empty clears the field, where it can be cleared.
 *
 * Its draft is the text editors' (`useDraft`): the text, and the value the form
 * opened on, which the save is guarded against. It waits in this browser if the
 * session ends while the form is open, and reopens it on the next load. While
 * a save is under way the form takes no input and does not close.
 */
function FormEditor({ detail, field, children }: EditorProps) {
  const kind = inputKind(field.route!, field.look);
  const [draft, setDraft] = useDraft(detail.self.id, field.name);
  const { save, button, pending, status } = useFieldSave(detail, field);
  return (
    <Popover.Root
      open={draft !== null}
      onOpenChange={(open) => {
        if (!pending) setDraft(open ? { text: inputText(field.value, kind), base: field.value } : null);
      }}
    >
      <Popover.Trigger asChild>{button(children)}</Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className="list-menu popover" align="start" sideOffset={4} aria-label={`Edit ${field.look.label}`}>
          {draft ? (
            <ValueForm
              id={detail.self.id}
              field={field}
              kind={kind}
              draft={draft}
              setDraft={setDraft}
              pending={pending}
              onSave={async (to, reason) => {
                if ((await save(to, reason, draft.base)) !== null) setDraft(null);
              }}
            >
              {status}
            </ValueForm>
          ) : null}
        </Popover.Content>
      </Popover.Portal>
      {draft ? null : status}
    </Popover.Root>
  );
}

/** The form: the value's input, an optional reason, and the server's answer as `children`. */
function ValueForm({
  id,
  field,
  kind,
  draft,
  setDraft,
  pending,
  onSave,
  children,
}: {
  id: string;
  field: Field;
  kind: InputKind;
  draft: Draft;
  setDraft: (draft: Draft | null) => void;
  pending: boolean;
  onSave: (value: unknown, reason: string) => void;
  children: ReactNode;
}) {
  useDraftKeeper(id, field.name, draft);
  const offline = useWriteMode() === "disabled";
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const input = {
    "aria-label": field.look.label,
    autoFocus: true,
    readOnly: pending,
    value: draft.text,
    onChange: (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      setDraft({ ...draft, text: event.target.value });
      setError(null);
    },
  };
  return (
    <form
      className="ed-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (pending || offline) return;
        // Unchanged text saves nothing: an input cannot always show the stored
        // value exactly (a time's seconds), so its text reads back changed.
        if (draft.text === inputText(draft.base, kind)) return setDraft(null);
        const parsed = parseInput(draft.text, kind);
        if ("error" in parsed) setError(parsed.error);
        else if (sameValue(parsed.value, draft.base) && sameValue(field.value, draft.base)) setDraft(null);
        else onSave(parsed.value, reason.trim());
      }}
    >
      <h5>{field.look.label}</h5>
      {kind === "json" ? (
        <textarea {...input} className="field mono ed-json" rows={6} />
      ) : (
        <input
          {...input}
          className={field.look.mono ? "field mono" : "field"}
          type={INPUT_TYPES[kind]}
          step={kind === "integer" ? 1 : kind === "number" ? "any" : undefined}
          autoComplete="off"
        />
      )}
      {error ? (
        <span className="form-err" role="alert">
          {error}
        </span>
      ) : null}
      <input
        className="field"
        aria-label="Reason"
        placeholder="Reason (optional)"
        autoComplete="off"
        readOnly={pending}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
      />
      {children}
      <div className="pop-row">
        <span className="muted">{field.route!.delete ? "Empty clears it." : ""}</span>
        <WriteButton type="submit" className="btn primary" pending={pending} disabled={offline}>
          Save
        </WriteButton>
      </div>
    </form>
  );
}

/** A list toggle waiting to be sent, or being sent. */
type Queued = Toggle & { readonly edit: Edit };

/** The menu value that clears a field; no stored value is a NUL. */
const CLEAR = "\u0000clear";

const INPUT_TYPES = { text: "text", integer: "number", number: "number", day: "date", datetime: "datetime-local", json: "text" } as const;
