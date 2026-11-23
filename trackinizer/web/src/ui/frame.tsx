// THE FRAME EVERY ANSWER SURFACE DRAWS: the bar that names a pane, the caption above
// each level, the ranked queue under it, and the centred panel one of them opens to.
// Like the primitives in sections.tsx these own structure and ink and no content — a
// surface says what the words are, these say where they sit and which tier of ink each
// word takes. They were the register's own classes until every surface redrew them.
import { type ReactNode } from "react";
import { Dialog } from "radix-ui";
import { Block } from "./block";
import { Path } from "./sections";
import "./frame.css";

/** The bar that names a surface: the fixed word, what it is about, how big the answer
 *  is, and at most one action. The subject is read; the count beside it only locates. */
export function FrameBar({ name, who, meta, action }: {
  readonly name: string;
  readonly who: ReactNode;
  readonly meta?: ReactNode;
  readonly action?: ReactNode;
}) {
  return (
    <header className="ui-bar">
      <span className="ui-bar-ttl">{name}</span>
      <span className="ui-bar-who">{who}</span>
      <span className="ui-bar-sp" />
      {meta === undefined ? null : <span className="ui-bar-meta">{meta}</span>}
      {action === undefined ? null : <span className="ui-bar-act">{action}</span>}
    </header>
  );
}

/** One level's caption: the noun in white at the frame's letterspaced mono, the gloss
 *  beside it in the machine tier, and the measure — when there is one — at the end of
 *  the line, where the code stage paints its +N −M. */
export function Caption({ title, note, size }: {
  readonly title: ReactNode;
  readonly note?: ReactNode;
  readonly size?: ReactNode;
}) {
  return (
    <p className="ui-head">
      <b>{title}</b>
      {note === undefined ? null : <i>{note}</i>}
      {size === undefined ? null : <em>{size}</em>}
    </p>
  );
}

/** One place in a queue: its rank, what it claims, the lines under the claim, and the
 *  address that proves it. The lines are the surface's, because only it knows what its
 *  rank is made of; the row's anatomy is not its business. */
export type QueueItem = {
  readonly key: string;
  readonly slot: ReactNode;
  readonly claim: string;
  readonly lines: readonly ReactNode[];
  readonly foot: string;
};

/** The ranked row: a queue of blocks with one open, under the caption that names it.
 *  A queue is an index and not a paragraph, so the read of what is open sits under it.
 *  A surface may hand over the arrangement its own answer needs, as rows of slots; the
 *  sheet draws each row it is given and a row wider than the pane scrolls. */
export function Queue({ title, note, label, items, open, onPick, rows }: {
  readonly title: string;
  readonly note?: string;
  readonly label: string;
  readonly items: readonly QueueItem[];
  readonly open: number;
  readonly onPick: (index: number) => void;
  readonly rows?: readonly (readonly string[])[];
}) {
  const bySlot = new Map(items.map((item) => [String(item.slot), item]));
  const laid = (rows ?? []).map((row) => row.map((slot) => bySlot.get(slot)).filter((i) => i !== undefined) as readonly QueueItem[]);
  const cells = (part: readonly QueueItem[], cls: string, key?: number) => (
    <div className={`ui-queue ${cls}`} aria-label={label} key={key}>
      {part.map((item) => (
        <Block
          density="row"
          key={item.key}
          selected={items.indexOf(item) === open}
          onSelect={() => onPick(items.indexOf(item))}
          slot={item.slot}
          claim={item.claim}
          lines={item.lines}
          foot={<Path address={item.foot} />}
        />
      ))}
    </div>
  );
  return (
    <>
      <Caption title={title} note={note} />
      {laid.length > 0 ? laid.map((part, index) => cells(part, "is-row", index)) : cells(items, "is-packed")}
    </>
  );
}

/** Which place in a queue an address asks for, so a chat link can put a reader on one.
 *  A rank outside the queue falls back to the queue's own first, because the order the
 *  model returned is its read. */
export const rankFrom = (hash: string, count: number, key: string): number => {
  const cut = hash.indexOf("?");
  const asked = cut < 0 ? null : new URLSearchParams(hash.slice(cut + 1)).get(key);
  const index = asked === null ? 0 : Number(asked) - 1;
  return Number.isInteger(index) && index >= 0 && index < count ? index : 0;
};

/** The centred panel one thing opened to: the address it was opened from, the claim it
 *  answers, the body, and at most one way out of it. Escape and the backdrop close it,
 *  so the foot always says so in words and the surface's own link stands beside that. */
export function Modal({ cls, kicker, note, title, titleNote, children, foot, onClose }: {
  readonly cls?: string;
  /** The address the panel was opened from, in the hue a level wears — never a word. */
  readonly kicker?: ReactNode;
  /** What the opened row answers, at the far end of the same line. */
  readonly note?: ReactNode;
  readonly title: ReactNode;
  /** The gloss on the title's own line, in the machine tier. */
  readonly titleNote?: ReactNode;
  readonly children: ReactNode;
  /** The panel's one action: a link out to the record, or a question back to the chat. */
  readonly foot?: ReactNode;
  readonly onClose: () => void;
}) {
  return (
    <Dialog.Root open onOpenChange={(next) => { if (!next) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="modal-backdrop" />
        <Dialog.Content className={`modal ui-panel${cls ? ` ${cls}` : ""}`} aria-describedby={undefined} onCloseAutoFocus={(event) => event.preventDefault()}>
          {kicker === undefined ? null : (
            <p className="ui-panel-kicker">
              <b>{kicker}</b>
              {note === undefined ? null : <i>{note}</i>}
            </p>
          )}
          <Dialog.Title className="ui-panel-h">
            {title}
            {titleNote === undefined ? null : <span className="ui-panel-sub">{titleNote}</span>}
          </Dialog.Title>
          <div className="modal-b">{children}</div>
          <div className="modal-f">
            <span className="ui-panel-esc">esc closes</span>
            {foot}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
