import { Icon, type IconName } from "./icons";

/** How a kind is named and drawn. */
export type KindLook = {
  /** For a list or a sidebar entry: `Code changes`. */
  readonly plural: string;
  /** For one row, in running text: `code change`. */
  readonly one: string;
  readonly icon: IconName;
};

/**
 * How `kind` is named and drawn, from the mock's copy.
 *
 * Which kinds exist comes from the server (`inquiry_kind_all`); this only
 * names them. A kind the mock never named shows its own name and a plain box,
 * so a new server kind appears without a UI change.
 */
export function kindLook(kind: string): KindLook {
  return LOOKS[kind] ?? { plural: kind, one: kind, icon: "box" };
}

/** The icon of `kind`. */
export function KindIcon({ kind, size = 16 }: { kind: string; size?: number }) {
  return <Icon name={kindLook(kind).icon} size={size} />;
}

const LOOKS: { readonly [kind: string]: KindLook } = {
  Issue: { plural: "Issues", one: "issue", icon: "issue" },
  Belief: { plural: "Beliefs", one: "belief", icon: "belief" },
  Experiment: { plural: "Experiments", one: "experiment", icon: "flask" },
  Paper: { plural: "Papers", one: "paper", icon: "book" },
  Artifact: { plural: "Artifacts", one: "artifact", icon: "box" },
  CodeChange: { plural: "Code changes", one: "code change", icon: "commit" },
  // Not "search": that magnifier is the sidebar's search button, beside it.
  WebSearch: { plural: "Web searches", one: "web search", icon: "textSearch" },
  WebResult: { plural: "Web results", one: "web result", icon: "globe" },
  AgentSession: { plural: "Agent sessions", one: "agent session", icon: "bot" },
};
