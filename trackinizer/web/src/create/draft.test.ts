import { afterEach, expect, test, vi } from "vitest";
import { editableFields } from "../api/fields";
import openapi from "../api/openapi.json";
import { stubFetch } from "../api/testing";
import { META, uuid } from "../detail/testing";
import { formatRoute } from "../router/route";
import {
  createEdit,
  createKindFor,
  creatableKinds,
  type Draft,
  emptyDraft,
  formFields,
  nextDraft,
  type Related,
  relationOptions,
  switchKind,
} from "./draft";

const NEW = uuid(900);
const PARENT = { id: uuid(1), kind: "Issue", seq: 1, title: "Parent" } as const;
const BLOCKER = { id: uuid(2), kind: "Issue", seq: 2, title: "Blocker" } as const;
const PAPER = { id: uuid(3), kind: "Paper", seq: 3, title: "A paper" } as const;
const RESULT = { id: uuid(4), kind: "WebResult", seq: 4, title: "A page" } as const;
const COMMIT = { id: uuid(5), kind: "CodeChange", seq: 5, title: "A commit" } as const;

afterEach(() => {
  vi.unstubAllGlobals();
});

/** A draft of `kind` with `title`, and `change` over its defaults. */
function draftOf(kind: Draft["kind"], change: Partial<Draft> = {}): Draft {
  return { ...emptyDraft(kind), title: "Ship it", ...change };
}

/** `relations` of `kind`'s draft, by option key and target. */
function related(kind: Draft["kind"], picks: readonly [string, Related["target"]][]): Related[] {
  const options = relationOptions(kind, META.edges);
  return picks.map(([key, target]) => ({ option: options.find((option) => option.key === key)!, target }));
}

/** Send `draft`'s create against a server that creates `NEW`; returns the id and the one request it made. */
async function send(draft: Draft) {
  const sent = stubFetch((request) =>
    Response.json(new URL(request.url).pathname.endsWith("/batch") ? { ids: [NEW] } : { id: NEW }, { status: 201 }),
  );
  const made = createEdit(draft, formFields(draft.kind, META.fieldOwners));
  if ("error" in made) throw new Error(made.error);
  const id = await made.edit.request.send();
  expect(sent).toHaveLength(1);
  return { id, request: sent[0]!, touches: made.edit.touches };
}

test("an issue with relations its body takes is one create, with those relations in the body", async () => {
  const draft = draftOf("Issue", {
    title: "  Ship   it ",
    description: " Why \n",
    fields: { validation: "It ships" },
    chips: { ...emptyDraft("Issue").chips, owner: "ada@example.com", labels: ["webui"] },
    relations: related("Issue", [
      ["narrows:out", PARENT],
      ["requires:out", BLOCKER],
    ]),
  });
  const { id, request, touches } = await send(draft);
  expect(id).toBe(NEW);
  expect(touches).toEqual([PARENT.id, BLOCKER.id]);
  const key = (request.body as { idempotency_key: string }).idempotency_key;
  expect(key).toMatch(/^[0-9a-f-]{36}$/);
  expect(request).toEqual({
    method: "POST",
    path: "/api/inquiries/issue",
    query: "",
    headers: { "content-type": "application/json" },
    body: {
      title: "Ship it",
      description: "Why",
      validation: "It ships",
      status: "active",
      priority: 20,
      issue_kind: ["task"],
      owner: "ada@example.com",
      labels: ["webui"],
      narrows: [[PARENT.id, null]],
      requires: [BLOCKER.id],
      idempotency_key: key,
    },
  });
});

test("a belief's evidence goes in its body as citations", async () => {
  const draft = draftOf("Belief", {
    fields: { confidence: "0.7" },
    relations: related("Belief", [
      ["proves:in", PAPER],
      ["favors:in", RESULT],
    ]),
  });
  const { request } = await send(draft);
  expect(request.path).toBe("/api/inquiries/belief");
  expect(request.body).toMatchObject({
    judgement: "unproven",
    confidence: 0.7,
    proved_by: [{ artifact_id: PAPER.id, artifact_kind: "Paper" }],
    favored_by: [{ artifact_id: RESULT.id, artifact_kind: "WebResult" }],
  });
});

