# Trackinizer user guide (pre-release)

Until the next release, `trax` installs from the public repository's main branch.

This guide follows one person from signing in to the web app to running agents
on their own computer: the web app, the `trax` command line, and how the two
meet. It is a walk-through, not a reference; the last section lists where the
details live.

## Before you start

- A Trackinizer server that offers sign-in, and an account on it. The server's
  admin decides who may sign in.
- A recent browser.
- For terminals and agents: a Linux or macOS computer of your own with
  [uv](https://docs.astral.sh/uv/).

A server you start yourself with `trackinizer --no-auth` serves the app with no
sign-in, and with no terminals: a terminal opens only for a signed-in person.

## 1. Sign in and install the app

### Sign in

1. Open the server's address. It takes you to the app, at `/app/`.
2. On the sign-in page, choose **Sign in with Google**. A server with no
   sign-in set up says "Sign-in is not configured on this server."
3. After signing in you land on the page you first asked for.

The top bar folds into a thin strip along the top of the window. Rest the
pointer on the strip, or tap it, to open the bar; the pin (📌) keeps it open.
From left to right it holds the logo (the graph), **Go** (every page), Search
(⌘K, or Ctrl+K), New, your workstreams, **Machines** (where commands run),
**Workbench** (the canvas's visuals) and your avatar. The avatar's menu has
**Your settings**, Appearance, the other accounts this browser has used,
**Add another account** and **Sign out**.

### Install it as an app

The app ships a web app manifest, so a browser that installs web apps can
install it: use the browser's Install or Add to Home Screen command. The
installed app opens in a window of its own at `/app/`. It also gets a few keys
that a browser keeps for itself in a plain tab (see
[Terminals and machines](#terminals-and-machines)).

### On a phone

On a screen 760 px wide or less, the app shows Chat or the canvas, one at a
time. A switch along the bottom, **Chat | Canvas**, picks which, and Chat comes
first. Both stay open behind the switch, so a message you were typing is still
there when you come back. A reload shows Chat.

On the canvas every tile takes the full width, the tiles stack, and you scroll
down through them.

## 2. Chat and the canvas

The app has two parts side by side: Chat, a column on the left, and the canvas,
which shows the page you are on and visuals beside it. The assistant in Chat
sees what is on your canvas and can change it: open a page, show a visual,
light up the rows it is talking about.

### Chats and workstreams

A **chat** is one conversation with the assistant together with its own
canvas. (The code and the Console call it a station.) A **workstream** groups
the chats of one effort, and is an Issue in the graph. Chats in no workstream
are in **Scratch**.

- The top bar lists your workstreams. Click one to open its pinned chat, else
  its newest; a workstream with no chat gets a new one.
- **+ Workstream** makes a workstream; type its name and press Enter.
- Double-click a workstream to rename it. Right-click it for Info, Pin, Move
  left, Move right, Rename and Hide. Drag one to reorder.
- A workstream's Info lists its chats and the agent sessions in each.

### The chat header

The header's buttons show while the pointer is over the header, and always on
a touch screen.

- **The chat's name.** Click it to rename the chat: type, then Enter (Esc
  cancels). The menu under it lists this workstream's chats, pinned ones first,
  then the five most recent, each with Pin and Hide. It also has **+ New chat**
  and **Search all chats…**, which finds hidden chats too. A chat you never
  named shows as "Scratch chat".
- **The workstream chip** (for example "Scratch ▾") moves the chat to another
  workstream. Type a name and press Enter: the chat moves to the workstream of
  that name, which is created if you have none. When the server suggests a
  workstream, a chip with its name and a question mark moves the chat there in
  one click.
- **The terminal icon** flips Chat over to a terminal on your own computer
  ([section 3](#3-a-terminal-on-your-own-machine)).
- **▷ or ◁** docks Chat on the other side.
- **⋯** opens the chat's menu:
  - **Clear chat** starts a new chat on the same canvas.
  - **Hide panel** hides the Chat column; **Pin panel** brings it back.
  - **Delete chat forever** shows for the chat's starter while they are its
    only poster, and for admins.
  - **Info** shows the chat's ids and, for a chat made for an agent session,
    that session ("Made for"), live or ended.
  - **Public** or **Private** shows for the chat's starter. It says which the
    chat is; a click switches it.
  - **Share** shows for the chat's starter. It adds people by email, and each
    person listed has Remove.

### Who reads a chat

The line above the message box says who reads the chat, for example "Read by
everyone signed in to this server." Who "public" takes in depends on the
server: everyone signed in, or the starter's organization, or only the starter
and the people they share it with. A private chat is read only by its starter
and the people they share it with. Do not type secrets into Chat.

Anyone who reads a chat joins it by typing in it. **Fork from here**, on an
answer in a public chat, starts a new chat holding the lines up to that answer.

### Talking to the assistant

Type into the box and press Enter; Shift+Enter starts a new line. Each message
carries the page you are on and the few pages before it, so "this" means what
you see. When the page is a record, a line above the messages says which:
"On screen: Issue#7 …". The reply shows as it is written. Rows the reply names
light up on the canvas; **Your settings** → Agent workspace → "Highlight rows
the assistant mentions" turns that off.

Keys: Ctrl+\\ (⌘\\ on a Mac) hides and shows Chat. Alt+Enter makes Chat fill
the window, and Esc returns. ⌘K (Ctrl+K) opens the palette, which has New chat
and New terminal among its commands.

### The canvas

The bar along the top of the canvas works like a browser's:

- the pen draws red boxes and the eraser clears them (next section);
- Back, Forward and Home step through what this tab has shown;
- the trash can clears the canvas and closes its other tabs, after asking;
- tabs: + opens one, a double click renames one, × closes one.

The canvas holds the page you navigate to and visuals beside it: Context graph,
Lineage and timeline, Artifact, Files, Terminal, and whatever else the server
offers. Turn each on or off in the top bar's **Workbench** menu (the sliders
button). A visual that needs a record says "Open a record to enable this
visual." until you open one. The assistant shows and moves visuals too.

Each visual is a tile. Drag a tile's bar to float it or to dock it at an edge;
◧ and ◨ dock it at the left or the right; × dismisses it. Drag the edge between
two tiles to resize them. In a window 900 px wide or less the tiles stack and
the canvas scrolls.

### Red boxes

Draw a red box to point at something on the canvas:

1. Click the pen at the left end of the canvas bar.
2. Drag across what you mean. A box drawn over a tile moves with the tile.
3. Press Esc, or click the pen again, to stop drawing.

Each box notes the page, and the records and visuals under it. The assistant
hears of the boxes drawn since your last message along with your next one, so
each box reaches it once. A `trax run claude` session filed in this chat hears
of them too ([section 4](#red-boxes-reach-claude)). Boxes belong to the page
they were drawn on. Click a box's outline to remove it; the eraser removes
every box on the page.

### When Chat is off

Chat needs a partner, the server's shared assistant. When the server has none, or
it is not running, Chat says so and the message box stays off.

## 3. A terminal on your own machine

Flip Chat over and it becomes a terminal: a shell on a computer of yours, in
the browser. `trax machine connect`, running on that computer, makes it
possible. The computer dials out to the server and listens on nothing.

### Connect a computer

The top bar's **Machines** menu (the server icon) lists your machines, with
Allow, and these same steps with your server's address filled in.

1. Make an API token: your avatar → **Your settings** → **API tokens** →
   **New token**. Give it a label, such as "laptop trax", keep the writer role,
   and click **Create token**. Copy the secret; it is shown only once.

2. On the computer, install `trax` from the public repository:

   ```bash
   uv tool install git+https://github.com/rekursiv-ai/trackinizer
   ```

3. Point `trax` at the server, with your server's address and the token:

   ```bash
   trax profile url to https://trackinizer.example
   trax profile token to <TOKEN>
   ```

4. Connect, and leave it running, in tmux or as a user service:

   ```bash
   trax machine connect
   ```

   It names the machine after the computer's short hostname (`laptop` here),
   enrolls it as your personal machine, which only you and admins see, and
   serves terminals until you stop it.

5. In the browser, click the terminal icon in Chat's header. A bar says "New
   machine laptop wants terminals." Click **Allow**. A machine enrolled with a
   token waits for your click in the browser, since anything holding your
   token could have enrolled it. An admin's machines need no Allow.

6. Click **Start terminal on laptop**.

The terminal side's **More help** gives steps 2 and 3 as one command, with your
server's address filled in. If `trax machine help` fails or lists no `connect`,
your `trax` is older than it: install again as in step 2.

The first `connect` saves the machine's own credential, readable only by you,
and later runs use it. While it runs it prints whether the direct link is on
(see [below](#typing-feels-slow-the-direct-link)), `joined: machine laptop` the
first time, and `terminal opened by` with your address for each terminal.

If the computer is one you can afford to lose, such as a disposable cloud box or
a virtual machine, say so when you connect it:

```bash
trax machine connect --sandbox
```

`trax machine NAME connect --sandbox` works the same way. Connect prints
`sandbox: on`, and from then on every `trax run` on that computer starts its
agent without the usual questions:

- codex skips its approvals and its own sandbox;
- claude runs in its bypassPermissions mode, with its startup warning skipped
  for that launch only (claude 2.1.306 saves nothing to your settings).

Flags you give the agent yourself, such as `--permission-mode plan` for claude
or `-s read-only` for codex, are never replaced. A claude `--settings` you
pass as inline JSON is kept, with the skip added; one you pass as a file
leaves claude in its auto permission mode for that run. The declaration describes
the computer, not one machine name, and each connect sets it: a `trax machine
connect` without `--sandbox` turns it off again.

Use it on a computer built to be thrown away. Do not use it on your own
laptop, or on anything that holds work or keys you would mind losing: an agent
with free rein can delete files and run any command you could. Keep that
computer for yourself, and give agents a sandbox of their own. The idea is in
[the session and the computer it works on](#the-session-and-the-computer-it-works-on).

The shell is your login shell, started in your home directory with a fresh
environment, as an ssh login is. It knows the chat that opened it
(`TRAX_WORKSPACE`, `TRAX_WORKSTREAM`) and the server (`TRACKINIZER_PROFILE`),
so `trax` in it reports to that server, and agents started in it are filed in
that chat.

### Terminals and machines

- Chat's terminals are named after their machine and a number: "laptop 1",
  "laptop 2".
- Flipped, the name button in Chat's header opens the terminal menu: the
  terminals in this chat, then **+ New terminal**, or one **+ New terminal on
  NAME** for each ready machine.
- Connect more computers the same way; each is offered once it is connected
  and allowed.
- The canvas has a Terminal tile too: Workbench → Terminal, or New terminal in
  the palette. It holds several terminals as tabs: + opens one, asking which
  machine when more than one is ready, and × closes one.
- In the tile, Ctrl+Shift+T opens a tab, Ctrl+Shift+W closes one, and
  Ctrl+Shift+PageDown and Ctrl+Shift+PageUp switch tabs. **Your settings** →
  Appearance → "Ctrl+T, Ctrl+W and Ctrl+Tab open, close and switch terminal
  tabs" adds the short forms. Chrome keeps those in a plain tab and gives them
  to the page only in the installed app.
- Every key typed in a terminal goes to the shell, the app's shortcuts
  included: click outside the terminal to use ⌘K or Ctrl+\\.
- ⤢ makes a terminal fill the canvas, and ⤡ restores it. **Your settings** →
  Appearance → Terminal font size sets its text size.
- The assistant can open a Terminal tile on a machine it names; the terminal
  then says "Opened at NAME's request."

### The flipped header

Flipped, Chat's header names the terminal in view, and the chat icon flips back
to Chat, which is as you left it, draft included. The ⋯ menu holds:

- **Close terminal**, which stops the shell in view;
- **Terminal help**, which brings back the bar and its More help;
- Hide panel, Info and Public or Private, as in Chat. Clear chat, Delete chat
  forever and Share are grayed until you flip back.

This browser remembers which chats you left flipped.

### Typing feels slow: the direct link

A terminal's traffic goes through the server. When the server is far from you,
let your browser talk to the computer directly, over WebRTC; the server only
introduces the two. Add the library to `trax`:

```bash
uv tool install --with aiortc git+https://github.com/rekursiv-ai/trackinizer
```

Then restart `trax machine connect`. It prints `direct terminal link: on`, and
the terminal shows a **Direct link** badge while the link works. If the direct
path drops, the terminal carries on through the server.
`TRAX_MACHINE_TERMINAL_DIRECT=0` turns the link off. Keep `--with aiortc` when
you reinstall `trax`: an install without it removes the library.

### Restarting and stopping connect

- Restarting `trax machine connect` serves at once: the new one takes the
  machine over, and the old one stops.
- Ctrl-C stops it, and the machine shows offline at once.
- Either way, the shells it was serving end.
- Restart it with the same flags: a connect without `--sandbox` turns the
  sandbox off.

If one terminal is open in two windows, the newer window takes it, and the
older one says "Another window took this terminal over." with a **Take over**
button to take it back.

### What survives

| What happens | The shell |
|---|---|
| You reload the page | re-attaches |
| No page shows it for over a minute (tab closed, laptop asleep) | ends |
| No typing for 60 minutes | is closed |
| 24 hours after it opened | is closed |
| `trax machine connect` restarts or stops | ends |

Ending a shell ends its process group, as closing ssh does; `tmux`, `nohup` and
`setsid` jobs keep running. For long work, use tmux or a detached run
([section 4](#detached-runs)), which survives all of the above.

### Over ssh

`ssh` from a web terminal keeps the chat: the shell's `LC_TRAX_*` variables go
along when ssh forwards `LC_*` variables (`SendEnv LC_*`). With `trax`
installed and set up on the far host, `trax run claude` there is filed in the
same chat. A far host with no profile for the server says how to add one.

### One computer, two servers

`trax` keeps a named profile per server, and `trax machine connect` keeps the
machine's credential per server, so one computer can be a machine of two
servers. Make a profile for the second server, with a token from its own
Settings, and run a second connect beside the first:

```bash
trax profile work url to https://work.example
trax profile work token to <TOKEN>
trax --profile work machine connect
```

Each server's web app asks you to Allow the machine once. A shell carries the
profile of the server that opened it, so `trax` and agents in it report back
to that server. `trax profile` lists the profiles, and `trax profile current
work` makes `work` the one used when no `--profile` is given.

## 4. Agent sessions

`trax run` wraps an agent CLI, `claude`, `codex` or `gemini`, or a plain shell,
`sh`, and records the session on the server as it runs. Nothing else about the
CLI changes: the same screen, the same keys, the same exit code. Arguments
after `--` go to the CLI.

```bash
trax run claude
trax run codex -- "fix the failing test"
```

### The session and the computer it works on

Think of a session as two parts. The session is the brain: its conversation,
its records and its history live in Trackinizer. The computer it works on is
the hand: the place where it reads files and runs commands. The two need not be
the same.

- A session can work on your laptop today and on a disposable box or a GPU
  machine tomorrow, and keep its history. Resume it on the other computer with
  `trax agentsession 7 run claude`.
- So the place you develop need not be the place an agent acts. Keep the
  computer that holds your work and your keys for yourself, and give agents
  their own.
- A hand with nothing to lose can be given free rein. Connect a disposable
  computer with `--sandbox`, and agents there stop asking before they act
  ([Connect a computer](#connect-a-computer)).

### Where a session lands

The server files each session in one of your workstreams and a chat, by the
first rule that fits:

1. the chat whose terminal you started it from, directly or over ssh;
2. where the session was filed before, when you resume one;
3. the workstream of your newest session on the same git branch, in a new chat;
4. Scratch, in a new chat.

On Chat's terminal side the bar says where: "Sessions land in" and the
workstream.
A chat made for a session names it under ⋯ → Info → Made for. The Console
(Go → Console, or `g c`) shows sessions' messages live; opened inside a chat,
it starts on that chat's sessions ("This station").

Flags worth knowing:

- `--as NAME`: the name others message it by, `@NAME` in the Console.
- `--room ROOM`: join a room; repeat it to join several.
- `--private`: start the session hidden: only you, staff and admins read it.
- `--diff`: show a live git diff of the files it edits on its page.

### Detached runs

`--detach` runs the agent in a background host that outlives the terminal, so
a long-running agent needs no tmux:

```bash
trax run --detach --name lead claude
trax run ls                    # live hosts; --all adds exited and lost ones
trax run attach lead           # drive it here; Ctrl-\ detaches
trax run log lead --follow     # scrollback, then live output
trax run send lead "status?"   # type a line in and press Enter
trax run stop lead             # stop it; waits until it has exited
```

The name defaults to the `--as` name, else the CLI's, and a name already
running is refused. Hosts belong to the computer that started them, so run
`ls`, `attach` and the rest there. A detached run started in a web terminal is
still filed in that chat, and outlives the tab and the terminal's limits.

### The agent knows its canvas

A `trax run claude` or `trax run codex` session filed in a chat is told, when
it starts, that "the canvas" means that chat's, and how to read and change it
with `trax workspace`. It is also told that the conversation is already logged to
trax as the chat's AgentSession, and to file each result as a row with the `trax`
CLI, `--as` set to `$TRAX_ACTOR`. Claude hears it through `--append-system-prompt`, codex
through `-c developer_instructions`; one you pass yourself wins, and then the
session is not told. Reattaching to a detached session keeps its canvas.

### What a switch loads

When a chat switches between the assistant, claude and codex, the next one
loads what the last one was looking at: everything from the last compaction or
`/clear` on, in any of the chat's sessions, opened by that compaction's summary.
A codex summary is sealed, so only codex reads it; any other gets codex's kept
turns. Past each one's budget the oldest whole turns are left out, and a result
over 16 KB keeps its head and tail. The first line it loads counts what was
left out and names the command that prints it, such as
`trax agentsession 7 records --to 0:41` (`--part P` for one part,
`--format json` for the stored records).

### Red boxes reach claude

A `trax run claude` session filed in a chat hears of the red boxes drawn on
that chat's canvas. Before each prompt you type into claude, `trax` adds the
boxes drawn since the last one, up to the newest 20, each with the records and
visuals under it. Each box reaches the session once, the first time after it is
drawn, as with the assistant in Chat. Only claude gets boxes this way, and not
when you give claude a `--settings` file of your own.

## 5. Records from the command line

The graph holds Issues, the work, and Artifacts, what the work produced:
Beliefs, Experiments, Papers, CodeChanges, WebResults, WebSearches and
AgentSessions. Edges join them: an Issue narrows another, an Experiment proves
a Belief. `trax` reads and writes them all in one grammar, a subject and then
what to do with it:

```bash
trax issue                                        # list issues
trax issue title to "Retry bug" priority to high  # create one
trax issue 7                                      # show Issue#7
trax issue 7 priority to high                     # set a field
trax issue 7 narrows issue 3                      # link: 7 is part of 3
trax belief title to "PNT holds" confidence to 0.9 judgement to proven
trax title re retry                               # search every kind
trax recent --limit 10                            # the latest changes
```

Every row shows in the web app, and a `Kind#seq` such as `Issue#7` in Chat
links to it. `trax help`, `trax issue help` and `trax issue 7 priority help`
explain each part. For an agent, `trax doctor --fix --target claude` (or
`codex`, or `cursor`) installs the skills that teach it how to write each kind
of row.

## Where to read more

- [`trax/README.md`](../trax/README.md): profiles, environment variables,
  machines and `trax run`.
- [`trax/docs/GRAMMAR.md`](../trax/docs/GRAMMAR.md): the full command grammar.
- [`trax/docs/skills/`](../trax/docs/skills/README.md): what a well-formed
  Issue, Belief, Experiment or other row holds.
- [`epistemy.md`](epistemy.md): why each edge is named what it is.
- [`web/README.md`](../web/README.md): the web app's behavior in detail.
- [`api.md`](api.md): the HTTP API.
