// THE CODE PANE. A research session's source, imported by the reader, read-only on the page.
//
// The asymmetry is the point and it is deliberate: the READER supplies the tree (drop a
// folder, or pick one) and may read every byte of it; the AGENT writes the files inside it.
// So there is no editing affordance here and that is not an omission — a caret in this pane
// would promise a write the reader does not own. What the reader owns is the import.
//
// The tree is a snapshot of the chat session's own record, so a session's research and the
// code it is about are one thing. A station with no session yet files against its workstream
// instead (`code/scope.ts`), and the pane names which, since the two do not live equally long.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type DragEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { readCodeFile, readCodeSnapshot } from "../api/code";
import { chunk } from "../router/lazy";
import { languageOf } from "../detail/transcript/steps";
import { useHighlighter } from "../markdown/CodeBlock";
import type { ElementContent } from "hast";
import { type FileNode, fileTree } from "../visuals/filesDoc";
import { EmptyState } from "../ui/view";
import { ReadFailure } from "../ui/failure";
import { Icon } from "../ui/icons";
import { skippedCount, walkDataTransfer, walkFileList, type Walked } from "./importFolder";
import type { Editor } from "./editor";
import { type CodeScope, scopeName } from "./scope";
import { uploadWalk } from "./upload";
import "../markdown/highlight.css";
import "./code.css";

/** How far an import has got, for the bar; null when none is running. */
type Progress = { readonly phase: "reading" | "sending"; readonly sent: number; readonly total: number };

/**
 * A station's code: the tree on the left, the file open on the right.
 *
 * `scope` says which record the snapshot is filed against and whether that is a session or
 * the workstream (`code/scope.ts`), and `owner` is the account it is filed under. With no
 * scope there is nothing to file code against, which the pane says rather than offering an
 * import that cannot land.
 */
