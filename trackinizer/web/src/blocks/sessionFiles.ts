// The session's code files: programs an answer registers, kept for the whole
// session so later prompts still have them. The registry is one browser-side
// store (localStorage), merged across answers: a file is keyed by its path and
// a later answer may overwrite it, the way a file system would.

export type SessionFile = {
  readonly path: string;
  readonly text: string;
  readonly language?: string;
};

const KEY = "blocks:session:files:v1";

export function loadSessionFiles(): SessionFile[] {
  try {
    const raw = localStorage.getItem(KEY);
    const parsed = raw ? (JSON.parse(raw) as SessionFile[]) : [];
    return Array.isArray(parsed) ? parsed.filter((f) => f && f.path && typeof f.text === "string") : [];
  } catch {
    return [];
  }
}

export function saveSessionFiles(files: readonly SessionFile[]): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(files));
  } catch {
    // a full or blocked store loses the session files; the answer itself still carries them
  }
}

/** Merge new answer files into the session registry (path-keyed, later wins), persist, return the registry. */
export function registerSessionFiles(incoming: readonly SessionFile[] | undefined): SessionFile[] {
  if (!incoming || incoming.length === 0) return loadSessionFiles();
  const byPath = new Map(loadSessionFiles().map((f) => [f.path, f]));
  for (const f of incoming) {
    if (f && f.path && typeof f.text === "string") byPath.set(f.path, { path: f.path, text: f.text, language: f.language });
  }
  const merged = [...byPath.values()];
  saveSessionFiles(merged);
  window.dispatchEvent(new Event("blocks:files"));
  return merged;
}
