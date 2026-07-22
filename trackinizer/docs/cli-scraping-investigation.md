# CLI Scraping Investigation

Empirical study of structured/streaming output modes for `claude`, `gemini`,
`codex`, and `cursor` — what each emits, what each misses, and how to fill
the gaps without speculation.

> **Status:** the conclusions here were superseded by a live-capture spike
> on 2026-05-31 against newer binaries (claude 2.1.158, codex 0.135.0); the
> authoritative result is `docs/design_agent_session_logging.md`. Two claims
> below were **disproven** by that spike and are corrected inline:
>
> 1. **Codex reasoning text IS recoverable from the rollout JSONL** — the
>    `reasoning` item's `summary[].text`, present when codex is spawned with
>    `-c model_reasoning_summary=detailed`. The "no reasoning text" finding
>    below was a flag error (the test omitted that flag).
> 2. **`codex app-server --stdio` is NOT required.** It streams the same
>    summary as live deltas (`item/reasoning/summaryTextDelta`) — finer
>    granularity, identical content. The rollout-JSONL tailer (same shape as
>    the claude adapter) is the chosen path; app-server is an optional
>    fidelity upgrade only. Raw (non-summary) CoT is encrypted unconditionally
>    (`codex-rs/core/src/client.rs:747`) and unrecoverable by any path.
>
> The `exec --json` and `app-server` analyses below remain accurate as
> descriptions of those interfaces.

## Versions

| CLI | Version | Verified |
|---|---|---|
| `claude` | 2.1.153 | `claude --version` on tron |
| `gemini` | 0.28.0 | `gemini --version` on tron |
| `codex` | 0.133.0 (codex-cli) | `codex --version` on tron |
| `cursor` (`agent` binary) | not installed | no binary, no `~/.cursor` dir; official docs used as fallback |

---

## 1. Structured Streaming Flags

### claude 2.1.153

Source: the Anthropic CLI provider integration (internal).

```
claude \
  --print \
  --input-format stream-json \
  --output-format stream-json \
  --include-partial-messages \
  --verbose \
  --model <model_id> \
  --system-prompt <text> \
  --no-session-persistence \
  --setting-sources "" \
  --mcp-config <json> \
  --strict-mcp-config \
  --tools "" \
  --disable-slash-commands \
  --permission-mode bypassPermissions
```

`--print` = non-interactive one-shot.
`--output-format stream-json` = JSONL events on stdout.
`--include-partial-messages` = text deltas emitted before completion.
`--verbose` = also emits thinking deltas and system/init metadata.

### gemini 0.28.0

Source: the Gemini CLI provider integration (internal).

```
gemini --experimental-acp --model <model_id>
```

`--experimental-acp` enables JSON-RPC 2.0 over stdio; carries
`session/update` notifications with `agent_message_chunk`, `agent_thought_chunk`,
`tool_call`, `tool_call_update` payloads.

### codex-cli 0.133.0

Two modes:

**a) `codex exec --json`** (stdout JSONL stream):
```
codex exec --json "<prompt>"
```
Sample output from live run (tron, 2026-05-30T00:32):
```
{"type":"thread.started","thread_id":"019e764c-3561-7c23-b6d3-16eba345e8fa"}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"/bin/bash -lc 'echo hello'","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"/bin/bash -lc 'echo hello'","aggregated_output":"hello\n","exit_code":0,"status":"completed"}}
{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"hello"}}
{"type":"turn.completed","usage":{"input_tokens":35316,"cached_input_tokens":26880,"output_tokens":43,"reasoning_output_tokens":0}}
```

**b) rollout JSONL** (on-disk session log, written during every `exec` run):
```
~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl
```
Verified file: `~/.codex/sessions/2026/05/29/rollout-2026-05-29T17-32-46-019e764c-3561-7c23-b6d3-16eba345e8fa.jsonl`

