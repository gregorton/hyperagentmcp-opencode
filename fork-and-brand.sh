#!/usr/bin/env bash
# Fork opencode into your own branded CLI, wired to Hyperagent only.
#
#   ./fork-and-brand.sh [BRAND_NAME] [TARGET_DIR]
#   ./fork-and-brand.sh hypercode ~/src/hypercode
#
# What it does:
#   1. clones sst/opencode (MIT licensed — forking is fair game)
#   2. renames the CLI binary to your brand
#   3. bakes in the Hyperagent provider config so it needs no setup
#   4. builds a standalone binary with bun
#
# You do NOT need this to use Hyperagent with opencode — `hyperagent_code.py
# setup` does that with stock opencode and no fork to maintain. This exists for
# when you want your own named tool that can't talk to anything else.

set -euo pipefail

BRAND="${1:-hypercode}"
TARGET="${2:-$HOME/src/$BRAND}"
SHIM_URL="${HYPERAGENT_SHIM_URL:-http://127.0.0.1:8787/v1}"

command -v bun >/dev/null || { echo "bun is required: curl -fsSL https://bun.sh/install | bash"; exit 1; }
command -v git >/dev/null || { echo "git is required"; exit 1; }

echo "==> Cloning opencode into $TARGET"
[ -d "$TARGET" ] && { echo "$TARGET already exists; remove it or pick another path."; exit 1; }
git clone --depth 1 https://github.com/sst/opencode.git "$TARGET"
cd "$TARGET"

echo "==> Renaming CLI binary to '$BRAND'"
python3 - "$BRAND" <<'PY'
import json, sys, pathlib
brand = sys.argv[1]
p = pathlib.Path("packages/opencode/package.json")
d = json.loads(p.read_text())
old = next(iter(d.get("bin", {"opencode": "./bin/opencode"})))
d["bin"] = {brand: d.get("bin", {}).get(old, "./bin/opencode")}
p.write_text(json.dumps(d, indent=2) + "\n")
print(f"   bin: {old} -> {brand}")
PY

echo "==> Baking in the Hyperagent-only config"
mkdir -p "packages/opencode/config"
cat > "packages/opencode/config/hyperagent.json" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "provider": {
    "hyperagent": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Hyperagent",
      "options": { "baseURL": "$SHIM_URL", "apiKey": "not-needed" },
      "models": {}
    }
  },
  "enabled_providers": ["hyperagent"]
}
EOF
echo "   wrote packages/opencode/config/hyperagent.json"
echo "   (models are filled in per-machine by: python hyperagent_code.py setup)"

echo "==> Installing dependencies (bun install)"
bun install

echo "==> Building"
cd packages/opencode && bun run build

cat <<EOF

Done. Your fork lives in $TARGET

Next:
  1. Start the shim:   python hyperagent_code.py serve
  2. Run your CLI:     $TARGET/packages/opencode/bin/$BRAND
  3. Link it globally: ln -s $TARGET/packages/opencode/bin/$BRAND /usr/local/bin/$BRAND

Upstream stays available as a git remote, so you can pull opencode's updates:
  git remote add upstream https://github.com/sst/opencode.git && git fetch upstream
EOF
