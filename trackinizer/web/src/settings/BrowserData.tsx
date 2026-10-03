import { type ChangeEvent, useRef, useState } from "react";
import { useProfile } from "../app/boot";
import { CopyDetails } from "../debug/CopyDetails";
import { useBrowserState } from "../state/store";
import { type BrowserState, exportState, mergeImport, parseState } from "../state/value";
import { Icon } from "../ui/icons";
import { useCopy, useToast } from "../ui/toast";
import { KeyValue, Section } from "./account";

/**
 * What this browser keeps for you and trackinizer does not: stars, saved
 * views, your names, people added by hand, and read state. Export it as JSON;
 * import another browser's export, which merges into this one and deletes
 * nothing (`mergeImport`).
 */
export function BrowserDataSection() {
  const { email } = useProfile();
  const [state, update] = useBrowserState();
  const toast = useToast();
  const copy = useCopy();
  const chooser = useRef<HTMLInputElement>(null);
  const [failure, setFailure] = useState<{ readonly message: string; readonly error: unknown } | null>(null);
  const download = () => {
    const url = URL.createObjectURL(new Blob([exportState(state)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `trackinizer-${email}-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };
  const importFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    // Cleared, so choosing the same file again imports it again.
    event.target.value = "";
    if (!file) return;
    let imported: BrowserState;
    try {
      imported = parseState(parseJson(await file.text()));
    } catch (error) {
      setFailure({ message: `Could not import ${file.name}. ${error instanceof Error ? error.message : String(error)}`, error });
      return;
    }
    let summary = "";
    try {
      update((current) => {
        const merged = mergeImport(current, imported);
        summary = importSummary(current, merged);
        return merged;
      });
    } catch (error) {
      setFailure({ message: `Could not save the import. ${error instanceof Error ? error.message : String(error)}`, error });
      return;
    }
    setFailure(null);
    toast(summary);
  };
  return (
    <Section title="This browser">
      <KeyValue label="Stars">{state.stars.length}</KeyValue>
      <KeyValue label="Saved views">{state.views.length}</KeyValue>
      <KeyValue label="People added">{Object.keys(state.people).length}</KeyValue>
      <div className="st-row st-actions">
        <button type="button" className="btn ghost" onClick={() => void copy(exportState(state), "Copied this browser's state as JSON")}>
          <Icon name="copy" size={13} />
          Copy as JSON
        </button>
        <button type="button" className="btn ghost" onClick={download}>
          Download JSON
        </button>
        <button type="button" className="btn ghost" onClick={() => chooser.current?.click()}>
          Import JSON…
        </button>
        <input
          ref={chooser}
          type="file"
          accept="application/json,.json"
          aria-label="Import JSON file"
          hidden
          onChange={(event) => void importFile(event)}
        />
      </div>
      <p className="st-note">An import adds what this browser lacks and never deletes. Collapsed sections and lenses stay as they are here.</p>
      {failure ? (
        <p className="form-err" role="alert">
          {failure.message}
          <CopyDetails message={failure.message} error={failure.error} />
        </p>
      ) : null}
    </Section>
  );
}

/** What an import added, as one sentence. */
export function importSummary(before: BrowserState, after: BrowserState): string {
  const added = [
    count(after.stars.length - before.stars.length, "star"),
    count(after.views.length - before.views.length, "saved view"),
    count(after.aliases.length - before.aliases.length, "name"),
    count(Object.keys(after.people).length - Object.keys(before.people).length, "person", "people"),
  ].filter((part) => part !== null);
  if (after.read.boundary !== before.read.boundary || after.read.marks.length > before.read.marks.length) {
    added.push("read state");
  }
  if (added.length === 0) return "Nothing to import: this browser already has everything in the file.";
  return `Imported ${added.length === 1 ? added[0] : `${added.slice(0, -1).join(", ")} and ${added.at(-1)}`}.`;
}

function count(n: number, one: string, many = `${one}s`): string | null {
  return n === 0 ? null : `${n} ${n === 1 ? one : many}`;
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    throw new Error("It is not JSON.");
  }
}
