// Reading a folder the reader dropped or picked, into the blobs a snapshot is made of.
//
// A dropped tree is not a tree of source. It is source plus `.git`, plus `node_modules`,
// plus build output, plus images — and a naive walk of this repo sends 400 MB to store
// 4 MB of code. So the walk SKIPS by rule and COUNTS what it skipped: a reader who
// dropped a folder and got 412 of 9,742 files must be told, or the pane looks broken.
//
// Nothing here talks to the server. It answers what to upload; `upload.ts` sends it.
import { MAX_BLOB_BYTES } from "./limits";

/** Why a file was left out of the import. */
export type SkipReason = "ignored" | "binary" | "too-large" | "empty-path";

/** One file read and ready to store: its path in the tree, its text, and its content hash. */
export type ReadFile = { readonly path: string; readonly text: string; readonly sha256: string };

/** What a walk found: the files to store, and a count per reason for everything it left. */
export type Walked = {
  readonly files: readonly ReadFile[];
  readonly skipped: Readonly<Record<SkipReason, number>>;
  /** The folder's own name, as the drop reported it; `""` when the drop carried loose files. */
  readonly root: string;
};

/**
 * Directories never imported, whatever a `.gitignore` says. These are the ones that make
 * a drop unusable rather than merely noisy: a dependency tree, a VCS database, build output.
 */
const SKIP_DIRS: readonly string[] = [
  ".git",
  ".hg",
  ".svn",
  "node_modules",
  ".venv",
  "venv",
  "__pycache__",
  ".mypy_cache",
  ".pytest_cache",
  ".ruff_cache",
  ".next",
  ".turbo",
  ".cache",
  "dist",
  "build",
  "target",
  "coverage",
  ".DS_Store",
];

/** Extensions that are never text, so never worth a blob. */
const BINARY_EXTENSIONS: readonly string[] = [
  "png", "jpg", "jpeg", "gif", "webp", "avif", "ico", "bmp", "tiff", "icns",
  "pdf", "zip", "gz", "tgz", "bz2", "xz", "zst", "7z", "rar", "jar", "war",
  "woff", "woff2", "ttf", "otf", "eot",
  "mp3", "mp4", "wav", "ogg", "webm", "mov", "avi", "mkv", "flac",
  "so", "dylib", "dll", "exe", "bin", "o", "a", "class", "pyc", "pyo", "wasm",
  "db", "sqlite", "sqlite3", "parquet", "npy", "npz", "pkl", "pt", "pth", "onnx", "safetensors",
];

/** Whether `path` lies under a directory the walk never imports. */
export function inSkippedDir(path: string): boolean {
  return path.split("/").slice(0, -1).some((segment) => SKIP_DIRS.includes(segment));
}

/** Whether `path` names a file whose bytes are not text. */
export function isBinaryPath(path: string): boolean {
  const name = path.split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  if (dot <= 0) return false;
  return BINARY_EXTENSIONS.includes(name.slice(dot + 1).toLowerCase());
}

/**
 * The sha256 hex of `text`'s UTF-8 bytes — the name the store gives that blob, and the
 * hash the server rechecks, so it must be computed over exactly the bytes sent.
 */
