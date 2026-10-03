// Compile-time tests for the typed client; `tsc --noEmit` checks them and nothing
// runs them. Each call in `rejected` has exactly one mistake, and its correct twin
// in `accepted` compiles, so every `@ts-expect-error` fails for the reason given.
// If a mistake stops being an error, `tsc` reports the unused directive.
import { client } from "./client";
import { keyed } from "./idempotency";
import { removeEdge } from "./edges";
import { clearField, createInquiry, listInquiries, purgeInquiry, setField } from "./inquiries";

export async function rejected(id: string): Promise<void> {
  const target = { params: { path: { target_id: id } } };
  // A route that does not exist.
  // @ts-expect-error
  await client.GET("/api/inquiries/{target_id}/nonexistent", target);
  // POST to a route that only takes PUT.
  // @ts-expect-error
  await client.POST("/api/inquiries/{target_id}/status", { ...target, body: { value: "complete" } });
  // PATCH on a field that is not a list.
  // @ts-expect-error
  await client.PATCH("/api/inquiries/{target_id}/title", { ...target, body: { op: "add", value: "x" } });
  // A status outside the Status enum.
  // @ts-expect-error
  await client.PUT("/api/inquiries/{target_id}/status", { ...target, body: { value: "done" } });
  // A list operation other than add or sub.
  // @ts-expect-error
  await client.PATCH("/api/inquiries/{target_id}/labels", { ...target, body: { op: "set", value: "x" } });
  // A kind that does not exist, in a list query.
  // @ts-expect-error
  await client.GET("/api/inquiries", { params: { query: { kind: ["Ticket"] } } });
  // A number where the title takes a string.
  // @ts-expect-error
  await client.PUT("/api/inquiries/{target_id}/title", { ...target, body: { value: 3 } });
  // DELETE without its body; the server answers 422.
  // @ts-expect-error
  await client.DELETE("/api/inquiries/{target_id}/description", target);
  // A missing path parameter.
  // @ts-expect-error
  await client.GET("/api/web/get/{target_id}", {});
  // The API functions carry the same checks: a judgement outside its enum,
  // @ts-expect-error
  await setField("/api/belief/{target_id}/judgement", id, keyed({ value: "maybe" }));
  // an actor, which the server must stamp itself,
  // @ts-expect-error
  await setField("/api/inquiries/{target_id}/title", id, keyed({ value: "x", actor: "me" }));
  // clearing a field that cannot be cleared,
  // @ts-expect-error
  await clearField("/api/inquiries/{target_id}/title", id, keyed({}));
  // a body without its idempotency key,
  // @ts-expect-error
  await createInquiry("Issue", { title: "x" });
  // a kind the schema does not name,
  // @ts-expect-error
  await createInquiry("Ticket", keyed({ title: "x" }));
  // a create field of the wrong type,
  // @ts-expect-error
  await createInquiry("Issue", keyed({ title: "x", priority: "high" }));
  // a field another kind's create takes,
  // @ts-expect-error
  await createInquiry("Belief", keyed({ title: "x", priority: 10 }));
  // a relation in a shape its create does not take,
  // @ts-expect-error
  await createInquiry("Issue", keyed({ title: "x", narrows: [{ id, priority: 10 }] }));
  // a list page of a kind the schema does not name,
  // @ts-expect-error
  await listInquiries({ kinds: ["Ticket"], filters: [], limit: 1, offset: 0 });
  // and an actor on a clear, a purge or an edge removal, even in a body built
  // apart, where `tsc` does not check for excess keys.
  const withActor = { reason: "x", actor: "me" };
  // @ts-expect-error
  await clearField("/api/inquiries/{target_id}/description", id, keyed(withActor));
  // @ts-expect-error
  await purgeInquiry(id, keyed(withActor));
  // @ts-expect-error
  await removeEdge({ from: id, kind: "narrows", to: id }, keyed(withActor));
}

export async function accepted(id: string): Promise<void> {
  const target = { params: { path: { target_id: id } } };
  await client.GET("/api/inquiries/{target_id}/confidence", target);
  await client.PUT("/api/inquiries/{target_id}/status", { ...target, body: { value: "complete" } });
  await client.PATCH("/api/inquiries/{target_id}/labels", { ...target, body: { op: "add", value: "x" } });
  await client.PUT("/api/inquiries/{target_id}/status", { ...target, body: { value: "active" } });
  await client.PATCH("/api/inquiries/{target_id}/labels", { ...target, body: { op: "sub", value: "x" } });
  await client.GET("/api/inquiries", { params: { query: { kind: ["Issue"] } } });
  await client.PUT("/api/inquiries/{target_id}/title", { ...target, body: { value: "3" } });
  await client.DELETE("/api/inquiries/{target_id}/description", { ...target, body: {} });
  await client.GET("/api/web/get/{target_id}", target);
  await setField("/api/belief/{target_id}/judgement", id, keyed({ value: "proven" }));
  await setField("/api/inquiries/{target_id}/title", id, keyed({ value: "x" }));
  await clearField("/api/inquiries/{target_id}/description", id, keyed({}));
  await createInquiry("Issue", keyed({ title: "x" }));
  await createInquiry("Artifact", keyed({ title: "x" }));
  await createInquiry("Issue", keyed({ title: "x", priority: 10 }));
  await createInquiry("Belief", keyed({ title: "x", confidence: 0.5 }));
  await createInquiry("Issue", keyed({ title: "x", narrows: [[id, 10]] }));
  await listInquiries({ kinds: ["Issue"], filters: [], limit: 1, offset: 0 });
  const withReason = { reason: "x" };
  await clearField("/api/inquiries/{target_id}/description", id, keyed(withReason));
  await purgeInquiry(id, keyed(withReason));
  await removeEdge({ from: id, kind: "narrows", to: id }, keyed(withReason));
}
