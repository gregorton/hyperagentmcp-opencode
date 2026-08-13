#!/usr/bin/env python3
"""Does Hyperagent expose an agent's reply while it's still being written?

That single fact decides whether real streaming is possible through MCP. The
docs only say "poll until it's no longer running", so this measures it.

    python probe_streaming.py                 # uses your first agent
    python probe_streaming.py <agentId>

Costs one agent run. It asks for a long answer, then polls get_thread twice a
second and records how the visible reply length changes over time.

Verdict:
  GROWS   -> partial text is visible: real streaming works.
            Turn it on with  set HYPERAGENT_PARTIAL_STREAM=1
  JUMPS   -> text appears only when finished: token streaming isn't possible
            over MCP. The shim's heartbeat keeps opencode responsive instead.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from shim.hyperagent_client import HyperagentClient

PROMPT = (
    "Count slowly from 1 to 40. Put each number on its own line with a short "
    "sentence about it. Do not use any tools. Write it as plain prose."
)


async def main() -> None:
    agent_id = sys.argv[1] if len(sys.argv) > 1 else None

    async with HyperagentClient() as client:
        if not agent_id:
            raw = await client.list_agents()
            try:
                data = json.loads(raw)
                data = data.get("agents", data) if isinstance(data, dict) else data
                agent_id = data[0]["id"]
                print(f"Using agent: {agent_id} ({data[0].get('name','')})")
            except Exception:
                sys.exit(f"Couldn't pick an agent automatically. Raw list:\n{raw[:800]}")

        print("Starting a thread and polling twice a second...\n")
        started = time.time()
        thread_id = await client.create_thread(agent_id, PROMPT)

        samples: list[tuple[float, int]] = []
        last_len = 0
        while time.time() - started < 300:
            running, text = await client.peek_reply(thread_id)
            n = len(text or "")
            if n != last_len:
                elapsed = time.time() - started
                samples.append((elapsed, n))
                print(f"  t={elapsed:6.1f}s  visible reply: {n:6d} chars  running={running}")
                last_len = n
            if not running and n:
                break
            await asyncio.sleep(0.5)

        total = time.time() - started
        print(f"\nFinished in {total:.1f}s with {len(samples)} distinct length reading(s).")

        growth_before_finish = [s for s in samples[:-1]] if samples else []
        if len(samples) >= 3 and growth_before_finish:
            print("\nVERDICT: GROWS — partial text is visible mid-run.")
            print("Real streaming is possible. Enable it with:")
            print("    set HYPERAGENT_PARTIAL_STREAM=1     (Windows)")
            print("    export HYPERAGENT_PARTIAL_STREAM=1  (mac/Linux)")
        else:
            print("\nVERDICT: JUMPS — the reply only appears once the turn ends.")
            print("Token streaming isn't available over MCP. Leave partial streaming off;")
            print("the shim's heartbeat already keeps opencode's spinner alive.")


if __name__ == "__main__":
    asyncio.run(main())
