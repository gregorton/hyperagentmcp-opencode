"""Boot the shim with a scripted fake Hyperagent backend, for driving real opencode.

The fake "agent" behaves like a well-behaved model following our protocol:
  - first turn: calls the write tool to create hello.py
  - after a tool result: replies with prose
It also dumps everything it receives to prompts.log so we can inspect exactly
what opencode sent through the shim.
"""
import asyncio
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shim import server as S
from shim.bridge import ThreadMap

LOG = Path(__file__).parent / "prompts.log"
LOG.write_text("")


class ScriptedAgent:
    def __init__(self):
        self.threads = {}
        self.turn = 0

    async def list_agents(self):
        return json.dumps([{"id": "test-agent", "name": "Test Coder", "description": "fake"}])

    async def create_thread(self, agent_id, message):
        self.turn += 1
        self._log("CREATE_THREAD", message)
        self.threads["t1"] = message
        return "t1"

    async def send_message(self, thread_id, message):
        self.turn += 1
        self._log("SEND_MESSAGE", message)
        self.threads[thread_id] = message
        return "ok"

    async def peek_reply(self, thread_id):
        return True, None  # pretend the server hides text until the turn ends

    async def wait_for_reply(self, thread_id, **kw):
        delay = float(os.environ.get("FAKE_DELAY", "0"))
        if delay:
            await asyncio.sleep(delay)
        last = self.threads.get(thread_id, "")
        if "TOOL RESULT" in last:
            reply = "Created hello.py with a greeting function. Task complete."
        else:
            # a real model reads the schema it was sent; ours mimics that
            reply = json.dumps({"tool_calls": [{
                "name": "write",
                "arguments": {"filePath": "/tmp/oc-project/hello.py",
                              "content": "def greet(name):\n    return f'Hello, {name}!'\n"},
            }]})
        self._log("REPLY", reply)
        return reply

    def _log(self, kind, text):
        with LOG.open("a") as f:
            f.write(f"\n{'=' * 30} {kind} (turn {self.turn}) {'=' * 30}\n{text}\n")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    S.STATE.update(client=ScriptedAgent(), loop=loop, threads=ThreadMap(), debug=True,
                   agents=[{"id": "test-agent", "name": "Test Coder", "description": ""}])
    srv = ThreadingHTTPServer(("127.0.0.1", port), S.Handler)
    print(f"fake shim listening on http://127.0.0.1:{port}/v1", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
