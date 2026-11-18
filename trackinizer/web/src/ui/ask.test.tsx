import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Askable, type Asked } from "./ask";

afterEach(cleanup);

/** A composition's item, as `compose.tsx` marks one up for the bar to point at. */
function Surface() {
  return <div data-node="figure" data-item='{"kind":"chart"}'>a figure</div>;
}

/**
 * Point at the item, type `question`, and press Enter — the three steps a reader takes.
 *
 * Enter is sent the way the browser sends it: a click on the submit button, then the
 * form's submit event. Firing submit alone passes over the click, which is where the
 * bar used to lose the question.
 */
function ask(question: string) {
  fireEvent.click(screen.getByText("a figure"));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: "ask" }));
  fireEvent.submit(screen.getByRole("textbox"));
}

test("an answer returned by the asker is shown under the question, where it was asked", async () => {
  render(<Askable onAsk={() => Promise.resolve("2.59% of resident bytes, at batch 1.")}><Surface /></Askable>);
  ask("what share is the cache?");

  // The question stands while the answer is in flight: a bar that cleared itself would
  // leave the reader looking at the page they asked about with nothing to show for it.
  expect(screen.getByText("thinking…")).toBeTruthy();
  expect(await screen.findByText("2.59% of resident bytes, at batch 1.")).toBeTruthy();
  expect(screen.getByText("what share is the cache?")).toBeTruthy();
  expect(screen.queryByText("thinking…")).toBeNull();
});

test("the answer stays where it was asked after the reader stops pointing at the node", async () => {
  // Clicking off the node puts the ring away. The exchange is not the ring: an agent
  // that takes a minute would otherwise answer into a box the reader already closed by
  // looking back at the thing they asked about.
  render(<Askable onAsk={() => Promise.resolve("because the second seed was never run.")}><Surface /></Askable>);
  ask("why only one seed?");
  expect(await screen.findByText("because the second seed was never run.")).toBeTruthy();

  fireEvent.click(screen.getByText("a figure").parentElement!);
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByText("because the second seed was never run.")).toBeTruthy();
  expect(screen.getByText("why only one seed?")).toBeTruthy();
});

test("the answer renders as markdown, because that is what the agent writes", async () => {
  // Printed as plain text it put `**` and `|` on screen and made a table unreadable.
  render(
    <Askable onAsk={() => Promise.resolve("**18.41%** at step 5700\n\n- one eval\n- then back")}>
      <Surface />
    </Askable>,
  );
  ask("what is the dip?");

  const strong = await screen.findByText("18.41%");
  expect(strong.tagName).toBe("STRONG");
  expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual(["one eval", "then back"]);
});

test("the answer can be put away without asking something else", async () => {
  render(<Askable onAsk={() => Promise.resolve("the clamp arm.")}><Surface /></Askable>);
  ask("which arm?");
  expect(await screen.findByText("the clamp arm.")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "close this answer" }));
  expect(screen.queryByText("the clamp arm.")).toBeNull();
  expect(screen.queryByText("which arm?")).toBeNull();
});

test("a question that cannot be answered says so in the bar rather than hanging on 'thinking'", async () => {
  render(<Askable onAsk={() => Promise.reject(new Error("this tab is not on a station yet"))}><Surface /></Askable>);
  ask("why?");
  expect(await screen.findByText("Not answered: this tab is not on a station yet.")).toBeTruthy();
});

test("the answer to a question the reader has moved on from does not overwrite the one on screen", async () => {
  const replies: ((text: string) => void)[] = [];
  render(
    <Askable onAsk={() => new Promise<string>((resolve) => { replies.push(resolve); })}><Surface /></Askable>,
  );
  ask("first");
  ask("second");
  // The first question's answer lands late. The bar shows the second, so the first is
  // dropped: an answer under the wrong question is worse than no answer.
  await act(async () => { replies[0]!("late answer to the first"); });
  expect(screen.queryByText("late answer to the first")).toBeNull();
  await act(async () => { replies[1]!("the answer to the second"); });
  expect(await screen.findByText("the answer to the second")).toBeTruthy();
});

test("an asker that answers nothing leaves the store path intact, and a reply written there shows", async () => {
  localStorage.clear();
  const onAsk = vi.fn<(asked: Asked) => void>();
  render(<Askable onAsk={onAsk}><Surface /></Askable>);
  ask("answered by something else");

  // No promise came back, so the question is filed for whatever writes replies beside it.
  const [[asked]] = onAsk.mock.calls;
  expect(asked.question).toBe("answered by something else");
  const key = Object.keys(localStorage).find((k) => k.endsWith(":q"));
  expect(key).toBeTruthy();
  expect(JSON.parse(localStorage.getItem(key!)!).node).toBe("figure");

  localStorage.setItem(key!.replace(/:q$/, ":a"), "written by the other side");
  expect(await screen.findByText("written by the other side", undefined, { timeout: 3000 })).toBeTruthy();
});
