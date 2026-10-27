#!/usr/bin/env bash
# Drive every hook entry point with a representative payload, the way Claude Code would,
# into a throwaway data directory, then print the status report and the log.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export EPISTEMIC_DATA_DIR="${EPISTEMIC_DATA_DIR:-$(mktemp -d)/epistemic}"
SID="smoke-$(date +%s)"
FIX="$ROOT/tests/fixtures/transcript_synthetic_older_format.jsonl"
hook() { local ev="$1"; shift; printf '%s' "$1" | python3 -I "$ROOT/bin/em-hook" hook "$ev"; echo; }
echo "data dir: $EPISTEMIC_DATA_DIR"
hook session_start      "{\"session_id\":\"$SID\",\"cwd\":\"$ROOT\",\"source\":\"startup\"}"
hook user_prompt_submit "{\"session_id\":\"$SID\",\"cwd\":\"$ROOT\",\"prompt\":\"Let us keep PostgreSQL.\"}"
hook pre_tool_use       "{\"session_id\":\"$SID\",\"cwd\":\"$ROOT\",\"tool_name\":\"Bash\",\"tool_use_id\":\"t1\",\"tool_input\":{\"command\":\"sed -i s/a/b/ src/x.py\"}}"
hook post_tool_use      "{\"session_id\":\"$SID\",\"cwd\":\"$ROOT\",\"tool_name\":\"Bash\",\"duration_ms\":12}"
hook post_tool_batch    "{\"session_id\":\"$SID\",\"cwd\":\"$ROOT\",\"tool_calls\":[{\"tool_name\":\"Bash\"}]}"
hook pre_compact        "{\"session_id\":\"$SID\",\"cwd\":\"$ROOT\",\"trigger\":\"auto\",\"transcript_path\":\"$FIX\",\"custom_instructions\":null}"
hook post_compact       "{\"session_id\":\"$SID\",\"cwd\":\"$ROOT\",\"trigger\":\"auto\",\"compact_summary\":\"summary text\"}"
hook session_start      "{\"session_id\":\"$SID\",\"cwd\":\"$ROOT\",\"source\":\"compact\"}"
hook subagent_start     "{\"session_id\":\"$SID\",\"cwd\":\"$ROOT\",\"agent_type\":\"Explore\"}"
hook subagent_stop      "{\"session_id\":\"$SID\",\"cwd\":\"$ROOT\",\"agent_id\":\"a1\"}"
hook stop               "{\"session_id\":\"$SID\",\"cwd\":\"$ROOT\",\"last_assistant_message\":\"done\",\"stop_hook_active\":false}"
hook session_end        "{\"session_id\":\"$SID\",\"cwd\":\"$ROOT\",\"reason\":\"other\",\"transcript_path\":\"$FIX\"}"
echo "--- status ---"; (cd "$ROOT" && python3 -I bin/em-hook status)
echo "--- interventions.jsonl (kinds) ---"; python3 -I -c "import json,sys; [print(json.loads(l)['kind'], json.loads(l).get('event',''), 'ok' if json.loads(l).get('ok', True) else 'ERROR') for l in open(sys.argv[1])]" "$EPISTEMIC_DATA_DIR/interventions.jsonl"
