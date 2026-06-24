#!/usr/bin/env bash
# Read-only health check for lark-reaction-as-user.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/scripts/lark-reaction-as-user"
STATE_DIR="${LARK_REACTION_STATE_DIR:-$HOME/.local/state/lark-reaction-as-user}"
HEARTBEAT_FILE="$STATE_DIR/heartbeat.json"
HEARTBEAT_MAX_AGE_SEC="${LARK_REACTION_HEARTBEAT_MAX_AGE_SEC:-300}"
FORMAT="text"

case "${1:-}" in
  --json) FORMAT="json" ;;
  --gate) FORMAT="gate" ;;
esac

run_rc() {
  "$@" >/dev/null 2>&1
  printf '%s' "$?"
}

json_or_default() {
  local input="$1" default_json="$2"
  if jq -e . >/dev/null 2>&1 <<<"$input"; then
    jq -c . <<<"$input"
  else
    printf '%s' "$default_json"
  fi
}

redact_local() {
  sed -E \
    -e "s|$HOME|~|g" \
    -e 's/(ou|oc|om)_[A-Za-z0-9]+/\1_REDACTED/g' \
    -e 's/cli_[A-Za-z0-9]+/cli_REDACTED/g'
}

syntax_rc="$(run_rc python3 -m py_compile "$SCRIPT")"
script_exec=false
[[ -x "$SCRIPT" ]] && script_exec=true

command -v lark-cli >/dev/null 2>&1
lark_cli_rc="$?"
command -v claude >/dev/null 2>&1
claude_rc="$?"
command -v codex >/dev/null 2>&1
codex_rc="$?"

set +e
auth_status="$(lark-cli auth status --json 2>&1)"
auth_status_rc=$?
reaction_dry_run="$(lark-cli im reactions create --as user --params '{"message_id":"om_health_dummy"}' --data '{"reaction_type":{"emoji_type":"OK"}}' --dry-run --format json 2>&1)"
reaction_dry_run_rc=$?
config_json="$("$SCRIPT" --print-config 2>&1)"
config_rc=$?
set -e

auth_status="$(printf '%s' "$auth_status" | redact_local)"
reaction_dry_run="$(printf '%s' "$reaction_dry_run" | redact_local)"
config_json="$(printf '%s' "$config_json" | redact_local)"

auth_status_json="$(json_or_default "$auth_status" '{"ok":false,"parse_error":true}')"
config_json="$(json_or_default "$config_json" '{"parse_error":true}')"

processes="$(ps -axo pid,ppid,command | rg 'lark-reaction-as-user' | rg -v 'rg lark-reaction-as-user|health\.sh' || true)"
process_count="$(rg -c 'scripts/lark-reaction-as-user|/lark-reaction-as-user ' <<<"$processes" || true)"

heartbeat='{}'
heartbeat_age_sec=999999
if [[ -s "$HEARTBEAT_FILE" ]]; then
  heartbeat="$(json_or_default "$(cat "$HEARTBEAT_FILE" | redact_local)" '{}')"
  heartbeat_updated_ts="$(jq -r '.updated_ts // 0' <<<"$heartbeat" 2>/dev/null || printf '0')"
  heartbeat_age_sec=$(( $(date +%s) - ${heartbeat_updated_ts:-0} ))
  (( heartbeat_age_sec < 0 )) && heartbeat_age_sec=999999
fi

