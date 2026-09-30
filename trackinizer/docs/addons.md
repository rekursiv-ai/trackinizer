# Addons

An addon is an application built on the trax graph: a sweep that keeps derived
data current, a chat bot that files what it hears, an embedding service. Visual
modules (see [visual_modules.md](visual_modules.md)) choose how the browser
shows the graph; addons are everything that runs behind it.

The server does not know any addon by name. A **deployment** is a configgle
config holding everything one site runs: its visual catalog and its addons. The
same deployment drives the server and the standalone runner. Leaving an addon
out of a deployment switches it off everywhere: in production, a site that must
not load an embedding model simply does not list it.

## One rule about state

Durable state lives only in trackinizer, in the database. An addon keeps only
caches: things it can rebuild from the graph and lose on a restart without
losing anything. An addon that needs to remember something writes a row; it does
not write a file, a table of its own, or a note in memory that matters after a
crash. That is what lets any addon restart, move host, or run twice while
another copy is down.

## The contract

`addons/addon.py` defines it. An addon is a trusted class with a nested
`Config(Fig[...])`. Its `manifest()` returns an `AddonManifest`, which names the
parts it contributes and where each one runs:

| Part | Runs | Gets |
|---|---|---|
| `server_services` | as tasks inside the trackinizer server | `ServerContext`: the server's `Store` and live-session inbound queue |
| `standalone_services` | in their own process, started by the runner | `StandaloneContext`: a trax HTTP client from the process's trax profile |
| `routers` | mounted by the server under `/api/addons/<name>` | FastAPI routers; every route requires at least `viewer` |

A manifest has no name. An addon's name is its field name in the deployment
config, so one addon class can run twice under two names.

A service has a `name`, a one-sentence `description`, and `async run(context)`.
It serves until cancelled. If it raises, the error is logged and the service
restarts after a delay that doubles up to a minute. If it returns, it has
finished and is not restarted. A failing addon never takes the API down.

Routers are HTTP-only. A router that holds a WebSocket route, however deeply
included, makes the deployment fail to build with an error naming the addon and
the route. The catalog lists the routes of included sub-routers too.

Choose the placement by trust, not convenience:

- A **server service** shares the API's process, its database pool and its
  event loop. It must not block the loop or hold third-party credentials.
  Use it for derived data the graph owns, such as a sweep over `change_log`.
- A **standalone service** runs as its own user with its own credentials, and
  reaches trax only through the HTTP API with a profile token. Use it for
  anything that talks to the outside world, spawns agents, or needs a GPU.

Building a deployment constructs every addon and calls its `manifest()`, in the
server as well as in the runner, including for an addon that has only standalone
services. Both must stay cheap: no connections and no secret reads. Build
connections in `run()`.

## Deployments

`addons/deployment.py` composes a site:

```python
from dataclasses import field

from configgle import Makes
from trackinizer.addons.deployment import Deployment


class Production(Makes["Deployment"], Deployment.Config):
    chat: ChatBridge.Config = field(default_factory=ChatBridge.Config)
    """The chat bridge."""

    lineage: LineageSweep.Config | None = field(default_factory=LineageSweep.Config)
    """The lineage sweep; ``None`` switches it off."""


def production() -> Production:
    cfg = Production()
    cfg.chat.model = "small"
    return cfg
```

`Deployment.Config` itself holds only `visuals`, the visual catalog config
(`Workspace.Config`). A site subclasses it and adds one field per addon, typed
with that addon's own config, so a factory sets addon fields with full type
checking. The field name is the addon's name: it appears in
`/api/addons/<name>` and in `--addon <name>`. A field that is `None` is
switched off; any other field must hold an addon config, or building the
deployment fails naming the field. The factory is a zero-argument function,
resolved by dotted path.

Start the server with it. Its server services start after the store
bootstraps, its routes are mounted before the first request, and its visuals
become the catalog the visual routes serve:

```sh
python -m trackinizer.server --addons mysite.deployments.production
# or TRACKINIZER_ADDONS=mysite.deployments.production
```

`GET /api/addons` lists what the deployment runs, services by placement and
mounted routes, without touching any service.

Run an addon's standalone services, and inspect a deployment before shipping
it:

```sh
python -m trackinizer.addons run mysite.deployments.production --addon chat
python -m trackinizer.addons show mysite.deployments.production
```

`show` prints the finalized config (`pprint(hide_default_values=False)`) and
the catalog. `--override PATH=VALUE` edits the deployment config on both
commands, and `--addon-override` does the same on the server. A path reaches an
addon's fields through its name, and the visuals through `visuals`; `NAME=null`
switches an optional addon off:

```sh
python -m trackinizer.addons run mysite.deployments.production \
    --addon chat --override chat.model=small
python -m trackinizer.server --addons mysite.deployments.production \
    --addon-override visuals.default_visual=trax.chat
```

A bad factory path, a bad override, or an unknown `--addon` exits with a
message and a nonzero status, not a traceback.

## Secrets and configuration

A config holds values that can be printed, diffed and shared, never a secret.
When a service needs a credential, its config names the environment variable
that holds it, and the service reads it in `run()`. Under systemd, that
variable comes from the unit's `EnvironmentFile`.

Inject capabilities rather than enumerating them. A slot typed
`Makeable[SomeProtocol]` lets a test or a local run swap in a fake: a fake
Slack server, or a stub embedder. A `Literal` naming the implementations would
not.

Browser and agent input never reaches configgle's `deserialize()` or a factory
path. Only the operator, through flags, unit files or the runner's arguments,
chooses a deployment.
