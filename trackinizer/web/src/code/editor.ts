// The code pane's editor engine: CodeMirror 6, read-only, dressed in the app's own tokens.
//
// This module is a CHUNK OF ITS OWN and must stay that way. First-load JavaScript is held to
// a 250 KB budget (`scripts/bundle-size.ts`) with about 30 KB spare, and CodeMirror with its
// grammars is several times that. `CodePane` reaches it through `chunk(import("./editor"))`,
// so it is fetched the first time someone opens the Code view and never by a first visit.
// Nothing outside that dynamic import may import this file.
//
// Read-only is set TWICE on purpose: `EditorState.readOnly` stops transactions that would
// change the document, and `EditorView.editable` takes the DOM out of contentEditable so
// there is no caret and no IME. Either alone leaves half an editor — a caret that silently
// refuses input, or a surface a plugin could still write through.
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import {
  bracketMatching,
  foldGutter,
  foldKeymap,
  HighlightStyle,
  indentUnit,
  LanguageSupport,
  syntaxHighlighting,
} from "@codemirror/language";
import { highlightSelectionMatches, search, searchKeymap } from "@codemirror/search";
import { Compartment, EditorState, type Extension } from "@codemirror/state";
import { EditorView, highlightActiveLine, highlightActiveLineGutter, keymap, lineNumbers } from "@codemirror/view";
import { tags } from "@lezer/highlight";

/**
 * A path's grammar, loaded on demand.
 *
 * Each grammar is its own dynamic import, so opening a `.ts` file never fetches the markdown
 * or HTML parsers. Bundled together they roughly doubled this chunk; apart, a reader pays
 * only for the languages they actually open. `null` is plain text, which needs no grammar.
 */
async function languageFor(path: string): Promise<Extension | null> {
  const name = path.split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  const extension = dot > 0 ? name.slice(dot + 1).toLowerCase() : name.toLowerCase();
  switch (extension) {
    case "ts":
      return (await import("@codemirror/lang-javascript")).javascript({ typescript: true });
    case "tsx":
      return (await import("@codemirror/lang-javascript")).javascript({ typescript: true, jsx: true });
    case "jsx":
      return (await import("@codemirror/lang-javascript")).javascript({ jsx: true });
    case "js":
    case "mjs":
    case "cjs":
      return (await import("@codemirror/lang-javascript")).javascript();
    case "py":
    case "pyi":
      return (await import("@codemirror/lang-python")).python();
    case "css":
    case "scss":
    case "less":
      return (await import("@codemirror/lang-css")).css();
    case "json":
    case "jsonc":
      return (await import("@codemirror/lang-json")).json();
    case "md":
    case "markdown":
      return (await import("@codemirror/lang-markdown")).markdown();
    case "html":
    case "htm":
    case "svg":
      return (await import("@codemirror/lang-html")).html();
    case "yml":
    case "yaml":
      return (await import("@codemirror/lang-yaml")).yaml();
    default:
      return null;
  }
}

/**
 * Syntax colours, mapped onto the pane's `--syn-*` ramp (`styles.css`), which is the station
 * bar's four group hues and clears WCAG AA on every surface in both themes (`styles.test.ts`).
 * CodeMirror's own themes are not used: one would bring a second palette that neither the
 * theme switch nor the contrast test knows about.
 */
const highlight = HighlightStyle.define([
  { tag: [tags.keyword, tags.moduleKeyword, tags.controlKeyword, tags.operatorKeyword], color: "var(--syn-keyword)" },
  { tag: [tags.string, tags.special(tags.string), tags.regexp], color: "var(--syn-string)" },
  { tag: [tags.number, tags.bool, tags.null, tags.integer, tags.float], color: "var(--syn-number)" },
  { tag: [tags.atom, tags.className, tags.typeName, tags.namespace], color: "var(--syn-function)" },
  { tag: [tags.propertyName, tags.attributeName, tags.variableName, tags.labelName], color: "var(--syn-property)" },
  { tag: [tags.function(tags.variableName), tags.function(tags.propertyName)], color: "var(--syn-function)" },
  { tag: [tags.comment, tags.lineComment, tags.blockComment, tags.docComment], color: "var(--muted)", fontStyle: "italic" },
  { tag: [tags.heading, tags.strong], color: "var(--text)", fontWeight: "600" },
  { tag: tags.emphasis, fontStyle: "italic" },
  { tag: [tags.link, tags.url], color: "var(--accent-hover)", textDecoration: "underline" },
  { tag: tags.invalid, color: "var(--error-text)" },
  { tag: [tags.meta, tags.punctuation, tags.separator, tags.bracket], color: "var(--ink-muted)" },
]);

/**
 * The editor's own chrome in the app's tokens. Every colour is a `var(--…)`, so the theme
 * switch moves the editor with the rest of the page and no second palette exists to drift.
 */