14-line structure for the same "echo hello" run:
- Line 1: `session_meta` — id, timestamp, cwd, originator, `cli_version=0.133.0`,
  `model_provider=openai`, full `base_instructions` system prompt, git metadata
- Line 2: `event_msg` type=`task_started` — `turn_id`, `started_at`, `model_context_window=258400`
- Lines 3–5: `response_item` type=`message` (developer/user/user) — permissions,
  AGENTS.md injections, actual user text
- Line 6: `event_msg` type=`user_message` — raw user text
- Line 7: `turn_context` — `model=gpt-5.5`, `approval_policy`, `sandbox_policy`, `reasoning_effort=medium`
- Line 8: `response_item` type=`function_call` — `name=exec_command`,
  `arguments={"cmd":"echo hello","workdir":"..."}`, `call_id`
- Line 9: `response_item` type=`function_call_output` — `call_id`, full shell output
- Line 10: `event_msg` type=`token_count` — `input_tokens`, `cached_input_tokens`,
  `output_tokens`, `reasoning_output_tokens`, rate limit details (primary/secondary windows,
  `used_percent`, `plan_type=pro`)
- Line 11: `event_msg` type=`agent_message` — text, `phase=final_answer`
- Line 12: `response_item` type=`message` role=`assistant` — `output_text` content
- Line 13: `event_msg` type=`token_count` — cumulative counts
- Line 14: `event_msg` type=`task_complete` — `duration_ms=3424`, `time_to_first_token_ms=1913`

Config (`~/.codex/config.toml`):
```toml
model = "gpt-5.5"
model_reasoning_effort = "medium"
```
This host uses a ChatGPT subscription account; `o4-mini` rejected with HTTP 400.

### cursor (agent binary)

Not installed on tron (no binary, no `~/.cursor` directory).
Source: `cursor.com/docs/cli/reference/output-format` + `cursor.com/docs/cli/headless`
(fetched 2026-05-29 via WebFetch).

```
agent --print --output-format stream-json [--stream-partial-output]
```

Install: `curl https://cursor.com/install -fsS | bash` → `~/.local/bin/agent`
Auth: `CURSOR_API_KEY` env var for scripts; OAuth login for interactive.
File writes in print mode require `--force` or `--yolo`.

Event types for `--output-format stream-json`:

| Type | Subtype | Key fields |
|---|---|---|
| `system` | `init` | `apiKeySource`, `cwd`, `session_id`, `model` (display name), `permissionMode` |
| `user` | — | `message.role="user"`, `message.content[{type,text}]` |
| `assistant` | — | `message.content[{type,text}]`; with `--stream-partial-output`: also `timestamp_ms`, optionally `model_call_id` |
| `tool_call` | `started` | `call_id`, `tool_call.{readToolCall\|writeToolCall\|function}.args` |
| `tool_call` | `completed` | `call_id`, `tool_call.{readToolCall\|writeToolCall}.result.success` |
| `result` | `success` | `duration_ms`, `duration_api_ms`, `is_error`, `result` (full text) |

Tool call example (`tool_call/started`):
```json
{"type":"tool_call","subtype":"started","call_id":"toolu_vrtx_01NnjaR886UcE8whekg2MGJd",
 "tool_call":{"readToolCall":{"args":{"path":"README.md"}}},"session_id":"..."}
```

Tool call example (`tool_call/completed`):
```json
{"type":"tool_call","subtype":"completed","call_id":"toolu_vrtx_01NnjaR886UcE8whekg2MGJd",
 "tool_call":{"readToolCall":{"args":{"path":"README.md"},"result":{"success":{"content":"...","totalLines":54}}}},"session_id":"..."}
```

Note: tool call schema uses named keys (`readToolCall`, `writeToolCall`) rather
than a generic `name` field; other tools may use `function: {name, arguments}`.

---

## 2. Coverage Comparison Table

