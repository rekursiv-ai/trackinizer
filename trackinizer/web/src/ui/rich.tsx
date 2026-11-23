// THE RICH NODE. The one surface on the canvas that is written into, not just read.
//
// react-markdown renders what the store already holds; this is for text a person is
// composing — a note on a block, a draft the agent and a reader pass back and forth.
// Tiptap arrives with the browser's own prose defaults, so nodes.css binds it to the
// design's: the prose face, the prose measure (630px, which is where a line stops
// costing the reader their place coming back from the right edge), mono on a token.
//
// Pinned to Tiptap 2: the 3.x line resolves starter-kit onto a second copy of
// @tiptap/core, and two ProseMirror schemas in one bundle is an editor that throws on
// its own `doc` node. One copy is the thing to check if this is ever moved forward.
import { useEffect, useState } from "react";
import "./nodes.css";

/** A writing surface. */
export type Rich = {
  /** The text it opens with, as markdown. */
  readonly doc: string;
  /** What it says while empty — it locates rather than reads, so it is machine ink. */
  readonly placeholder?: string;
  /** Called with the text on every change, debounced by the caller. Read-only without
   *  it, which is how a draft is shown to someone who may not edit it. */
  readonly onChange?: (text: string) => void;
};

/** Tiptap and its starter kit, once both have arrived. The editor cannot be built from
 *  one of them: a schema with no extensions has no `doc` node and ProseMirror throws. */
type Loaded = {
  readonly react: typeof import("@tiptap/react");
  readonly kit: (typeof import("@tiptap/starter-kit"))["default"];
};

/**
 * One writing surface.
 *
 * Tiptap and ProseMirror are a sizeable share of the app's first-load budget, so they
 * load on first render of this node and never before. Until they arrive the text
 * stands as prose, which means a reader sees the content immediately and gains the
 * ability to change it a moment later — rather than seeing a spinner where the
 * sentence is.
 */
export function RichView({ doc, placeholder, onChange }: Rich) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  useEffect(() => {
    let live = true;
    // Both at once, and the editor is built from neither until both are in hand.
    void Promise.all([import("@tiptap/react"), import("@tiptap/starter-kit")]).then(([react, kit]) => {
      if (live) setLoaded({ react, kit: kit.default });
    });
    return () => { live = false; };
  }, []);

  if (!loaded) return <div className="ui-rich"><p className="ui-prose">{doc}</p></div>;
  return <Editor loaded={loaded} doc={doc} placeholder={placeholder} onChange={onChange} />;
}

/** The editor itself, mounted only once both modules are in hand, so `useEditor` is
 *  never called with a schema that has no top node. */
function Editor({ loaded, doc, placeholder, onChange }: Rich & { readonly loaded: Loaded }) {
  const editor = loaded.react.useEditor({
    extensions: [loaded.kit],
    content: doc,
    editable: onChange !== undefined,
    // The design owns the surface, so Tiptap's own class list stays off it.
    editorProps: { attributes: placeholder ? { "data-placeholder": placeholder } : {} },
    onUpdate: onChange ? ({ editor: live }) => { onChange(live.getText()); } : undefined,
    immediatelyRender: false,
  });

  return <div className="ui-rich"><loaded.react.EditorContent editor={editor} /></div>;
}
