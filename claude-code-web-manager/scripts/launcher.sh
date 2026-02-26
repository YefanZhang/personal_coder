#!/usr/bin/env bash
# launcher.sh — Reads prompts from a file and POSTs them as a batch to the API.
#
# Usage:
#   ./launcher.sh <prompts-file> [base-url] [api-key]
#
# The prompts file should contain one task per line in the format:
#   title | prompt text
#
# Lines starting with # are ignored.
#
# Examples:
#   echo "Fix login bug | Fix the login bug in auth.py" > tasks.txt
#   ./launcher.sh tasks.txt
#   ./launcher.sh tasks.txt http://localhost:8000 my-api-key

set -euo pipefail

PROMPTS_FILE="${1:?Usage: $0 <prompts-file> [base-url] [api-key]}"
BASE_URL="${2:-http://localhost:8000}"
API_KEY="${3:-}"

if [[ ! -f "$PROMPTS_FILE" ]]; then
    echo "Error: file not found: $PROMPTS_FILE" >&2
    exit 1
fi

# Build JSON array of task requests
TASKS="[]"
while IFS= read -r line || [[ -n "$line" ]]; do
    # Skip empty lines and comments
    [[ -z "$line" || "$line" == \#* ]] && continue

    # Split on first pipe: "title | prompt"
    if [[ "$line" == *"|"* ]]; then
        title="${line%%|*}"
        prompt="${line#*|}"
        # Trim whitespace
        title="$(echo "$title" | xargs)"
        prompt="$(echo "$prompt" | xargs)"
    else
        # No pipe — use the whole line as both title and prompt
        title="$(echo "$line" | xargs | head -c 60)"
        prompt="$(echo "$line" | xargs)"
    fi

    # Append to JSON array using jq if available, otherwise python
    if command -v jq &>/dev/null; then
        TASKS=$(echo "$TASKS" | jq \
            --arg t "$title" \
            --arg p "$prompt" \
            '. + [{"title": $t, "prompt": $p, "priority": "medium"}]')
    else
        TASKS=$(python3 -c "
import json, sys
tasks = json.loads(sys.argv[1])
tasks.append({'title': sys.argv[2], 'prompt': sys.argv[3], 'priority': 'medium'})
print(json.dumps(tasks))
" "$TASKS" "$title" "$prompt")
    fi
done < "$PROMPTS_FILE"

TASK_COUNT=$(echo "$TASKS" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")

if [[ "$TASK_COUNT" -eq 0 ]]; then
    echo "No tasks found in $PROMPTS_FILE"
    exit 0
fi

echo "Submitting $TASK_COUNT task(s) to $BASE_URL/api/tasks/batch ..."

# Build curl headers
CURL_ARGS=(-s -X POST "${BASE_URL}/api/tasks/batch"
    -H "Content-Type: application/json"
    -d "$TASKS")

if [[ -n "$API_KEY" ]]; then
    CURL_ARGS+=(-H "X-API-Key: $API_KEY")
fi

RESPONSE=$(curl "${CURL_ARGS[@]}" -w "\n%{http_code}" 2>&1)
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [[ "$HTTP_CODE" == "201" ]]; then
    echo "Success! $TASK_COUNT task(s) created."
    echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
else
    echo "Error: HTTP $HTTP_CODE" >&2
    echo "$BODY" >&2
    exit 1
fi
