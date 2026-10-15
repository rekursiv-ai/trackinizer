// `trax.files`: a record's files and how they differ from a base, each change with its diff, from the
// files document a trusted account filed for the record (`filesDoc.ts`, `api/files.ts`).
import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { type FilesFound, loadFiles } from "../api/files";
import { readTrust } from "../api/summaries";
import { CopyDetails } from "../debug/CopyDetails";
import { languageOf } from "../detail/transcript/steps";
import { ReadFailure } from "../ui/failure";
import { type ChangedFile, type DiffNote, type FileChange, type FileNode, type FilesDoc, fileTree, openedPaths, pathMatcher } from "./filesDoc";
import { useVisualMark } from "./marks";
import { type DiffMode, DiffModeToggle, FileDiff, useDiffMode } from "./SplitDiff";
import { patchFiles } from "./splitDiff";
import type { RendererProps } from "./registry";
import { useWorkspaceActions } from "./workspaceActions";
import "../detail/transcript/transcript.css";
import "./Files.css";

/** What a files pane narrows to: `path` a prefix or glob, `changed` the changed files alone, `file` the diffs it opens. */
export type FilesParams = { readonly path: string; readonly changed: boolean; readonly file: string };

/** The catalog's defaults: every file, no diff opened. */
export const FILES_DEFAULTS: FilesParams = { path: "", changed: false, file: "" };

/** A read of a record's files document: `data` undefined until read, null when none is filed. */
export type FilesRead = {
  readonly data: FilesFound | null | undefined;
  readonly error: Error | null;
  readonly refetch: () => void;
  readonly isSuccess: boolean;
};

/** What a caller says in place of the panel's own words: when no document is filed, and under the file tree. */
export type FilesNotes = { readonly empty?: string; readonly tree?: string };

/**
 * The files visual, `trax.files`, on any record: the files document filed for
 * it, narrowed by the instance's `path`, `changed` and `file`. In a canvas each
 * control shows the visual again with all three, so whoever reads the canvas
 * sees what the reader sees; outside one, the controls narrow this pane alone.
 */
export function Files({ instance, workspace }: RendererProps): ReactNode {
  const record = instance.record_id ?? null;
  const read = useFiles(record);
  const actions = useWorkspaceActions();
  const given = filesParams(instance.params ?? {});
  const [local, setLocal] = useState(given);
  useVisualMark(instance, workspace, "data", read.isSuccess);
  if (record === null) return <div className="visual-unsupported">Open a record to see its files.</div>;
  const show = actions
    ? ({ path, changed, file }: FilesParams) =>
        actions.operate({ kind: "show", visual_type: instance.type, record_id: record, params: { path, changed, file } })
    : setLocal;
  return (
    <section className="fv" aria-label="Files">
      <FilesPanel read={read} params={actions ? given : local} onParams={show} busy={actions?.busy ?? false} />
    </section>
  );
}

/**
 * A files document as a panel: the changed files, each opening to its diff, then
 * the file tree, narrowed by `params`; `onParams` takes each change to them.
 * Nothing here opens or downloads a file: diffs are the document's own text.
 */
export function FilesPanel({
  read,
  params,
  onParams,
  notes = NO_NOTES,
  busy = false,
}: {
  readonly read: FilesRead;
  readonly params: FilesParams;
  readonly onParams: (next: FilesParams) => void;
  readonly notes?: FilesNotes;
  readonly busy?: boolean;
}): ReactNode {
  const { data, error, refetch } = read;
  if (data === undefined) return error ? <ReadFailure error={error} retry={refetch} /> : <p className="fv-note">Loading the files…</p>;
  const stale = error ? `Could not refresh the files: ${error.message}` : null;
  return (
    <div className="fv-panel">
      {stale ? (
        <p className="fv-error" role="alert">
          {stale}
          <button type="button" className="btn ghost" onClick={refetch}>
            Retry
          </button>
          <CopyDetails message={stale} error={error} />
        </p>
      ) : null}
      {data === null ? (
        <p className="fv-note">{notes.empty ?? "No files document is filed for this record yet."}</p>
      ) : (
        <Narrowed found={data} params={params} onParams={onParams} notes={notes} busy={busy} />
      )}
    </div>
  );
}

