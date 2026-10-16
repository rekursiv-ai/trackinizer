/** Where the caret's line is, in px from the box's border-box top-left corner, as the box shows it now. */
export type CaretPoint = { readonly left: number; readonly top: number; readonly height: number };

/** Styles that decide where text wraps and how tall a line is; the mirror copies each. */
const COPIED = [
  "paddingLeft", "paddingRight", "paddingTop", "paddingBottom", "font", "fontFamily", "fontSize",
  "fontWeight", "fontStyle", "letterSpacing", "lineHeight", "textTransform", "textIndent", "tabSize",
  "wordSpacing", "overflowWrap", "wordBreak",
] as const;

/**
 * Where `index` of `box`'s text shows: a hidden mirror of the box, styled to
 * wrap as it does, holds the text up to `index` and a marker, whose offset is
 * the caret's from the box's padding edge; less the box's scroll and plus its
 * border (`clientLeft`, `clientTop`), it is where the caret is from the box's
 * outer corner. A textarea reports no caret geometry of its own.
 */
export function caretPoint(box: HTMLTextAreaElement, index: number): CaretPoint {
  const style = getComputedStyle(box);
  const mirror = document.createElement("div");
  for (const name of COPIED) mirror.style[name] = style[name];
  Object.assign(mirror.style, { position: "absolute", visibility: "hidden", top: "0", left: "-9999px", whiteSpace: "pre-wrap", overflow: "hidden" });
  // Text wraps inside the box's content width, which a vertical scrollbar narrows: the mirror is a
  // borderless content-box as wide as the box's client area less its padding, so neither shifts a wrap.
  mirror.style.boxSizing = "content-box";
  mirror.style.width = `${box.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)}px`;
  mirror.textContent = box.value.slice(0, index);
  const marker = document.createElement("span");
  // A marker with no text has no height; the rest of the word keeps a wrap where the box makes it.
  marker.textContent = box.value.slice(index).match(/^\S*/)![0] || ".";
  mirror.append(marker);
  document.body.append(mirror);
  try {
    return {
      left: box.clientLeft + marker.offsetLeft - box.scrollLeft,
      top: box.clientTop + marker.offsetTop - box.scrollTop,
      height: marker.offsetHeight,
    };
  } finally {
    mirror.remove();
  }
}

/**
 * Where a menu `menu` px wide starts, from its container's left edge, to open at
 * `caret`: there, unless that runs past the viewport's right edge, when it
 * moves left just enough, though never past the viewport's left edge. The
 * container's width does not bound it, so a narrow column's menu overhangs it.
 */
export function menuLeft({ caret, menu, containerLeft, viewport }: { caret: number; menu: number; containerLeft: number; viewport: number }): number {
  return Math.max(-containerLeft, Math.min(caret, viewport - containerLeft - menu));
}