| Axis | claude 2.1.153 | gemini 0.28.0 | codex-cli 0.133.0 | cursor (agent, docs only) |
|---|---|---|---|---|
| **Native structured streaming** | yes — `--output-format stream-json` | yes — `--experimental-acp` (JSON-RPC 2.0 over stdio) | yes — `codex exec --json` | yes — `agent --print --output-format stream-json` |
| **Flags to enable** | `--print --input-format stream-json --output-format stream-json --include-partial-messages --verbose` | `--experimental-acp` | `codex exec --json` | `agent --print --output-format stream-json [--stream-partial-output]` |
| **Captures user messages** | yes (`user` event in stream) | yes (`session/update` user turn) | partial: not echoed in `--json` stdout; **yes** in rollout JSONL (`user_message` event + `response_item` role=user) | yes (`user` event type) |
| **Captures assistant text** | yes (text deltas via `--include-partial-messages`) | yes (`agent_message_chunk` in `session/update`) | yes (`item.completed` type=`agent_message`) | yes (`assistant` event; real-time deltas with `--stream-partial-output`) |
| **Captures thinking/reasoning** | yes (thinking deltas when model returns them, via `--verbose`) | yes (`agent_thought_chunk` in `session/update`) | **yes** (corrected) — `reasoning` item `summary[].text` in the rollout JSONL (and `item.completed type=reasoning` in `exec --json`), when spawned with `-c model_reasoning_summary=detailed`. Raw (non-summary) CoT stays encrypted/unrecoverable | **no** — "thinking events are suppressed in print mode and will not appear in any output format" (official docs) |
| **Captures tool calls + args** | yes (tool_use events with full args) | yes (`tool_call` in `session/update`) | yes (`item.started` type=`command_execution` with `command` field) — shell-exec only; non-shell MCP tools would need `--mcp-config` | yes (`tool_call/started` with full args per tool schema) |
| **Captures tool results** | yes (tool_result events) | yes (`tool_call_update` with result) | yes (`item.completed` type=`command_execution` with `aggregated_output`, `exit_code`) | yes (`tool_call/completed` with `result.success`) |
| **Captures token counts** | yes (`result` event `usage` field) | yes (in ACP `session/prompt` response) | yes — `turn.completed` usage: `input_tokens=35316`, `cached_input_tokens=26880`, `output_tokens=43`, `reasoning_output_tokens=0`; rate limits also in rollout JSONL `token_count` events | **no** — not documented, not emitted in any format |
| **Captures model identity per event** | yes (`system` init event; model in every request header) | yes (`session/new` response) | partial — not in `--json` stdout; **yes** in rollout JSONL `session_meta` (`cli_version`, `model_provider`) + `turn_context` (`model=gpt-5.5`) | partial — only in `system/init` event (`model` = display name e.g. "Claude 4 Sonnet"); not repeated per-event |
| **Captures compaction events** | yes (sagent provider handles these) | yes (ACP carries session state transitions) | unknown — not observed in tested runs; not documented in `exec --json` spec | **no** — not documented |
| **Is CLI the only observation point?** | yes for full fidelity including thinking | yes | no — rollout JSONL on disk contains everything the stdout stream misses | mostly — no supplementary log file; API proxy required for token counts |

---

## 3. Non-CLI Hook Strategies

### 3a. API Proxy (local HTTPS MITM)

**Mechanism**: route CLI traffic through `mitmproxy` or similar to capture
raw API request/response bodies at the HTTP layer.

| CLI | Viability | What it adds vs. streaming mode | Caveats |
|---|---|---|---|
| `claude` | possible but redundant | raw API request shapes; no new semantic content | `--output-format stream-json` already captures everything; proxy adds no observability value |
| `gemini` | possible | raw Google AI API I/O | ACP already captures user/assistant/thought/tool streams; proxy mainly useful for inspecting request shapes; pinning unknown |
| `codex` | no added value | would NOT surface raw reasoning: codex requests only `reasoning.encrypted_content` (`client.rs:747`), so the wire carries ciphertext, not plaintext CoT | file-tail of rollout JSONL is the chosen path and already yields the summary CoT |
| `cursor` | **best supplementary option for token counts** | token counts (not emitted in any stream format), raw model I/O | Cursor backend is a proprietary proxy; SSL cert pinning unknown; adds complexity |

