// THE BLOCK: the one card every answer surface is made of.
//
// The fold's cards, the distilled flow's cards and the review register's chips are
// the same object — a slot, a claim, what it rests on, and a footer that names the
// records — drawn at two densities. Before this there were three copies of that
// anatomy in three stylesheets, which is how the fold's card and the register's
// chip came to disagree about what a selected block looks like.
//
// What a consumer may choose is the density and its own container; the slot's
// place, the claim's rank, the footer's rule and the ink tiers are fixed here, so
// the next answer surface costs no design code.
import type { ReactNode, Ref } from "react";
import { ticks } from "./sections";
import "./block.css";

/** `card` is a block you read; `row` is a block you scan across a register. */
export type BlockDensity = "card" | "row";

export type BlockProps = {
  /** The slot: a block's address and its status glyph, as `K#1 ✓`. Omit for a row. */
  readonly slot?: ReactNode;
  /** The claim — the assertion the block stands for, and its headline. */
  readonly claim: ReactNode;
  /** What stands under the claim, one line each: the body, the price, the verdict. */
  readonly lines?: readonly ReactNode[];
  /** The footer past the rule: provenance for a card, an address for a row. */
  readonly foot?: ReactNode;
  readonly density?: BlockDensity;
  readonly selected?: boolean;
  readonly onSelect?: () => void;
  /** The block's status, worn as `data-status` for the surface that draws a glyph by it. */
  readonly status?: string;
  /** The surface's own class, so it can paint its box: `knowledge-card`, a chip grid. */
  readonly className?: string;
  /** Stepped back because a focus elsewhere owns the page. The flow dims what it lights past. */
  readonly dim?: boolean;
  readonly onMouseEnter?: () => void;
  readonly onMouseLeave?: () => void;
  /** The flow measures a card's box to route its wires, so the node is the consumer's. */
  readonly ref?: Ref<HTMLButtonElement>;
};

export function Block({
  slot, claim, lines, foot, density = "card", selected, onSelect, status, className, dim, onMouseEnter, onMouseLeave, ref,
}: BlockProps) {
  return (
    <button
      className={`ui-block is-${density}${slot === undefined ? "" : " has-slot"}${className ? ` ${className}` : ""}`}
      type="button"
      ref={ref}
      data-status={status}
      style={dim ? { opacity: 0.42 } : undefined}
      aria-pressed={!!selected}
      onClick={onSelect}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}>
      {slot === undefined ? null : <span className="ui-block-slot">{slot}</span>}
      <span className="ui-block-claim">{asText(claim)}</span>
      {(lines ?? []).map((line, index) => (
        // A surface may hand over a line it did not have — a record that recorded no
        // body — and an empty span is a hole the footer then falls into.
        line === undefined || (typeof line === "string" && line.trim() === "") ? null : (
          <span className="ui-block-line" key={index}>
            {asText(line)}
          </span>
        )
      ))}
      {foot === undefined ? null : <span className="ui-block-foot">{asText(foot)}</span>}
    </button>
  );
}

/** A block's words are the model's, so they get the same tick reading as a section's:
 *  a backtick is markup for a code token and never a character on the card. */
const asText = (node: ReactNode): ReactNode => (typeof node === "string" ? ticks(node) : node);
