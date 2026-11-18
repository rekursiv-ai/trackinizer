import { afterEach, expect, test, vi } from "vitest";
import { caretPoint, menuLeft } from "./caretPoint";

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

test("the caret's point is its marker's offset in a mirror of the box, less the box's scroll, and the mirror goes", () => {
  const box = document.createElement("textarea");
  box.value = "first line\nsecond @is";
  box.style.font = "13px monospace";
  box.style.width = "200px";
  box.style.padding = "0 6px";
  box.style.border = "2px solid";
  document.body.append(box);
  box.scrollTop = 7;
  box.scrollLeft = 2;
  const seen: { text: string; font: string; width: string; wrap: string; border: string; sizing: string }[] = [];
  // The content width a scrollbar leaves: 180px client area, less 6px of padding each side.
  vi.spyOn(box, "clientWidth", "get").mockReturnValue(180);
  vi.spyOn(HTMLElement.prototype, "offsetLeft", "get").mockImplementation(function (this: HTMLElement) {
    return this.tagName === "SPAN" ? 40 : 0;
  });
  vi.spyOn(box, "clientLeft", "get").mockReturnValue(2);
  vi.spyOn(box, "clientTop", "get").mockReturnValue(2);
  vi.spyOn(HTMLElement.prototype, "offsetTop", "get").mockImplementation(function (this: HTMLElement) {
    const mirror = this.parentElement!;
    seen.push({ text: mirror.firstChild!.textContent!, font: mirror.style.font, width: mirror.style.width, wrap: mirror.style.whiteSpace, border: mirror.style.borderLeftStyle, sizing: mirror.style.boxSizing });
    return this.tagName === "SPAN" ? 30 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(16);
  // The mirror has no border of its own: its offsets start at its content, and the box's border is added once.
  // The marker sits at (40, 30) in the mirror's content box; the box's 2px border puts its text 2px further in.
  expect(caretPoint(box, 18)).toEqual({ left: 40, top: 25, height: 16 });
  expect(seen[0]).toMatchObject({ text: "first line\nsecond ", wrap: "pre-wrap", width: "168px", border: "", sizing: "content-box" });
  expect(seen[0]!.font).toContain("13px");
  expect(document.body.children).toHaveLength(1);
});

test("the menu starts at the caret, and moves left only as far as keeps it in the viewport, past a narrow column", () => {
  expect(menuLeft({ caret: 40, menu: 320, containerLeft: 0, viewport: 1000 })).toBe(40);
  // A column 200px wide at x=600: the menu overhangs it to the right, not pinned to its left edge.
  expect(menuLeft({ caret: 150, menu: 320, containerLeft: 600, viewport: 1000 })).toBe(80);
  expect(menuLeft({ caret: 150, menu: 320, containerLeft: 300, viewport: 1000 })).toBe(150);
  // Never past the viewport's left edge, even when it is narrower than the menu.
  expect(menuLeft({ caret: 100, menu: 400, containerLeft: 50, viewport: 300 })).toBe(-50);
});
