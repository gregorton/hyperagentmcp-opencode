"""Tests for the streaming path: heartbeats, partial text, tool-call safety."""
import asyncio
import json
import threading
import time
from http.server import ThreadingHTTPServer

import httpx

from shim import server as S
from shim.bridge import ThreadMap

failures = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


class SlowAgent:
    """Takes `delay` seconds to answer; optionally reveals text progressively."""

    def __init__(self, final, delay=6.0, progressive=None):
        self.final, self.delay, self.progressive = final, delay, progressive
        self.started = None

    async def list_agents(self):
        return json.dumps([{"id": "a1", "name": "Slow"}])

    async def create_thread(self, agent_id, message):
        self.started = time.time()
        return "t1"

    async def send_message(self, thread_id, message):
        self.started = time.time()
        return "ok"

    async def wait_for_reply(self, thread_id, **kw):
        await asyncio.sleep(self.delay)
        return self.final

    async def peek_reply(self, thread_id):
        if not self.progressive:
            return True, None
        elapsed = time.time() - (self.started or time.time())
        idx = min(int(elapsed / 1.5) + 1, len(self.progressive))
        return True, "".join(self.progressive[:idx])


def boot(agent):
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    S.STATE.update(client=agent, loop=loop, threads=ThreadMap(), debug=False,
                   agents=[{"id": "a1", "name": "Slow", "description": ""}])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


MSGS = [{"role": "user", "content": "hi"}]


def frames(text):
    return [json.loads(l[6:]) for l in text.split("\n")
            if l.startswith("data: ") and l != "data: [DONE]"]


print("\n[1] heartbeats keep the stream alive while the agent thinks")
S.PARTIAL_STREAM = False
S.HEARTBEAT_SECONDS = 1.0
base, srv = boot(SlowAgent("All done thinking.", delay=5.0))

first_byte = {}
with httpx.stream("POST", f"{base}/v1/chat/completions",
                  json={"model": "a1", "messages": MSGS, "stream": True}, timeout=60) as r:
    t0 = time.time()
    collected = []
    for line in r.iter_lines():
        if line.startswith("data: "):
            if "first" not in first_byte:
                first_byte["first"] = time.time() - t0
            collected.append(line)
body = "\n".join(collected)
f = frames(body)
hb = [p for p in f if p["choices"][0]["delta"] == {} and p["choices"][0]["finish_reason"] is None]
check("first frame arrives fast (before the 5s reply)", first_byte["first"] < 2.0, f"{first_byte['first']:.2f}s")
check("heartbeat frames were sent while waiting", len(hb) >= 2, f"{len(hb)} heartbeats")
check("heartbeats carry no content (render nothing)",
      all(p["choices"][0]["delta"] == {} for p in hb))
text = "".join(p["choices"][0]["delta"].get("content", "") for p in f)
check("final text still delivered intact", text == "All done thinking.", text)
check("stream terminated properly", body.strip().endswith("data: [DONE]") or "[DONE]" in body)
srv.shutdown()

print("\n[2] partial streaming forwards text as it appears")
S.PARTIAL_STREAM = True
S.PARTIAL_POLL_SECONDS = 0.5
pieces = ["Reading the file. ", "Found the bug. ", "Here is the fix."]
base, srv = boot(SlowAgent("".join(pieces), delay=6.0, progressive=pieces))
arrival = []
with httpx.stream("POST", f"{base}/v1/chat/completions",
                  json={"model": "a1", "messages": MSGS, "stream": True}, timeout=60) as r:
    t0 = time.time()
    lines = []
    for line in r.iter_lines():
        if line.startswith("data: ") and line != "data: [DONE]":
            p = json.loads(line[6:])
            if p["choices"][0]["delta"].get("content"):
                arrival.append(time.time() - t0)
        lines.append(line)
f = frames("\n".join(lines))
text = "".join(p["choices"][0]["delta"].get("content", "") for p in f)
check("text arrived in multiple deltas over time", len(arrival) >= 2, f"{len(arrival)} content frames")
check("first text arrived before the reply finished", arrival and arrival[0] < 4.0,
      f"{arrival[0]:.2f}s" if arrival else "none")
check("assembled text is exact, no duplication", text == "".join(pieces), repr(text))
srv.shutdown()

print("\n[3] a tool call is never leaked as prose while streaming")
tool_json = json.dumps({"tool_calls": [{"name": "write", "arguments": {"filePath": "a.py", "content": "x"}}]})
partial_json = [tool_json[:20], tool_json[20:45], tool_json[45:]]
base, srv = boot(SlowAgent(tool_json, delay=5.0, progressive=partial_json))
r = httpx.post(f"{base}/v1/chat/completions",
               json={"model": "a1", "messages": MSGS, "stream": True}, timeout=60)
f = frames(r.text)
content = "".join(p["choices"][0]["delta"].get("content", "") for p in f)
tcs = [p["choices"][0]["delta"]["tool_calls"][0] for p in f if p["choices"][0]["delta"].get("tool_calls")]
check("no JSON leaked into content", content == "", repr(content[:80]))
check("emitted a proper tool_call instead", len(tcs) == 1 and tcs[0]["function"]["name"] == "write", str(tcs))
check("finish_reason is tool_calls", f[-1]["choices"][0]["finish_reason"] == "tool_calls")
check("usage still reported", bool(f[-1].get("usage")))
srv.shutdown()

print("\n" + ("ALL STREAMING CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
raise SystemExit(1 if failures else 0)