test("each field is sent as its value type, and an empty one not at all", async () => {
  const paper = draftOf("Paper", {
    fields: { authors: " Ada Lovelace, , Alan Turing ", publish_date: "2024-07-21", venue: "  ", publication_type: "article" },
  });
  expect((await send(paper)).request.body).toEqual({
    title: "Ship it",
    status: "active",
    authors: ["Ada Lovelace", "Alan Turing"],
    publish_date: "2024-07-21T00:00:00+00:00",
    publication_type: "article",
    idempotency_key: expect.any(String),
  });
  const experiment = draftOf("Experiment", {
    fields: { config: '{"lr": 0.1}', outcome: "Better" },
    relations: related("Experiment", [["field:codechanges", COMMIT]]),
  });
  expect((await send(experiment)).request.body).toMatchObject({ config: { lr: 0.1 }, outcome: "Better", codechanges: [COMMIT.id] });
});

test("a relation the body cannot hold makes it one batch: the row, and edges from item 0, as Respond does", async () => {
  const answer = draftOf("Artifact", { relations: related("Artifact", [["produced_by:out", PARENT]]) });
  const { id, request } = await send(answer);
  expect(id).toBe(NEW);
  const [item] = (request.body as { items: { idempotency_key: string }[] }).items;
  expect(request).toMatchObject({ method: "POST", path: "/api/inquiries/batch" });
  expect(request.body).toEqual({
    items: [{ kind: "Artifact", title: "Ship it", status: "active", idempotency_key: item!.idempotency_key }],
    edges: [{ edge_kind: "produced_by", from_index: 0, to_id: PARENT.id }],
  });
  expect(request.headers["idempotency-key"]).toBeUndefined();

  // The body still holds what it can; an edge into the new row names it as its `to` end.
  const issue = draftOf("Issue", {
    relations: related("Issue", [
      ["narrows:out", PARENT],
      ["narrows:in", BLOCKER],
    ]),
  });
  expect((await send(issue)).request.body).toMatchObject({
    items: [{ kind: "Issue", narrows: [[PARENT.id, null]] }],
    edges: [{ edge_kind: "narrows", from_id: BLOCKER.id, to_index: 0 }],
  });
});

test("a field that cannot be sent stops the create with its label and why; nothing is sent", () => {
  const sent = stubFetch();
  const draft = draftOf("Experiment", { fields: { config: "{nope" } });
  const made = createEdit(draft, formFields("Experiment", META.fieldOwners));
  expect(made).toEqual({ error: expect.stringMatching(/^Config: Not valid JSON/) });
  expect(sent).toEqual([]);
});

test("the form asks for exactly the fields each kind's create body takes, less chips and relations", () => {
  const fields = Object.fromEntries(
    creatableKinds(META.kinds).map((kind) => [kind, formFields(kind, META.fieldOwners).map((field) => field.name)]),
  );
  expect(fields).toEqual({
    Issue: ["validation"],
    Artifact: [],
    Experiment: ["outcome", "config"],
    Paper: [
      "abstract",
      "authors",
      "publication_type",
      "venue",
      "subvenue",
      "publish_date",
      "source",
      "google_scholar_cluster_id",
      "google_scholar_cites_id",
    ],
    Belief: ["confidence"],
    CodeChange: ["sha"],
    WebResult: ["url"],
    WebSearch: ["query", "provider"],
  });
  // Each is a property of the kind's create model in the committed schema, as is
  // each chip, of the value type the field's edit route takes: `createEdit`'s
  // body is typed by its keys, which this holds to the schema.
  for (const kind of creatableKinds(META.kinds)) {
    const model = schemaOf({ $ref: `#/components/schemas/Submit${kind}` });
    const routes = editableFields(kind);
    for (const name of [...fields[kind]!, ...Object.keys(emptyDraft(kind).chips)]) {
      const property = model.properties?.[name];
      expect(property, `${kind}.${name}`).toBeDefined();
      expect(jsonType(property!), `${kind}.${name}`).toBe(routes[name]!.value.type);
    }
  }
});

/** A schema in the committed `openapi.json`, as far as these tests read one. */
type Schema = {
  readonly $ref?: string;
  readonly type?: string;
  readonly anyOf?: readonly Schema[];
  readonly properties?: { readonly [name: string]: Schema };
  readonly [key: string]: unknown;
};

function schemaOf(schema: Schema): Schema {
  const schemas: { readonly [name: string]: Schema } = openapi.components.schemas;
  return schema.$ref ? schemaOf(schemas[schema.$ref.split("/").at(-1)!]!) : schema;
}