### 3b. File-tail of Session JSONL

**Mechanism**: `tail -f ~/.../rollout-*.jsonl` concurrent with a `codex exec` run.

| CLI | Viability | What it adds | Caveats |
|---|---|---|---|
| `claude` | possible (`~/.claude/projects/<hash>/<session-id>.jsonl`) | session-level replay; persistent history | format is less documented than stream-json; `--output-format stream-json` already provides full coverage; only useful when session persistence is enabled |
| `gemini` | uncertain | unknown — session log path/format not documented | ACP already captures everything; file-tail is an uncertain fallback |
| `codex` | **the chosen path** | model identity, full system prompt, raw `function_call` args+name, `function_call_output`, cumulative `token_count` with rate limits, turn timing, **and `reasoning` summary CoT** (`summary[].text`, with `-c model_reasoning_summary=detailed`) | file is written incrementally; must watch for the rollout filename (timestamp-uuid in `~/.codex/sessions/YYYY/MM/DD/`) |
| `cursor` | **not viable** | — | no documented session log path for the `agent` headless binary; `~/.cursor/` directory for the GUI app does not exist on tron and would not contain `agent` output |

### 3c. MCP Server Impersonation

**Mechanism**: run a local MCP server that the CLI is configured to call;
log all tool invocations through it.

| CLI | Viability | What it captures | Caveats |
|---|---|---|---|
| `claude` | **native** — exact mechanism sagent uses (`anthropic_cli.py:708–713`) | all tool invocations routed through the MCP bridge | does not capture model internals (thinking, token counts); those come from stream-json |
| `gemini` | viable — `mcpServers` in ACP `session/new` | all MCP tool invocations | same limitation as claude: internal model content comes from ACP stream |
| `codex` | limited — codex runs `exec_command` shell calls, not MCP tools by default | nothing useful unless `--mcp-config` is provided and codex routes through MCP | `codex mcp-server` subcommand inverts it: makes codex act AS the MCP server |
| `cursor` | partial — cursor supports `--mcp-config` | MCP-routed tool calls only; built-in tools (`readToolCall`, `writeToolCall`) are not MCP and are not intercepted | cursor's headless `agent` MCP support is documented but limited |

### 3d. OTel Exporter

None of the four CLIs ship an OTel integration or document one.
Not viable for any of them.

---

## 4. Recommendations Per CLI

### claude — streaming mode is sufficient

Use:
```
claude --print --input-format stream-json --output-format stream-json \
  --include-partial-messages --verbose --model <id>
```

Covers everything: user messages, assistant deltas, thinking deltas, tool
calls + args + results, token counts, model identity (in `system` init event),
compaction events. No supplementary strategy needed. API proxy and file-tail
add no observability value when this mode is active.

**Supplementary**: MCP server impersonation is the mechanism for providing
tools — not an observability add-on.

### gemini — ACP mode is sufficient

Use:
```
gemini --experimental-acp --model <id>
```

ACP (JSON-RPC 2.0 over stdio) carries: user messages, assistant chunks
(`agent_message_chunk`), thinking chunks (`agent_thought_chunk`), tool calls
(`tool_call` + `tool_call_update`), token usage (in `session/prompt` response),
model identity (in `session/new`), and compaction events. No gap requiring a
supplementary strategy.

**Risk**: `--experimental-acp` is marked experimental; the event schema has
changed between gemini versions. Pin the version and re-verify on upgrade.

### codex — combine `exec --json` with rollout JSONL tail

