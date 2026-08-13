"""End-to-end test of the shim's HTTP surface using a fake Hyperagent backend.

Verifies the parts that don't need a real MCP sign-in: request translation,
tool-call round trip, SSE streaming shape, and thread reuse across turns.
"""
import asyncio
import json
import threading
from http.server import ThreadingHTTPServer

import httpx

from shim import server as S
from shim.bridge import ThreadMap


class FakeClient:
    """Stands in for HyperagentClient; scripts agent replies in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.threads = {}
        self.calls = []

    async def list_agents(self):
        return json.dumps([{"id": "agent_abc123", "name": "Local Coder", "description": "codes"}])

    async def create_thread(self, agent_id, message):
        tid = f"thread_{len(self.threads) + 1}"
        self.threads[tid] = [message]
        self.calls.append(("create_thread", agent_id, message))
        return tid

    async def send_message(self, thread_id, message):
        self.threads.setdefault(thread_id, []).append(message)
        self.calls.append(("send_message", thread_id, message))
        return "queued"

    async def wait_for_reply(self, thread_id, **kw):
        return self.replies.pop(0)


def boot(fake):
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    S.STATE.update(client=fake, loop=loop, threads=ThreadMap(), debug=False,
                   agents=[{"id": "agent_abc123", "name": "Local Coder", "description": ""}])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


TOOLS = [{
    "type": "function",
    "function": {
        "name": "edit",
        "description": "Edit a file",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]},
    },
}]

failures = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


fake = FakeClient([
    # turn 1: agent asks for a tool call (fenced, to test tolerant parsing)
    '```json\n{"tool_calls":[{"name":"edit","arguments":{"path":"app.py","content":"print(1)"}}]}\n```',
    # turn 2: after tool result, prose answer
    "Added the print statement to app.py. The file now runs cleanly.",
])
base, srv = boot(fake)

print("\n[1] GET /v1/models")
r = httpx.get(f"{base}/v1/models", timeout=30)
models = r.json()
check("200 + model list", r.status_code == 200 and models["data"][0]["id"] == "agent_abc123", r.text[:200])

print("\n[2] GET /health")
check("health ok", httpx.get(f"{base}/health", timeout=30).json()["status"] == "ok")

print("\n[3] POST /v1/chat/completions (non-streaming, expects tool_calls)")
msgs = [{"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "add a print to app.py"}]
r = httpx.post(f"{base}/v1/chat/completions",
               json={"model": "agent_abc123", "messages": msgs, "tools": TOOLS}, timeout=60)
body = r.json()
choice = body["choices"][0]
tc = choice["message"].get("tool_calls") or []
check("status 200", r.status_code == 200, r.text[:300])
check("finish_reason == tool_calls", choice["finish_reason"] == "tool_calls", str(choice))
check("one tool call named edit", len(tc) == 1 and tc[0]["function"]["name"] == "edit", str(tc))
check("arguments are a JSON string", isinstance(tc[0]["function"]["arguments"], str))
args = json.loads(tc[0]["function"]["arguments"])
check("arguments parse to the right dict", args == {"path": "app.py", "content": "print(1)"}, str(args))
check("content is null when calling tools", choice["message"]["content"] is None)
check("tool schema was sent to agent", "edit" in fake.calls[0][2] and "OUTPUT PROTOCOL" in fake.calls[0][2])
check("system prompt forwarded", "You are a coding agent." in fake.calls[0][2])

print("\n[4] POST again with tool result appended (thread reuse + prose reply)")
msgs2 = msgs + [
    {"role": "assistant", "content": None, "tool_calls": tc},
    {"role": "tool", "tool_call_id": tc[0]["id"], "content": "file written"},
]
r = httpx.post(f"{base}/v1/chat/completions",
               json={"model": "agent_abc123", "messages": msgs2, "tools": TOOLS, "stream": True},
               timeout=60)
check("stream status 200", r.status_code == 200)
check("SSE content-type", r.headers["content-type"].startswith("text/event-stream"), r.headers.get("content-type", ""))

lines = [l for l in r.text.split("\n") if l.startswith("data: ")]
check("ends with [DONE]", lines[-1] == "data: [DONE]")
payloads = [json.loads(l[6:]) for l in lines if l != "data: [DONE]"]
check("first chunk sets role", payloads[0]["choices"][0]["delta"].get("role") == "assistant")
text = "".join(p["choices"][0]["delta"].get("content", "") for p in payloads)
check("streamed text reassembles", "Added the print statement" in text, text[:120])
check("final chunk finish_reason=stop", payloads[-1]["choices"][0]["finish_reason"] == "stop")
check("chunk object type correct", payloads[0]["object"] == "chat.completion.chunk")

reused = [c for c in fake.calls if c[0] == "send_message"]
created = [c for c in fake.calls if c[0] == "create_thread"]
check("reused existing thread (no 2nd create)", len(created) == 1 and len(reused) == 1,
      f"created={len(created)} sent={len(reused)}")
check("delta only carried new messages", "TOOL RESULT" in reused[0][2] and "You are a coding agent." not in reused[0][2])

print("\n[5] opencode's strict event schema (from packages/llm/src/protocols/openai-chat.ts)")
# Every SSE event must decode as {choices: [{delta?, finish_reason?}], usage?}
schema_ok, tool_delta_ok, usage_ok = True, True, False
for p in payloads:
    if "choices" not in p or not isinstance(p["choices"], list) or not p["choices"]:
        schema_ok = False
    ch = p["choices"][0]
    if "delta" not in ch or "finish_reason" not in ch:
        schema_ok = False
    if u := p.get("usage"):
        usage_ok = isinstance(u.get("total_tokens"), int)
check("every chunk matches OpenAIChatEvent shape", schema_ok)
check("final chunk carries usage (include_usage)", usage_ok, str(payloads[-1]))

fake2 = FakeClient(['{"tool_calls":[{"name":"edit","arguments":{"path":"x","content":"y"}}]}'])
S.STATE.update(client=fake2, threads=ThreadMap())
r = httpx.post(f"{base}/v1/chat/completions",
               json={"model": "agent_abc123", "messages": msgs, "tools": TOOLS,
                     "tool_choice": "required", "stream": True,
                     "stream_options": {"include_usage": True}}, timeout=60)
tool_payloads = [json.loads(l[6:]) for l in r.text.split("\n")
                 if l.startswith("data: ") and l != "data: [DONE]"]
deltas = [p["choices"][0]["delta"] for p in tool_payloads]
tc_delta = next((d["tool_calls"][0] for d in deltas if d.get("tool_calls")), None)
check("tool_call delta has required int index", isinstance(tc_delta and tc_delta.get("index"), int), str(tc_delta))
check("tool_call delta carries id and name", bool(tc_delta and tc_delta.get("id") and tc_delta["function"]["name"]))
check("arguments delta is a string", isinstance(tc_delta["function"]["arguments"], str))
check("finish_reason maps to tool_calls", tool_payloads[-1]["choices"][0]["finish_reason"] == "tool_calls")
check("tool_choice=required forwarded to agent", "MUST call at least one tool" in fake2.calls[0][2])

print("\n[6] parser robustness")
from shim.bridge import parse_reply
check("bare JSON object", parse_reply('{"tool_calls":[{"name":"read","arguments":{"path":"a"}}]}').tool_calls[0]["function"]["name"] == "read")
check("prose stays prose", parse_reply("Just explaining things here.").content == "Just explaining things here.")
check("prose mentioning tool_calls word isn't hijacked", parse_reply("I considered tool_calls but chose not to.").finish_reason == "stop")
check("single call object form", parse_reply('{"name":"bash","arguments":{"cmd":"ls"}}').content is not None)

srv.shutdown()
print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
raise SystemExit(1 if failures else 0)
