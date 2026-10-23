#!/usr/bin/env bash
# Smoke-test a running API against an empty database: health, one POST, one read.
#
#     scripts/smoke_api.sh [BASE_URL]      default http://localhost:8000
#
# Used by the docker job in .github/workflows/ci.yml. Needs curl and jq.
set -euo pipefail

base=${1:-http://localhost:8000}
body=$(mktemp)
trap 'rm -f "$body"' EXIT

curl -fsS "$base/health"
echo

code=$(curl -s -o "$body" -w '%{http_code}' -X POST "$base/batches" \
  -H 'content-type: application/json' \
  -d '{"source": "smoke", "expected_count": 2, "records": [
        {"name": "Harbor Point Labs", "country": "GB", "website": "harborpoint.example"},
        {"name": "HARBOR POINT LABS LTD", "country": "gb", "website": "www.harborpoint.example"}]}')
cat "$body"
echo
test "$code" = 201
jq -e '.inserted == 1 and .skipped_duplicates == 1' "$body" >/dev/null

curl -fsS "$base/organizations" | jq -e '.items | length == 1' >/dev/null
echo "smoke test passed"
