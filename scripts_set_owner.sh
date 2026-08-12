#!/usr/bin/env bash
# Replace the OWNER placeholder in docs and badges with your GitHub username.
#   ./scripts_set_owner.sh your-username
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <github-username>" >&2
  exit 1
fi

OWNER="$1"
FILES=$(grep -rl 'OWNER/ff-draft-mcp' --include='*.md' --include='*.yml' . || true)

if [ -z "$FILES" ]; then
  echo "Nothing to replace — already set?"
  exit 0
fi

for f in $FILES; do
  sed -i.bak "s|OWNER/ff-draft-mcp|${OWNER}/ff-draft-mcp|g" "$f"
  rm -f "$f.bak"
  echo "  updated $f"
done

echo
echo "Done. Remaining references to OWNER (should be none):"
grep -rn 'OWNER/ff-draft-mcp' --include='*.md' --include='*.yml' . || echo "  none"
