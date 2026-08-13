# opencode, powered by Hyperagent only

The real opencode CLI — its TUI, its tools, its permission prompts, its LSP
integration — with your Hyperagent agents as the only available model, and no
other provider reachable. No Anthropic key, no OpenAI key, no Claude Code
subscription.

```
your machine                                        hyperagent.com
┌────────────────────────────────────────┐         ┌─────────────────┐
│ opencode (stock, unmodified)           │         │                 │
│   reads/writes files, runs commands    │         │  your agent     │
│              │ POST /v1/chat/completions         │  (the model)    │
│              ▼                          │  MCP    │       ▲         │
│  shim  ──────────────────────────────────────────┘       │         │
│   translates chat-completions <-> threads                │         │
└────────────────────────────────────────┘         └─────────────────┘
```

opencode wants a model API. Hyperagent MCP is thread orchestration. The shim is
the adapter between them: it speaks OpenAI chat-completions to opencode and MCP
threads to Hyperagent.

## Why a shim instead of a rewritten fork

opencode's provider layer is a protocol implementation
(`packages/llm/src/protocols/openai-chat.ts`), and everything above it — tools,
sessions, TUI — is provider-agnostic. Teaching that layer to speak MCP means
rewriting the core loop and re-doing it after every upstream release. The shim
touches nothing, so `npm i -g opencode-ai@latest` keeps working. If you still
want your own branded binary, `fork-and-brand.sh` builds one.

## Setup

```bash
# 0. get the code
git clone https://github.com/gregorton/hyperagentmcp-opencode.git
cd hyperagentmcp-opencode

# 1. dependencies (Python 3.10+)
python -m pip install "mcp>=2" httpx
npm i -g opencode-ai          # or: brew install sst/tap/opencode

# 2. create the agent in the Hyperagent web UI
#    paste AGENT_SYSTEM_PROMPT.md as its system prompt, turn OFF all its tools

# 3. sign in, discover your agents, write opencode's config
python hyperagent_code.py setup

# 4. go
python hyperagent_code.py run
```

`setup` signs you in through the browser once (token cached in
`~/.hyperagent-harness/`), lists your agents, and writes
`~/.config/opencode/opencode.json` with:

- a `hyperagent` provider pointing at the local shim
- one model entry per Hyperagent agent — switch with `/models` in the TUI
- `"enabled_providers": ["hyperagent"]`, which hides every other provider

Any existing opencode config is backed up first, and unrelated keys are kept.

## Commands

| Command | Does |
|---|---|
| `python hyperagent_code.py setup` | sign in, discover agents, write config (backs up the old one) |
| `python hyperagent_code.py run` | start the shim, launch opencode, clean up on exit |
| `python hyperagent_code.py run -- run "fix the failing test"` | pass args through to opencode |
| `python hyperagent_code.py serve` | shim only, if you'd rather launch opencode yourself |
| `--debug` | log every translation: threads created, prompt sizes, tool calls parsed |

## What was verified

**The real thing ran.** opencode 1.18.16, installed from npm, pointed at the
shim with a scripted backend standing in for Hyperagent:

```
$ opencode run "create hello.py with a greet function"
> build · test-agent
← Write hello.py
Wrote file successfully.
Created hello.py with a greeting function. Task complete.

$ cat hello.py
def greet(name):
    return f'Hello, {name}!'
```

opencode loaded the provider, streamed from the shim, parsed the tool call,
wrote the file, posted the result back, took the follow-up reply, and exited 0.
It sent all 14 of its tools through the shim with full JSON schemas intact.
Reproduce it with `live_test/fake_serve.py` (see the header in that file).

Also checked against opencode's source, not guesswork:

- it always streams (`Framing.sse`, `stream: true`), so the shim streams
- its SSE decoder drops `[DONE]`, which the shim emits
- every event matches the `OpenAIChatEvent` schema it validates against, and
  tool-call deltas carry the required integer `index` plus `id` and `name`
- `stream_options.include_usage` is requested, so the final chunk reports usage
- `tool_choice` (`required` / `none` / a named function) is passed to the agent
  as a constraint

29 automated checks cover this, with a fake Hyperagent backend:
`python test_shim.py`. The MCP client itself was verified against MCP SDK 2.0.0.
The one untested link is the MCP sign-in to your real account, which needs your
browser.

## How a turn actually flows

1. opencode POSTs the whole conversation plus tool schemas to the shim.
2. The shim finds the Hyperagent thread for that conversation prefix and sends
   only what's new. (opencode resends full history every time; replaying it
   would burn a run per message.) First turn creates the thread.
3. The agent replies with prose, or with
   `{"tool_calls":[{"name":"edit","arguments":{...}}]}`.
4. The shim converts that to OpenAI tool_calls and streams it back.
5. opencode executes the tool on your machine, asks your permission if it's
   configured to, and posts the result. Back to step 1.

## Known rough edges

**Latency.** Each model call is a full Hyperagent agent run, so expect seconds
per turn. The shim opens the SSE stream immediately and sends heartbeat frames
while the agent thinks, so opencode stays responsive instead of looking hung
(verified live against 12-second turns).

**Streaming.** See below.

**Cost.** One opencode turn equals one billed Hyperagent run. Agentic coding is
turn-hungry — a bug fix might be a dozen. opencode also spends one extra call
per session generating a title; `setup --small-model <agentId>` sends those to a
cheaper agent. Watch the meter before you set it loose.

**Protocol drift.** If the agent answers a tool request with prose, the shim
returns prose and opencode just talks instead of acting. Fix in the agent's
system prompt, not the shim. Keeping the agent's own tools disabled matters
most here.

**Images.** Screenshots pasted into opencode become `[image omitted]`.

**Reasoning.** `reasoning_content` isn't populated, so the TUI shows no
thinking blocks.

## Streaming

MCP gives you `get_thread` polling, not a token stream, so how live the output
feels depends on whether your account exposes an agent's reply while it is
still being written. Two modes:

**Heartbeat mode (default, always on).** The stream opens instantly and sends
event-free keep-alive frames until the reply lands, then delivers it. opencode
shows a working spinner rather than a frozen screen, and long turns can't trip
a chunk timeout.

**Partial mode (opt-in).** The shim polls for text the agent has produced so far
and forwards it as real deltas. Whether that text exists mid-run is an empirical
question, so measure it:

```bash
python probe_streaming.py        # costs one agent run
```

It prints GROWS (partial text visible, real streaming works) or JUMPS (text only
appears at the end). If GROWS, switch it on:

```
set HYPERAGENT_PARTIAL_STREAM=1        # Windows
export HYPERAGENT_PARTIAL_STREAM=1     # mac/Linux
```

Partial mode never leaks a half-written tool call as prose: any reply that opens
like JSON is buffered and emitted as a proper tool call once complete.

## Files

| File | Purpose |
|---|---|
| `shim/server.py` | OpenAI-compatible HTTP server: `/v1/models`, `/v1/chat/completions`, `/health` |
| `shim/bridge.py` | message rendering, reply parsing, thread mapping, SSE chunking |
| `shim/hyperagent_client.py` | MCP client with OAuth sign-in and token cache |
| `hyperagent_code.py` | setup / run / serve launcher |
| `test_shim.py` | 29 checks against a fake backend |
| `test_streaming.py` | 13 checks for heartbeats, partial text, tool-call safety |
| `probe_streaming.py` | measures whether real streaming is possible on your account |
| `AGENT_SYSTEM_PROMPT.md` | system prompt for the Hyperagent agent |
| `fork-and-brand.sh` | optional: clone opencode as your own branded binary |
