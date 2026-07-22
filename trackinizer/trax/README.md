# trax

`trax` is the command-line client for the trackinizer server.

## Grammar

The language `trax` accepts is defined in [GRAMMAR.md](GRAMMAR.md).
That document is the source of truth -- the parser, runner, and help
text all derive from it. If you change the grammar, edit GRAMMAR.md
and `grammar.py` together; CI will fail otherwise.

## Quick start

```bash
trax help                                              # top-level help
trax help issue                                        # per-verb help
trax issue                                             # list issues
trax issue summary to "Retry bug" priority to high     # create
trax issue 7                                           # show issue 7
trax issue 7 priority to high                          # mutate
trax issue 7 blocks issue 8                            # add edge
trax profile                                           # active profile + URL
```

## Profiles

`trax` keeps named server profiles under
`~/.config/trax/`. Use `trax profile` to list, show, set,
or switch.

```bash
trax profile                                           # list all (active *)
trax profile prod                                      # show one profile
trax profile prod url to https://trackinizer.example   # define one
trax profile current prod                              # switch active
```

## Run (CLI shim)

`trax run <cli> -- <args>` PTY-spawns a supported agent CLI and tails
its session log in parallel, emitting trackinizer-shaped events as
JSONL. The wrapped CLI sees a real TTY and gets full passthrough --
keystrokes, signals, exit code; the wrapper is invisible to it.

Supported: `claude`, `gemini`, `codex`. Captured events sync to the
Trackinizer server resolved from the active trax profile (URL plus
auth) by default -- the same server every other `trax` verb talks to.
`--no-sync` (or `--out PATH`, or `--dry-run`) captures to a local JSONL
file with no network instead.

```bash
trax run claude -- "fix the failing test"                # sync to profile server
trax run gemini --model gemini-3-pro -- "design a logger"
trax run codex --verbose -- "refactor the auth module"

trax run codex --no-sync -- "your prompt"                # local JSONL, no network
trax run codex --out /tmp/events.jsonl -- "your prompt"  # local JSONL at PATH
trax run codex --dry-run                                 # tail existing files, no spawn
```

Adapters live in [`run/adapters/`](run/adapters/); each one knows where
its CLI writes session JSONL and how to map a line to an `Event`. See
[`docs/cli-scraping-investigation.md`](../docs/cli-scraping-investigation.md)
for the empirical investigation of each CLI's log shape.
