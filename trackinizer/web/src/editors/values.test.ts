import { QueryClient } from "@tanstack/react-query";
import { expect, test } from "vitest";
import { detail, row } from "../detail/testing";
import { inputText, loadedValues, parseInput, withToggles } from "./values";

test("toggles apply in order, and applying one already landed changes nothing", () => {
  const toggles = [
    { op: "add", value: "b" },
    { op: "sub", value: "a" },
    { op: "add", value: "b" },
  ] as const;
  expect(withToggles(["a"], toggles)).toEqual(["b"]);
  expect(withToggles(["b"], toggles)).toEqual(["b"]);
});

test("a byline's toggles keep order and repeats, as the server's do: add appends, remove drops the first match (E8-06)", () => {
  const byline = ["Ada", "Alan", "Ada"];
  expect(withToggles(byline, [{ op: "sub", value: "Ada" }], true)).toEqual(["Alan", "Ada"]);
  expect(withToggles(byline, [{ op: "add", value: "Alan" }], true)).toEqual(["Ada", "Alan", "Ada", "Alan"]);
  expect(withToggles(byline, [{ op: "sub", value: "Grace" }], true)).toEqual(byline);
});

test("loaded values come from cached rows and details, never from neighbours on edges", () => {
  const queryClient = new QueryClient();
  queryClient.setQueryData(["inquiries", "list", 1], [row("Issue", 1, { labels: ["b", "a"], owner: "ada" })]);
  const hub = row("Issue", 2, { labels: ["c"] });
  queryClient.setQueryData(["detail", hub.id], detail(hub, { edges: { narrows: [{ ...row("Issue", 3), labels: ["edge-only"] }] } }));
  expect(loadedValues(queryClient, "labels")).toEqual(["a", "b", "c"]);
  expect(loadedValues(queryClient, "owner")).toEqual(["ada"]);
});

test("an input's text reads back as the value it stands for; empty clears", () => {
  expect(parseInput("  ", "text")).toEqual({ value: null });
  expect(parseInput(" 15 ", "integer")).toEqual({ value: 15 });
  expect(parseInput("1.5", "integer")).toEqual({ value: 1.5 });
  expect(parseInput("x", "number")).toEqual({ error: "Enter a number." });
  expect(inputText("2024-01-05T00:00:00+00:00", "day")).toBe("2024-01-05");
  expect(parseInput("2024-01-05", "day")).toEqual({ value: "2024-01-05T00:00:00+00:00" });
  const local = inputText("2026-09-27T14:05:00+00:00", "datetime");
  expect(parseInput(local, "datetime")).toEqual({ value: "2026-09-27T14:05:00.000Z" });
  expect(parseInput(inputText({ lr: 0.1 }, "json"), "json")).toEqual({ value: { lr: 0.1 } });
  expect(parseInput("{lr: 1}", "json")).toMatchObject({ error: expect.stringMatching(/^Not valid JSON/) });
});