/**
 * Read the files document filed for `record`, through the accounts the profile
 * trusts, again every `refreshMs`; `labels` narrows it to the documents that
 * also carry them.
 */
export function useFiles(
  record: string | null,
  { labels = NO_LABELS, refreshMs = REFRESH_MS }: { readonly labels?: readonly string[]; readonly refreshMs?: number | false } = {},
): FilesRead {
  const trust = useQuery({
    queryKey: ["me", "trust"],
    queryFn: ({ signal }) => readTrust({ signal }),
    staleTime: 300_000,
    enabled: record !== null,
  });
  const accounts = trust.data?.accounts;
  const read = useQuery({
    queryKey: ["visual", "files", record, labels, accounts],
    queryFn: ({ signal }) => loadFiles(record!, accounts!, { labels, signal }),
    enabled: record !== null && accounts !== undefined,
    refetchInterval: refreshMs,
  });
  return {
    data: read.data,
    error: trust.error ?? read.error,
    refetch: () => void (trust.error ? trust.refetch() : read.refetch()),
    isSuccess: read.isSuccess,
  };
}

/** A pane's narrowing from its instance's parameters: each one missing or of another type is its default. */
export function filesParams(params: { readonly [name: string]: string | number | boolean }): FilesParams {
  const { path, changed, file } = params;
  return {
    path: typeof path === "string" ? path : FILES_DEFAULTS.path,
    changed: typeof changed === "boolean" ? changed : FILES_DEFAULTS.changed,
    file: typeof file === "string" ? file : FILES_DEFAULTS.file,
  };
}

/** The document again every minute: one bounded list read per open pane. */
const REFRESH_MS = 60_000;

const NO_LABELS: readonly string[] = [];
const NO_NOTES: FilesNotes = {};
const NOTHING_TOGGLED: ReadonlyMap<string, boolean> = new Map();

/** What a diff's note says beside it. */
const NOTES: Readonly<Record<Exclude<DiffNote, "">, string>> = {
  clipped: "The diff is cut here.",
  withheld: "This diff is not shown.",
  omitted: "Its changes are not shown.",
};

/** Which changes and directories show open: each the reader toggled, else its default. */
type Openness = {
  readonly isOpen: (key: string, fallback: boolean) => boolean;
  readonly flip: (key: string, open: boolean) => void;
};

/**
 * The document narrowed: the path filter narrows live as it is typed and is
 * kept on Enter; a change to `params` from outside (an agent showing the pane
 * again) replaces the draft and the reader's own toggles.
 */
function Narrowed({
  found: { doc, count },
  params,
  onParams,
  notes,
  busy,
}: {
  readonly found: FilesFound;
  readonly params: FilesParams;
  readonly onParams: (next: FilesParams) => void;
  readonly notes: FilesNotes;
  readonly busy: boolean;
}) {
  const [draft, setDraft] = useState(params.path);
  const [toggled, setToggled] = useState(NOTHING_TOGGLED);
  const [given, setGiven] = useState(params);
  if (given.path !== params.path || given.file !== params.file) {
    setGiven(params);
    if (given.path !== params.path) setDraft(params.path);
    if (given.file !== params.file) setToggled(NOTHING_TOGGLED);
  }
  const path = draft.trim();
  const changed = useMemo(() => {
    const matches = pathMatcher(path);
    return doc.changed.filter(({ path: each }) => matches(each));
  }, [doc.changed, path]);
  const opened = new Set(openedPaths(changed, params.file));
  const commit = (next: Partial<FilesParams>) => onParams({ ...params, path, ...next });
  const panel = useRef<HTMLDivElement>(null);
  const [scrollTo, setScrollTo] = useState<{ readonly path: string } | null>(null);
  useEffect(() => {
    const items = [...(panel.current?.querySelectorAll<HTMLElement>("[data-path]") ?? [])];
    if (scrollTo) items.find((item) => item.dataset.path === scrollTo.path)?.scrollIntoView({ block: "nearest" });
  }, [scrollTo]);
  const openness: Openness = {
    isOpen: (key, fallback) => toggled.get(key) ?? fallback,
    flip: (key, open) => setToggled(new Map(toggled).set(key, !open)),
  };
  const reveal = (shown: string) => {
    setToggled(new Map(toggled).set(`change ${shown}`, true));
    setScrollTo({ path: shown });
  };
  return (
    <div className="fv-body" ref={panel}>
      <Controls draft={draft} setDraft={setDraft} params={params} commit={commit} busy={busy} />
      {count > 1 ? <p className="fv-hint">{`Newest of ${count} files documents for this record.`}</p> : null}
      {params.file && opened.size === 0 ? <p className="fv-hint">{`No listed changed file is named ${params.file}.`}</p> : null}
      <ChangedList doc={doc} changed={changed} narrowing={path !== ""} opened={opened} openness={openness} />
      {params.changed ? null : <Tree doc={doc} path={path} openness={openness} reveal={reveal} note={notes.tree} />}
      {doc.hidden > 0 ? (
        <p className="fv-hint">{`${doc.hidden} ${doc.hidden === 1 ? "path is" : "paths are"} withheld by the source that filed this list.`}</p>
      ) : null}
    </div>
  );
}

