import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { watchEvents } from "../debug/testing";
import { LOGIN_URL, restoreReturnHash, Session, SessionContext, useDraftSaver } from "./session";

beforeEach(() => {
  history.replaceState(null, "", location.pathname);
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("leaving saves every draft and the hash, then loads the login page, once", () => {
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
  history.replaceState(null, "", "#/ref/Issue/7");
  const logged = watchEvents();

  session.leaveForLogin();
  session.leaveForLogin();
  expect(order).toEqual(["broken", "title"]);
  expect(sessionStorage.getItem("trackinizer.v2.return_hash")).toBe("#/ref/Issue/7");
  expect(assign.mock.calls).toEqual([[LOGIN_URL]]);
  expect(logged().filter(({ event }) => event === "session.ended")).toEqual([expect.objectContaining({ level: "warn", fields: { drafts: 2 } })]);
  expect(new URL(LOGIN_URL, location.origin).searchParams.get("next")).toBe("/app/");
});

test("the next boot restores the saved hash, unless it opened on its own", () => {
  sessionStorage.setItem("trackinizer.v2.return_hash", "#/list/Belief");
  restoreReturnHash();
  expect(location.hash).toBe("#/list/Belief");
  expect(sessionStorage.getItem("trackinizer.v2.return_hash")).toBeNull();

  sessionStorage.setItem("trackinizer.v2.return_hash", "#/list/Paper");
  restoreReturnHash();
  expect(location.hash).toBe("#/list/Belief");
  expect(sessionStorage.getItem("trackinizer.v2.return_hash")).toBeNull();
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
