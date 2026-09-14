#!/usr/bin/env bash
# Stop the offline downtime demo started by start-offline-demo.sh.
#
# Kills the PIDs recorded in .downtime-logs/pids.json, then - because a PID file
# can be stale, and a half-stopped demo that still holds :8200 is worse than no
# demo - falls back to whatever is still listening on the two ports.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/.downtime-logs/pids.json"

# Git Bash on Windows has the venv under Scripts/, POSIX under bin/; either way
# any python will do for reading one small JSON file.
if   [[ -x "$ROOT/.venv/bin/python" ]];         then PYBIN="$ROOT/.venv/bin/python"
elif [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then PYBIN="$ROOT/.venv/Scripts/python.exe"
else PYBIN="python"
fi

PORT=""
ENGINE_PORT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--port)        PORT="$2"; shift 2 ;;
    -e|--engine-port) ENGINE_PORT="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }

# The PID file is JSON, so read it with python rather than a grep that breaks
# the first time a field moves. The file is passed as a path, not on stdin -
# `python -` already uses stdin for the program text.
read_json() {  # read_json <key> <default>
  [[ -f "$PID_FILE" ]] || { echo "$2"; return; }
  "$PYBIN" -c '
import json, sys
path, key, default = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, encoding="utf-8") as fh:
        print(json.load(fh).get(key, default))
except Exception:
    print(default)
' "$PID_FILE" "$1" "$2"
}

[[ -n "$PORT" ]]        || PORT="$(read_json port 8200)"
[[ -n "$ENGINE_PORT" ]] || ENGINE_PORT="$(read_json enginePort 2575)"

stop_pid() {  # stop_pid <label> <pid>
  local label="$1" pid="$2"
  [[ -n "$pid" && "$pid" != "None" ]] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 0.3
    kill -9 "$pid" 2>/dev/null || true
    echo "    stopped $label pid $pid"
    return 0
  fi
  # Git Bash: a PID that came out of Windows netstat is not in bash's process
  # table, so kill(1) cannot see it - taskkill can.
  if command -v taskkill >/dev/null 2>&1 && taskkill //PID "$pid" //F >/dev/null 2>&1; then
    echo "    stopped $label pid $pid (taskkill)"
    return 0
  fi
  echo "    $label pid $pid already gone"
}

step "Stopping recorded PIDs"
if [[ -f "$PID_FILE" ]]; then
  stop_pid app    "$(read_json app '')"
  stop_pid engine "$(read_json engine '')"
  rm -f "$PID_FILE"
else
  echo "    no $PID_FILE - falling back to port listeners"
fi

listeners() {  # listeners <port> -> PIDs, on Windows or POSIX
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:$port" -sTCP:LISTEN 2>/dev/null
  elif command -v netstat >/dev/null 2>&1; then
    # Git Bash: Windows netstat, "TCP 0.0.0.0:8200 ... LISTENING <pid>"
    netstat -ano 2>/dev/null | tr -d '\r' \
      | awk -v p=":$port" '$1=="TCP" && $2 ~ p"$" && $4=="LISTENING" {print $5}' | sort -u
  fi
}

step "Clearing any remaining listeners on :$PORT and :$ENGINE_PORT"
for prt in "$PORT" "$ENGINE_PORT"; do
  for pid in $(listeners "$prt"); do
    stop_pid "listener on :$prt" "$pid"
  done
done

sleep 0.5
for prt in "$PORT" "$ENGINE_PORT"; do
  if [[ -n "$(listeners "$prt")" ]]; then
    printf '\033[33m    :%s is STILL in use\033[0m\n' "$prt"
  else
    printf '\033[32m    :%s is free\033[0m\n' "$prt"
  fi
done

printf '\033[32mSTOPPED\033[0m\n'