/**
 * The path filter, which narrows from its draft as it is typed and keeps it on
 * Enter (× clears it), Changed only, and the file opened with Close.
 */
function Controls({
  draft,
  setDraft,
  params,
  commit,
  busy,
}: {
  readonly draft: string;
  readonly setDraft: (draft: string) => void;
  readonly params: FilesParams;
  readonly commit: (next: Partial<FilesParams>) => void;
  readonly busy: boolean;
}) {
  return (
    <div className="fv-controls">
      <input
        type="search"
        aria-label="Filter by path"
        placeholder="Path prefix or glob: src/, **/*.py"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          // A write waits for the one in flight: built from the params as they stand, it would undo it.
          if (event.key === "Enter" && !busy && draft.trim() !== params.path) commit({});
        }}
      />
      {draft || params.path ? (
        <button
          type="button"
          className="btn ghost"
          aria-label="Clear the path filter"
          title="Clear the path filter"
          disabled={busy}
          onClick={() => {
            setDraft("");
            if (params.path) commit({ path: "" });
          }}
        >
          ×
        </button>
      ) : null}
      <label className="fv-check">
        <input type="checkbox" checked={params.changed} disabled={busy} onChange={(event) => commit({ changed: event.target.checked })} />
        Changed only
      </label>
      {params.file ? (
        <span className="fv-opened">
          Opened <code>{params.file}</code>
          <button type="button" className="btn ghost" disabled={busy} onClick={() => commit({ file: "" })}>
            Close
          </button>
        </span>
      ) : null}
    </div>
  );
}

/** The changed files `path` leaves, each a fold that opens to its diff; those `file` names start open. */
function ChangedList({
  doc,
  changed,
  narrowing,
  opened,
  openness,
}: {
  readonly doc: FilesDoc;
  readonly changed: readonly ChangedFile[];
  readonly narrowing: boolean;
  readonly opened: ReadonlySet<string>;
  readonly openness: Openness;
}) {
  const counted = countOf(changed.length, { of: doc.changed.length, narrowing, more: doc.more_changes });
  const [mode, setMode] = useDiffMode();
  return (
    <div className="fv-part">
      <div className="fv-changed-head">
        <h3>{doc.base ? `Changed from ${doc.base} (${counted})` : `Changed (${counted})`}</h3>
        {changed.length > 0 ? <DiffModeToggle mode={mode} onChange={setMode} /> : null}
      </div>
      {doc.changed.length === 0 ? (
        <p className="fv-note">{`No file differs from ${doc.base || "the base"}.`}</p>
      ) : changed.length === 0 ? (
        <p className="fv-note">No changed file matches this path.</p>
      ) : (
        <ul className="fv-changed" aria-label="Changed files">
          {changed.map((file) => {
            const key = `change ${file.path}`;
            const open = openness.isOpen(key, opened.has(file.path));
            return (
              <li key={file.path} data-path={file.path}>
                <details open={open}>
                  {/* The fold follows state alone, so a show from outside and a tree click can open it. */}
                  <summary
                    onClick={(event) => {
                      event.preventDefault();
                      openness.flip(key, open);
                    }}
                  >
                    <span className="fv-path">{file.path}</span>
                    <Kind change={file.change} />
                  </summary>
                  {open ? <ChangeBody file={file} mode={mode} /> : null}
                </details>
              </li>
            );
          })}
        </ul>
      )}
      {doc.more_changes ? <p className="fv-hint">More files changed than are listed here.</p> : null}
    </div>
  );
}

