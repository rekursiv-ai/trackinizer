import { expect, test } from "vitest";
import { editableFields } from "./fields";

test("a kind's fields are the common ones plus its own", () => {
  const belief = editableFields("Belief");
  expect(belief.title).toEqual({
    path: "/api/inquiries/{target_id}/title",
    value: { type: "string" },
    patch: false,
    delete: false,
  });
  expect(belief.confidence).toEqual({
    path: "/api/belief/{target_id}/confidence",
    value: { type: "number" },
    patch: false,
    delete: true,
  });
  expect(belief.priority).toBeUndefined();
  expect(editableFields("Issue").priority?.value).toEqual({ type: "integer" });
});

test("value types come from each route's body", () => {
  expect(editableFields("Paper").authors).toMatchObject({
    value: { type: "array", items: { type: "string" } },
    patch: true,
  });
  expect(editableFields("Experiment").config?.value).toEqual({ type: "object" });
  expect(editableFields("Paper").publish_date?.value).toEqual({
    type: "string",
    format: "date-time",
  });
  expect(editableFields("CodeChange").sha?.path).toBe("/api/codechange/{target_id}/sha");
});

test("a field without a PUT route is not editable", () => {
  expect(editableFields("Issue").opened_by_api_key_id).toBeUndefined();
  expect(editableFields("Issue").cost).toBeUndefined();
});
