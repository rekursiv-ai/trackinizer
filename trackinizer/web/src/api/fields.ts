import { FIELD_ROUTES } from "./generated/fields";
import type { SetFieldPath } from "./inquiries";

/** A field value's JSON type, from its `PUT` route's body, `null` aside. */
export type ValueType = {
  readonly type: "string" | "integer" | "number" | "boolean" | "object" | "array";
  /** `date-time` or `uuid`, when the schema names a format. */
  readonly format?: string;
  /** For an `array`, the type of one element. */
  readonly items?: ValueType;
};

/** One editable field of a kind. */
export type FieldRoute = {
  /** The field's `PUT` route, which `setField` takes. */
  readonly path: SetFieldPath;
  readonly value: ValueType;
  /** `PATCH` adds or removes one element: a list field. */
  readonly patch: boolean;
  /** `DELETE` clears the field. Without it the field always holds a value. */
  readonly delete: boolean;
};

/**
 * The editable fields of `kind` (`Issue`, `CodeChange`), by field name.
 *
 * A field is editable exactly when it has a `PUT` route. Fields every kind has
 * route under `/api/inquiries/`, the rest under the lowercase kind. The map is
 * generated from `openapi.json` at build time (`scripts/codegen.ts`), so the
 * browser never downloads the schema.
 */
export function editableFields(kind: string): { readonly [field: string]: FieldRoute } {
  const owners: { readonly [owner: string]: { readonly [field: string]: FieldRoute } } =
    FIELD_ROUTES;
  return { ...owners.inquiries, ...owners[kind.toLowerCase()] };
}