`codex exec --json` alone is not sufficient:
- Missing: model identity per event (available in rollout JSONL `turn_context`)
- Missing: full system prompt and injected context (in rollout JSONL `response_item` role=developer)
- Missing: raw `function_call` argument JSON (in rollout JSONL; stdout only shows the shell command string)
- Token counts: present in `turn.completed` stdout stream but with more detail (rate limits, per-turn breakdown) in rollout JSONL

Thinking text is **not** missing (corrected): the rollout's `reasoning` item
carries `summary[].text` when codex is spawned with
`-c model_reasoning_summary=detailed`, and `exec --json` mirrors it in an
`item.completed type=reasoning` event. The original "thinking missing" bullet
was a flag error.

**Recommendation (updated 2026-05-31): tail the rollout JSONL alone, spawning
codex with `-c model_reasoning_summary=detailed`.** The rollout file carries
everything — model identity (`turn_context.model`), full system prompt,
`function_call` args, token counts with rate limits, and the `reasoning`
item's `summary[].text`. This is the chosen path and matches the claude
adapter's tail-the-on-disk-log shape. `exec --json` is the equivalent stdout
alternative; combining the two is unnecessary.

```bash
ROLLOUT_DIR=~/.codex/sessions/$(date +%Y/%m/%d)
codex exec -c model_reasoning_summary=detailed "your prompt" &
CODEX_PID=$!
sleep 0.5  # rollout file created shortly after exec starts
tail -f "$ROLLOUT_DIR"/rollout-*.jsonl &
wait $CODEX_PID
```

The rollout filename contains the same UUID as `thread_id` in the `--json`
stdout stream, enabling correlation.

**What remains unavailable**: raw (non-summary) chain-of-thought. Codex
hardcodes `include = ["reasoning.encrypted_content"]` on every reasoning
request (`codex-rs/core/src/client.rs:747`); there is no config/`-c` override.
The plaintext **summary** CoT (above) is recoverable and sufficient for the
thinking field; only the encrypted raw CoT is lost.

### cursor — `stream-json` + API proxy for token counts

Primary:
```
agent --print --output-format stream-json --stream-partial-output
```

Covers: user messages, assistant text (with real-time deltas via
`--stream-partial-output`), tool calls + args + results, model identity
(in `system/init` event only).

Unavailable from any cursor output path:
- **Thinking**: architecturally suppressed in print mode per docs; cannot be recovered
- **Token counts**: not emitted in any stream format; only recoverable via API proxy
- **Compaction events**: not documented

**If token counts are required**: route `agent` traffic through `mitmproxy`.
No file-tail alternative exists — the headless `agent` binary does not write
a local session log.

**If thinking is required**: cursor CLI cannot provide it. Use a different
CLI or direct API access.

---

## Evidence Index

| Claim | Source |
|---|---|
| claude flags | Anthropic CLI provider integration (internal) |
| claude version 2.1.153 | `claude --version` (tron, 2026-05-30) |
| gemini flag | Gemini CLI provider integration (internal) |
| gemini version 0.28.0 | `gemini --version` (tron, 2026-05-30) |
| codex version 0.133.0 | `codex --version` (tron, 2026-05-30) |
| codex `exec --json` sample | live run, tron 2026-05-30T00:32 |
| codex rollout JSONL schema | `~/.codex/sessions/2026/05/29/rollout-2026-05-29T17-32-46-019e764c-3561-7c23-b6d3-16eba345e8fa.jsonl` (14 lines read) |
| codex config (model, reasoning_effort) | `~/.codex/config.toml` |
| codex `o4-mini` rejection | live run: HTTP 400 "not supported when using Codex with a ChatGPT account" |
| cursor event types + thinking suppressed | `cursor.com/docs/cli/reference/output-format` (fetched 2026-05-29) |
| cursor install + `agent` binary name | `cursor.com/docs/cli/headless` (fetched 2026-05-29) |
| cursor not installed on tron | `which cursor cursor-agent agent 2>/dev/null`; `ls ~/.cursor` → not found |
