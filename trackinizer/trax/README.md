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
`config_dir()/rekursiv-ai/trax/profiles/`. Use `trax profile` to list, show,
set, or switch.

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

On the machine's own host, `connect` joins with the enrollment token on stdin
and heartbeats until the machine is revoked, using the machine's own credential.

`trax machine connect` with no name is for your own computer. It uses the short
hostname, enrolls the computer as a personal machine (writer role; only you and
admins see it), saves its credential and serves terminals, so the web app can open
a shell on it. A named `connect` serves terminals only when the host sets
`TRAX_MACHINE_TERMINAL=1`. Each open and close is logged to
`$XDG_STATE_HOME/rekursiv-ai/trax/machines/NAME.terminals.log`, with who the
server says opened it. A shell it opens starts as your login shell and carries
`TRAX_WORKSTREAM`, `TRAX_WORKSPACE` and `TRACKINIZER_PROFILE`, and `LC_TRAX_*`
copies (with `LC_TRAX_SERVER`, the server URL, never a key) that `ssh` forwards, so
`trax` on the far side reports to the same server. Closing a terminal ends the shell's
process group, as closing ssh does: `nohup`, `setsid` and `tmux` jobs keep running.

If typing in the web terminal feels slow because the server is far from you, the
terminal can talk to your browser directly (a WebRTC data channel the server only
introduces; the server stays the fallback). Install aiortc into the environment
`trax` runs from, then restart `trax machine connect`:

```bash
uv tool install --with aiortc git+https://github.com/rekursiv-ai/trackinizer
```

`trax machine connect` prints `direct terminal link: on` or `off` at start. An
install from a local checkout needs the same source: `uv tool install --with aiortc
/path/to/that/checkout` (`uv tool list --show-paths` and the tool's `uv-receipt.toml`
name it). `TRAX_MACHINE_TERMINAL_DIRECT=0` turns the link off.

`connect --sandbox`, in either form, declares the computer a disposable sandbox
where agents may act freely. Each connect records the answer for this user,
beside the machine credential: on with the flag, off without it. While it is on,
`trax run codex` starts codex with `--dangerously-bypass-approvals-and-sandbox`
and `trax run claude` starts claude with `--permission-mode bypassPermissions` and
`--settings '{"skipDangerousModePermissionPrompt":true}'`, unless you pass the
agent's own permission flags. That setting, which reaches claude inside the one
settings file `trax run` hands it, skips claude's bypass-mode startup dialog for that
launch only: on claude 2.1.306 the inline flag opened straight to the prompt and
left `~/.claude/settings.json` and `~/.claude.json` unchanged. Your own `--settings`
as inline JSON gets the key added; as a file it leaves claude in
`--permission-mode auto` for that run. `trax run` has no such flag: codex's `--sandbox`
stays codex's. Do not declare a computer that holds work or keys you would mind
losing.

```bash
trax machine                                           # list
trax machine gpu-box                                   # show one
trax machine gpu-box role to dev                       # set a field (creates)
trax machine gpu-box how to @how.txt                   # how line from a file
trax machine gpu-box label add gpu                     # add a label
trax machine gpu-box del                               # unregister
trax machine gpu-box connect < token                   # on the host: join, heartbeat
trax machine connect                                   # this computer: enroll, serve a terminal
trax machine connect --sandbox                         # the same, on a disposable computer
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
trax run ls                         # live hosts; --all adds exited N / lost
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
