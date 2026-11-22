# Messaging a session

One request sends a message into a live session: `POST /api/messages`. A person
reaches it with `trax send`; an agent of one session reaches an agent of another
with sagent's own AgentSend, which its agent process turns into the same request.

```bash
trax send lead#412 "status?"            # the agent lead of session 412
trax send lead#0b1c...-uuid "status?"   # the same session, by its UUID
trax send @scientist:sear "status?"     # every live session of a routing name
```

```text
AgentSend(to="lead#412", content="status?")   # an agent, any session, any host
```

## The path

```
AgentSend ─▶ Team ─▶ Relay.send ─┐  (the agent process)
trax send ───────────────────────┴─▶ POST /api/messages ─▶ SessionRelay ─▶ Input
```

| Step | Where | What |
|---|---|---|
| Send, agent | the sender's agent process | sagent's default `Relay` for `label#session`, posting with `Client.message_agent` |
| Send, CLI | `trax/verbs.py` `Send` | `label#session` or `@actor[:room]` |
| Route | `server/api/sessions_routes.py` `send_message_route` | Resolves the target, checks who may post, writes the sender, dedupes |
| Deliver | `server/session_relay.py` `send_input` | To the agent process's socket, or held while it starts |
| Arrive | the receiving agent process | An agent's message goes to its team, booked; a person's line to the agent it names |

## Naming

- `label#412` is one agent: its label in its session, then the session's number.
  The session's UUID reads in its place. The session is what follows the last `#`,
  so a label may hold `#` too. A number or UUID that is no session is 404.
- `@actor[:room]` is a routing name, the session's `--as` owner, which may reach
  several live sessions; `@scientist#2` is the routing name `scientist#2`. A bare
  name with `#` is always `label#number`.
- The body never guesses one from the other: `to` is a session's agent, `actor` and
  `room` a routing name, and a body with both or neither is 422.
- The server keeps no list of reachable agents. An agent learns a label by being told
  it, or by being sent a message from it; it answers that sender as it would any.

## Who sends

The route writes the sender; the body never names one (`sender`, `source` or
`author` is 422).

- A person's key sends as its account's email.
- An agent process names its own session and its agent's label (`from_session`,
  `from_label`, `booked`). The server takes the session only from a key that the
  session's attached agent process holds its socket with (`SessionRelay.socket_key`),
  else 403 `not_its_process`. The sender is then `label#<that session's number>`.
  Two processes on one key each send as their own session, and none as another's.

## Delivery

- Each live session the target names gets the text through the agent process
  serving it (an `Input`, held while the process starts). A session ended, or one
  no agent process serves, gets nothing.
- The receipt's `delivered` lists the sessions sent to; empty means none took it.
  Delivery is drop-if-absent: a live message held for a session that has moved on
  is worse than one not sent.
- The access rule is the inputs': only the session's account or opening key may post,
  and one session the target names that the caller may not post to refuses the whole
  send (403). A science chat takes a message from whoever may post a line into it,
  as `POST .../inputs` does; a browser's send to one is told to open its canvas
  (409).
- A send repeated under its `Idempotency-Key` answers the first send's receipt and
  sends nothing again, on either way; one that reached no one is not kept, so a retry
  once the session is live still sends.

## Agents across sessions

An `Input` from an agent carries how its sender's team booked it (`ask`, `reply` or
`tell`) and the agent it is for (none: the root). The receiving process hands it to
its team as received from `label#number`, so an ask is owed a reply and a reply
settles what was asked, as between the agents of one session.

AgentSend says a message to another session's agent was sent, not delivered: the
server answers after the tool call returns. A send that reaches no one is told to
the sending agent as a note of its team's, never as a message from the agent it
was for: `Your message to lead#412 was not delivered: <why>`, for the server's
refusal, an empty receipt, or a process that ended first. A message the receiving
team cannot take (no such agent, or peer messaging off) is refused back to its
sender as a `tell` from the agent it named, `[message refused] <why>`, unless it is
a `tell`: refusals are tells, so none goes back and forth.

## Not here

- Waking a stopped agent for a message: until `SessionRelay` does it for every
  sender, a session with no process is not reached.
- A directory of reachable agents, pushed to each process.

The PTY-injection runner this document once designed is gone; git history holds it.