export function CodePane({ scope, owner }: { readonly scope: CodeScope | null; readonly owner: string | null }) {
  const record = scope?.record ?? null;
  const where = scope ? scopeName(scope.kind) : "";
  const queries = useQueryClient();
  const [path, setPath] = useState<string | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [report, setReport] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  // A drag crossing child elements fires dragleave on each; counting keeps the overlay up
  // until the pointer truly leaves the pane.
  const depth = useRef(0);

  const snapshot = useQuery({
    queryKey: ["code", "snapshot", record],
    enabled: record !== null,
    retry: false,
    // A record with no snapshot yet answers null, not an error: that is the empty state.
    queryFn: ({ signal }) => readCodeSnapshot(record!, { signal }),
  });

  const open = useQuery({
    queryKey: ["code", "file", record, path],
    enabled: record !== null && path !== null,
    queryFn: ({ signal }) => readCodeFile(record!, path!, { signal }),
  });

  const importing = useMutation({
    mutationFn: async (walked: Walked) => {
      if (!record || !owner) throw new Error("Nothing to file code against.");
      setProgress({ phase: "sending", sent: 0, total: walked.files.length });
      const done = await uploadWalk(record, walked, {
        owner,
        // Each import builds on the session itself, so the store can diff one against the next.
        parentRecordId: null,
        onProgress: (sent, total) => setProgress({ phase: "sending", sent, total }),
      });
      return { done, walked };
    },
    onSuccess: ({ done, walked }) => {
      const left = skippedCount(walked.skipped);
      setReport(
        `Imported ${done.files} ${done.files === 1 ? "file" : "files"}` +
          `${done.sent < done.files ? `, ${done.files - done.sent} already stored` : ""}` +
          `${left > 0 ? `; skipped ${left} (${reasons(walked)})` : ""}` +
          `${done.dropped > 0 ? `; ${done.dropped} past the snapshot limit were left out` : ""}.`,
      );
      void queries.invalidateQueries({ queryKey: ["code", "snapshot", record] });
    },
    onSettled: () => setProgress(null),
  });

  async function take(read: () => Promise<Walked>) {
    setReport(null);
    setProgress({ phase: "reading", sent: 0, total: 0 });
    try {
      const walked = await read();
      if (walked.files.length === 0) {
        setProgress(null);
        setReport(
          skippedCount(walked.skipped) > 0
            ? `Nothing to import: every file was skipped (${reasons(walked)}).`
            : "Nothing to import: that folder holds no text files.",
        );
        return;
      }
      importing.mutate(walked);
    } catch (error) {
      setProgress(null);
      setReport(`Could not read that folder: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    depth.current = 0;
    setOver(false);
    if (!record || !owner) return;
    const transfer = event.dataTransfer;
    void take(() => walkDataTransfer(transfer));
  }

  const files = snapshot.data?.files ?? [];
  const nodes = useMemo(() => fileTree(files.map((file) => file.path)), [files]);
  const busy = progress !== null;

  return (
    <div
      className={`cp ui-panel${over ? " is-over" : ""}`}
      onDragEnter={(event) => {
        if (!record || !owner) return;
        event.preventDefault();
        depth.current += 1;
        setOver(true);
      }}
      onDragOver={(event) => {
        if (!record || !owner) return;
        // Without this the browser navigates to the dropped file instead of giving it here.
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={() => {
        depth.current = Math.max(0, depth.current - 1);
        if (depth.current === 0) setOver(false);
      }}
      onDrop={onDrop}
    >
      <div className="cp-bar">
        <span className="cp-count">
          {snapshot.isPending && record ? "Loading…" : `${files.length} ${files.length === 1 ? "file" : "files"}`}
        </span>
        {snapshot.data?.partial ? <span className="cp-partial">{snapshot.data.partial}</span> : null}
        <div className="spacer" />
        <span className="cp-ro" title={`Filed against ${where || "nothing yet"}. The agent writes these files; you read them`}>
          <Icon name="lock" size={13} /> Read-only
        </span>
        <button
          type="button"
          className="btn ghost"
          disabled={!record || !owner || busy}
          title={record ? `Import a folder into ${where}` : "Nothing to file code against"}
          onClick={() => picker.current?.click()}
        >
          <Icon name="folder" size={14} />
          <span className="hide-sm">Import folder</span>
        </button>
        <input
          ref={picker}
          type="file"
          className="cp-picker"
          // `webkitdirectory` is how a file input picks a folder; every browser the app
          // supports answers it, and React needs the lowercase DOM spelling here.
          {...{ webkitdirectory: "", directory: "" }}
          multiple
          onChange={(event) => {
            const list = event.target.files;
            event.target.value = "";
            if (list && list.length > 0) void take(() => walkFileList(list));
          }}
        />
      </div>

      {busy ? (
        <div className="cp-progress" role="status">
          {progress.phase === "reading"
            ? "Reading the folder…"
            : `Storing ${progress.sent} of ${progress.total} ${progress.total === 1 ? "file" : "files"}…`}
          <span className="cp-track">
            <span
              className="cp-fill"
              style={{ width: progress.total > 0 ? `${Math.round((progress.sent / progress.total) * 100)}%` : "0%" }}
            />
          </span>
        </div>
      ) : null}
      {report ? (
        <p className="cp-report" role="status">
          {report}
        </p>
      ) : null}
      {importing.error ? <p className="cp-report is-bad">Import failed: {importing.error.message}</p> : null}

      {!record ? (
        <EmptyState icon={<Icon name="folder" size={24} />} title="Nowhere to file code">
          <p>Code is filed against a chat session, or the workstream around it. This station names neither yet.</p>
        </EmptyState>
      ) : snapshot.error ? (
        <ReadFailure error={snapshot.error} retry={() => void snapshot.refetch()} />
      ) : files.length === 0 && !snapshot.isPending ? (
        <EmptyState icon={<Icon name="folder" size={24} />} title={`No code for ${where}`}>
          <p>Drop a folder anywhere on this pane, or use Import folder. You read these files; the agent writes them.</p>
        </EmptyState>
      ) : (
        <div className="cp-split">
          <ul className="cp-tree" aria-label="File tree">
            <TreeNodes nodes={nodes} open={path} onPick={setPath} />
          </ul>
          <div className="cp-file">
            {path === null ? (
              <p className="cp-hint">Pick a file to read it.</p>
            ) : open.isPending ? (
              <p className="cp-hint">Loading {path}…</p>
            ) : open.error ? (
              <ReadFailure error={open.error} retry={() => void open.refetch()} />
            ) : open.data ? (
              <FileText path={open.data.path} text={open.data.text} />
            ) : null}
          </div>
        </div>
      )}

      {over ? (
        <div className="cp-drop" aria-hidden="true">
          <span className="cp-drop-icon">
            <Icon name="folder" size={22} />
          </span>
          <p>Drop a folder to import it</p>
        </div>
      ) : null}
    </div>
  );
}

/** What was skipped, in words, so a thin import is explained rather than mysterious. */
function reasons({ skipped }: Walked): string {
  const names: Readonly<Record<string, string>> = {
    ignored: "build and VCS folders",
    binary: "binary files",
    "too-large": "files over 1 MB",
    "empty-path": "unnamed",
  };
  return (
    Object.entries(skipped)
      .filter(([, count]) => count > 0)
      .map(([reason, count]) => `${count} ${names[reason] ?? reason}`)
      .join(", ") || "none"
  );
}

/** A folder's own row, which opens and closes it. Its own state, so the tree keeps shape. */
function Folder({
  node,
  open,
  onPick,
}: {
  readonly node: Extract<FileNode, { kind: "dir" }>;
  readonly open: string | null;
  readonly onPick: (path: string) => void;
}) {
  // A folder holding the open file stays open, so picking a file never hides it.
  const [shut, setShut] = useState(node.children.length > COLLAPSE_OVER);
  const holdsOpen = open !== null && open.startsWith(node.path);
  const showing = !shut || holdsOpen;
  return (
    <li>
      <button
        type="button"
        className="cp-row-btn cp-dir"
        aria-expanded={showing}
        onClick={() => setShut(showing)}
      >
        <span className="cp-chev">
          <Icon name={showing ? "chevD" : "chevR"} size={12} />
        </span>
        <span className="cp-name">{node.name}</span>
      </button>
      {showing ? (
        <ul>
          <TreeNodes nodes={node.children} open={open} onPick={onPick} />
        </ul>
      ) : null}
    </li>
  );
}

function TreeNodes({
  nodes,
  open,
  onPick,
}: {
  readonly nodes: readonly FileNode[];
  readonly open: string | null;
  readonly onPick: (path: string) => void;
}): ReactNode {
  return nodes.map((node) =>
    node.kind === "file" ? (
      <li key={node.path}>
        <button
          type="button"
          className={`cp-row-btn cp-leaf${node.path === open ? " is-open" : ""}`}
          aria-current={node.path === open ? "true" : undefined}
          onClick={() => onPick(node.path)}
        >
          <span className={`cp-dot is-${extensionOf(node.name)}`} />
          <span className="cp-name">{node.name}</span>
        </button>
      </li>
    ) : (
      <Folder key={node.path} node={node} open={open} onPick={onPick} />
    ),
  );
}

/** A folder with more children than this starts closed, so a big tree opens readable. */
const COLLAPSE_OVER = 20;

/** A file name's extension, lowercased, which the tree colours its glyph by; `""` when it has none. */
function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** The open file's path as a breadcrumb: a long file must never lose which file it is. */
function Crumbs({ path, lines }: { readonly path: string; readonly lines: number }) {
  const parts = path.split("/");
  return (
    <div className="cp-crumbs">
      {parts.map((part, at) => (
        <span key={at} className={at === parts.length - 1 ? "cp-crumb-last" : undefined}>
          {at > 0 ? <span className="cp-crumb-sep"> › </span> : null}
          {part}
        </span>
      ))}
      <span className="cp-lines-note">
        {lines} {lines === 1 ? "line" : "lines"}
      </span>
    </div>
  );
}

/**
 * One file, read-only: CodeMirror once its chunk has arrived, the plain highlighted listing
 * until then.
 *
 * The listing is not dead weight. It paints immediately on the first open, while CodeMirror
 * is still being fetched, and it is what a browser that fails to load the chunk keeps — so
 * the pane degrades to a readable file rather than to nothing.
 */
function FileText({ path, text }: { readonly path: string; readonly text: string }) {
  const lines = useMemo(() => text.replace(/\n$/, "").split("\n").length, [text]);
  const editor = useEditorModule();
  return (
    <>
      <Crumbs path={path} lines={lines} />
      {editor ? <CodeMirrorFile mount={editor.mountEditor} path={path} text={text} /> : <PlainFile path={path} text={text} />}
    </>
  );
}

/** The editor module, once its chunk has loaded; undefined until then. Loaded once per page. */
function useEditorModule(): EditorModule | undefined {
  const [module, setModule] = useState<EditorModule | undefined>(loadedEditor);
  useEffect(() => {
    if (loadedEditor) return;
    let mounted = true;
    void (editorLoad ??= chunk(import("./editor")))
      .then((loaded) => {
        loadedEditor = loaded;
        if (mounted) setModule(loaded);
      })
      // A chunk that will not load leaves the plain listing in place, which still reads.
      .catch(() => {});
    return () => {
      mounted = false;
    };
  }, []);
  return module;
}

type EditorModule = typeof import("./editor");
let loadedEditor: EditorModule | undefined;
let editorLoad: Promise<EditorModule> | undefined;

/** The CodeMirror view: mounted once, then handed each file in turn. */
function CodeMirrorFile({
  mount,
  path,
  text,
}: {
  readonly mount: EditorModule["mountEditor"];
  readonly path: string;
  readonly text: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<Editor | null>(null);
  useEffect(() => {
    const parent = host.current;
    if (!parent) return;
    const view = mount(parent, path, text);
    editor.current = view;
    return () => {
      editor.current = null;
      view.destroy();
    };
    // Mounted once: a later file is handed to the live view below, which keeps its
    // scroll handling and find panel rather than tearing the editor down per file.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mount]);
  useEffect(() => {
    editor.current?.show(path, text);
  }, [path, text]);
  return <div className="cp-cm" ref={host} />;
}

/** The fallback listing: highlighted and numbered, no editor. */
function PlainFile({ path, text }: { readonly path: string; readonly text: string }) {
  const highlighter = useHighlighter(true);
  const language = languageOf(path);
  const coloured = useMemo(() => highlighter?.highlightLines(text, language), [highlighter, text, language]);
  const rows = useMemo(() => text.replace(/\n$/, "").split("\n"), [text]);
  return (
    <div className="cp-scroll">
      <pre className="cp-pre">
        {rows.map((row, at) => (
          <span className="cp-row" key={at}>
            <span className="cp-n">{at + 1}</span>
            <code className="cp-tok">{coloured?.[at] ? <Tokens nodes={coloured[at]!} /> : row}</code>
          </span>
        ))}
      </pre>
    </div>
  );
}

/** A hast tree, as highlight.js coloured it, read into React (the shape `ui/code.tsx` uses). */
function Tokens({ nodes }: { readonly nodes: readonly ElementContent[] }): ReactNode {
  return nodes.map((node, k) =>
    node.type === "text" ? (
      node.value
    ) : node.type === "element" ? (
      <span key={k} className={Array.isArray(node.properties.className) ? node.properties.className.join(" ") : undefined}>
        <Tokens nodes={node.children} />
      </span>
    ) : null,
  );
}
