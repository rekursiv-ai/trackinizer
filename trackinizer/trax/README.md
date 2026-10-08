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
trax issue title to "Retry bug" priority to high       # create
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

### Choosing the server from the environment

A launched agent, or any non-interactive caller, needs no profile file. The
server and key resolve in this order, highest first:

| # | Source | Server | Key |
|---|---|---|---|
| 1 | `--profile NAME` | the profile | the profile (`TRACKINIZER_TOKEN` is ignored) |
| 2 | `TRACKINIZER_URL` and `TRACKINIZER_TOKEN` | `TRACKINIZER_URL` | `TRACKINIZER_TOKEN` |
| 3 | `TRACKINIZER_URL` alone | `TRACKINIZER_URL` | none |
| 4 | `TRACKINIZER_PROFILE`, the `current` profile, then `default` | the profile | the profile |

`--host` and `--port` rewrite the chosen URL afterwards, and the key goes with
it. `TRACKINIZER_TOKEN` without `TRACKINIZER_URL` is ignored, with no warning:
row 4 applies as if it were unset, and the token is never sent to a profile's
server. A shell that only seeds the token therefore keeps using its profile's
own key. An empty token is unset; a non-empty one used with a URL must be
printable ASCII with no whitespace. With the token and a URL both set, `trax`
runs in the calling process and never uses the daemon, so the key never
crosses the daemon socket. A token alone is ignored, so the daemon still
serves the command.

```bash
TRACKINIZER_URL=https://trackinizer.example TRACKINIZER_TOKEN=trax__abcdef trax issue
```

## Environment variables

`trax env` lists, sets and deletes the org's environment variables. A
secret's value is write-only: it comes from stdin or a file, never the
command line, and listings show `(secret)` in its place. Setting and
deleting need the admin role.

```bash
trax env                                               # list (secrets masked)
trax env REGION to eu-west                             # set a plain value
trax env NOTES to @notes.txt                           # plain value from a file
trax env secret API_TOKEN to -                         # secret from stdin
trax env secret API_TOKEN to @token.txt                # secret from a file
trax env REGION del                                    # delete
```

## Machines

`trax machine` records where campaigns may run: a name, a role, one `how`
line telling an agent how to use the machine, and labels. The registry only
records machines; it never reaches one. Listing and showing need the writer
role; setting, labelling and deleting need the admin role.

```bash
trax machine                                           # list
trax machine gpu-box                                   # show one
trax machine gpu-box role to dev                       # set a field (creates)
trax machine gpu-box how to @how.txt                   # how line from a file
trax machine gpu-box label add gpu                     # add a label
trax machine gpu-box del                               # unregister
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

### Detached runs

`--detach` hosts the CLI in a background process that outlives the
terminal, so a long-running agent needs no tmux. Capture, sync, and
inbound delivery are the same as a foreground run; the command returns
once the host is up. Each host has a name (`--name`, else the `--as`
name, else the CLI) and keeps every byte the CLI writes, in order, as
its scrollback.

```bash
trax run --detach --name lead --as lead claude -- --model haiku
trax run ls                         # hosts: running / exited N / lost
trax run attach lead                # drive it here; Ctrl-\ detaches
trax run log lead --follow          # scrollback, then live output
trax run send lead "status?"        # type a message in and press Enter
trax run stop lead                  # stop it; waits until it has exited
```

State lives under `state_dir()/rekursiv-ai/trax/run/hosts/<name>/`:
`host.json`, `scrollback.log`, and `host.log` (the runner's own
messages, the first place to look when a host fails to start).

Adapters live in [`run/adapters/`](run/adapters/); each one knows where
its CLI writes session JSONL and how to map a line to an `Event`. See
[`docs/cli-scraping-investigation.md`](../docs/cli-scraping-investigation.md)
for the empirical investigation of each CLI's log shape.
