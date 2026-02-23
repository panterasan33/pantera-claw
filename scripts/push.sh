#!/bin/bash
# Push to GitHub using GITHUB_PAT from .config/secrets.env
set -e
cd "$(dirname "$0")/.."
GITHUB_PAT=$(grep -E '^GITHUB_PAT_PANTERA_CLAWW_APP=' .config/secrets.env 2>/dev/null | cut -d= -f2-)
if [ -z "$GITHUB_PAT" ]; then
  echo "Error: GITHUB_PAT_PANTERA_CLAWW_APP not set in .config/secrets.env"
  exit 1
fi
git push "https://panterasan33:${GITHUB_PAT}@github.com/panterasan33/pantera-claw.git" main
