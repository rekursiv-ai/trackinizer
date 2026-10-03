import { describe, expect, test } from "vitest";
import {
  appliesTo,
  compileQuery,
  escapeRegex,
  filterFields,
  type ListQuery,
  NO_PRIORITY,
  oneOf,
  removeChoice,
  toggleChoice,
  traxLine,
  withTab,
} from "./query";
import { priorityBand } from "../ui/glyphs";

// As `GET /api/meta/fields` answers: kind-specific fields only.
const FIELD_OWNERS = { priority: "issue", issue_kind: "issue", judgement: "belief", authors: "paper" };

const issues: ListQuery = { kinds: ["Issue"], tab: "active", choices: [] };

describe("one query drives the request and the trax line", () => {
  test("the Active tab is one status filter, and the trax line says the same", () => {
    const request = compileQuery(issues, FIELD_OWNERS);
    expect(request).toEqual({
      kinds: ["Issue"],
      filters: [{ field: "status", op: "is", value: "active" }],
    });
    expect(traxLine(request)).toBe("trax issue status is active");
  });

  test("Closed is status ne active, since the status param takes one value", () => {
    const request = compileQuery({ ...issues, tab: "closed" }, FIELD_OWNERS);
    expect(request.filters).toEqual([{ field: "status", op: "ne", value: "active" }]);
    expect(traxLine(request)).toBe("trax issue status ne active");
  });

  test("COLD-05: the All tab adds no status filter to the query shown", () => {
    const shown = toggleChoice({ ...issues, tab: "all" }, "status", "complete");
    const request = compileQuery(shown, FIELD_OWNERS);
    expect(request.filters).toEqual([{ field: "status", op: "is", value: "complete" }]);
    expect(traxLine(request)).toBe("trax issue status is complete");
  });

  test("COLD-06: a Kind choice narrows the kinds, in the request and the trax line", () => {
    const mine: ListQuery = { kinds: ["Issue", "Belief", "Paper"], tab: "all", choices: [] };
    const request = compileQuery(toggleChoice(mine, "kind", "Paper"), FIELD_OWNERS);
    expect(request).toEqual({ kinds: ["Paper"], filters: [] });
    expect(traxLine(request)).toBe("trax paper");
  });

  test("choices AND together in the order they were added, after the tab", () => {
    let query = toggleChoice(issues, "owner", "josh");
    query = toggleChoice(query, "labels", "ui");
    query = toggleChoice(query, "labels", "docs");
    expect(traxLine(compileQuery(query, FIELD_OWNERS))).toBe(
      "trax issue status is active owner is josh labels re '^(ui|docs)$'",
    );
  });

  test("a value the shell would split or expand is single-quoted", () => {
    const query = toggleChoice(issues, "owner", "it's me");
    expect(traxLine(compileQuery(query, FIELD_OWNERS))).toBe(
      `trax issue status is active owner is 'it'\\''s me'`,
    );
  });

  test("a # (a comment, or zsh's glob) or a leading = (zsh's command path) is quoted (WEB-24)", () => {
    const line = (label: string) => traxLine(compileQuery(toggleChoice(issues, "labels", label), FIELD_OWNERS));
    expect(line("#urgent")).toBe("trax issue status is active labels is '#urgent'");
    expect(line("c#2")).toBe("trax issue status is active labels is 'c#2'");
    expect(line("=ls")).toBe("trax issue status is active labels is '=ls'");
    expect(line("a=b")).toBe("trax issue status is active labels is a=b");
  });

  test("a value that starts with - follows a --, so trax does not read it as a flag", () => {
    const request = compileQuery(toggleChoice(issues, "labels", "-wip"), FIELD_OWNERS);
    expect(traxLine(request)).toBe("trax issue -- status is active labels is -wip");
  });
});

