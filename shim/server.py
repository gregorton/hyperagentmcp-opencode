"""OpenAI-compatible HTTP server backed by Hyperagent MCP.

Run this, point opencode at http://127.0.0.1:8787/v1, and opencode's own CLI,
TUI, tools and permission prompts all work unmodified — every model call is
served by one of your Hyperagent agents.

    python -m shim.server --port 8787

Endpoints:
    GET  /v1/models            -> your Hyperagent agents, as selectable models
    POST /v1/chat/completions  -> streamed or buffered completion
    GET  /health               -> readiness probe
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from concurrent.futures import TimeoutError as FutureTimeout

from .bridge import (
    SSEWriter, ThreadMap, build_delta, build_opening, completion_response,
    estimate_usage, looks_like_tool_json, parse_reply, stream_chunks,
)
from .hyperagent_client import HyperagentClient

STATE: dict = {"client": None, "loop": None, "agents": [], "threads": ThreadMap(), "debug": False}

# A turn is a full agent run, so keep the HTTP connection warm while waiting.
HEARTBEAT_SECONDS = 5.0
PARTIAL_POLL_SECONDS = 1.5
# Opt-in: only useful if your account exposes reply text mid-run.
# Find out with probe_streaming.py.
PARTIAL_STREAM = os.environ.get("HYPERAGENT_PARTIAL_STREAM", "").lower() in ("1", "true", "yes")


def run_async(coro, timeout: float = 1800.0):
    """Run a coroutine on the shared MCP event loop from a request thread."""
    loop = STATE["loop"]
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", (name or "agent").lower()).strip("-") or "agent"


def load_agents() -> list[dict]:
    """Fetch the user's agents and present each as a model."""
    raw = run_async(STATE["client"].list_agents(), timeout=120)
    agents: list[dict] = []
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("agents") or data.get("results") or []
        for a in data:
            if isinstance(a, dict) and a.get("id"):
                agents.append({"id": a["id"], "name": a.get("name") or a["id"],
                               "description": a.get("description", "")})
    except json.JSONDecodeError:
        for m in re.finditer(r'"id"\s*:\s*"([^"]+)"[^}]*?"name"\s*:\s*"([^"]*)"', raw):
            agents.append({"id": m.group(1), "name": m.group(2), "description": ""})
    if not agents:
        print("WARNING: no agents parsed from list_agents. Raw response:\n" + raw[:1500],
              file=sys.stderr)
    return agents


def resolve_agent_id(model: str) -> str:
    """opencode sends a model id; map it to a Hyperagent agent id."""
    for a in STATE["agents"]:
        if model == a["id"] or model == _slugify(a["name"]):
            return a["id"]
    if STATE["agents"]:
        return STATE["agents"][0]["id"]
    raise RuntimeError(f"no Hyperagent agent matches model {model!r}")


def begin_completion(body: dict) -> dict:
    """Fast half: pick the agent, start or continue the thread. No waiting."""
    messages = body.get("messages") or []
    model = body.get("model") or "hyperagent"
    agent_id = resolve_agent_id(model)
    client, threads = STATE["client"], STATE["threads"]

    thread_id, new_messages = threads.lookup(messages)

    if thread_id is None:
        prompt = build_opening(messages, body.get("tools"), body.get("tool_choice"))
        if STATE["debug"]:
            print(f"\n[shim] NEW thread on {agent_id}, prompt {len(prompt)} chars", file=sys.stderr)
        thread_id = run_async(client.create_thread(agent_id, prompt))
    else:
        prompt = build_delta(new_messages, body.get("tool_choice"))
        if STATE["debug"]:
            print(f"\n[shim] reuse thread {thread_id}, delta {len(new_messages)} msg(s)", file=sys.stderr)
        run_async(client.send_message(thread_id, prompt))

    return {"thread_id": thread_id, "messages": messages, "model": model, "prompt": prompt}


def finalize_completion(ctx: dict, reply: str) -> tuple:
    """Slow half's aftermath: parse, remember the turn, estimate usage."""
    threads = STATE["threads"]
    messages, thread_id = ctx["messages"], ctx["thread_id"]
    threads.record(messages, thread_id)

    parsed = parse_reply(reply)
    # remember the assistant turn too, so the next request finds this prefix
    assistant_msg: dict = {"role": "assistant", "content": parsed.content}
    if parsed.tool_calls:
        assistant_msg = {"role": "assistant", "content": None, "tool_calls": parsed.tool_calls}
    threads.record(messages + [assistant_msg], thread_id)

    if STATE["debug"]:
        print(f"[shim] reply: {len(parsed.tool_calls)} tool_call(s), "
              f"{len(parsed.content or '')} chars prose", file=sys.stderr)
    return parsed, estimate_usage(len(ctx["prompt"]), parsed)


def handle_completion(body: dict) -> tuple:
    """Blocking round trip, for the non-streaming path."""
    ctx = begin_completion(body)
    reply = run_async(STATE["client"].wait_for_reply(ctx["thread_id"]))
    parsed, usage = finalize_completion(ctx, reply)
    return parsed, ctx["model"], usage


