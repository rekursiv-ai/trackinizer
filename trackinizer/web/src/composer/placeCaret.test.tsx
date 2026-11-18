import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, test } from "vitest";
import { useCaretPlacement } from "./placeCaret";

afterEach(cleanup);

/** A box whose button sets its text and asks for the caret at `caret`, as a recall or a pick does. */
function Host({ text, caret }: { text: string; caret: number }) {
  const [value, setValue] = useState("start");
  const place = useCaretPlacement();
  return (
    <>
      <textarea aria-label="box" value={value} onChange={(event) => setValue(event.currentTarget.value)} />
      <button
        type="button"
        onClick={() => {
          setValue(text);
          place(screen.getByRole<HTMLTextAreaElement>("textbox"), caret);
        }}
      >
        set
      </button>
      <button type="button" onClick={() => setValue((held) => `${held}!`)}>
        again
      </button>
    </>
  );
}

test("the caret goes where asked once the new text is in the box, once: a later render leaves it alone", () => {
  render(<Host text="hello world" caret={5} />);
  const box = screen.getByRole<HTMLTextAreaElement>("textbox");
  fireEvent.click(screen.getByRole("button", { name: "set" }));
  expect(box.value).toBe("hello world");
  expect([box.selectionStart, box.selectionEnd]).toEqual([5, 5]);
  box.setSelectionRange(2, 2);
  fireEvent.click(screen.getByRole("button", { name: "again" }));
  expect(box.value).toBe("hello world!");
  expect(box.selectionStart).not.toBe(5);
});

test("a null box asks for nothing", () => {
  function Bare() {
    const place = useCaretPlacement();
    const [n, setN] = useState(0);
    return <button type="button" onClick={() => { place(null, 3); setN(n + 1); }}>{n}</button>;
  }
  render(<Bare />);
  fireEvent.click(screen.getByRole("button"));
  expect(screen.getByRole("button").textContent).toBe("1");
});