export async function hashText(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * Whether a file's own bytes are text: a NUL byte means they are not, whatever the
 * extension claimed. This catches an extensionless binary, which the extension list cannot.
 */
function looksBinary(text: string): boolean {
  return text.includes("\0");
}

/** A file and the path to store it under, as a walk produces before reading it. */
type Found = { readonly path: string; readonly file: File };

/**
 * Read `found` into blobs, skipping what the rules leave out, and strip the dropped
 * folder's own name from every path so a snapshot's paths are relative to the root.
 */
async function readFound(found: readonly Found[]): Promise<Walked> {
  const root = commonRoot(found.map(({ path }) => path));
  const files: ReadFile[] = [];
  const skipped: Record<SkipReason, number> = { ignored: 0, binary: 0, "too-large": 0, "empty-path": 0 };
  for (const { path, file } of found) {
    const relative = root && path.startsWith(`${root}/`) ? path.slice(root.length + 1) : path;
    if (!relative) {
      skipped["empty-path"] += 1;
      continue;
    }
    if (inSkippedDir(path) || SKIP_DIRS.includes(relative.split("/").pop() ?? "")) {
      skipped.ignored += 1;
      continue;
    }
    if (isBinaryPath(relative)) {
      skipped.binary += 1;
      continue;
    }
    if (file.size > MAX_BLOB_BYTES) {
      skipped["too-large"] += 1;
      continue;
    }
    const text = await file.text();
    if (looksBinary(text)) {
      skipped.binary += 1;
      continue;
    }
    // Measured in UTF-8, as the store bounds it: `file.size` already is, but a text
    // decoded with replacement characters can grow past it.
    if (new TextEncoder().encode(text).length > MAX_BLOB_BYTES) {
      skipped["too-large"] += 1;
      continue;
    }
    files.push({ path: relative, text, sha256: await hashText(text) });
  }
  files.sort((left, right) => (left.path < right.path ? -1 : left.path > right.path ? 1 : 0));
  return { files, skipped, root };
}

/**
 * The one directory every path starts with, which is the dropped folder itself; `""` when
 * the paths share no root, as loose files dropped together do not.
 */
export function commonRoot(paths: readonly string[]): string {
  const first = paths[0]?.split("/") ?? [];
  if (first.length < 2) return "";
  const root = first[0]!;
  return paths.every((path) => path.startsWith(`${root}/`)) ? root : "";
}

/** Read the folder a file input with `webkitdirectory` reports. */
export async function walkFileList(list: FileList | readonly File[]): Promise<Walked> {
  const found = [...list].map((file) => ({
    // `webkitRelativePath` is the path under the picked folder, the folder's own name first.
    path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
    file,
  }));
  return readFound(found);
}

function entryFile(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

/** One `readEntries` call; it answers a page at a time and `[]` only when the directory is done. */
function readPage(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

/** Every file under `entry`, depth first, with its path below `prefix`. */
async function walkEntry(entry: FileSystemEntry, prefix: string): Promise<Found[]> {
  const path = prefix ? `${prefix}/${entry.name}` : entry.name;
  if (entry.isFile) {
    try {
      return [{ path, file: await entryFile(entry as FileSystemFileEntry) }];
    } catch {
      // A file the browser will not open (a permission, a vanished path) is left out
      // rather than failing the whole import.
      return [];
    }
  }
  if (!entry.isDirectory || SKIP_DIRS.includes(entry.name)) return [];
  const reader = (entry as FileSystemDirectoryEntry).createReader();
  const found: Found[] = [];
  // `readEntries` pages: it must be called until it answers an empty list, so one call
  // is not the directory — a folder of 200 files arrives in several.
  for (;;) {
    let page: FileSystemEntry[];
    try {
      page = await readPage(reader);
    } catch {
      break;
    }
    if (page.length === 0) break;
    for (const child of page) found.push(...(await walkEntry(child, path)));
  }
  return found;
}

/**
 * Read the folders and files a drop carried. Chromium, Firefox and Safari all answer
 * `webkitGetAsEntry`, which is why the drop path uses it rather than the directory picker
 * (`showDirectoryPicker`, Chromium only).
 */
export async function walkDataTransfer(transfer: DataTransfer): Promise<Walked> {
  const entries = [...transfer.items]
    .filter((item) => item.kind === "file")
    .map((item) => item.webkitGetAsEntry())
    .filter((entry): entry is FileSystemEntry => entry !== null);
  // A browser that gave no entries still gave files; a flat drop is better than nothing.
  if (entries.length === 0) return walkFileList(transfer.files);
  const found: Found[] = [];
  for (const entry of entries) found.push(...(await walkEntry(entry, "")));
  return readFound(found);
}

/** How many files a walk left out, over every reason. */
export function skippedCount(skipped: Walked["skipped"]): number {
  return Object.values(skipped).reduce((total, count) => total + count, 0);
}
