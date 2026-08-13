"""End-to-end test of request dedupe + local title answering.

Uses the same fake-backend harness style as test_shim.py: spin up the HTTP
server against a fake Hyperagent client that counts create_thread/send_message
calls and can delay replies, then drive it over real HTTP.
"""
import asyncio
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer

import httpx

from shim import server as S
from shim.bridge import ThreadMap


class FakeClient:
    """Stands in for HyperagentClient; counts calls, delays replies."""

    def __init__(self, reply="done", delay=0.0):
        self.reply = reply
        self.delay = delay
        self.threads = {}
        self.creates = 0
        self.sends = 0

    async def list_agents(self):
        return json.dumps([{"id": "agent_abc123", "name": "Local Coder", "description": "codes"}])

    async def create_thread(self, agent_id, message):
        self.creates += 1
        tid = f"thread_{self.creates}"
        self.threads[tid] = [message]
        return tid

    async def send_message(self, thread_id, message):
        self.sends += 1
        self.threads.setdefault(thread_id, []).append(message)
        return "queued"

    async def wait_for_reply(self, thread_id, **kw):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.reply


def boot():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    S.STATE.update(loop=loop, debug=False,
                   agents=[{"id": "agent_abc123", "name": "Local Coder", "description": ""}])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def reset(fake):
    S.STATE["client"] = fake
    S.STATE["threads"] = ThreadMap()
    S.STATE["pending"] = {}


failures = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def post(base, body, timeout=60):
    return httpx.post(f"{base}/v1/chat/completions", json=body, timeout=timeout)


def stream_text(resp):
    lines = [l for l in resp.text.split("\n") if l.startswith("data: ")]
    payloads = [json.loads(l[6:]) for l in lines if l != "data: [DONE]"]
    text = "".join(p["choices"][0]["delta"].get("content", "") for p in payloads)
    return text, lines


base, srv = boot()

MSGS1 = [{"role": "system", "content": "You are a coding agent."},
         {"role": "user", "content": "add a print to app.py"}]


print("\n[1] First-turn duplicate race (concurrent identical streaming POSTs)")
fake = FakeClient(reply="All set, print added.", delay=1.5)
reset(fake)
results = {}


def fire(idx):
    r = post(base, {"model": "agent_abc123", "messages": MSGS1, "stream": True}, timeout=60)
    results[idx] = r


t1 = threading.Thread(target=fire, args=(0,))
t2 = threading.Thread(target=fire, args=(1,))
t1.start()
t2.start()
t1.join()
t2.join()
check("exactly 1 create_thread", fake.creates == 1, f"creates={fake.creates}")
check("0 send_message", fake.sends == 0, f"sends={fake.sends}")
txt0, lines0 = stream_text(results[0])
txt1, lines1 = stream_text(results[1])
check("both responses carry final text", "All set, print added." in txt0 and "All set, print added." in txt1,
      f"a={txt0[:60]!r} b={txt1[:60]!r}")
check("both end with [DONE]", lines0[-1] == "data: [DONE]" and lines1[-1] == "data: [DONE]")


print("\n[2] Retry after completion (same body again → no new calls)")
fake = FakeClient(reply="Reply text here.", delay=0.0)
reset(fake)
r = post(base, {"model": "agent_abc123", "messages": MSGS1}, timeout=60)
check("first turn 200", r.status_code == 200, r.text[:200])
c_after_first, s_after_first = fake.creates, fake.sends
r2 = post(base, {"model": "agent_abc123", "messages": MSGS1}, timeout=60)
check("counters unchanged on retry",
      fake.creates == c_after_first and fake.sends == s_after_first,
      f"creates={fake.creates} sends={fake.sends}")
body2 = r2.json()
check("retry carries reply text", body2["choices"][0]["message"]["content"] == "Reply text here.",
      str(body2["choices"][0]["message"]))


