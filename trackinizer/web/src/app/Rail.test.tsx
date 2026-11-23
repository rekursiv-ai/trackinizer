import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { setTopic } from "../graph/topic";
import type { Route } from "../router/route";
import { Rail } from "./Rail";

const navigate = vi.fn();
let route: Route = { name: "blocks" };

vi.mock("../router/router", () => ({ useRouter: () => ({ route, navigate }) }));
vi.mock("../visuals/chatColumnState", () => ({
  setChatHidden: vi.fn(),
  useChatColumn: () => ({ hidden: false }),
}));

afterEach(() => {
  cleanup();
  setTopic(null);
  navigate.mockReset();
  route = { name: "blocks" };
});

test("Graph opens bare when the canvas is about nothing nameable", () => {
  render(<Rail />);
  fireEvent.click(screen.getByRole("button", { name: /Graph/ }));
  expect(navigate).toHaveBeenCalledWith({ name: "graph" });
});

test("Graph opens lit on what the canvas is about, so a reader is not hunting one record among thousands", () => {
  // The seeded graph is 5,151 nodes. Opened bare after reading an answer about one
  // Belief, finding that Belief in it is a manual search — the answer and the graph of
  // what the answer is about were two unconnected places. A focus lights and does not
  // filter, so this takes nothing away from the view it replaces.
  setTopic({ kind: "Belief", seq: 651 });
  render(<Rail />);
  const graph = screen.getByRole("button", { name: /Graph/ });
  expect(graph.title).toBe("The newest records and the wires between them, opened on Belief#651");
  fireEvent.click(graph);
  expect(navigate).toHaveBeenCalledWith({ name: "graph", focus: { ref: { kind: "Belief", seq: 651 }, hops: 2 } });
});

test("a focused graph still lights the Graph place: it is the same place, opened differently", () => {
  route = { name: "graph", focus: { ref: { kind: "Belief", seq: 651 }, hops: 2 } };
  render(<Rail />);
  expect(screen.getByRole("button", { name: /Graph/ }).getAttribute("aria-current")).toBe("page");
});

test("the subject moves only Graph: the other places go where they always went", () => {
  setTopic({ kind: "Belief", seq: 651 });
  render(<Rail />);
  for (const [name, to] of [
    ["Files", { name: "list", kind: "CodeChange" }],
    ["Runs", { name: "console" }],
    ["Evidence", { name: "list", kind: "Issue" }],
  ] as const) {
    navigate.mockReset();
    fireEvent.click(screen.getByRole("button", { name: new RegExp(name) }));
    expect(navigate).toHaveBeenCalledWith(to);
  }
});
