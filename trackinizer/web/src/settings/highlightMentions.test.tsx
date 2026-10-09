import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ProfileContext } from "../app/boot";
import { chooseHighlightMentions, readHighlightMentions, useHighlightMentions } from "./highlightMentions";
import { profile } from "./testing";

const ADA = profile("writer");
const GRACE = { ...ADA, email: "grace@example.com" };

function Switch() {
  const [on] = useHighlightMentions();
  return <output data-testid="on">{String(on)}</output>;
}

const shown = () => screen.getByTestId("on").textContent;

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  act(() => chooseHighlightMentions(ADA.email, true));
  act(() => chooseHighlightMentions(GRACE.email, true));
});

test("on until someone turns it off", () => {
  expect(readHighlightMentions(ADA.email)).toBe(true);
  render(<ProfileContext value={ADA}><Switch /></ProfileContext>);
  expect(shown()).toBe("true");
});

test("turning it off is held for that user in this browser and shown at once", () => {
  render(<ProfileContext value={ADA}><Switch /></ProfileContext>);
  act(() => chooseHighlightMentions(ADA.email, false));
  expect(shown()).toBe("false");
  expect(readHighlightMentions(ADA.email)).toBe(false);
  expect(readHighlightMentions(GRACE.email)).toBe(true);
  cleanup();
  render(<ProfileContext value={GRACE}><Switch /></ProfileContext>);
  expect(shown()).toBe("true");
});

test("another tab's choice reaches this one", () => {
  render(<ProfileContext value={ADA}><Switch /></ProfileContext>);
  localStorage.setItem(`trackinizer.v2.highlight-mentions.${ADA.email}`, "off");
  act(() => void dispatchEvent(new Event("storage")));
  expect(shown()).toBe("false");
  localStorage.removeItem(`trackinizer.v2.highlight-mentions.${ADA.email}`);
  act(() => void dispatchEvent(new Event("storage")));
  expect(shown()).toBe("true");
});

test("with storage refused the choice still holds for the page, and the default is on", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  render(<ProfileContext value={ADA}><Switch /></ProfileContext>);
  expect(shown()).toBe("true");
  act(() => chooseHighlightMentions(ADA.email, false));
  expect(shown()).toBe("false");
});