print("\n[3] Second-turn duplicate (concurrent identical follow-ups)")
fake = FakeClient(reply="Turn two reply.", delay=1.5)
reset(fake)
# complete turn one normally
r = post(base, {"model": "agent_abc123", "messages": MSGS1}, timeout=60)
check("turn one 200", r.status_code == 200, r.text[:200])
c_t1, s_t1 = fake.creates, fake.sends
follow = MSGS1 + [
    {"role": "assistant", "content": "Reply text here."},
    {"role": "user", "content": "now also add a comment"},
]
res = {}


def fire2(idx):
    res[idx] = post(base, {"model": "agent_abc123", "messages": follow}, timeout=60)


f1 = threading.Thread(target=fire2, args=(0,))
f2 = threading.Thread(target=fire2, args=(1,))
f1.start()
f2.start()
f1.join()
f2.join()
check("send_message +1 total", fake.sends == s_t1 + 1, f"sends before={s_t1} after={fake.sends}")
check("create_thread unchanged", fake.creates == c_t1, f"creates before={c_t1} after={fake.creates}")
check("both follow-ups got the reply",
      res[0].json()["choices"][0]["message"]["content"] == "Turn two reply." and
      res[1].json()["choices"][0]["message"]["content"] == "Turn two reply.")


print("\n[4] Distinct conversations get distinct threads")
fake = FakeClient(reply="ok", delay=0.0)
reset(fake)
post(base, {"model": "agent_abc123", "messages": MSGS1}, timeout=60)
other = [{"role": "system", "content": "You are a coding agent."},
         {"role": "user", "content": "delete tmp.py"}]
post(base, {"model": "agent_abc123", "messages": other}, timeout=60)
check("create_thread == 2", fake.creates == 2, f"creates={fake.creates}")


TITLE_MSGS = [
    {"role": "system",
     "content": "You are a title generator. You output ONLY a thread title. "
                "Nothing else.\n<task>...</task>"},
    {"role": "user", "content": "Fix the login bug in auth.py\nsecond line ignored"},
]


print("\n[5] Local title (non-stream + stream), zero client calls")
fake = FakeClient(reply="SHOULD-NOT-BE-USED", delay=0.0)
reset(fake)
r = post(base, {"model": "agent_abc123", "messages": TITLE_MSGS}, timeout=60)
check("non-stream 200", r.status_code == 200, r.text[:200])
check("non-stream title content",
      r.json()["choices"][0]["message"]["content"] == "Fix the login bug in auth.py",
      str(r.json()["choices"][0]["message"]))
r = post(base, {"model": "agent_abc123", "messages": TITLE_MSGS, "stream": True}, timeout=60)
txt, lines = stream_text(r)
check("stream title content", txt == "Fix the login bug in auth.py", txt)
check("stream ends with [DONE]", lines[-1] == "data: [DONE]")
check("zero create_thread", fake.creates == 0, f"creates={fake.creates}")
check("zero send_message", fake.sends == 0, f"sends={fake.sends}")


print("\n[6] Opt-out via HYPERAGENT_LOCAL_TITLES=0 → client IS called")
prev = os.environ.get("HYPERAGENT_LOCAL_TITLES")
os.environ["HYPERAGENT_LOCAL_TITLES"] = "0"
try:
    fake = FakeClient(reply="agent-produced title", delay=0.0)
    reset(fake)
    r = post(base, {"model": "agent_abc123", "messages": TITLE_MSGS}, timeout=60)
    check("opt-out 200", r.status_code == 200, r.text[:200])
    check("client was called when opted out", fake.creates == 1, f"creates={fake.creates}")
finally:
    if prev is None:
        os.environ.pop("HYPERAGENT_LOCAL_TITLES", None)
    else:
        os.environ["HYPERAGENT_LOCAL_TITLES"] = prev


srv.shutdown()
print("\n" + ("ALL DEDUPE CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
raise SystemExit(1 if failures else 0)
