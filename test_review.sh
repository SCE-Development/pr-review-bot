#!/usr/bin/env bash
# Usage: ./test_review.sh owner/repo pr_number
set -e

: "${1:?Usage: $0 owner/repo pr_number}"
: "${2:?Usage: $0 owner/repo pr_number}"

export GITHUB_TOKEN="${GITHUB_TOKEN:-$(gh auth token 2>/dev/null)}"
SHA=$(curl -sS -H "Authorization: token $GITHUB_TOKEN" "https://api.github.com/repos/${1}/pulls/${2}" | jq -r '.head.sha')
EVENT=/tmp/pr-review-event.json
printf '{"pull_request":{"number":%s,"head":{"sha":"%s"}}}\n' "$2" "$SHA" > "$EVENT"

export GITHUB_REPOSITORY="$1" GITHUB_EVENT_PATH="$EVENT" DRY_RUN=1
python review.py
