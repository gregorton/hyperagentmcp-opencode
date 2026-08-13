"""Tests for persistent ThreadMap: disk round trips, corruption tolerance,
eviction, forget_thread, and restart continuity over real HTTP.

Homegrown check style like test_dedupe.py: PASS/FAIL lines, exit 1 on failure,
final "ALL PERSISTENCE CHECKS PASSED" line on success.
"""
import asyncio
import json
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

from shim import server as S
from shim.bridge import ThreadMap


failures = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


CONVO_A = [{"role": "system", "content": "You are a coding agent."},
           {"role": "user", "content": "add a print to app.py"}]
CONVO_B = [{"role": "system", "content": "You are a coding agent."},
           {"role": "user", "content": "delete tmp.py"}]


print("\n[1] Round trip: record then reload finds mappings and computes deltas")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "threads.json"
    tm = ThreadMap(path=p)
    tm.record(CONVO_A, "tA")
    tm.record(CONVO_B, "tB")
    tm2 = ThreadMap(path=p)
    check("reload finds convo A", tm2.lookup_exact(CONVO_A) == "tA", tm2.lookup_exact(CONVO_A))
    check("reload finds convo B", tm2.lookup_exact(CONVO_B) == "tB", tm2.lookup_exact(CONVO_B))
    extra = {"role": "user", "content": "one more thing"}
    tid, new = tm2.lookup(CONVO_A + [extra])
    check("lookup returns thread + delta", tid == "tA" and new == [extra], f"tid={tid} new={new}")


print("\n[2] Missing file: starts empty, no exception")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "does_not_exist.json"
    try:
        tm = ThreadMap(path=p)
        check("missing file starts empty", tm.lookup_exact(CONVO_A) is None)
    except Exception as e:
        check("missing file no exception", False, repr(e))


print("\n[3] Corrupt file: starts empty; recording rewrites a valid file")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "threads.json"
    p.write_bytes(b"not json{{")
    try:
        tm = ThreadMap(path=p)
        check("corrupt file starts empty", tm.lookup_exact(CONVO_A) is None)
    except Exception as e:
        check("corrupt file no exception", False, repr(e))
        tm = None
    if tm is not None:
        tm.record(CONVO_A, "tA")
        tm3 = ThreadMap(path=p)
        check("rewritten file reloads", tm3.lookup_exact(CONVO_A) == "tA", tm3.lookup_exact(CONVO_A))


print("\n[4] Eviction persists across reload")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "threads.json"
    tm = ThreadMap(max_entries=3, path=p)
    convos = [[{"role": "user", "content": f"msg {i}"}] for i in range(4)]
    for i, c in enumerate(convos):
        tm.record(c, f"t{i}")
    tm2 = ThreadMap(max_entries=3, path=p)
    check("oldest evicted after reload", tm2.lookup_exact(convos[0]) is None, tm2.lookup_exact(convos[0]))
    check("newest three present after reload",
          all(tm2.lookup_exact(convos[i]) == f"t{i}" for i in (1, 2, 3)),
          [tm2.lookup_exact(convos[i]) for i in (1, 2, 3)])


print("\n[5] forget_thread removes only that thread's mappings, persists")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "threads.json"
    tm = ThreadMap(path=p)
    c1 = [{"role": "user", "content": "a"}]
    c2 = [{"role": "user", "content": "b"}]
    c3 = [{"role": "user", "content": "c"}]
    tm.record(c1, "t1")
    tm.record(c2, "t1")
    tm.record(c3, "t2")
    tm.forget_thread("t1")
    tm2 = ThreadMap(path=p)
    check("t1 mapping 1 gone", tm2.lookup_exact(c1) is None, tm2.lookup_exact(c1))
    check("t1 mapping 2 gone", tm2.lookup_exact(c2) is None, tm2.lookup_exact(c2))
    check("t2 mapping remains", tm2.lookup_exact(c3) == "t2", tm2.lookup_exact(c3))


# --------------------------------------------------------------------------
# [6] Restart simulation over HTTP, reusing the FakeClient harness pattern.
# --------------------------------------------------------------------------

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


def post(base, body, timeout=60):
    return httpx.post(f"{base}/v1/chat/completions", json=body, timeout=timeout)


base, srv = boot()

print("\n[6] Restart simulation over HTTP: persisted map continues thread")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "threads.json"
    fake = FakeClient(reply="First turn done.", delay=0.0)
    S.STATE["client"] = fake
    S.STATE["threads"] = ThreadMap(path=p)
    S.STATE["pending"] = {}

    r = post(base, {"model": "agent_abc123", "messages": CONVO_A}, timeout=60)
    check("first turn 200", r.status_code == 200, r.text[:200])
    c_after, s_after = fake.creates, fake.sends
    check("first turn created a thread", c_after == 1, f"creates={c_after}")

    # Simulate restart: fresh ThreadMap from disk, clear in-memory pending.
    S.STATE["threads"] = ThreadMap(path=p)
    S.STATE["pending"] = {}

    follow = CONVO_A + [
        {"role": "assistant", "content": "First turn done."},
        {"role": "user", "content": "now also add a comment"},
    ]
    r = post(base, {"model": "agent_abc123", "messages": follow}, timeout=60)
    check("follow-up 200 after restart", r.status_code == 200, r.text[:200])
    check("create_thread unchanged after restart", fake.creates == c_after,
          f"creates before={c_after} after={fake.creates}")
    check("send_message +1 (delta not replay)", fake.sends == s_after + 1,
          f"sends before={s_after} after={fake.sends}")

    c_after2, s_after2 = fake.creates, fake.sends
    # Re-post the exact completed follow-up conversation -> rewait, no new calls.
    r = post(base, {"model": "agent_abc123", "messages": follow}, timeout=60)
    check("repost 200", r.status_code == 200, r.text[:200])
    check("counters unchanged on repost (rewait)",
          fake.creates == c_after2 and fake.sends == s_after2,
          f"creates={fake.creates} sends={fake.sends}")


srv.shutdown()
print("\n" + ("ALL PERSISTENCE CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
raise SystemExit(1 if failures else 0)
