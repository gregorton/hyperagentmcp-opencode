#!/usr/bin/env python3
"""hyperagent-code — run the real opencode CLI with Hyperagent as its only provider.

    python hyperagent_code.py setup    # sign in, discover agents, write opencode config
    python hyperagent_code.py run      # start the shim and launch opencode
    python hyperagent_code.py serve    # just the shim (run opencode yourself)

`setup` backs up any existing opencode config before writing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen

PORT = int(os.environ.get("HYPERAGENT_SHIM_PORT", "8787"))
BASE = f"http://127.0.0.1:{PORT}"
PROVIDER_ID = "hyperagent"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "opencode"
CONFIG_PATH = CONFIG_DIR / "opencode.json"
HERE = Path(__file__).resolve().parent


IS_WINDOWS = os.name == "nt"


def stop_process(proc: subprocess.Popen) -> None:
    """Shut the shim down. Windows has no SIGINT for child processes."""
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            proc.terminate()
        else:
            proc.send_signal(signal.SIGINT)
    except (ValueError, OSError, ProcessLookupError):
        proc.kill()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def shim_command(debug: bool = False) -> list[str]:
    cmd = [sys.executable, "-m", "shim.server", "--port", str(PORT)]
    if debug:
        cmd.append("--debug")
    return cmd


def wait_for_health(timeout: float = 600) -> dict:
    """Block until the shim answers /health (sign-in may happen in here)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen(f"{BASE}/health", timeout=5) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(1)
    raise TimeoutError("shim did not become healthy; run `serve` alone to see its output")


def fetch_models() -> list[dict]:
    with urlopen(f"{BASE}/v1/models", timeout=30) as r:
        return json.loads(r.read())["data"]


def build_config(models: list[dict], existing: dict | None = None,
                 small_model: str | None = None) -> dict:
    cfg = dict(existing or {})
    cfg["$schema"] = "https://opencode.ai/config.json"
    cfg.setdefault("provider", {})
    cfg["provider"][PROVIDER_ID] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Hyperagent",
        "options": {"baseURL": f"{BASE}/v1", "apiKey": "not-needed"},
        "models": {
            m["id"]: {
                "name": f"{m.get('name', m['id'])} (Hyperagent)",
                "tool_call": True,
                "limit": {"context": 200000, "output": 32000},
            }
            for m in models
        },
    }
    # Hyperagent only: hide every provider opencode would otherwise autoload.
    cfg["enabled_providers"] = [PROVIDER_ID]
    if models:
        cfg["model"] = f"{PROVIDER_ID}/{models[0]['id']}"
    # opencode fires a small extra call per session (title generation). Point it
    # at a cheap agent so it doesn't spend a run on your main coder.
    if small_model:
        cfg["small_model"] = (small_model if "/" in small_model
                              else f"{PROVIDER_ID}/{small_model}")
    return cfg


def cmd_setup(args) -> None:
    print("Starting the shim so it can list your agents (a browser may open to sign in)...")
    proc = subprocess.Popen(shim_command(args.debug), cwd=HERE)
    try:
        wait_for_health()
        models = fetch_models()
    finally:
        stop_process(proc)

    if not models:
        sys.exit("No agents found on your Hyperagent account. Create one in the web UI first "
                 "(see AGENT_SYSTEM_PROMPT.md), then rerun setup.")

    existing = None
    if CONFIG_PATH.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = CONFIG_PATH.with_suffix(f".backup-{stamp}.json")
        shutil.copy2(CONFIG_PATH, backup)
        print(f"Existing config backed up to {backup}")
        try:
            existing = json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            print("(existing config wasn't valid JSON; writing a fresh one)")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = build_config(models, existing, args.small_model)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")

    print(f"\nWrote {CONFIG_PATH}")
    print(f"Provider locked to: {PROVIDER_ID}")
    print("Models (your Hyperagent agents):")
    for m in models:
        print(f"  - {PROVIDER_ID}/{m['id']}   {m.get('name','')}")
    print(f"\nDefault model: {cfg.get('model')}")
    if cfg.get("small_model"):
        print(f"Small model (titles): {cfg['small_model']}")
    elif len(models) > 1:
        print("\nTip: opencode spends one extra run per session on title generation.\n"
              "     Point it at a cheap agent with:  setup --small-model <agentId>")
    print("\nNow run:  python hyperagent_code.py run")


def cmd_serve(args) -> None:
    # execv behaves oddly on Windows; a plain child process is portable.
    sys.exit(subprocess.call(shim_command(args.debug), cwd=HERE))


def cmd_run(args) -> None:
    if not shutil.which("opencode"):
        sys.exit("opencode is not installed or not on PATH.\n"
                 "Install it with:  npm i -g opencode-ai   (or: brew install sst/tap/opencode)")
    if not CONFIG_PATH.exists():
        sys.exit(f"No opencode config at {CONFIG_PATH}. Run:  python hyperagent_code.py setup")

    print("Starting Hyperagent shim...")
    proc = subprocess.Popen(shim_command(args.debug), cwd=HERE)
    try:
        health = wait_for_health()
        print(f"Shim ready ({health.get('agents', 0)} agent(s)). Launching opencode.\n")
        opencode = shutil.which("opencode") or "opencode"
        subprocess.call([opencode, *args.opencode_args], cwd=os.getcwd(), shell=False)
    finally:
        stop_process(proc)
        print("\nShim stopped.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--debug", action="store_true", help="verbose shim logging")
    sub = ap.add_subparsers(dest="cmd", required=True)
    setup = sub.add_parser("setup", help="discover agents and write opencode config")
    setup.add_argument("--small-model", help="agent id to use for cheap calls like title generation")
    setup.set_defaults(func=cmd_setup)
    sub.add_parser("serve", help="run only the shim").set_defaults(func=cmd_serve)
    run = sub.add_parser("run", help="start shim + opencode")
    run.add_argument("opencode_args", nargs=argparse.REMAINDER,
                     help="arguments passed straight through to opencode")
    run.set_defaults(func=cmd_run)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
