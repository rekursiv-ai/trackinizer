import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { watchEvents } from "../debug/testing";
import { LOGIN_URL, loginUrl, Session, SessionContext, useDraftSaver } from "./session";

beforeEach(() => {
  history.replaceState(null, "", "/");
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("leaving saves every draft, then loads the login page, which returns to this page, once", () => {
  const assign = vi.fn();
  const session = new Session(assign);
  const order: string[] = [];
  session.addDraftSaver(() => {
    order.push("broken");
    throw new Error("quota exceeded");
  });
  session.addDraftSaver(() => order.push("title"));
  const stop = session.addDraftSaver(() => order.push("removed"));
  stop();
  vi.spyOn(console, "error").mockImplementation(() => {});
  history.replaceState(null, "", "/trax/ref/Issue/7?tab=2");
  const logged = watchEvents();

  session.leaveForLogin();
  session.leaveForLogin();
  expect(order).toEqual(["broken", "title"]);
  expect(assign.mock.calls).toEqual([[loginUrl("/trax/ref/Issue/7?tab=2")]]);
  expect(logged().filter(({ event }) => event === "session.ended")).toEqual([expect.objectContaining({ level: "warn", fields: { drafts: 2 } })]);
  expect(new URL(LOGIN_URL, location.origin).searchParams.get("next")).toBe("/trax/");
});

test("leaving an old link's #/... address returns to its route's path", () => {
  const assign = vi.fn();
  history.replaceState(null, "", "/trax/#/list/Belief");
  new Session(assign).leaveForLogin();
  expect(assign.mock.calls).toEqual([[loginUrl("/trax/list/Belief")]]);
  expect(new URL(loginUrl("/trax/list/Belief"), location.origin).searchParams.get("next")).toBe("/trax/list/Belief");
});

test("a mounted editor's latest draft saver runs; an unmounted one's does not", () => {
  const session = new Session(vi.fn());
  const saved: string[] = [];
  function Editor({ text }: { text: string }) {
    useDraftSaver(() => saved.push(text));
    return null;
  }
  const view = render(
    <SessionContext value={session}>
      <Editor text="first" />
    </SessionContext>,
  );
  view.rerender(
    <SessionContext value={session}>
      <Editor text="second" />
    </SessionContext>,
  );
  view.unmount();
  session.leaveForLogin();
  expect(saved).toEqual([]);

  const again = new Session(vi.fn());
  render(
    <SessionContext value={again}>
      <Editor text="open" />
    </SessionContext>,
  );
  again.leaveForLogin();
  expect(saved).toEqual(["open"]);
});