const theme = EditorView.theme({
  "&": { height: "100%", backgroundColor: "var(--bg)", color: "var(--text)" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": {
    fontFamily: "var(--mono)",
    fontSize: "12.5px",
    lineHeight: "1.65",
    // The pane scrolls inside itself; the page never scrolls sideways for a long line.
    overflow: "auto",
  },
  ".cm-content": { padding: "8px 0 24px" },
  // A read-only view still draws a cursor layer; there is no caret to show.
  ".cm-cursorLayer": { display: "none" },
  ".cm-gutters": {
    backgroundColor: "var(--bg)",
    color: "var(--ink-machine)",
    border: "none",
    borderRight: "1px solid var(--line)",
  },
  ".cm-lineNumbers .cm-gutterElement": { padding: "0 10px 0 14px", fontVariantNumeric: "tabular-nums" },
  ".cm-foldGutter .cm-gutterElement": { padding: "0 2px", color: "var(--muted)", cursor: "pointer" },
  ".cm-activeLine": { backgroundColor: "var(--accent-soft)" },
  ".cm-activeLineGutter": { backgroundColor: "var(--accent-soft)", color: "var(--text)" },
  ".cm-line": { padding: "0 16px 0 14px" },
  ".cm-selectionBackground, ::selection": { backgroundColor: "var(--accent-soft)" },
  "&.cm-focused .cm-selectionBackground": { backgroundColor: "var(--accent-soft)" },
  ".cm-matchingBracket, &.cm-focused .cm-matchingBracket": {
    backgroundColor: "var(--accent-soft)",
    outline: "1px solid var(--accent)",
  },
  ".cm-selectionMatch": { backgroundColor: "var(--surface-hover)" },
  // The find panel, which `search` mounts into the view.
  ".cm-panels": { backgroundColor: "var(--surface)", color: "var(--text)", borderTop: "1px solid var(--line)" },
  ".cm-panel.cm-search": { padding: "6px 10px", fontFamily: "var(--sans)", fontSize: "12px" },
  ".cm-panel.cm-search input, .cm-panel.cm-search button": {
    backgroundColor: "var(--bg)",
    color: "var(--text)",
    border: "1px solid var(--line-strong)",
    borderRadius: "var(--radius)",
    padding: "2px 6px",
    fontFamily: "var(--sans)",
  },
  ".cm-panel.cm-search label": { color: "var(--muted)" },
  ".cm-panel.cm-search [name=close]": { color: "var(--muted)", border: "none", background: "none" },
  ".cm-tooltip": {
    backgroundColor: "var(--surface-overlay)",
    color: "var(--text)",
    border: "1px solid var(--line-strong)",
    borderRadius: "var(--radius)",
  },
});

/** Swapped when the open file changes, so one view serves every file rather than remounting. */
const languageSlot = new Compartment();

/** What a reader may do here: move, select, copy, fold and find. Never write. */
const readOnlyKeymap = keymap.of([
  // `defaultKeymap` carries the editing commands too; they are inert under `readOnly`, and
  // dropping them would also drop cursor motion and selection, which a reader does need.
  ...defaultKeymap,
  ...historyKeymap,
  ...searchKeymap,
  ...foldKeymap,
]);

/** Build the extensions a read-only view runs with. The grammar arrives later, into `languageSlot`. */
function baseExtensions(): Extension[] {
  return [
    lineNumbers(),
    foldGutter(),
    highlightActiveLine(),
    highlightActiveLineGutter(),
    bracketMatching(),
    highlightSelectionMatches(),
    history(),
    search({ top: false }),
    syntaxHighlighting(highlight),
    indentUnit.of("  "),
    // No `EditorView.lineWrapping`: long lines scroll sideways, as an editor does. Wrapping
    // would let one file line occupy several rows, and the gutter numbers a reader counts
    // against would stop matching the file.
    readOnlyKeymap,
    theme,
    EditorState.readOnly.of(true),
    EditorView.editable.of(false),
    languageSlot.of([]),
  ];
}

/** A mounted read-only editor, and the two things its host needs to do to it. */
export type Editor = {
  /** Show another file: its text and the grammar its path names, with no remount. */
  show(path: string, text: string): void;
  /** Tear the view down and free its DOM. */
  destroy(): void;
};

/**
 * Mount a read-only editor into `parent` showing `text` as `path`'s language.
 *
 * The view is created once and re-used: `show` replaces the document and swaps the language
 * compartment, which keeps scroll handling and the find panel alive across files.
 */
export function mountEditor(parent: HTMLElement, path: string, text: string): Editor {
  const view = new EditorView({
    parent,
    state: EditorState.create({ doc: text, extensions: baseExtensions() }),
  });
  let alive = true;
  // Which file the grammar is being fetched for. A reader who clicks through three files
  // faster than the parsers load must end on the third one's grammar, not whichever
  // import happened to settle last.
  let wanted = path;

  function applyLanguage(forPath: string) {
    void languageFor(forPath)
      .then((language) => {
        if (!alive || wanted !== forPath) return;
        view.dispatch({ effects: languageSlot.reconfigure(language ?? []) });
      })
      // A grammar that will not load leaves the file as plain text, which still reads.
      .catch(() => {});
  }
  applyLanguage(path);

  return {
    show(nextPath: string, nextText: string) {
      wanted = nextPath;
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: nextText },
        // A new file starts at its top, not wherever the last one was scrolled to.
        selection: { anchor: 0 },
        scrollIntoView: true,
      });
      applyLanguage(nextPath);
    },
    destroy() {
      alive = false;
      view.destroy();
    },
  };
}

/** Exported for the tests: which grammar a path resolves to, by name; null when it is plain text. */
export async function languageNameFor(path: string): Promise<string | null> {
  const support = await languageFor(path);
  return support instanceof LanguageSupport ? support.language.name : null;
}