/** The JSON type a value of `schema` has when set: `null` aside, `$ref`s followed. */
function jsonType(schema: Schema): string | undefined {
  const resolved = schemaOf(schema);
  const set = resolved.anyOf?.map(schemaOf).filter((member) => member.type !== "null");
  return set?.length === 1 ? jsonType(set[0]!) : resolved.type;
}

test("a new Belief starts at the old UI's 0.5 confidence, which its create sends unless changed (B8)", async () => {
  expect(emptyDraft("Belief").fields).toEqual({ confidence: "0.5" });
  expect(emptyDraft("Issue").fields).toEqual({});
  expect((await send(draftOf("Belief"))).request.body).toMatchObject({ judgement: "unproven", confidence: 0.5 });
  expect(nextDraft(draftOf("Belief", { fields: { confidence: "0.9" } })).fields).toEqual({ confidence: "0.5" });
});

test("a new draft's chips are the kind's, at the mock's defaults, with no owner", () => {
  expect(emptyDraft("Issue").chips).toEqual({ status: "active", priority: 20, issue_kind: ["task"], owner: null, subscribers: [], labels: [] });
  expect(emptyDraft("Belief").chips).toEqual({ judgement: "unproven", status: "active", owner: null, subscribers: [], labels: [] });
  expect(Object.keys(emptyDraft("WebSearch").chips)).toEqual(["status", "owner", "subscribers", "labels"]);
});

test("switching kind keeps the text, the shared chips and the relations the new kind can take", () => {
  const issue = draftOf("Issue", {
    description: "Why",
    fields: { validation: "It ships" },
    chips: { ...emptyDraft("Issue").chips, status: "complete", priority: 5, labels: ["webui"] },
    relations: related("Issue", [
      ["narrows:out", PARENT],
      ["produced_by:out", PAPER],
    ]),
  });
  const artifact = switchKind(issue, "Artifact", META.edges);
  expect(artifact).toEqual({
    ...emptyDraft("Artifact"),
    title: "Ship it",
    description: "Why",
    chips: { status: "complete", owner: null, subscribers: [], labels: ["webui"] },
    relations: related("Artifact", [["produced_by:out", PAPER]]),
  });
  // Create more keeps the kind and the chips, and nothing else.
  expect(nextDraft(issue)).toEqual({ ...emptyDraft("Issue"), chips: issue.chips });
});

test("the relations offered are the topology's, each read from the new inquiry, then its lists of ids", () => {
  const options = (kind: Draft["kind"]) => relationOptions(kind, META.edges).map((option) => `${option.label}: ${option.targetKinds.join(" ")}`);
  expect(options("Experiment")).toEqual([
    "Produced by: Issue Artifact Experiment Paper Belief CodeChange WebResult WebSearch AgentSession",
    "Produces: Issue Artifact Experiment Paper Belief CodeChange WebResult WebSearch AgentSession",
    "Proves: Belief Experiment",
    "Proved by: Artifact Experiment Paper Belief CodeChange WebResult WebSearch AgentSession",
    "Favors: Belief Experiment",
    "Favored by: Artifact Experiment Paper Belief CodeChange WebResult WebSearch AgentSession",
    "Supersedes: Issue Artifact Experiment Paper Belief CodeChange WebResult WebSearch AgentSession",
    "Superseded by: Issue Artifact Experiment Paper Belief CodeChange WebResult WebSearch AgentSession",
    "Code changes: CodeChange",
  ]);
});

test("every kind but AgentSession can be created; New makes the kind on screen when it can", () => {
  const creatable = creatableKinds([...META.kinds, "Ticket"]);
  expect(creatable).toEqual(["Issue", "Artifact", "Experiment", "Paper", "Belief", "CodeChange", "WebResult", "WebSearch"]);
  expect(createKindFor({ name: "list", kind: "Paper" }, creatable)).toBe("Paper");
  expect(createKindFor({ name: "ref", kind: "Belief", seq: 3 }, creatable)).toBe("Belief");
  expect(createKindFor({ name: "list", kind: "AgentSession" }, creatable)).toBe("Issue");
  expect(createKindFor({ name: "activity" }, creatable)).toBe("Issue");
  expect(formatRoute({ name: "new", kind: "Paper" })).toBe("#/new/Paper");
});