emit_json() {
  jq -n \
    --arg syntax_rc "$syntax_rc" \
    --arg script_exec "$script_exec" \
    --arg lark_cli_rc "$lark_cli_rc" \
    --arg claude_rc "$claude_rc" \
    --arg codex_rc "$codex_rc" \
    --arg auth_status_rc "$auth_status_rc" \
    --argjson auth_status "$auth_status_json" \
    --arg reaction_dry_run_rc "$reaction_dry_run_rc" \
    --arg reaction_dry_run "$reaction_dry_run" \
    --arg config_rc "$config_rc" \
    --argjson config "$config_json" \
    --arg processes "$processes" \
    --arg process_count "$process_count" \
    --argjson heartbeat "$heartbeat" \
    --arg heartbeat_age_sec "$heartbeat_age_sec" \
    --arg heartbeat_max_age_sec "$HEARTBEAT_MAX_AGE_SEC" \
    '(
      {
        syntax: {
          script_ok: (($syntax_rc | tonumber? // 1) == 0),
          script_exec: ($script_exec == "true")
        },
        dependencies: {
          lark_cli_ok: (($lark_cli_rc | tonumber? // 1) == 0),
          claude_ok: (($claude_rc | tonumber? // 1) == 0),
          codex_ok: (($codex_rc | tonumber? // 1) == 0)
        },
        auth: ($auth_status + {exit_code: ($auth_status_rc | tonumber? // 1)}),
        config: ($config + {exit_code: ($config_rc | tonumber? // 1)}),
        entrypoints: {
          reaction_dry_run_ok: (($reaction_dry_run_rc | tonumber? // 1) == 0 and ($reaction_dry_run | contains("/reactions"))),
          reaction_dry_run_exit_code: ($reaction_dry_run_rc | tonumber? // 1),
          reaction_dry_run: $reaction_dry_run
        },
        processes: {
          poller_count: ($process_count | tonumber? // 0),
          raw: $processes
        },
        heartbeat: {
          ok: (($heartbeat_age_sec | tonumber? // 999999) <= ($heartbeat_max_age_sec | tonumber? // 300) and (($heartbeat.status // "") != "error")),
          age_sec: ($heartbeat_age_sec | tonumber? // 999999),
          max_age_sec: ($heartbeat_max_age_sec | tonumber? // 300),
          data: $heartbeat
        }
      }
    ) as $h
    | $h + {
      objective_gate: {
        ready_ok: ($h.syntax.script_ok and $h.syntax.script_exec and $h.dependencies.lark_cli_ok),
        reaction_entrypoint_ok: $h.entrypoints.reaction_dry_run_ok,
        model_fallback_ok: ($h.dependencies.claude_ok or $h.dependencies.codex_ok),
        user_open_id_available: ($h.config.user_open_id_set == true),
        listening_ok: ($h.processes.poller_count >= 1 and $h.heartbeat.ok),
        complete: (
          ($h.syntax.script_ok and $h.syntax.script_exec and $h.dependencies.lark_cli_ok)
          and $h.entrypoints.reaction_dry_run_ok
          and ($h.dependencies.claude_ok or $h.dependencies.codex_ok)
          and ($h.config.user_open_id_set == true)
          and ($h.processes.poller_count >= 1 and $h.heartbeat.ok)
        )
      }
    }'
}

if [[ "$FORMAT" == "json" ]]; then
  emit_json
  exit 0
fi

if [[ "$FORMAT" == "gate" ]]; then
  gate="$(emit_json | jq -c '.objective_gate')"
  printf '%s\n' "$gate"
  complete="$(jq -r '.complete' <<<"$gate")"
  [[ "$complete" == "true" ]] && exit 0
  exit 1
fi

section() {
  printf '\n== %s ==\n' "$1"
}

status_line() {
  local label="$1" rc="$2"
  if [[ "$rc" -eq 0 ]]; then
    printf 'OK   %s\n' "$label"
  else
    printf 'WARN %s (rc=%s)\n' "$label" "$rc"
  fi
}

section "syntax"
status_line "$SCRIPT" "$syntax_rc"
[[ "$script_exec" == "true" ]] && printf 'OK   executable\n' || printf 'WARN not executable\n'

section "dependencies"
status_line "lark-cli" "$lark_cli_rc"
status_line "claude" "$claude_rc"
status_line "codex fallback" "$codex_rc"

section "auth"
printf '%s\n' "$auth_status" | jq . 2>/dev/null || printf '%s\n' "$auth_status"

section "config"
printf '%s\n' "$config_json" | jq .

section "reaction dry-run"
printf '%s\n' "$reaction_dry_run"
status_line "lark-cli im reactions create --dry-run" "$reaction_dry_run_rc"

section "processes"
printf '%s\n' "$processes"

section "heartbeat"
jq -n \
  --argjson heartbeat "$heartbeat" \
  --arg age "$heartbeat_age_sec" \
  --arg max_age "$HEARTBEAT_MAX_AGE_SEC" \
  '{ok:(($age|tonumber? // 999999) <= ($max_age|tonumber? // 300) and (($heartbeat.status // "") != "error")), age_sec:($age|tonumber? // 999999), max_age_sec:($max_age|tonumber? // 300), data:$heartbeat}'
