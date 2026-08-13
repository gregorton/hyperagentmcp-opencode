"""Translation layer: OpenAI chat-completions <-> Hyperagent MCP threads.

opencode (via @ai-sdk/openai-compatible) speaks chat completions: it sends the
whole conversation plus tool schemas, and expects either prose or tool_calls
back. Hyperagent speaks threads: create_thread / send_message / get_thread.

This module does the impedance matching:
  * renders OpenAI messages + tool schemas into a self-contained prompt
  * reuses a Hyperagent thread across turns of the same conversation, sending
    only the new messages (opencode resends full history every request)
  * parses the agent's reply back into OpenAI-shaped tool_calls or content
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field

PROTOCOL = """\
=== OUTPUT PROTOCOL (read carefully) ===
You are acting as the model behind a coding CLI. The CLI executes tools on the
user's machine; you never execute anything yourself and you have no filesystem
of your own in this role.

Reply with EXACTLY ONE of the following:

1. To use tools, output a single JSON object and nothing else:
   {"tool_calls": [{"name": "<tool name>", "arguments": {<args matching its schema>}}]}
   You may include several entries to run tools in parallel. Argument values
   must satisfy the tool's JSON schema exactly.

2. To answer the user or explain, output plain prose with no JSON wrapper.

Never mix prose and the JSON object in one reply. Never invent a tool that is
not listed. Never wrap the JSON in explanation. Read files before editing them.
"""


def _stringify_content(content) -> str:
    """OpenAI content can be a string or a list of typed parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(p.get("text", ""))
                elif p.get("type") == "image_url":
                    parts.append("[image omitted]")
                else:
                    parts.append(json.dumps(p))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    return str(content)


def render_tools(tools: list[dict] | None) -> str:
    if not tools:
        return ""
    lines = ["=== AVAILABLE TOOLS ===",
             "Each tool is given as name, description, and JSON schema for its arguments.", ""]
    for t in tools:
        fn = t.get("function", t)
        lines.append(f"- {fn.get('name')}: {fn.get('description', '').strip()}")
        params = fn.get("parameters")
        if params:
            lines.append(f"  schema: {json.dumps(params)}")
    return "\n".join(lines)


def render_message(msg: dict) -> str:
    """Render one OpenAI message as a labeled block for the agent."""
    role = msg.get("role")
    if role == "system":
        return f"=== SYSTEM INSTRUCTIONS FROM THE CLI ===\n{_stringify_content(msg.get('content'))}"
    if role == "user":
        return f"=== USER ===\n{_stringify_content(msg.get('content'))}"
    if role == "assistant":
        calls = msg.get("tool_calls")
        if calls:
            rendered = [
                {"id": c.get("id"), "name": c.get("function", {}).get("name"),
                 "arguments": c.get("function", {}).get("arguments")}
                for c in calls
            ]
            return f"=== YOUR PREVIOUS TOOL CALLS ===\n{json.dumps(rendered, indent=1)}"
        return f"=== YOUR PREVIOUS REPLY ===\n{_stringify_content(msg.get('content'))}"
    if role == "tool":
        return (f"=== TOOL RESULT (call_id: {msg.get('tool_call_id')}) ===\n"
                f"{_stringify_content(msg.get('content'))}")
    return f"=== {str(role).upper()} ===\n{_stringify_content(msg.get('content'))}"


def render_tool_choice(tool_choice) -> str:
    """opencode may force, forbid, or pin a tool call. Tell the agent."""
    if not tool_choice or tool_choice == "auto":
        return ""
    if tool_choice == "required":
        return "=== CONSTRAINT ===\nYou MUST call at least one tool this turn. Prose is not accepted."
    if tool_choice == "none":
        return "=== CONSTRAINT ===\nDo NOT call any tool this turn. Reply with prose only."
    if isinstance(tool_choice, dict):
        name = tool_choice.get("function", {}).get("name")
        if name:
            return f"=== CONSTRAINT ===\nYou MUST call the tool {name!r} this turn, and no other."
    return ""


