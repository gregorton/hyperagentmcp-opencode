"""MCP client for Hyperagent's hosted MCP server.

Handles the OAuth browser sign-in (one time; tokens cached on disk) and wraps
the six Hyperagent tools: list_agents, create_thread, send_message, get_thread,
list_threads, create_attachment_upload.

Built against MCP Python SDK v2 (pip install "mcp>=2").
"""

from __future__ import annotations

import asyncio
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# MCP SDK v2 is built on httpx2, and its OAuthClientProvider subclasses
# httpx2.Auth. Handing that to a plain httpx client raises
# 'TypeError: Invalid "auth" argument', so match the SDK's own HTTP library.
try:
    import httpx2 as httpx_lib  # MCP SDK v2 (mcp>=2)
except ImportError:  # pragma: no cover - fallback for MCP SDK v1
    import httpx as httpx_lib

from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

SERVER_URL = "https://hyperagent.com/api/mcp"
CALLBACK_PORT = 3117
CONFIG_DIR = Path.home() / ".hyperagent-harness"


class FileTokenStorage(TokenStorage):
    """Persist OAuth tokens so sign-in happens once, not every run."""

    def __init__(self, path: Path = CONFIG_DIR / "tokens.json"):
        self.path = path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))
        self.path.chmod(0o600)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._load().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json")
        self._save(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._load()
        data["client_info"] = client_info.model_dump(mode="json")
        self._save(data)


class _CallbackServer:
    """Tiny localhost HTTP server that catches the OAuth redirect."""

    def __init__(self, port: int = CALLBACK_PORT):
        self.port = port
        self.data: dict[str, Any] = {"code": None, "state": None, "iss": None, "error": None}
        self._event = threading.Event()
        data, event = self.data, self._event

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                q = parse_qs(urlparse(self.path).query)
                if "code" in q:
                    data.update(code=q["code"][0], state=q.get("state", [None])[0], iss=q.get("iss", [None])[0])
                    self.send_response(200)
                    self.send_header("Content-type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><body><h2>Signed in.</h2>You can close this tab.</body></html>")
                    event.set()
                elif "error" in q:
                    data["error"] = q["error"][0]
                    self.send_response(400)
                    self.end_headers()
                    event.set()
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("localhost", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait(self, timeout: float = 300) -> None:
        if not self._event.wait(timeout):
            raise TimeoutError("timed out waiting for OAuth callback")
        if self.data["error"]:
            raise RuntimeError(f"OAuth error: {self.data['error']}")

    def stop(self) -> None:
        self._server.shutdown()


def _build_auth() -> OAuthClientProvider:
    callback_server = _CallbackServer()

    async def callback_handler() -> AuthorizationCodeResult:
        try:
            await asyncio.to_thread(callback_server.wait)
            d = callback_server.data
            return AuthorizationCodeResult(code=d["code"], state=d["state"], iss=d["iss"])
        finally:
            callback_server.stop()

    async def redirect_handler(authorization_url: str) -> None:
        print(f"\nOpening browser to sign in to Hyperagent:\n  {authorization_url}\n")
        callback_server.start()
        webbrowser.open(authorization_url)

    return OAuthClientProvider(
        # Must be the FULL MCP URL. The SDK derives the expected OAuth resource
        # from this and compares it to the server's advertised resource; a
        # trimmed origin fails with "Protected resource ... does not match".
        server_url=SERVER_URL,
        client_metadata=OAuthClientMetadata.model_validate({
            "client_name": "Hyperagent Local Harness",
            "redirect_uris": [f"http://localhost:{CALLBACK_PORT}/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }),
        storage=FileTokenStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


def _result_text(result: Any) -> str:
    """Flatten a call_tool result into text (prefers structured content)."""
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if structured:
        return json.dumps(structured)
    parts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


class HyperagentClient:
    """Async context manager exposing Hyperagent's MCP tools."""

    def __init__(self, debug: bool = False):
        self.debug = debug
        self._session: ClientSession | None = None
        self._stack: list = []

    async def __aenter__(self) -> "HyperagentClient":
        auth = _build_auth()
        self._http = httpx_lib.AsyncClient(auth=auth, follow_redirects=True, timeout=60.0)
        await self._http.__aenter__()
        self._transport = streamable_http_client(url=SERVER_URL, http_client=self._http)
        read, write = await self._transport.__aenter__()
        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._session_cm.__aexit__(*exc)
        await self._transport.__aexit__(*exc)
        await self._http.__aexit__(*exc)

    async def call(self, tool: str, args: dict | None = None) -> str:
        assert self._session is not None
        result = await self._session.call_tool(tool, args or {})
        text = _result_text(result)
        if self.debug:
            print(f"[debug] {tool}({json.dumps(args or {})[:200]}) ->\n{text[:2000]}\n")
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            raise RuntimeError(f"{tool} failed: {text[:500]}")
        return text

    async def list_tool_names(self) -> list[str]:
        assert self._session is not None
        result = await self._session.list_tools()
        return [t.name for t in result.tools]

    # -- Hyperagent-specific helpers ------------------------------------

    async def list_agents(self) -> str:
        return await self.call("list_agents")

    async def create_thread(self, agent_id: str, message: str) -> str:
        """Returns the new threadId (extracted from the tool result)."""
        text = await self.call("create_thread", {"agentId": agent_id, "message": message})
        return _extract_field(text, "threadId") or text.strip()

    async def send_message(self, thread_id: str, message: str) -> str:
        return await self.call("send_message", {"threadId": thread_id, "message": message})

    async def get_thread(self, thread_id: str) -> dict:
        text = await self.call("get_thread", {"threadId": thread_id})
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    async def peek_reply(self, thread_id: str) -> tuple[bool, str | None]:
        """One poll: (still_running, assistant_text_so_far).

        Whether text is visible before the turn finishes depends on the server.
        probe_streaming.py answers that for your account.
        """
        thread = await self.get_thread(thread_id)
        return _is_running(thread), _last_agent_message(thread)

    async def wait_for_reply(self, thread_id: str, poll_seconds: float = 5.0, timeout: float = 900.0) -> str:
        """Poll get_thread until the agent's turn finishes; return its last message."""
        waited = 0.0
        while waited < timeout:
            thread = await self.get_thread(thread_id)
            if not _is_running(thread):
                reply = _last_agent_message(thread)
                if reply:
                    return reply
            await asyncio.sleep(poll_seconds)
            waited += poll_seconds
            poll_seconds = min(poll_seconds * 1.3, 20.0)
        raise TimeoutError(f"thread {thread_id} still running after {timeout}s")


def _extract_field(text: str, field: str) -> str | None:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        import re
        m = re.search(rf'"{field}"\s*:\s*"([^"]+)"', text)
        return m.group(1) if m else None
    if isinstance(obj, dict):
        return obj.get(field)
    return None


def _is_running(thread: dict) -> bool:
    for key in ("running", "isRunning", "is_running"):
        if key in thread:
            return bool(thread[key])
    status = str(thread.get("status", "")).lower()
    return status in ("running", "in_progress", "pending")


def _last_agent_message(thread: dict) -> str | None:
    messages = thread.get("messages") or []
    for msg in reversed(messages):
        role = str(msg.get("role", "")).lower()
        if role in ("assistant", "agent"):
            content = msg.get("content") or msg.get("text") or ""
            if isinstance(content, list):
                content = "\n".join(
                    c.get("text", "") if isinstance(c, dict) else str(c) for c in content
                )
            if content:
                return content
    if "raw" in thread:
        return thread["raw"]
    return None
