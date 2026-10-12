// The summary documents the summary service files as Artifact descriptions, as plain JSON.
// They hold the words and counts of a summary and item numbers, never a transcript line.

export type Step = {
  readonly start: number;
  readonly end: number;
  readonly title: string;
  readonly text: string;
  readonly learned: string;
};

export type Evidence = { readonly at: number; readonly valence: number };

export type Belief = {
  readonly id: string;
  readonly claim: string;
  readonly verdict: string;
  readonly confidence: number;
  readonly evidence: readonly Evidence[];
};

/** Where an item is stored: the part and idx of its record. Only a reader of the session's audience can open it. */
export type Anchor = { readonly item: number; readonly part: number; readonly idx: number };

export type State = "running" | "closed";

export type SessionDoc = {
  readonly kind: "session";
  readonly session: string;
  readonly ref: string;
  readonly state: State;
  readonly as_of: string;
  readonly title: string;
  readonly ask: string;
  readonly overview: string;
  readonly outcome: string;
  readonly items: number;
  readonly tool_calls: number;
  readonly errors: number;
  readonly learnings: number;
  readonly started: string;
  readonly steps: readonly Step[];
  readonly phases: readonly Step[];
  readonly beliefs: readonly Belief[];
  readonly anchors: readonly Anchor[];
};

export type SessionRow = {
  readonly session: string;
  readonly ref: string;
  readonly title: string;
  readonly outcome: string;
  readonly started: string;
  readonly items: number;
  readonly errors: number;
  readonly learnings: number;
};

export type EffortMember = { readonly session: string; readonly title: string; readonly text: string };
export type Effort = { readonly title: string; readonly text: string; readonly members: readonly EffortMember[] };
export type ClaimMember = { readonly session: string; readonly claim: string; readonly verdict: string };
export type Claim = {
  readonly claim: string;
  readonly status: string;
  readonly supporting: number;
  readonly refuting: number;
  readonly members: readonly ClaimMember[];
};

export type CorpusDoc = {
  readonly kind: "campaign";
  readonly campaign: string;
  readonly issue: string;
  readonly state: State;
  readonly as_of: string;
  readonly sessions: readonly SessionRow[];
  readonly efforts: readonly Effort[];
  readonly claims: readonly Claim[];
};

type Obj = Readonly<Record<string, unknown>>;

const isObj = (value: unknown): value is Obj => typeof value === "object" && value !== null && !Array.isArray(value);
const str = (value: unknown): value is string => typeof value === "string";
const int = (value: unknown): value is number => typeof value === "number" && Number.isInteger(value);
const num = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const list = <T>(value: unknown, ok: (item: unknown) => item is T): value is readonly T[] =>
  Array.isArray(value) && value.every(ok);

function isStep(value: unknown): value is Step {
  return isObj(value) && int(value.start) && int(value.end) && str(value.title) && str(value.text)
    && str(value.learned);
}
function isEvidence(value: unknown): value is Evidence {
  return isObj(value) && int(value.at) && (value.valence === 1 || value.valence === -1);
}
function isBelief(value: unknown): value is Belief {
  return isObj(value) && str(value.id) && str(value.claim) && str(value.verdict) && num(value.confidence)
    && list(value.evidence, isEvidence);
}
function isAnchor(value: unknown): value is Anchor {
  return isObj(value) && int(value.item) && int(value.part) && int(value.idx);
}
function isState(value: unknown): value is State {
  return value === "running" || value === "closed";
}
function isSessionRow(value: unknown): value is SessionRow {
  return isObj(value) && str(value.session) && str(value.ref) && str(value.title)
    && str(value.outcome) && str(value.started) && int(value.items) && int(value.errors) && int(value.learnings);
}
function isEffort(value: unknown): value is Effort {
  return isObj(value) && str(value.title) && str(value.text)
    && list(value.members, (m): m is EffortMember => isObj(m) && str(m.session) && str(m.title) && str(m.text));
}
function isClaim(value: unknown): value is Claim {
  return isObj(value) && str(value.claim) && str(value.status) && int(value.supporting) && int(value.refuting)
    && list(value.members, (m): m is ClaimMember => isObj(m) && str(m.session) && str(m.claim) && str(m.verdict));
}

function parse(text: string | null): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** A session document from an Artifact's description, or null when it is not one this build reads. */
export function parseSessionDoc(text: string | null): SessionDoc | null {
  const value = parse(text);
  if (!isObj(value) || value.kind !== "session" || value.version !== 1) return null;
  const ok = str(value.session) && str(value.ref) && isState(value.state) && str(value.as_of)
    && str(value.title) && str(value.ask) && str(value.overview) && str(value.outcome) && int(value.items)
    && int(value.tool_calls) && int(value.errors) && int(value.learnings) && str(value.started)
    && list(value.steps, isStep) && list(value.phases, isStep) && list(value.beliefs, isBelief)
    && list(value.anchors, isAnchor);
  return ok ? (value as unknown as SessionDoc) : null;
}

/** A campaign document from an Artifact's description, or null when it is not one this build reads. */
export function parseCorpusDoc(text: string | null): CorpusDoc | null {
  const value = parse(text);
  if (!isObj(value) || value.kind !== "campaign" || value.version !== 1) return null;
  const ok = str(value.campaign) && str(value.issue) && isState(value.state) && str(value.as_of)
    && list(value.sessions, isSessionRow) && list(value.efforts, isEffort) && list(value.claims, isClaim);
  return ok ? (value as unknown as CorpusDoc) : null;
}

/** The campaign's sessions by start time, earliest first; the campaign's team shape is not shown, so there is no tree. */
export function sessionsByStart(rows: readonly SessionRow[]): SessionRow[] {
  return [...rows].sort((a, b) => a.started.localeCompare(b.started) || a.session.localeCompare(b.session));
}

/** The anchor of an item, or undefined when the summary named it without one. */
export function anchorOf(doc: SessionDoc, item: number): Anchor | undefined {
  return doc.anchors.find((anchor) => anchor.item === item);
}

/** The item range of a step as a reader writes it: `#7`, or `#7–23`. */
export function itemRange(span: { readonly start: number; readonly end: number }): string {
  return span.start === span.end ? `#${span.start}` : `#${span.start}–${span.end}`;
}

/** A session document's line in the sessions list, for a campaign whose own summary is not filed yet. */
export function rowOfDoc(doc: SessionDoc): SessionRow {
  const { session, ref, title, outcome, started, items, errors, learnings } = doc;
  return { session, ref, title, outcome, started, items, errors, learnings };
}
