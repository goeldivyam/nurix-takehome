#!/bin/bash
# Remind the user to run /update-docs if the session modified code or schema files.
# Non-blocking — Claude still stops normally.

INPUT=$(cat)

# Prevent infinite loop: if this hook already fired, exit silently.
STOP_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')
if [ "$STOP_ACTIVE" = "true" ]; then
  exit 0
fi

# Check the session transcript for Edit/Write tool calls (meaning files were changed).
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
  if grep -qE '"(Edit|Write)"' "$TRANSCRIPT" 2>/dev/null; then
    echo '{"systemMessage": "If you changed modules, API routes, schema.sql, CLAUDE.md, or env vars, consider running /update-docs to keep documentation current."}'
  fi
fi

exit 0
