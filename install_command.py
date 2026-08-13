#!/usr/bin/env python3
"""Install a global `hypercode` command so you can launch from any folder.

    python install_command.py

Writes a tiny launcher into a directory that's already on your PATH (npm's
global bin, where opencode itself lives), pointing at this package's absolute
location. After that, `hypercode` works in any project directory:

    cd C:\\projects\\anything
    hypercode

Nothing about your setup is per-project. Sign-in and config are stored in your
home directory, so this only saves you typing.

    python install_command.py --uninstall     removes it again
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "hyperagent_code.py"
NAME = "hypercode"
IS_WINDOWS = os.name == "nt"


def npm_bin_dir() -> Path | None:
    """npm's global bin is already on PATH (that's how `opencode` resolves)."""
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        out = subprocess.run([npm, "config", "get", "prefix"], capture_output=True,
                             text=True, timeout=60, shell=IS_WINDOWS)
        prefix = Path(out.stdout.strip())
    except Exception:
        return None
    if not prefix or not prefix.exists():
        return None
    # Windows: <prefix>\hypercode.cmd ; unix: <prefix>/bin/hypercode
    return prefix if IS_WINDOWS else prefix / "bin"


def fallback_bin_dir() -> Path:
    d = Path.home() / ("bin" if not IS_WINDOWS else "bin")
    d.mkdir(parents=True, exist_ok=True)
    return d


def launcher_path(bin_dir: Path) -> Path:
    return bin_dir / (f"{NAME}.cmd" if IS_WINDOWS else NAME)


def write_launcher(bin_dir: Path) -> Path:
    path = launcher_path(bin_dir)
    if IS_WINDOWS:
        body = f'@echo off\r\n"{sys.executable}" "{TARGET}" run %*\r\n'
    else:
        body = f'#!/usr/bin/env bash\nexec "{sys.executable}" "{TARGET}" run "$@"\n'
    path.write_text(body)
    if not IS_WINDOWS:
        path.chmod(0o755)
    return path


def on_path(bin_dir: Path) -> bool:
    entries = [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return any(p.resolve() == bin_dir.resolve() for p in entries if p.exists())


def main() -> None:
    ap = argparse.ArgumentParser(description="install a global hypercode command")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    if not TARGET.exists():
        sys.exit(f"Can't find {TARGET}. Run this from inside the package folder.")

    bin_dir = npm_bin_dir()
    source = "npm global bin (already on PATH)"
    if bin_dir is None or not bin_dir.exists():
        bin_dir, source = fallback_bin_dir(), "fallback folder"

    if args.uninstall:
        removed = []
        for d in {bin_dir, fallback_bin_dir()}:
            p = launcher_path(d)
            if p.exists():
                p.unlink()
                removed.append(str(p))
        print("Removed:\n  " + "\n  ".join(removed) if removed else "Nothing to remove.")
        return

    path = write_launcher(bin_dir)
    print(f"Installed: {path}")
    print(f"Location:  {source}")
    print(f"Points at: {TARGET}")

    found = shutil.which(NAME)
    if found:
        print(f"\nVerified: `{NAME}` resolves to {found}")
        print(f"\nUse it from any project folder:\n    cd <your project>\n    {NAME}")
    elif on_path(bin_dir):
        print(f"\n`{NAME}` is installed. Open a new terminal, then run it from any folder.")
    else:
        print(f"\nOne more step: {bin_dir} is not on your PATH.")
        if IS_WINDOWS:
            print(f'Run this once, then open a NEW terminal:\n'
                  f'    setx PATH "%PATH%;{bin_dir}"')
        else:
            print(f'Add this to your shell profile:\n    export PATH="$PATH:{bin_dir}"')


if __name__ == "__main__":
    main()
