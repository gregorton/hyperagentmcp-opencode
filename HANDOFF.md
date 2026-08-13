# Handoff: opencode CLI powered by Hyperagent MCP

Context document for a fresh assistant session. Everything below is verified
unless marked otherwise.

## Where the code lives

**https://github.com/gregorton/hyperagentmcp-opencode**

That repo is the single source of truth. Don't ask for files to be attached in
chat and don't rebuild anything from scratch — clone it and work from the
tree:

```bash
git clone https://github.com/gregorton/hyperagentmcp-opencode.git
cd hyperagentmcp-opencode
```

Changes should land as commits and a push to that repo, not as downloads.

## The goal

Run a Claude-Code-style agentic coding CLI on a local Windows machine that
creates and edits files autonomously, using **Hyperagent as the only model
provider** (no Anthropic/OpenAI keys, no Claude Code subscription).

## The core constraint that shapes everything

**Hyperagent's MCP server is agent orchestration, not a model API.** At
`https://hyperagent.com/api/mcp` it exposes exactly six tools:
`list_agents`, `create_thread`, `send_message`, `get_thread`, `list_threads`,
`create_attachment_upload`. There is no chat-completions endpoint and no token
stream. It cannot be dropped in where an OpenAI base URL goes.
Docs: https://www.hyperagent.com/docs/concepts/agents/mcp-server

## What was built

Two packages. The second is the one in use.

### 1. `hyperagent-harness` (first attempt, superseded)
A minimal custom CLI loop: sends a task plus file listing to a Hyperagent
agent, agent replies with a JSON array of actions (`read`/`write`/`edit`/
`run`/`done`), harness executes them locally. Works, but reinvents an IDE.

### 2. `hyperagent-opencode` (the real deliverable, working)
A **local OpenAI-compatible shim** so the stock opencode CLI runs unmodified
with Hyperagent agents as its only provider.

```
opencode (stock)  --POST /v1/chat/completions-->  shim (localhost:8787)
                                                    |
                                                    | MCP threads
                                                    v
                                            Hyperagent agent (the brain)
```

Deliberately **not** a fork. opencode's provider layer is a protocol
implementation (`packages/llm/src/protocols/openai-chat.ts`); everything above
it is provider-agnostic. Rewriting it to speak MCP would mean redoing the work
after every upstream release. The shim means `npm i -g opencode-ai@latest`
keeps working.

How a turn flows:
1. opencode POSTs the full conversation plus tool schemas.
2. Shim finds the Hyperagent thread matching that conversation prefix and sends
   only the new messages (opencode resends full history; replaying it would
   burn a run per message). First turn creates the thread.
3. Agent replies with prose, or `{"tool_calls":[{"name":...,"arguments":{...}}]}`.
4. Shim converts to OpenAI `tool_calls` and streams it back.
5. opencode executes the tool locally, posts the result, loop repeats.

Each Hyperagent agent is exposed as a selectable "model" (`/models` in the TUI).
`"enabled_providers": ["hyperagent"]` in opencode's config hides every other
provider.

## Files

| File | Purpose |
|---|---|
| `shim/server.py` | HTTP server: `/v1/models`, `/v1/chat/completions`, `/health`; streaming + heartbeat logic |
| `shim/bridge.py` | message rendering, reply parsing, thread mapping, SSE frame writer |
| `shim/hyperagent_client.py` | MCP client, OAuth sign-in, token cache |
| `hyperagent_code.py` | launcher: `setup` / `run` / `serve` |
| `install_command.py` | installs a global `hypercode` command (NOT yet run by user) |
| `probe_streaming.py` | measures whether real streaming is possible (NOT yet run) |
| `test_shim.py` | 29 checks, fake backend |
| `test_streaming.py` | 13 checks: heartbeats, partial text, tool-call safety |
| `live_test/fake_serve.py` | scripted backend for driving real opencode |
| `AGENT_SYSTEM_PROMPT.md` | system prompt for the Hyperagent agent |
| `fork-and-brand.sh` | optional: clone opencode as a rebranded binary |

## Bugs found and fixed (do not reintroduce)

1. **MCP SDK v2 uses `httpx2`, not `httpx`.** `mcp==2.0.0` requires
   `httpx2>=2.5.0`, and `OAuthClientProvider` subclasses `httpx2.Auth`. Passing
   it to a plain `httpx.AsyncClient` raises
   `TypeError: Invalid "auth" argument`.
2. **OAuth `server_url` must be the full MCP URL**, `https://hyperagent.com/api/mcp`,
   not the bare origin. The SDK derives the expected resource from it and
   compares against the server's advertised resource; a trimmed origin fails
   with `Protected resource ... does not match expected ...`.
3. **Windows: no SIGINT for child processes.** `proc.send_signal(SIGINT)`
   raises; use `terminate()`. Handled by `stop_process()`.
4. **Windows: `os.execv` is quirky**; use `subprocess.call`.
5. **Windows: `pip` and `python` often point at different installs.** Always
   `python -m pip install ...`.
6. **opencode's write tool takes `filePath` (absolute), not `path`.**

