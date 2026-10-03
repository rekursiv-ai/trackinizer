import { expect, test } from "vitest";
import { rolesUpTo } from "./roles";

test("each role may hand out itself and the weaker roles, weakest first; an unknown one none", () => {
  expect(rolesUpTo("viewer")).toEqual(["viewer"]);
  expect(rolesUpTo("writer")).toEqual(["viewer", "writer"]);
  expect(rolesUpTo("admin")).toEqual(["viewer", "writer", "admin"]);
  expect(rolesUpTo("owner")).toEqual([]);
  expect(rolesUpTo("toString")).toEqual([]);
});
