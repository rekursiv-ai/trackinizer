import { expect, test } from "vitest";
import { isUnset, kindFields, sourceUrl, usd } from "./fields";
import { META, row } from "./testing";

test("an Issue's fields come from the schema, in the detail's order and places", () => {
  const fields = kindFields(row("Issue", 1, { priority: 10 }), META.fieldOwners);
  expect(fields.map((field) => [field.name, field.look.place])).toEqual([
    ["title", "title"],
    ["description", "text"],
    ["validation", "text"],
    ["status", "properties"],
    ["priority", "properties"],
    ["issue_kind", "properties"],
    ["owner", "properties"],
    ["subscribers", "properties"],
    ["labels", "properties"],
    ["recorded", "details"],
    ["account", "details"],
    ["marginal_cost_agent_usd", "details"],
    ["marginal_cost_resource_usd", "details"],
  ]);
  expect(fields.find((field) => field.name === "priority")?.route?.path).toBe("/api/issue/{target_id}/priority");
});

test("values come from the row: unset in any spelling is undefined, costs from their nesting", () => {
  const fields = kindFields(
    row("Issue", 1, {
      owner: "",
      labels: [],
      validation: null,
      marginal_cost: { agent_usd: 1.25, resource_usd: 0 },
    }),
    META.fieldOwners,
  );
  const value = (name: string) => fields.find((field) => field.name === name)?.value;
  expect([value("owner"), value("labels"), value("validation"), value("priority")]).toEqual([
    undefined,
    undefined,
    undefined,
    undefined,
  ]);
  expect([value("marginal_cost_agent_usd"), value("marginal_cost_resource_usd")]).toEqual([1.25, 0]);
  expect([undefined, null, "", []].every(isUnset)).toBe(true);
  expect([0, false, "x", ["x"]].some(isUnset)).toBe(false);
});

test("read-only fields join: the kind's routeless schema fields and any the row carries", () => {
  const fields = kindFields(row("AgentSession", 1, { ended: "2026-09-24T10:00:00+00:00", novel: "x" }), META.fieldOwners);
  const byName = new Map(fields.map((field) => [field.name, field]));
  expect(byName.get("opened_by_api_key_id")?.route).toBeUndefined();
  expect(byName.get("ended")?.value).toBe("2026-09-24T10:00:00+00:00");
  expect(byName.get("novel")?.look).toEqual({ label: "Novel", place: "details" });
  expect(byName.get("rooms")?.route?.patch).toBe(true);
});

test("an object-valued field the detail never named is shown as JSON", () => {
  const fields = kindFields(row("Artifact", 1, { settings: { a: 1 } }), META.fieldOwners);
  expect(fields.find((field) => field.name === "settings")?.look.place).toBe("json");
});

test("a source resolves as the old UI does", () => {
  expect(sourceUrl("arXiv:2401.00001")).toBe("https://arxiv.org/abs/2401.00001");
  expect(sourceUrl("doi: 10.1/x")).toBe("https://doi.org/10.1/x");
  expect(sourceUrl("https://example.com/a")).toBe("https://example.com/a");
  expect(sourceUrl("acm:123")).toBe("https://www.google.com/search?q=acm%3A123");
  expect(sourceUrl("no scheme")).toBeNull();
});

test("a cost keeps sub-cent values, to the server's six places, and cents above them", () => {
  expect([0.0042, 0.000001, 0.01, 1.25, 8678.123456, -0.5, -0.003].map(usd)).toEqual([
    "$0.0042",
    "$0.000001",
    "$0.01",
    "$1.25",
    "$8678.12",
    "-$0.50",
    "-$0.003",
  ]);
});