## Verified facts about opencode (v1.18.16, read from source)

- Protocol: `packages/llm/src/protocols/openai-chat.ts`, endpoint
  `{baseURL}/chat/completions`.
- **Always streams** (`stream: true as const`, `Framing.sse`).
- SSE framing **drops `[DONE]`** and empty keep-alives.
- Strict event schema: `{choices:[{delta?, finish_reason?}], usage?}`. Excess
  fields are ignored. Tool-call deltas need an integer `index` plus `id` and
  `name`.
- Requests `stream_options: {include_usage: true}`.
- Sends `tool_choice` (`auto` / `none` / `required` / named function).
- Custom provider config shape:
  `provider.<id> = {npm: "@ai-sdk/openai-compatible", name, options:{baseURL, apiKey}, models:{}}`.
- `enabled_providers` / `disabled_providers` gate autoloaded providers.
- `small_model` handles cheap calls like title generation (one extra run per
  session otherwise).
- **Config path uses `xdg-basedir`, which does NOT special-case Windows**, so
  the global config is `~/.config/opencode/opencode.json` on every platform
  (`C:\Users\<user>\.config\opencode\opencode.json`).
- Sends 14 tools; `websearch` is provider-independent (calls Exa/Parallel
  directly), so it works through the shim.

## Test status

- `test_shim.py`: 29 checks pass (tool-call translation, SSE conformance,
  thread reuse, parser edge cases).
- `test_streaming.py`: 13 checks pass (heartbeats, partial streaming, JSON
  never leaked as prose).
- **Live**: real opencode 1.18.16 from npm, driven against the shim with a
  scripted backend, wrote a file to disk and exited 0. Repeated with a 12s
  artificial delay per turn to confirm opencode accepts heartbeat frames.
- MCP client verified against MCP SDK 2.0.0 (imports, signatures, transport
  arity, token storage round-trip).
- **Real Hyperagent sign-in works on the user's machine** (confirmed by user).

## Streaming

MCP offers polling, not a token stream. Two modes:

- **Heartbeat mode (default, on).** Stream opens immediately, event-free
  keep-alive frames while the agent thinks, then the reply. Prevents the UI
  looking hung and avoids chunk timeouts.
- **Partial mode (opt-in, `HYPERAGENT_PARTIAL_STREAM=1`).** Polls for text the
  agent has written so far and forwards real deltas. Only useful if
  `get_thread` exposes partial text — **unknown, `probe_streaming.py` answers
  it and has not been run yet.** Any reply that opens like JSON is buffered so
  a half-written tool call never leaks as prose.

## Known limits

- **Cost**: one opencode turn = one billed Hyperagent run. Subagents (`task`)
  multiply this; each nested turn is another run.
- **Latency**: seconds per turn, since each call is a full agent run.
- **Images**: stripped to `[image omitted]` (MCP path is text-only).
- **Reasoning**: `reasoning_content` unpopulated, so no thinking blocks.
- **No prompt caching.**
- **Cost/token display** in `opencode stats` is estimated (chars/4) and shows
  near-zero cost; trust Hyperagent billing instead.
- **Protocol drift**: if the agent replies with prose when a tool call was
  wanted, opencode just talks. Fix in the agent's system prompt. The agent's
  own tools must stay disabled or it does the work in its own sandbox.

## User's environment

- Windows, Command Prompt.
- Python 3.14 at `C:\Users\sixth\AppData\Local\Python\pythoncore-3.14-64`.
- Package at `C:\Users\sixth\OneDrive\hyperagent-opencode`.
- opencode 1.18.16 installed via npm.
- Setup complete and working: signed in, config written, agent created.

## Setup (already done, for reference)

```
git clone https://github.com/gregorton/hyperagentmcp-opencode.git
cd hyperagentmcp-opencode
python -m pip install "mcp>=2" httpx
npm i -g opencode-ai
powershell -Command "(Get-Content AGENT_SYSTEM_PROMPT.md -Raw) -split '(?m)^---\s*$', 2 | Select-Object -Last 1 | Set-Clipboard"
# create agent in Hyperagent web UI, paste prompt, TURN OFF ALL ITS TOOLS
python hyperagent_code.py setup
python hyperagent_code.py run
```

Setup is **once per machine, not per project**. Sign-in lives in
`~/.hyperagent-harness/`, config in `~/.config/opencode/`. To work in another
directory, `cd` there and run the launcher by absolute path, or use
`serve` in one window plus plain `opencode` everywhere else.

## Open items

1. `install_command.py` — installs a global `hypercode` command into npm's bin
   (already on PATH). Written and tested in a Linux sandbox; **not yet run by
   the user on Windows.**
2. `probe_streaming.py` — not yet run; decides whether partial streaming is
   worth enabling.
3. An in-progress test verifying opencode operates in the *current working
   directory* (rather than the package directory) was interrupted before
   finishing. Code inspection shows `subprocess.call([opencode, ...],
   cwd=os.getcwd())`, so it should be correct, but it is unconfirmed by
   live test.
