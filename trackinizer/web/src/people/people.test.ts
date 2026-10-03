import { describe, expect, test } from "vitest";
import { actorName, draftFor, newActor, offered, type People, type PersonDraft } from "./people";

const BLANK: PersonDraft = { who: "person", name: "", email: "", handle: "" };

describe("a new person or agent is written as the value the server stores, never a display name", () => {
  test("a person with an email is written as the email, lowercased, and named by their name", () => {
    expect(newActor({ ...BLANK, name: " Grace Hopper ", email: " Grace@Example.com " })).toEqual({
      actor: "grace@example.com",
      person: { name: "Grace Hopper", type: "person" },
    });
  });

  test("a person without an email is written as their name; one with only an email is named by it", () => {
    expect(newActor({ ...BLANK, name: "Josh" })).toEqual({ actor: "Josh", person: { name: "Josh", type: "person" } });
    expect(newActor({ ...BLANK, email: "ada@example.com" })).toEqual({
      actor: "ada@example.com",
      person: { name: "ada@example.com", type: "person" },
    });
  });

  test("an agent is written as its handle; the person fields are ignored", () => {
    expect(newActor({ who: "agent", name: "Grace", email: "g@example.com", handle: " craftax-arm " })).toEqual({
      actor: "craftax-arm",
      person: { name: "craftax-arm", type: "agent" },
    });
  });

  test("what cannot be added says why", () => {
    expect(newActor(BLANK)).toEqual({ error: "Add a name or an email." });
    expect(newActor({ ...BLANK, name: "Grace", email: "grace@" })).toEqual({
      error: "Enter an email such as jane@example.com, or leave it empty.",
    });
    expect(newActor({ ...BLANK, who: "agent", handle: "two words" })).toEqual({ error: "Give the agent a one-word handle, such as craftax-arm." });
    expect(newActor({ ...BLANK, who: "agent", handle: "  " })).toEqual({ error: "Give the agent a one-word handle, such as craftax-arm." });
  });
});

test("typed text opens the dialog as the likelier kind, in the field it fits", () => {
  expect(draftFor("jane@example.com")).toEqual({ ...BLANK, email: "jane@example.com" });
  expect(draftFor("Jane Doe")).toEqual({ ...BLANK, name: "Jane Doe" });
  expect(draftFor("josh")).toEqual({ ...BLANK, name: "josh" });
  expect(draftFor("craftax-arm")).toEqual({ ...BLANK, who: "agent", handle: "craftax-arm" });
  expect(draftFor("arm2")).toEqual({ ...BLANK, who: "agent", handle: "arm2" });
  expect(draftFor("", "agent")).toEqual({ ...BLANK, who: "agent" });
});

test("pickers offer me first, then everyone by name, each once, with the value written as the hint", () => {
  const people: People = {
    "grace@example.com": { name: "Grace Hopper", type: "person" },
    "craftax-arm": { name: "craftax-arm", type: "agent" },
  };
  expect(offered("ada@example.com", ["josh", "grace@example.com", "craftax-arm", "ada@example.com", "josh"], people)).toEqual([
    { actor: "ada@example.com", label: "ada@example.com", hint: "me" },
    { actor: "craftax-arm", label: "craftax-arm", hint: "agent" },
    { actor: "grace@example.com", label: "Grace Hopper", hint: "grace@example.com" },
    { actor: "josh", label: "josh" },
  ]);
  expect(actorName("grace@example.com", people)).toBe("Grace Hopper");
  expect(actorName("josh", people)).toBe("josh");
});
