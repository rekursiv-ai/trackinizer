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
trax profile prod login https://trackinizer.example    # log in with a code you approve
```

`login` prints a link and a code. Sign in at the link, check who is asking, and
approve; the command saves the key it receives as the profile. The profile becomes
the current one only when none is set, so a login never switches a profile you chose.

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

## Machines and jobs

The registry of machines campaigns may run on, and GPU jobs, moved out of trax:
`trax machine` and `trax job` say so and exit 2.

## Agent runs

Running an agent CLI recorded as an AgentSession moved out of trax: `trax run`
and `trax agentsession N run` say so and exit 2. trax reads what a run records
(`trax agentsession N`, `trax agentsession N records`) and sends a message into
a live run with `trax send @NAME TEXT`. The run model the server and other
readers share stays in the package as `trackinizer.runs`; its adapters, in
[`runs/adapters/`](../runs/adapters/), each know where a CLI writes its session
JSONL and how to map a line to a record.
