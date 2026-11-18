import { useState } from "react";
import { ApiError } from "../api/client";
import { SETTINGS } from "../api/generated/settings";
import { setPreferences } from "../api/me";
import { type Preferences, receivePreferences, usePreferences } from "../preferences";
import { Section } from "./account";

/** One setting's key. */
type Key = (typeof SETTINGS)[number]["key"];

/** What is wrong with the document, and on which line (1-based). */
export type Problem = { readonly line: number; readonly message: string };

/**
 * The user's settings as one document, as VS Code's settings.json: each key with
 * its value, the server's description of it as the comment above. The comments
 * are the server's, not the user's: a save keeps the values and the document is
 * drawn afresh. Save sends only the keys whose values changed; a key taken out
 * goes back to its default. Every problem is shown with its line, and a document
 * with any problem changes nothing.
 */
export function PreferencesDocument() {
  const current = usePreferences();
  const [edited, setEdited] = useState<string | null>(null);
  const [problems, setProblems] = useState<readonly Problem[]>([]);
  const [saving, setSaving] = useState(false);
  // Not edited, it follows the preferences wherever they change: here, another tab, the agent.
  const text = edited ?? documentOf(current);
  const send = async (change: Partial<Preferences>) => {
    setSaving(true);
    try {
      receivePreferences(await setPreferences(change));
      setEdited(null);
      setProblems([]);
    } catch (error) {
      setProblems(refusal(error, text));
    } finally {
      setSaving(false);
    }
  };
  const save = () => {
    const read = readDocument(text);
    if ("problems" in read) {
      setProblems(read.problems);
      return;
    }
    const change = changeOf(current, read.values);
    if (Object.keys(change).length === 0) {
      setEdited(null);
      setProblems([]);
      return;
    }
    void send(change);
  };
  return (
    <Section title="Preferences document">
      <p className="st-note">Every setting, with what it does. Edit a value and save; take a line out to put it back to its default.</p>
      <textarea
        className="st-doc"
        aria-label="Preferences document"
        spellCheck={false}
        rows={text.split("\n").length}
        value={text}
        onChange={(event) => setEdited(event.currentTarget.value)}
      />
      {problems.length > 0 && (
        <ul className="st-doc-problems" role="alert">
          {problems.map((problem) => <li key={`${problem.line}:${problem.message}`}>Line {problem.line}: {problem.message}</li>)}
        </ul>
      )}
      <div className="st-row">
        <button type="button" className="btn" disabled={saving || edited === null} onClick={save}>Save</button>
        <button type="button" className="btn ghost" disabled={saving} onClick={() => void send(Object.fromEntries(SETTINGS.map(({ key }) => [key, null])) as Partial<Preferences>)}>
          Reset to defaults
        </button>
      </div>
    </Section>
  );
}

/** The document for `preferences`: each setting's comment, then its key and value. */
export function documentOf(preferences: Preferences): string {
  const lines = SETTINGS.flatMap(({ key, description }, index) => [
    `  // ${description}`,
    `  ${JSON.stringify(key)}: ${JSON.stringify(preferences[key])}${index < SETTINGS.length - 1 ? "," : ""}`,
  ]);
  return ["{", ...lines, "}"].join("\n");
}

/**
 * Read a document as JSON with `//` comments and trailing commas, into its
 * settings' values, or the problems that stop it. Comments and stray commas are
 * blanked, not cut, so a parse error's position is still its line.
 */
export function readDocument(text: string): { readonly values: { readonly [key: string]: unknown } } | { readonly problems: readonly Problem[] } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(blanked(text));
  } catch (error) {
    const at = /position (\d+)/.exec(String(error));
    const line = at ? lineAt(text, Number(at[1])) : 1;
    return { problems: [{ line, message: "This is not JSON with comments: check the quotes and commas." }] };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { problems: [{ line: 1, message: "The document is one { … } of settings." }] };
  }
  const known = new Set<string>(SETTINGS.map(({ key }) => key));
  const problems = Object.keys(parsed)
    .filter((key) => !known.has(key))
    .map((key) => ({ line: lineOf(text, key), message: `There is no setting ${JSON.stringify(key)}.` }));
  return problems.length > 0 ? { problems } : { values: parsed as { readonly [key: string]: unknown } };
}

/** The keys whose values the document changed, and those it took out (back to their defaults). */
function changeOf(current: Preferences, values: { readonly [key: string]: unknown }): Partial<Preferences> {
  const change: { [key: string]: unknown } = {};
  for (const { key, default: fallback } of SETTINGS) {
    if (key in values) {
      if (JSON.stringify(values[key]) !== JSON.stringify(current[key])) change[key] = values[key];
    } else if (JSON.stringify(current[key]) !== JSON.stringify(fallback)) {
      change[key] = null;
    }
  }
  return change as Partial<Preferences>;
}

/** The server's refusal of a save, on the lines of the settings it names. */
function refusal(error: unknown, text: string): readonly Problem[] {
  if (!(error instanceof ApiError) || error.status !== 422) {
    return [{ line: 1, message: `Not saved: ${error instanceof Error ? error.message : String(error)}` }];
  }
  // The server names each setting it refused: "theme: Input should be …".
  return error.detail.split(/;\s*|\n/).filter(Boolean).map((part) => {
    const key = SETTINGS.find((setting) => part.startsWith(`${setting.key}:`) || part.includes(` ${setting.key}`))?.key;
    return { line: key ? lineOf(text, key) : 1, message: part };
  });
}

/** `text` with each `//` comment, and each comma before a closing bracket, made spaces; strings kept. */
function blanked(text: string): string {
  const out = [...text];
  let inString = false;
  for (let at = 0; at < out.length; at += 1) {
    const char = out[at];
    if (inString) {
      if (char === "\\") at += 1;
      else if (char === '"') inString = false;
      continue;
    }
    if (char === '"') {
      inString = true;
    } else if (char === "/" && out[at + 1] === "/") {
      for (; at < out.length && out[at] !== "\n"; at += 1) out[at] = " ";
    } else if (char === ",") {
      let next = at + 1;
      while (next < out.length && /\s/.test(out[next]!)) next += 1;
      if (out[next] === "}" || out[next] === "]") out[at] = " ";
    }
  }
  return out.join("");
}

/** The 1-based line of the character at `position`. */
function lineAt(text: string, position: number): number {
  return text.slice(0, position).split("\n").length;
}

/** The line a key is written on, or 1. */
function lineOf(text: string, key: Key | string): number {
  const at = text.indexOf(JSON.stringify(key));
  return at < 0 ? 1 : lineAt(text, at);
}