describe("the plan's Who me is: in a read filter, my email stands for every name of mine", () => {
  const me = ["ada@example.com", "ada", "Agent Ada"];

  test("Me alone is the one anchored regex over my names, as meFilter makes it", () => {
    const request = compileQuery(toggleChoice(issues, "owner", "ada@example.com"), FIELD_OWNERS, me);
    expect(request.filters.at(-1)).toEqual({ field: "owner", op: "re", value: "^(ada@example\\.com|ada|Agent Ada)$" });
  });

  test("Me with another owner ORs them all; with no aliases Me is the email alone", () => {
    const both = toggleChoice(toggleChoice(issues, "owner", "josh"), "owner", "ada@example.com");
    expect(compileQuery(both, FIELD_OWNERS, me).filters.at(-1)!.value).toBe("^(josh|ada@example\\.com|ada|Agent Ada)$");
    const alone = compileQuery(toggleChoice(issues, "owner", "ada@example.com"), FIELD_OWNERS, ["ada@example.com"]);
    expect(alone.filters.at(-1)).toEqual({ field: "owner", op: "is", value: "ada@example.com" });
  });

  test("only the owner field reads me: a label named like my email is that label", () => {
    const request = compileQuery(toggleChoice(issues, "labels", "ada@example.com"), FIELD_OWNERS, me);
    expect(request.filters.at(-1)).toEqual({ field: "labels", op: "is", value: "ada@example.com" });
  });
});

describe("S8: kinds without a filtered field are left out of the request", () => {
  const mixed: ListQuery = { kinds: ["Issue", "Belief"], tab: "all", choices: [] };

  test("base fields apply to every kind; kind fields only to their own", () => {
    expect(appliesTo("owner", "Belief", FIELD_OWNERS)).toBe(true);
    expect(appliesTo("priority", "Issue", FIELD_OWNERS)).toBe(true);
    expect(appliesTo("priority", "Belief", FIELD_OWNERS)).toBe(false);
  });

  test("a priority filter requests Issues only, since Beliefs would answer 400", () => {
    const request = compileQuery(toggleChoice(mixed, "priority", "1"), FIELD_OWNERS);
    expect(request.kinds).toEqual(["Issue"]);
  });

  test("fields of two different kinds leave no kind to request", () => {
    let query = toggleChoice(mixed, "priority", "1");
    query = toggleChoice(query, "judgement", "proven");
    expect(compileQuery(query, FIELD_OWNERS).kinds).toEqual([]);
  });

  test("a base-field filter keeps every kind", () => {
    const request = compileQuery(toggleChoice(mixed, "owner", "josh"), FIELD_OWNERS);
    expect(request.kinds).toEqual(["Issue", "Belief"]);
  });
});

describe("COLD-07: the priority selection is one exact range", () => {
  // Whether `priority` passes every compiled filter, as the server would decide.
  const passes = (query: ListQuery, priority: number | null) =>
    compileQuery(query, FIELD_OWNERS)
      .filters.filter((f) => f.field === "priority")
      .every((f) => {
        if (f.op === "isnull") return priority === null;
        if (priority === null) return false;
        const bound = Number(f.value);
        return f.op === "ge" ? priority >= bound : f.op === "lt" ? priority < bound : false;
      });
  const ticked = (query: ListQuery) => query.choices.find((c) => c.field === "priority")?.values ?? [];

  test("ticking P0 then P2 fills in P1, so what is ticked is what matches", () => {
    let query = toggleChoice(issues, "priority", "0");
    query = toggleChoice(query, "priority", "2");
    expect(ticked(query)).toEqual(["0", "1", "2"]);
    for (const priority of [0, 5, 10, 20, 29, 30, 40, null]) {
      expect(passes(query, priority)).toBe(ticked(query).includes(String(priorityBand(priority))));
    }
  });

  test("Low has no upper bound, so backlog (40) matches it", () => {
    const query = toggleChoice(issues, "priority", "3");
    expect(compileQuery(query, FIELD_OWNERS).filters).toContainEqual({
      field: "priority",
      op: "ge",
      value: "30",
    });
    expect(passes(query, 40)).toBe(true);
    expect(passes(query, null)).toBe(false);
  });

  test("unticking an end shrinks the range; a middle band selects just itself", () => {
    let query = toggleChoice(toggleChoice(issues, "priority", "0"), "priority", "3");
    expect(ticked(query)).toEqual(["0", "1", "2", "3"]);
    query = toggleChoice(query, "priority", "0");
    expect(ticked(query)).toEqual(["1", "2", "3"]);
    query = toggleChoice(query, "priority", "2");
    expect(ticked(query)).toEqual(["2"]);
    query = toggleChoice(query, "priority", "2");
    expect(query.choices).toEqual([]);
  });

  test("No priority is isnull, and never mixes with a band", () => {
    let query = toggleChoice(issues, "priority", "1");
    query = toggleChoice(query, "priority", NO_PRIORITY);
    expect(ticked(query)).toEqual([NO_PRIORITY]);
    expect(compileQuery(query, FIELD_OWNERS).filters).toContainEqual({
      field: "priority",
      op: "isnull",
      value: "",
    });
    expect(traxLine(compileQuery(query, FIELD_OWNERS))).toBe(
      "trax issue status is active priority isnull",
    );
    expect(ticked(toggleChoice(query, "priority", "2"))).toEqual(["2"]);
  });
});

