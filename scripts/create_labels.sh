#!/usr/bin/env bash
# Create or update every label in labels.json on the ideas repo. Safe to rerun (--force updates in place).
# Usage: scripts/create_labels.sh [owner/repo]   (default ward-wise/ideas)
set -euo pipefail
REPO="${1:-ward-wise/ideas}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
python3 -c 'import json,sys
for l in json.load(open(sys.argv[1])): print(l["name"], l["color"], l["description"], sep="\t")' "$HERE/labels.json" |
while IFS=$'\t' read -r name color description; do
  gh label create "$name" --repo "$REPO" --color "$color" --description "$description" --force
done