/** One change opened: its diff's header lines, then its hunks side by side or unified, and its note. */
function ChangeBody({ file, mode }: { readonly file: ChangedFile; readonly mode: DiffMode }) {
  const parsed = useMemo(() => patchFiles(file.diff)[0], [file.diff]);
  return (
    <>
      {parsed ? (
        <div className="fv-diff" role="group" aria-label="Code diff">
          {parsed.head.length > 0 ? <pre className="fv-diff-head">{parsed.head.join("\n")}</pre> : null}
          <FileDiff file={parsed} mode={mode} language={languageOf(file.path)} />
        </div>
      ) : null}
      {file.note ? <p className="fv-hint">{NOTES[file.note]}</p> : null}
    </>
  );
}

/** The files `path` leaves, as a tree: a directory starts open while `path` narrows or when it holds a change. */
function Tree({
  doc,
  path,
  openness,
  reveal,
  note,
}: {
  readonly doc: FilesDoc;
  readonly path: string;
  readonly openness: Openness;
  readonly reveal: (path: string) => void;
  readonly note: string | undefined;
}) {
  const shown = useMemo(() => doc.files.filter(pathMatcher(path)), [doc.files, path]);
  const nodes = useMemo(() => fileTree(shown), [shown]);
  const changes = useMemo(() => new Map(doc.changed.map(({ path: each, change }) => [each, change])), [doc.changed]);
  const counted = countOf(shown.length, { of: doc.files.length, narrowing: path !== "", more: doc.more_files });
  return (
    <div className="fv-part">
      <h3>{`Files (${counted})`}</h3>
      {shown.length === 0 ? (
        <p className="fv-note">{doc.files.length === 0 ? "No file is listed." : "No file matches this path."}</p>
      ) : (
        <ul className="fv-tree" aria-label="File tree">
          <TreeNodes nodes={nodes} at={{ narrowing: path !== "", changes, openness, reveal }} />
        </ul>
      )}
      {doc.more_files ? <p className="fv-hint">More files exist than are listed here.</p> : null}
      {note ? <p className="fv-hint">{note}</p> : null}
    </div>
  );
}

/** What every level of the tree draws with. */
type TreeAt = {
  readonly narrowing: boolean;
  readonly changes: ReadonlyMap<string, FileChange>;
  readonly openness: Openness;
  readonly reveal: (path: string) => void;
};

function TreeNodes({ nodes, at }: { readonly nodes: readonly FileNode[]; readonly at: TreeAt }): ReactNode {
  return nodes.map((node) => {
    if (node.kind === "file") {
      const change = at.changes.get(node.path);
      return (
        <li key={node.path}>
          {change ? (
            <button type="button" className="fv-leaf" title="Show its diff" onClick={() => at.reveal(node.path)}>
              <span className="fv-path">{node.name}</span>
              <Kind change={change} />
            </button>
          ) : (
            <span className="fv-path">{node.name}</span>
          )}
        </li>
      );
    }
    const key = `dir ${node.path}`;
    const open = at.openness.isOpen(key, at.narrowing || holdsChange(node, at.changes));
    return (
      <li key={node.path}>
        <details open={open}>
          <summary
            onClick={(event) => {
              event.preventDefault();
              at.openness.flip(key, open);
            }}
          >
            <span className="fv-path">{node.name}</span>
          </summary>
          {open ? (
            <ul>
              <TreeNodes nodes={node.children} at={at} />
            </ul>
          ) : null}
        </details>
      </li>
    );
  });
}

/** Whether a file under `node` differs from the base. */
function holdsChange(node: FileNode, changes: ReadonlyMap<string, FileChange>): boolean {
  return node.kind === "file" ? changes.has(node.path) : node.children.some((child) => holdsChange(child, changes));
}

/** How many a list shows: `k of n` while a path narrows it, `+` when the source listed fewer than there are. */
function countOf(shown: number, { of, narrowing, more }: { readonly of: number; readonly narrowing: boolean; readonly more: boolean }): string {
  return `${narrowing ? `${shown} of ${of}` : of}${more ? "+" : ""}`;
}

/** How a file differs from the base, in words as well as colour. */
function Kind({ change }: { readonly change: FileChange }) {
  return <span className={`fv-kind is-${change}`}>{change}</span>;
}