def build_opening(messages: list[dict], tools: list[dict] | None, tool_choice=None) -> str:
    """First turn of a conversation: system prompt + tools + protocol + history."""
    blocks = [render_message(m) for m in messages if m.get("role") == "system"]
    tool_block = render_tools(tools)
    if tool_block:
        blocks.append(tool_block)
    blocks.append(PROTOCOL)
    blocks += [render_message(m) for m in messages if m.get("role") != "system"]
    blocks.append(render_tool_choice(tool_choice))
    blocks.append("Respond now, following the OUTPUT PROTOCOL exactly.")
    return "\n\n".join(b for b in blocks if b.strip())


def build_delta(new_messages: list[dict], tool_choice=None) -> str:
    blocks = [render_message(m) for m in new_messages if m.get("role") != "system"]
    blocks.append(render_tool_choice(tool_choice))
    blocks.append("Respond now, following the OUTPUT PROTOCOL exactly.")
    return "\n\n".join(b for b in blocks if b.strip())


# --------------------------------------------------------------------------
# Reply parsing
# --------------------------------------------------------------------------

def _find_json_object(text: str) -> dict | None:
    """Pull the first complete JSON object out of a reply, fenced or bare."""
    candidates: list[str] = []
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S):
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())
    decoder = json.JSONDecoder()
    for cand in candidates:
        start = cand.find("{")
        if start == -1:
            continue
        try:
            obj, _ = decoder.raw_decode(cand[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


@dataclass
class ParsedReply:
    content: str | None = None
    tool_calls: list[dict] = field(default_factory=list)  # OpenAI shape

    @property
    def finish_reason(self) -> str:
        return "tool_calls" if self.tool_calls else "stop"


def parse_reply(text: str) -> ParsedReply:
    """Turn the agent's reply into OpenAI-shaped content or tool_calls."""
    obj = _find_json_object(text) if "tool_calls" in text else None
    raw_calls = None
    if isinstance(obj, dict):
        if isinstance(obj.get("tool_calls"), list):
            raw_calls = obj["tool_calls"]
        elif obj.get("name") and "arguments" in obj:
            raw_calls = [obj]

    if not raw_calls:
        return ParsedReply(content=text.strip())

    calls = []
    for c in raw_calls:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") if isinstance(c.get("function"), dict) else c
        name = fn.get("name")
        if not name:
            continue
        args = fn.get("arguments", {})
        if not isinstance(args, str):
            args = json.dumps(args)
        calls.append({
            "id": c.get("id") or f"call_{uuid.uuid4().hex[:20]}",
            "type": "function",
            "function": {"name": name, "arguments": args},
        })
    if not calls:
        return ParsedReply(content=text.strip())
    return ParsedReply(tool_calls=calls)


# --------------------------------------------------------------------------
# Conversation -> thread mapping
# --------------------------------------------------------------------------

def _sig(messages: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(messages, sort_keys=True, default=str).encode()
    ).hexdigest()


class ThreadMap:
    """Maps a conversation prefix to a live Hyperagent thread.

    opencode resends the entire history each request. Rather than replaying it,
    we look up the longest known prefix and send only what's new.
    """

    def __init__(self, max_entries: int = 200):
        self._map: dict[str, str] = {}
        self._order: list[str] = []
        self.max_entries = max_entries

    def _remember(self, messages: list[dict], thread_id: str) -> None:
        key = _sig(messages)
        if key not in self._map:
            self._order.append(key)
        self._map[key] = thread_id
        while len(self._order) > self.max_entries:
            self._map.pop(self._order.pop(0), None)

    def lookup(self, messages: list[dict], max_lookback: int = 6):
        """Return (thread_id, new_messages) or (None, all_messages)."""
        for back in range(1, min(max_lookback, len(messages)) + 1):
            prefix = messages[:-back]
            if not prefix:
                break
            tid = self._map.get(_sig(prefix))
            if tid:
                return tid, messages[-back:]
        return None, messages

    def record(self, messages: list[dict], thread_id: str) -> None:
        self._remember(messages, thread_id)

    def lookup_exact(self, messages: list[dict]) -> str | None:
        return self._map.get(_sig(messages))


# --------------------------------------------------------------------------
# Local title answering
# --------------------------------------------------------------------------

TITLE_MARKER = "You are a title generator. You output ONLY a thread title."


def _text_of(content) -> str:
    """Flatten OpenAI content (str or list of typed parts) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
        return "\n".join(parts)
    return ""


def local_title(messages: list[dict]) -> str | None:
    """Answer opencode's title-generation request locally, without an agent call."""
    try:
        matched = any(
            m.get("role") == "system" and TITLE_MARKER in _text_of(m.get("content"))
            for m in messages
        )
        if not matched:
            return None
        user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_text = _text_of(m.get("content"))
                break
        first = ""
        for line in user_text.splitlines():
            if line.strip():
                first = line
                break
        title = re.sub(r"\s+", " ", first).strip()[:50]
        return title or "opencode session"
    except Exception:
        return "opencode session"


# --------------------------------------------------------------------------
# OpenAI response envelopes
# --------------------------------------------------------------------------

def looks_like_tool_json(text: str) -> bool:
    """Is this reply shaping up to be a tool call rather than prose?

    Used while streaming partial text: JSON must never be leaked to opencode as
    assistant prose, so anything that opens like an object gets buffered.
    """
    stripped = text.lstrip()
    if not stripped:
        return False
    return stripped.startswith(("{", "```json", "```\n{", "```{"))


class SSEWriter:
    """Emits chat.completion.chunk frames for one response."""

    def __init__(self, model: str):
        self.model = model
        self.cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())

    def chunk(self, delta: dict, finish=None, usage: dict | None = None) -> str:
        payload = {
            "id": self.cid, "object": "chat.completion.chunk", "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage:
            payload["usage"] = usage
        return f"data: {json.dumps(payload)}\n\n"

    def role(self) -> str:
        return self.chunk({"role": "assistant"})

    def heartbeat(self) -> str:
        """Schema-valid no-op frame. Keeps the connection warm while the agent
        thinks, so opencode shows activity instead of appearing hung."""
        return self.chunk({})

    def text(self, content: str) -> str:
        return self.chunk({"content": content})

    def tool_call(self, index: int, call: dict) -> str:
        return self.chunk({"tool_calls": [{
            "index": index, "id": call["id"], "type": "function",
            "function": {"name": call["function"]["name"],
                         "arguments": call["function"]["arguments"]},
        }]})

    def finish(self, reason: str, usage: dict | None = None) -> str:
        return self.chunk({}, finish=reason, usage=usage)

    @staticmethod
    def done() -> str:
        return "data: [DONE]\n\n"


def completion_response(model: str, parsed: ParsedReply) -> dict:
    message: dict = {"role": "assistant", "content": parsed.content}
    if parsed.tool_calls:
        message["content"] = None
        message["tool_calls"] = parsed.tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": parsed.finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def estimate_usage(prompt_chars: int, parsed: "ParsedReply") -> dict:
    """opencode asks for include_usage; a chars/4 estimate beats reporting nothing."""
    out_chars = len(parsed.content or "") + sum(
        len(c["function"]["arguments"]) + len(c["function"]["name"]) for c in parsed.tool_calls
    )
    p, c = max(prompt_chars // 4, 1), max(out_chars // 4, 1)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def stream_chunks(model: str, parsed: ParsedReply, usage: dict | None = None):
    """Yield SSE `data:` lines for a streamed completion."""
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def chunk(delta: dict, finish=None, usage_payload=None) -> str:
        payload = {
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage_payload:
            payload["usage"] = usage_payload
        return f"data: {json.dumps(payload)}\n\n"

    yield chunk({"role": "assistant"})
    if parsed.tool_calls:
        for i, call in enumerate(parsed.tool_calls):
            yield chunk({"tool_calls": [{
                "index": i, "id": call["id"], "type": "function",
                "function": {"name": call["function"]["name"],
                             "arguments": call["function"]["arguments"]},
            }]})
    elif parsed.content:
        # chunk the text so the TUI renders progressively
        text = parsed.content
        size = 180
        for i in range(0, len(text), size):
            yield chunk({"content": text[i:i + size]})
    yield chunk({}, finish=parsed.finish_reason, usage_payload=usage)
    yield "data: [DONE]\n\n"