describe("COLD-08: several literal values become one escaped, anchored regex", () => {
  test("one value is an exact match", () => {
    expect(oneOf("owner", ["dan@example.com"])).toEqual({
      field: "owner",
      op: "is",
      value: "dan@example.com",
    });
  });

  test("a dot in an owner matches only a dot", () => {
    const filter = oneOf("owner", ["dan@example.com", "craftax-arm"]);
    expect(filter).toEqual({ field: "owner", op: "re", value: "^(dan@example\\.com|craftax-arm)$" });
    const pattern = new RegExp(filter.value);
    expect(pattern.test("dan@example.com")).toBe(true);
    expect(pattern.test("craftax-arm")).toBe(true);
    expect(pattern.test("dan@exampleXcom")).toBe(false);
  });

  test("every regex metacharacter in a label is literal", () => {
    const labels = ["a(b", "c[d]", "x|y", "^$.*+?{}\\"];
    const pattern = new RegExp(oneOf("labels", labels).value);
    for (const label of labels) expect(pattern.test(label)).toBe(true);
    expect(pattern.test("x")).toBe(false);
    expect(pattern.test("ab")).toBe(false);
    expect(escapeRegex("a.b")).toBe("a\\.b");
  });
});

describe("editing a query", () => {
  test("ticking a value twice removes it, and the empty choice goes", () => {
    const query = toggleChoice(toggleChoice(issues, "owner", "josh"), "owner", "josh");
    expect(query.choices).toEqual([]);
  });

  test("removing a choice keeps the others", () => {
    const query = toggleChoice(toggleChoice(issues, "owner", "josh"), "labels", "ui");
    expect(removeChoice(query, "owner").choices).toEqual([{ field: "labels", values: ["ui"] }]);
  });

  test("a tab change drops the status choice, which the tab now decides", () => {
    const query = toggleChoice(toggleChoice({ ...issues, tab: "all" }, "status", "complete"), "owner", "x");
    expect(withTab(query, "active")).toEqual({
      kinds: ["Issue"],
      tab: "active",
      choices: [{ field: "owner", values: ["x"] }],
    });
  });
});

describe("the Filter menu offers each kind its own fields", () => {
  test("a kind's fields show only on lists that hold that kind", () => {
    expect(filterFields({ ...issues, tab: "all" }, FIELD_OWNERS)).toEqual(["status", "priority", "owner", "labels"]);
    expect(filterFields({ kinds: ["Belief"], tab: "all", choices: [] }, FIELD_OWNERS)).toEqual([
      "status",
      "judgement",
      "owner",
      "labels",
    ]);
    expect(filterFields({ kinds: ["Paper"], tab: "all", choices: [] }, FIELD_OWNERS)).toEqual([
      "status",
      "owner",
      "labels",
    ]);
  });

  test("a mixed list offers Kind and every kind's fields; S8 then narrows the request", () => {
    const mixed: ListQuery = { kinds: ["Issue", "Belief"], tab: "all", choices: [] };
    expect(filterFields(mixed, FIELD_OWNERS)).toEqual(["kind", "status", "priority", "judgement", "owner", "labels"]);
  });

  test("the Active tab decides status, so Status is not offered there", () => {
    expect(filterFields(issues, FIELD_OWNERS)).toEqual(["priority", "owner", "labels"]);
  });
});