def stream_completion(body: dict):
    """Yield SSE frames, starting immediately so opencode never looks hung.

    A Hyperagent turn is a whole agent run, so the reply can be many seconds
    away. Heartbeat frames (schema-valid, event-free) hold the connection open
    meanwhile. With HYPERAGENT_PARTIAL_STREAM=1 the shim also polls for text
    the agent has produced so far and forwards it as real deltas.
    """
    ctx = begin_completion(body)
    writer = SSEWriter(ctx["model"])
    client, thread_id = STATE["client"], ctx["thread_id"]

    yield writer.role()

    future = asyncio.run_coroutine_threadsafe(client.wait_for_reply(thread_id), STATE["loop"])
    streamed = ""
    reply = None

    while reply is None:
        try:
            reply = future.result(timeout=PARTIAL_POLL_SECONDS if PARTIAL_STREAM else HEARTBEAT_SECONDS)
        except FutureTimeout:
            if PARTIAL_STREAM:
                try:
                    _running, text = run_async(client.peek_reply(thread_id), timeout=30)
                except Exception:
                    text = None
                # never leak a half-written tool call as prose
                if text and not looks_like_tool_json(text) and len(text) > len(streamed):
                    delta, streamed = text[len(streamed):], text
                    yield writer.text(delta)
                    continue
            yield writer.heartbeat()

    parsed, usage = finalize_completion(ctx, reply)

    if parsed.tool_calls:
        for i, call in enumerate(parsed.tool_calls):
            yield writer.tool_call(i, call)
    elif parsed.content:
        final = parsed.content
        remainder = final[len(streamed):] if final.startswith(streamed) else final
        for i in range(0, len(remainder), 180):
            yield writer.text(remainder[i:i + 180])

    yield writer.finish(parsed.finish_reason, usage)
    yield writer.done()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, obj: dict, status: int = 200) -> None:
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, message: str, status: int = 500) -> None:
        self._send_json({"error": {"message": message, "type": "shim_error"}}, status)

    def _path(self) -> str:
        p = self.path.split("?")[0].rstrip("/")
        return p[3:] if p.startswith("/v1") else p

    def do_GET(self) -> None:
        path = self._path()
        if path in ("/health", ""):
            self._send_json({"status": "ok", "agents": len(STATE["agents"])})
        elif path == "/models":
            self._send_json({
                "object": "list",
                "data": [{"id": a["id"], "object": "model", "owned_by": "hyperagent",
                          "created": 0, "name": a["name"]} for a in STATE["agents"]],
            })
        else:
            self._error(f"unknown path {self.path}", 404)

    def do_POST(self) -> None:
        if self._path() != "/chat/completions":
            return self._error(f"unknown path {self.path}", 404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as e:
            return self._error(f"bad request body: {e}", 400)

        if body.get("stream"):
            # Headers go out before the agent replies, so the client sees life
            # immediately. Errors after this point can only be logged.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for chunk in stream_completion(body):
                    data = chunk.encode()
                    self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except BrokenPipeError:
                pass
            except Exception:
                traceback.print_exc()
                try:  # surface the failure inside the stream rather than hanging
                    w = SSEWriter(body.get("model", "hyperagent"))
                    for frame in (w.text("\n[shim error: see console]"), w.finish("stop"), w.done()):
                        data = frame.encode()
                        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except Exception:
                    pass
        else:
            try:
                parsed, model, _usage = handle_completion(body)
            except Exception as e:
                traceback.print_exc()
                return self._error(f"{type(e).__name__}: {e}")
            self._send_json(completion_response(model, parsed))

    def log_message(self, fmt: str, *args) -> None:
        if STATE["debug"]:
            sys.stderr.write("[http] " + fmt % args + "\n")


def start_mcp_loop(debug: bool) -> None:
    """Bring up the MCP session on a dedicated event loop thread."""
    ready = threading.Event()
    error: list[BaseException] = []

    def runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        STATE["loop"] = loop

        async def boot() -> None:
            client = HyperagentClient(debug=debug)
            await client.__aenter__()
            STATE["client"] = client
            ready.set()
            while True:            # keep the session alive
                await asyncio.sleep(3600)

        try:
            loop.run_until_complete(boot())
        except BaseException as e:  # noqa: BLE001
            error.append(e)
            ready.set()

    threading.Thread(target=runner, daemon=True).start()
    ready.wait(timeout=600)
    if error:
        raise error[0]
    if STATE["client"] is None:
        raise RuntimeError("timed out connecting to Hyperagent MCP")


def main() -> None:
    ap = argparse.ArgumentParser(description="Hyperagent -> OpenAI-compatible shim for opencode")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    STATE["debug"] = args.debug

    print("Connecting to Hyperagent MCP (a browser may open for sign-in)...")
    start_mcp_loop(args.debug)
    STATE["agents"] = load_agents()

    print(f"\nConnected. {len(STATE['agents'])} agent(s) available as models:")
    for a in STATE["agents"]:
        print(f"  - {a['id']}   ({a['name']})")
    print(f"\nServing OpenAI-compatible API at http://{args.host}:{args.port}/v1")
    print("Point opencode's provider baseURL there, then run: opencode\n")

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
